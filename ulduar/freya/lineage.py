"""Lineage: "derived from" edges between artifacts and datasets, and the ancestors and descendants of a node.

Input: the session's connection, settings and project (`Catalog` passes its own to `add_edges` and `walk`) and
nodes given as an `Artifact`, a `Dataset` or a `NodeRef`. Output: `LineageEdge` and `LineageNode` (the records
live in `_records`). The edge insert runs through the shared partition retry; the trigger of step 1 refuses a
node outside the project (SQLSTATE `UL002`, `UnknownRecordError`) and a cycle (`UL001`, `LineageCycleError`)
under a per-project lock. The queries are one recursive CTE each, read under the recording role's row-level
security.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg import sql
from psycopg.rows import TupleRow

from ulduar import _db, _ids
from ulduar.config import ArchivumSettings
from ulduar.errors import UlduarError
from ulduar.freya._records import (
    Artifact,
    Connection,
    Dataset,
    LineageEdge,
    LineageNode,
    NodeRef,
    Project,
    insert_with_partition_retry,
    qualified,
)

Node = Artifact | Dataset | NodeRef
Direction = Literal["ancestors", "descendants"]
# The edge column pair a walk reads at the node it stands on, and the pair it moves to: an ancestor walk
# stands on a child and moves to its parents, a descendant walk the other way.
_WALK_SIDES: dict[Direction, tuple[str, str]] = {"ancestors": ("child", "parent"), "descendants": ("parent", "child")}
_EDGE = qualified("lineage_edge")
_EDGE_COLUMNS = sql.SQL(
    "edge_id, child_kind, child_id, parent_kind, parent_id, run_id, event_at, recorded_at"
)  # the field order of LineageEdge
_INSERT_EDGE = sql.SQL(
    "INSERT INTO {table} (project_id, edge_id, event_at, child_kind, child_id, parent_kind, parent_id, run_id)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING {columns}"
).format(table=_EDGE, columns=_EDGE_COLUMNS)
_SELECT_EDGE = sql.SQL("SELECT {columns} FROM {table} WHERE project_id = %s AND edge_id = %s").format(
    table=_EDGE, columns=_EDGE_COLUMNS
)
# Each node once at its minimum depth. UNION keeps one row per (kind, id, depth), so a walk costs nodes times
# depths, never the number of paths. A cycle cannot get into the table (step 1's trigger, under the READ
# COMMITTED that _db pins); if one did, an unbounded walk would end at the statement timeout.
_WALK = sql.SQL(
    "WITH RECURSIVE walk(kind, id, depth) AS ("
    "SELECT %s::text, %s::uuid, 0"
    " UNION"
    " SELECT e.{next_kind}, e.{next_id}, walk.depth + 1 FROM {table} e"
    " JOIN walk ON e.{this_kind} = walk.kind AND e.{this_id} = walk.id"
    " WHERE e.project_id = %s{bound}"
    ")"
    " SELECT kind, id, min(depth) FROM walk WHERE depth > 0 GROUP BY kind, id ORDER BY min(depth), kind, id"
)
_DEPTH_BOUND = sql.SQL(" AND walk.depth < %s")
_NO_BOUND = sql.SQL("")


def _ref(node: Node) -> NodeRef:
    return node if isinstance(node, NodeRef) else node.ref


def _edge(row: TupleRow) -> LineageEdge:
    """A `LineageEdge` from a row selected in `_EDGE_COLUMNS` order."""
    return LineageEdge(row[0], NodeRef(row[1], row[2]), NodeRef(row[3], row[4]), row[5], row[6], row[7])


def _edge_ids(count: int, record_ids: Sequence[UUID] | None) -> list[UUID]:
    """The ids of *count* edges: the caller's *record_ids* (checked), else new ones sharing one millisecond.

    New ids share a millisecond so every edge of a call lands in the same month's partition.
    """
    if record_ids is None:
        first = _ids.uuid7()
        unix_ms = round(_ids.uuid7_time(first).timestamp() * 1000)
        return [first, *(_ids.uuid7(unix_ms) for _ in range(count - 1))]
    if len(record_ids) != count:
        raise ValueError(f"record_ids must hold one id per parent: {count} parents, {len(record_ids)} ids")
    for record_id in record_ids:
        _ids.uuid7_time(record_id)  # raises ValueError naming the id when it is not a version 7 UUID
    if len(set(record_ids)) != count:
        raise ValueError("record_ids must be distinct")
    return list(record_ids)


def _record_edge(
    tx: Connection,
    project_id: UUID,
    edge_id: UUID,
    event_at: datetime,
    child: NodeRef,
    parent: NodeRef,
    run_id: UUID | None,
) -> LineageEdge:
    """Insert the edge; one already recorded under *edge_id* is returned as stored when its content is the same."""
    params = (project_id, edge_id, event_at, child.kind, child.id, parent.kind, parent.id, run_id)
    row = tx.execute(_INSERT_EDGE, params).fetchone()
    if row is not None:
        return _edge(row)
    stored = tx.execute(_SELECT_EDGE, (project_id, edge_id)).fetchone()
    edge = None if stored is None else _edge(stored)
    if edge is None or (edge.child, edge.parent, edge.run_id) != (child, parent, run_id):
        raise UlduarError(f"Lineage edge {edge_id} is already recorded with different content")
    return edge


def add_edges(
    conn: Connection,
    settings: ArchivumSettings,
    project: Project,
    child: Node,
    parents: Sequence[Node],
    run_id: UUID | None,
    record_ids: Sequence[UUID] | None,
) -> list[LineageEdge]:
    """Record that *child* was derived from each of *parents*: every edge or none.

    With *record_ids* (one UUIDv7 per parent) the call is idempotent: an edge already recorded under its id
    with the same content is returned as stored, with other content it is a `UlduarError`.
    """
    child_ref, parent_refs = _ref(child), [_ref(parent) for parent in parents]
    if not parent_refs:
        raise ValueError(f"The parents of {child_ref.kind} {child_ref.id} must not be empty")
    if child_ref in parent_refs:
        raise ValueError(f"The {child_ref.kind} {child_ref.id} cannot be derived from itself")
    edge_ids = _edge_ids(len(parent_refs), record_ids)
    event_ats = [_ids.uuid7_time(edge_id) for edge_id in edge_ids]

    def insert(tx: Connection) -> list[LineageEdge]:
        return [
            _record_edge(tx, project.project_id, edge_id, event_at, child_ref, parent, run_id)
            for edge_id, event_at, parent in zip(edge_ids, event_ats, parent_refs, strict=True)
        ]

    with _db.translated(f"Recording lineage of {child_ref.kind} {child_ref.id}"):
        return insert_with_partition_retry(conn, settings, event_ats, insert)


def walk(
    conn: Connection,
    settings: ArchivumSettings,
    project: Project,
    direction: Direction,
    node: Node,
    max_depth: int | None,
) -> list[LineageNode]:
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
    params = (start.kind, start.id, project.project_id, *(() if max_depth is None else (max_depth,)))
    with _db.translated_transaction(conn, settings, f"Reading the {direction} of {start.kind} {start.id}") as tx:
        rows = tx.execute(query, params).fetchall()
    return [LineageNode(NodeRef(kind, id_), depth) for kind, id_, depth in rows]
