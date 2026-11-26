"""Lineage: "derived from" edges between artifacts and datasets, and the ancestors and descendants of a node.

Input: a `Catalog` session (`catalog.py` delegates its `add_lineage`, `ancestors` and `descendants` here) and
nodes given as an `Artifact`, a `Dataset` or a `NodeRef`. Output: the frozen records `LineageEdge` and
`LineageNode`. The edge insert runs through the shared partition retry; the trigger of step 1 refuses a node
outside the project (SQLSTATE `UL002`, `UnknownRecordError`) and a cycle (`UL001`, `LineageCycleError`) under a
per-project lock. The queries are one recursive CTE each, read under the recording role's row-level security.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from psycopg import sql
from psycopg.rows import TupleRow

from ulduar import _db, _ids
from ulduar.freya.catalog import Artifact, Connection, Dataset, NodeRef, insert_with_partition_retry

if TYPE_CHECKING:
    from ulduar.freya.catalog import Catalog

Node = Artifact | Dataset | NodeRef
Direction = Literal["ancestors", "descendants"]
# The edge column pair a walk reads at the node it stands on, and the pair it moves to: an ancestor walk
# stands on a child and moves to its parents, a descendant walk the other way.
_WALK_SIDES: dict[Direction, tuple[str, str]] = {"ancestors": ("child", "parent"), "descendants": ("parent", "child")}
_EDGE = sql.Identifier("ulduar", "lineage_edge")
_EDGE_COLUMNS = sql.SQL(
    "edge_id, child_kind, child_id, parent_kind, parent_id, run_id, event_at, recorded_at"
)  # the field order of LineageEdge
_INSERT_EDGE = sql.SQL(
    "INSERT INTO {table} (project_id, edge_id, event_at, child_kind, child_id, parent_kind, parent_id, run_id)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {columns}"
).format(table=_EDGE, columns=_EDGE_COLUMNS)
# Each node once at its minimum depth. UNION (not UNION ALL) keeps a diamond from being walked once per path.
_WALK = sql.SQL(
    "WITH RECURSIVE walk(kind, id, depth) AS ("
    "SELECT %s::text, %s::uuid, 0"
    " UNION"
    " SELECT e.{next_kind}, e.{next_id}, walk.depth + 1 FROM {table} e"
    " JOIN walk ON e.{this_kind} = walk.kind AND e.{this_id} = walk.id"
    " WHERE e.project_id = %s{bound}"
    ") SELECT kind, id, min(depth) FROM walk WHERE depth > 0 GROUP BY kind, id ORDER BY min(depth), kind, id"
)
_DEPTH_BOUND = sql.SQL(" AND walk.depth < %s")
_NO_BOUND = sql.SQL("")


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """A recorded fact: *child* was derived from *parent*, optionally by the run *run_id*."""

    edge_id: UUID
    child: NodeRef
    parent: NodeRef
    run_id: UUID | None
    event_at: datetime
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class LineageNode:
    """A node reached by a lineage walk, *depth* edges from the node it started at (at least 1)."""

    ref: NodeRef
    depth: int


def _ref(node: Node) -> NodeRef:
    return node if isinstance(node, NodeRef) else node.ref


def _edge(row: TupleRow) -> LineageEdge:
    """A `LineageEdge` from a row selected in `_EDGE_COLUMNS` order."""
    return LineageEdge(row[0], NodeRef(row[1], row[2]), NodeRef(row[3], row[4]), row[5], row[6], row[7])


def _edge_ids(count: int) -> list[UUID]:
    """*count* new edge ids that share one millisecond, so every edge of a call lands in the same month's partition."""
    first = _ids.uuid7()
    unix_ms = round(_ids.uuid7_time(first).timestamp() * 1000)
    return [first, *(_ids.uuid7(unix_ms) for _ in range(count - 1))]


def add_edges(catalog: Catalog, child: Node, parents: Sequence[Node], run_id: UUID | None) -> list[LineageEdge]:
    """Record that *child* was derived from each of *parents*: every edge or none."""
    child_ref, parent_refs = _ref(child), [_ref(parent) for parent in parents]
    if not parent_refs:
        raise ValueError(f"The parents of {child_ref.kind} {child_ref.id} must not be empty")
    if child_ref in parent_refs:
        raise ValueError(f"The {child_ref.kind} {child_ref.id} cannot be derived from itself")
    edge_ids = _edge_ids(len(parent_refs))
    event_at = _ids.uuid7_time(edge_ids[0])
    project_id = catalog.project.project_id

    def insert(tx: Connection) -> list[LineageEdge]:
        edges: list[LineageEdge] = []
        for edge_id, parent in zip(edge_ids, parent_refs, strict=True):
            params = (project_id, edge_id, event_at, child_ref.kind, child_ref.id, parent.kind, parent.id, run_id)
            row = tx.execute(_INSERT_EDGE, params).fetchone()
            assert row is not None
            edges.append(_edge(row))
        return edges

    with _db.translated(f"Recording lineage of {child_ref.kind} {child_ref.id}"):
        return insert_with_partition_retry(catalog._conn, catalog._settings, event_at, insert)


def walk(catalog: Catalog, direction: Direction, node: Node, max_depth: int | None) -> list[LineageNode]:
    """The nodes *direction* of *node*, each once at its minimum depth, ordered by depth, kind and id."""
    if max_depth is not None and (isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1):
        raise ValueError(f"max_depth {max_depth!r} must be an integer of at least 1, or None")
    start = _ref(node)
    this, next_ = _WALK_SIDES[direction]
    query = _WALK.format(
        table=_EDGE,
        this_kind=sql.Identifier(f"{this}_kind"),
        this_id=sql.Identifier(f"{this}_id"),
        next_kind=sql.Identifier(f"{next_}_kind"),
        next_id=sql.Identifier(f"{next_}_id"),
        bound=_NO_BOUND if max_depth is None else _DEPTH_BOUND,
    )
    params = (start.kind, start.id, catalog.project.project_id, *(() if max_depth is None else (max_depth,)))
    with catalog._transaction(f"Reading the {direction} of {start.kind} {start.id}") as tx:
        rows = tx.execute(query, params).fetchall()
    return [LineageNode(NodeRef(kind, id_), depth) for kind, id_, depth in rows]
