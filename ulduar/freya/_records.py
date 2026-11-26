"""What Freya's modules share: the frozen records, the schema's names and the partition retry.

Private: the facade re-exports the records, a consumer never imports this module. `qualified` is the one home of
the schema name; every composed statement reaches a table or function of the Archivum through it. The records
sit here, below the catalog, lineage and history modules, so none of them imports another to build one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeVar
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from ulduar import _db
from ulduar.config import ArchivumSettings

logger = logging.getLogger(__name__)

SCHEMA = "ulduar"
# `INSERT` into a partitioned table whose month has no partition: check_violation, and no constraint is named.
_CHECK_VIOLATION = "23514"

Connection = psycopg.Connection[TupleRow]
Result = TypeVar("Result")


def qualified(name: str) -> sql.Identifier:
    """The table or function *name* of schema `ulduar`, quoted."""
    return sql.Identifier(SCHEMA, name)


@dataclass(frozen=True, slots=True)
class Project:
    """A project of the Archivum, as the configuration names it."""

    project_id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A lineage node: an artifact or a dataset, by id."""

    kind: Literal["artifact", "dataset"]
    id: UUID


@dataclass(frozen=True, slots=True)
class Dataset:
    """A dataset, identified by project, scope, namespace and name; `event_at` is the time inside its id."""

    dataset_id: UUID
    project_id: UUID
    scope: str
    namespace: str
    name: str
    event_at: datetime
    recorded_at: datetime

    @property
    def ref(self) -> NodeRef:
        return NodeRef("dataset", self.dataset_id)


@dataclass(frozen=True, slots=True)
class Artifact:
    """An archived object's catalog entry: SHA-256 checksum, size, kind, origin and the dataset it belongs to."""

    artifact_id: UUID
    project_id: UUID
    scope: str
    kind: str
    checksum: bytes
    size: int
    origin: str | None
    dataset_id: UUID | None
    event_at: datetime
    recorded_at: datetime

    @property
    def ref(self) -> NodeRef:
        return NodeRef("artifact", self.artifact_id)


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
class RetentionAction:
    """A recorded application of a history-retention rule: the rows of *history* older than *cutoff* were removed.

    *keep_days* and *rule_event_at* are the applied rule's (*rule_id*); *rows_removed* is how many rows went;
    *event_at* is the time inside *action_id*.
    """

    action_id: UUID
    project: str
    history: str
    rule_id: UUID
    keep_days: int | None
    rule_event_at: datetime
    cutoff: datetime
    rows_removed: int
    event_at: datetime


@dataclass(frozen=True, slots=True)
class LineageNode:
    """A node reached by a lineage walk, *depth* edges from the node it started at (at least 1)."""

    ref: NodeRef
    depth: int


def _is_missing_partition(error: psycopg.Error) -> bool:
    return error.sqlstate == _CHECK_VIOLATION and error.diag.constraint_name is None


def insert_with_partition_retry(
    conn: Connection, settings: ArchivumSettings, event_ats: Sequence[datetime], insert: Callable[[Connection], Result]
) -> Result:
    """Run *insert* in a transaction; if a month it writes to has no partition, create them and run it once more.

    *event_ats* are the times of the rows *insert* writes. The failed insert has rolled back, so each distinct
    month is made by `ulduar.ensure_partitions` (the step's own function; the schema is never changed from
    Python) in a transaction of its own, then *insert* runs again. *insert* must be safe to run twice: it
    carries the records' own ids, so a retry cannot duplicate them.
    """
    try:
        with _db.transaction(conn, settings) as tx:
            return insert(tx)
    except psycopg.Error as e:
        if not _is_missing_partition(e):
            raise
    months = {(moment.year, moment.month): moment for moment in (at.astimezone(UTC) for at in event_ats)}
    made: dict[tuple[int, int], int] = {}
    with _db.transaction(conn, settings) as tx:
        for key, moment in months.items():
            row = tx.execute(sql.SQL("SELECT {}(%s, 0)").format(qualified("ensure_partitions")), (moment,)).fetchone()
            assert row is not None
            made[key] = row[0]
    for (year, month), partitions in made.items():
        if partitions > 0:
            logger.info("created %d time partition(s) for %04d-%02d", partitions, year, month)
    with _db.transaction(conn, settings) as tx:
        return insert(tx)
