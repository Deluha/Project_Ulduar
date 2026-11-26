"""ulduar.freya.lineage: edges between artifacts and datasets, cycles and foreign nodes refused, graph queries."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from tests.conftest import (
    CHECKSUM,
    Archivum,
    count,
    ms_ago,
    onboarded,
    partition_exists,
    recorder_of,
    wait_until_blocked,
)
from ulduar import _db, _ids
from ulduar.config import Config
from ulduar.errors import ArchivumUnavailable, LineageCycleError, UlduarError, UnknownRecordError
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


# --- idempotent edges ---


def test_edges_recorded_again_under_the_same_ids_are_returned_as_stored(archivum: Archivum, tmp_path: Path) -> None:
    ids = [_ids.uuid7(ms_ago(1)), _ids.uuid7(ms_ago(400))]  # two months, the older without a partition yet
    run = uuid.uuid4()
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        child, first, second = artifact(catalog, "child"), artifact(catalog, "p1"), artifact(catalog, "p2")
        edges = catalog.add_lineage(child, [first, second], run_id=run, record_ids=ids)
        assert [e.edge_id for e in edges] == ids
        assert [e.event_at for e in edges] == [_ids.uuid7_time(i) for i in ids]
        assert catalog.add_lineage(child, [first, second], run_id=run, record_ids=ids) == edges
        assert catalog.add_lineage(child, [first], run_id=run, record_ids=ids[:1]) == edges[:1]
        assert count(archivum, "SELECT count(*) FROM ulduar.lineage_edge") == 2
        assert [n.ref for n in catalog.ancestors(child)] == sorted([first.ref, second.ref], key=lambda r: r.id)


@pytest.mark.parametrize("change", ["parent", "child", "run"])
def test_an_edge_id_reused_for_other_content_is_refused_and_nothing_is_kept(
    archivum: Archivum, tmp_path: Path, change: str
) -> None:
    ids = [_ids.uuid7(), _ids.uuid7()]
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        child, first, second, other = (artifact(catalog, name) for name in ("child", "p1", "p2", "other"))
        run = uuid.uuid4()
        catalog.add_lineage(child, [first, second], run_id=run, record_ids=ids)
        again = {
            "parent": (child, [first, other], run),
            "child": (other, [first, second], run),
            "run": (child, [first, second], None),
        }[change]
        with pytest.raises(
            UlduarError, match=f"Lineage edge {ids[1] if change == 'parent' else ids[0]} is already"
        ) as caught:
            catalog.add_lineage(again[0], again[1], run_id=again[2], record_ids=ids)
        assert "different content" in str(caught.value)
        assert count(archivum, "SELECT count(*) FROM ulduar.lineage_edge") == 2


@pytest.mark.parametrize(
    ("record_ids", "message"),
    [
        ([_ids.uuid7()], "one id per parent: 2 parents, 1 ids"),
        ([_ids.uuid7()] * 3, "one id per parent: 2 parents, 3 ids"),
        ([_ids.uuid7(), uuid.uuid4()], "not a version 7 UUID"),
        ([uuid.UUID(int=7)] * 2, "not a version 7 UUID"),
    ],
)
def test_record_ids_must_be_one_uuid7_per_parent(
    archivum: Archivum, tmp_path: Path, record_ids: list[uuid.UUID], message: str
) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        child, first, second = artifact(catalog, "child"), artifact(catalog, "p1"), artifact(catalog, "p2")
        with pytest.raises(ValueError, match=message):
            catalog.add_lineage(child, [first, second], record_ids=record_ids)
        same = _ids.uuid7()
        with pytest.raises(ValueError, match="distinct"):
            catalog.add_lineage(child, [first, second], record_ids=[same, same])
        assert catalog.ancestors(child) == []


# --- the cycle guard under concurrency and isolation levels ---


def insert_edge(tx: psycopg.Connection, project_id: uuid.UUID, child: Dataset, parent: Dataset) -> None:
    edge_id = _ids.uuid7()
    tx.execute(
        "INSERT INTO ulduar.lineage_edge (project_id, edge_id, event_at, child_kind, child_id, parent_kind, parent_id)"
        " VALUES (%s, %s, %s, 'dataset', %s, 'dataset', %s)",
        (project_id, edge_id, _ids.uuid7_time(edge_id), child.dataset_id, parent.dataset_id),
    )


def two_datasets(config: Config) -> tuple[uuid.UUID, Dataset, Dataset]:
    with open_catalog(config) as catalog:
        return catalog.project.project_id, catalog.register_dataset("n", "one"), catalog.register_dataset("n", "two")


@pytest.mark.parametrize("role_default", ["read committed", "repeatable read"])
def test_opposite_edges_in_flight_together_cannot_both_commit(
    archivum: Archivum, tmp_path: Path, role_default: str
) -> None:
    config = onboarded(archivum, tmp_path, lock_timeout_s=30)
    project_id, one, two = two_datasets(config)
    role = recorder_of(archivum, config).role
    with archivum.superuser() as su:
        su.execute(
            psycopg.sql.SQL("ALTER ROLE {} SET default_transaction_isolation = {}").format(
                psycopg.sql.Identifier(role), psycopg.sql.Literal(role_default)
            )
        )
    inserted = threading.Event()

    def raw_edge() -> None:  # one derived from two, in a transaction that stays open until the other is queued
        with _db.open_connection(recorder_of(archivum, config).env, config.archivum) as conn:
            with _db.transaction(conn, config.archivum) as tx:
                insert_edge(tx, project_id, one, two)
                inserted.set()
                wait_until_blocked(archivum)

    def library_edge() -> None:  # two derived from one
        assert inserted.wait(timeout=30)
        with open_catalog(config) as catalog:
            catalog.add_lineage(two, [one])

    with ThreadPoolExecutor(max_workers=2) as pool:
        raw, library = pool.submit(raw_edge), pool.submit(library_edge)
        raw.result(timeout=60)
        with pytest.raises(LineageCycleError, match="would close a cycle"):
            library.result(timeout=60)
    assert count(archivum, "SELECT count(*) FROM ulduar.lineage_edge") == 1


def test_a_lineage_edge_outside_read_committed_is_refused_by_the_database(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    project_id, one, two = two_datasets(config)
    with _db.open_connection(recorder_of(archivum, config).env, config.archivum) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as tx:
            insert_edge(tx, project_id, one, two)
    assert caught.value.sqlstate == "UL003"
    error = _db.translate(caught.value, "Recording")
    assert type(error) is UlduarError
    assert "READ COMMITTED" in str(error)
    assert count(archivum, "SELECT count(*) FROM ulduar.lineage_edge") == 0


def test_a_walk_over_a_cycle_that_got_into_the_table_ends(archivum: Archivum, tmp_path: Path) -> None:
    """Bounded, the walk returns; unbounded, the statement timeout ends it — it never runs on."""
    config = onboarded(archivum, tmp_path, statement_timeout_s=1)
    with open_catalog(config) as catalog:
        first, second = artifact(catalog, "first"), artifact(catalog, "second")
        with archivum.superuser() as su:
            su.execute("SET session_replication_role = replica")  # no trigger: the guard that normally refuses this
            for child, parent in ((first, second), (second, first)):
                edge_id = _ids.uuid7()
                su.execute(
                    "INSERT INTO ulduar.lineage_edge (project_id, edge_id, event_at, child_kind, child_id, parent_kind,"
                    " parent_id) VALUES (%s, %s, %s, 'artifact', %s, 'artifact', %s)",
                    (
                        catalog.project.project_id,
                        edge_id,
                        _ids.uuid7_time(edge_id),
                        child.artifact_id,
                        parent.artifact_id,
                    ),
                )
        assert {n.ref for n in catalog.ancestors(first, max_depth=4)} == {first.ref, second.ref}
        with pytest.raises(ArchivumUnavailable):
            catalog.descendants(second)


def test_a_walk_over_stacked_diamonds_costs_nodes_times_depths_not_paths(archivum: Archivum, tmp_path: Path) -> None:
    """Twenty stacked diamonds hold 2**20 paths; the walk returns each of their 61 nodes once, within the timeout."""
    config = onboarded(archivum, tmp_path, statement_timeout_s=5)
    with open_catalog(config) as catalog:
        top = artifact(catalog, "top-0")
        bottom = top
        for level in range(20):
            left, right = artifact(catalog, f"left-{level}"), artifact(catalog, f"right-{level}")
            catalog.add_lineage(left, [bottom])
            catalog.add_lineage(right, [bottom])
            bottom = artifact(catalog, f"top-{level + 1}")
            catalog.add_lineage(bottom, [left, right])
        found = catalog.ancestors(bottom)
    assert len(found) == 60
    assert len({n.ref for n in found}) == 60
    assert found[-1] == LineageNode(top.ref, 40)
