"""ulduar.freya.lineage: edges between artifacts and datasets, cycles and foreign nodes refused, graph queries."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import Archivum
from tests.test_freya_catalog import CHECKSUM, onboarded, partition_exists
from ulduar import _ids
from ulduar.errors import LineageCycleError, UnknownRecordError
from ulduar.freya import Artifact, Catalog, Dataset, LineageEdge, LineageNode, NodeRef, open_catalog


def artifact(catalog: Catalog, label: str) -> Artifact:
    return catalog.register_artifact("log", CHECKSUM, 1, origin=label)


def by_depth(*levels: tuple[int, list[NodeRef]]) -> list[LineageNode]:
    """The nodes expected from a walk: each level's refs sorted by kind and id, shallowest level first."""
    return [LineageNode(ref, depth) for depth, refs in levels for ref in sorted(refs, key=lambda r: (r.kind, r.id))]


class Diamond:
    """A small graph: `sink` derives from `left` and `right` (both from `source`), and from `source` directly.

    source (dataset) <- left (artifact), right (dataset) <- middle (artifact) <- sink (artifact)
    sink <- source as well, so the source is at depth 1 and at depth 3 from the sink.
    """

    def __init__(self, catalog: Catalog) -> None:
        self.source = catalog.register_dataset("warehouse", "source")
        self.left = artifact(catalog, "left")
        self.right = catalog.register_dataset("warehouse", "right")
        self.middle = artifact(catalog, "middle")
        self.sink = artifact(catalog, "sink")
        catalog.add_lineage(self.left, [self.source])
        catalog.add_lineage(self.right, [self.source])
        catalog.add_lineage(self.middle, [self.left, self.right])
        catalog.add_lineage(self.sink, [self.middle, self.source])


# --- adding edges ---


def test_edges_connect_artifacts_and_datasets_in_any_combination(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        dataset, other = catalog.register_dataset("ns", "one"), catalog.register_dataset("ns", "two")
        first, second = artifact(catalog, "a"), artifact(catalog, "b")
        edges = catalog.add_lineage(first, [dataset, second, other.ref])
        assert [isinstance(e, LineageEdge) for e in edges] == [True] * 3
        assert [(e.child, e.parent) for e in edges] == [
            (first.ref, dataset.ref),
            (first.ref, second.ref),
            (first.ref, other.ref),
        ]
        assert catalog.add_lineage(dataset.ref, [second])[0].child == NodeRef("dataset", dataset.dataset_id)
        assert len({e.edge_id for e in edges}) == 3
        assert len({e.event_at for e in edges}) == 1  # one millisecond, so one partition
        assert all(e.event_at == _ids.uuid7_time(e.edge_id) and e.run_id is None for e in edges)


def test_the_run_reference_is_stored_with_every_edge(archivum: Archivum, tmp_path: Path) -> None:
    run = uuid.uuid4()
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        child, parents = artifact(catalog, "child"), [artifact(catalog, "p1"), artifact(catalog, "p2")]
        edges = catalog.add_lineage(child, parents, run_id=run)
    assert [e.run_id for e in edges] == [run, run]
    with archivum.superuser() as su:
        stored = su.execute("SELECT run_id, edge_id FROM ulduar.lineage_edge ORDER BY edge_id").fetchall()
    assert sorted(stored) == sorted((run, e.edge_id) for e in edges)


def test_a_missing_month_partition_is_created_for_the_edge_and_the_insert_retried(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = onboarded(archivum, tmp_path)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    with archivum.superuser() as su:
        su.execute(f"DROP TABLE ulduar.lineage_edge_{datetime.fromtimestamp(now_ms / 1000, UTC):%Y_%m}")
    assert not partition_exists(archivum, "lineage_edge", now_ms)
    with open_catalog(config) as catalog:
        [edge] = catalog.add_lineage(artifact(catalog, "c"), [artifact(catalog, "p")])
        assert [n.ref for n in catalog.ancestors(edge.child)] == [edge.parent]
    assert partition_exists(archivum, "lineage_edge", now_ms)


def test_a_cycle_is_refused_whatever_the_node_kinds_and_nothing_is_kept(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        data = catalog.register_dataset("ns", "data")
        first, second = artifact(catalog, "first"), artifact(catalog, "second")
        catalog.add_lineage(first, [data])
        catalog.add_lineage(second, [first])
        with pytest.raises(LineageCycleError, match="would close a cycle"):
            catalog.add_lineage(data, [second])
        with pytest.raises(LineageCycleError):
            catalog.add_lineage(data, [artifact(catalog, "fresh"), first])  # the second of two parents closes it
        assert catalog.ancestors(data) == []
        assert [n.ref for n in catalog.descendants(data)] == [first.ref, second.ref]
        assert len(catalog.ancestors(second)) == 2  # the session survives and the earlier edges stand


def test_a_self_edge_and_an_empty_parent_list_are_refused_before_any_sql(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        only = artifact(catalog, "only")
        with pytest.raises(ValueError, match="cannot be derived from itself"):
            catalog.add_lineage(only, [artifact(catalog, "other"), only.ref])
        with pytest.raises(ValueError, match="must not be empty"):
            catalog.add_lineage(only, [])
        assert catalog.ancestors(only) == []


def test_an_unknown_node_is_refused_and_a_call_is_all_or_none(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        child, good = artifact(catalog, "child"), artifact(catalog, "good")
        ghost = NodeRef("artifact", uuid.uuid4())
        with pytest.raises(UnknownRecordError, match="not in its project"):
            catalog.add_lineage(child, [good, ghost])
        with pytest.raises(UnknownRecordError):
            catalog.add_lineage(ghost, [good])
        assert catalog.ancestors(child) == [] and catalog.descendants(good) == []
        assert catalog.add_lineage(child, [good])[0].parent == good.ref


# --- isolation between projects ---


def test_a_project_cannot_link_to_or_read_the_lineage_of_another(archivum: Archivum, tmp_path: Path) -> None:
    config_a, config_b = onboarded(archivum, tmp_path, "alpha"), onboarded(archivum, tmp_path, "beta")
    with open_catalog(config_a) as catalog_a:
        diamond = Diamond(catalog_a)
    with open_catalog(config_b) as catalog_b:
        mine = artifact(catalog_b, "mine")
        with pytest.raises(UnknownRecordError):
            catalog_b.add_lineage(mine, [diamond.sink])
        with pytest.raises(UnknownRecordError):
            catalog_b.add_lineage(diamond.sink, [mine])
        assert catalog_b.ancestors(diamond.sink) == []
        assert catalog_b.descendants(diamond.source) == []
        assert catalog_b.ancestors(mine) == []
    with open_catalog(config_a) as catalog_a:
        assert len(catalog_a.ancestors(diamond.sink)) == 4


# --- ancestors and descendants ---


def test_ancestors_are_each_listed_once_at_their_minimum_depth(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        d = Diamond(catalog)
        assert catalog.ancestors(d.sink) == by_depth((1, [d.middle.ref, d.source.ref]), (2, [d.left.ref, d.right.ref]))
        assert catalog.ancestors(d.middle) == by_depth((1, [d.left.ref, d.right.ref]), (2, [d.source.ref]))
        assert catalog.ancestors(d.source) == []
        # every way of naming a node is the same node
        assert catalog.ancestors(d.sink.ref) == catalog.ancestors(d.sink)


def test_descendants_are_each_listed_once_at_their_minimum_depth(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        d = Diamond(catalog)
        assert catalog.descendants(d.source) == by_depth(
            (1, [d.left.ref, d.right.ref, d.sink.ref]), (2, [d.middle.ref])
        )
        assert catalog.descendants(d.left) == by_depth((1, [d.middle.ref]), (2, [d.sink.ref]))
        assert catalog.descendants(d.sink) == []
        assert catalog.descendants(NodeRef("artifact", uuid.uuid4())) == []


def test_max_depth_bounds_the_walk(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        d = Diamond(catalog)
        assert catalog.ancestors(d.sink, max_depth=1) == by_depth((1, [d.middle.ref, d.source.ref]))
        assert catalog.ancestors(d.sink, max_depth=2) == catalog.ancestors(d.sink)
        assert catalog.ancestors(d.sink, max_depth=50) == catalog.ancestors(d.sink)
        assert catalog.descendants(d.source, max_depth=1) == by_depth((1, [d.left.ref, d.right.ref, d.sink.ref]))


@pytest.mark.parametrize("max_depth", [0, -1, True, 1.5])
def test_max_depth_must_be_at_least_one(archivum: Archivum, tmp_path: Path, max_depth: object) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        node = artifact(catalog, "node")
        with pytest.raises(ValueError, match="max_depth"):
            catalog.ancestors(node, max_depth=max_depth)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_depth"):
            catalog.descendants(node, max_depth=max_depth)  # type: ignore[arg-type]


def test_a_deep_chain_is_walked_to_its_end(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        chain: list[Artifact | Dataset] = [artifact(catalog, f"link {i}") for i in range(12)]
        for child, parent in zip(chain[1:], chain, strict=False):
            catalog.add_lineage(child, [parent])
        walked = catalog.ancestors(chain[-1])
        assert [n.depth for n in walked] == list(range(1, 12))
        assert [n.ref for n in walked] == [a.ref for a in reversed(chain[:-1])]
