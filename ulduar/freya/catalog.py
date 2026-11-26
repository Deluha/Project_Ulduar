"""The catalog session: register and look up a project's datasets and artifacts in the Archivum.

Input: a `Config` (the recording role's connection string is named by `archivum.dsn_env`). Output: `open_catalog`,
which yields a `Catalog` holding one connection, and the records it returns (`_records`). Every `Catalog` call
is one transaction; the recording role can only select and insert, and row-level security shows it only its
own project, so nothing here can touch another project or change recorded history. A partitioned insert that
meets a month without a partition creates it once through the step's own function and retries
(`insert_with_partition_retry`, shared with lineage).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from uuid import UUID

from psycopg import sql

from ulduar import _db, _ids
from ulduar.config import ArchivumSettings, Config, check_name
from ulduar.errors import UlduarError, UnknownRecordError
from ulduar.freya import ledger, lineage
from ulduar.freya._records import (
    Artifact,
    Connection,
    Dataset,
    LineageEdge,
    LineageNode,
    NodeRef,
    Project,
    Result,
    insert_with_partition_retry,
    qualified,
)

# The rows of `ulduar.artifact_kind` (step 1 is the schema's home; a test holds the two equal).
ARTIFACT_KINDS = ("raw_file", "database_dump", "report", "model", "log")
CHECKSUM_ALGORITHM = "sha256"
CHECKSUM_BYTES = 32
NAMESPACE_MAX = 200
NAME_MAX = 500
ORIGIN_MAX = 1000

_PROJECT = qualified("project")
_SCOPE = qualified("scope")
_DATASET = qualified("dataset")
_ARTIFACT = qualified("artifact")


def _columns(*names: str) -> sql.Composable:
    return sql.SQL(", ").join(sql.Identifier(name) for name in names)


# Selected in the field order of the records, so a row builds its record positionally.
_DATASET_COLUMNS = _columns("dataset_id", "project_id", "scope", "namespace", "name", "event_at", "recorded_at")
_ARTIFACT_COLUMNS = _columns(
    "artifact_id",
    "project_id",
    "scope",
    "kind",
    "checksum",
    "size_bytes",
    "origin",
    "dataset_id",
    "event_at",
    "recorded_at",
)
_INSERT_DATASET = sql.SQL(
    "INSERT INTO {table} (project_id, scope, dataset_id, namespace, name, event_at) VALUES (%s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (project_id, scope, namespace, name) DO NOTHING RETURNING {columns}"
).format(table=_DATASET, columns=_DATASET_COLUMNS)
_SELECT_DATASET = sql.SQL(
    "SELECT {columns} FROM {table} WHERE project_id = %s AND scope = %s AND namespace = %s AND name = %s"
).format(table=_DATASET, columns=_DATASET_COLUMNS)
_INSERT_ARTIFACT = sql.SQL(
    "INSERT INTO {table} (project_id, scope, artifact_id, event_at, kind, checksum_algorithm, checksum, size_bytes,"
    " origin, dataset_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING {columns}"
).format(table=_ARTIFACT, columns=_ARTIFACT_COLUMNS)
_SELECT_ARTIFACT = sql.SQL("SELECT {columns} FROM {table} WHERE project_id = %s AND artifact_id = %s").format(
    table=_ARTIFACT, columns=_ARTIFACT_COLUMNS
)
_FIND_ARTIFACTS = sql.SQL("SELECT {columns} FROM {table} WHERE {conditions} ORDER BY event_at, artifact_id")


def _check_text(argument: str, value: str | None, limit: int, *, required: bool = True) -> None:
    """*value* is a string of 1 to *limit* characters (or None when not *required*); the message names the length."""
    if value is None and not required:
        return
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        found = f"{len(value)} characters" if isinstance(value, str) else f"a {type(value).__name__}"
        raise ValueError(f"The {argument} must be a string of 1 to {limit} characters, got {found}")


def check_kind(kind: str) -> None:
    """`ValueError` unless *kind* is one of `ARTIFACT_KINDS`."""
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"Artifact kind {kind!r} is not one of {', '.join(ARTIFACT_KINDS)}")


def check_checksum(checksum: bytes) -> None:
    """`ValueError` unless *checksum* is a SHA-256 digest of `CHECKSUM_BYTES` bytes."""
    if not isinstance(checksum, bytes) or len(checksum) != CHECKSUM_BYTES:
        found = f"{len(checksum)} bytes" if isinstance(checksum, bytes) else f"a {type(checksum).__name__}"
        raise ValueError(f"The checksum must be a SHA-256 digest of {CHECKSUM_BYTES} bytes, got {found}")


def check_origin(origin: str | None) -> None:
    """`ValueError` unless *origin* is None or a string of 1 to `ORIGIN_MAX` characters."""
    _check_text("origin", origin, ORIGIN_MAX, required=False)


def _check_artifact(kind: str, checksum: bytes, size: int, origin: str | None) -> None:
    check_kind(kind)
    check_checksum(checksum)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(f"The size {size!r} must be a non-negative integer")
    check_origin(origin)


def _record_id(record_id: UUID | None) -> UUID:
    """The caller's idempotency key when given (it must be a UUIDv7), else a new id."""
    if record_id is None:
        return _ids.uuid7()
    _ids.uuid7_time(record_id)  # raises ValueError naming the id when it is not a version 7 UUID
    return record_id


class Catalog:
    """One project's catalog session over one connection; made by `open_catalog`, closed with it.

    `project` is the project the configuration names and `scope` its default scope. Every method is one
    transaction. The connection is not thread-safe: a job holds one session at a time.
    """

    def __init__(self, conn: Connection, settings: ArchivumSettings, project: Project, scope: str) -> None:
        self._conn = conn
        self._settings = settings
        self.project = project
        self.scope = scope

    def _transaction(self, what: str) -> AbstractContextManager[Connection]:
        """One transaction whose driver errors are raised as the package's exceptions, worded `<what> failed`."""
        return _db.translated_transaction(self._conn, self._settings, what)

    def transaction(
        self, what: str, work: Callable[[Connection], Result], *, event_ats: Sequence[datetime] = ()
    ) -> Result:
        """Run *work* in one transaction on this session's connection and return what it returns.

        The hook through which Ulduar's other components (Hodir now, Thorim and Algalon later) record on the
        job's one connection (H23): the transaction carries the session's time limits, and a driver error is
        raised as the package's exception, worded `<what> failed: ...`. With *event_ats*, the times of the
        partitioned rows *work* writes, a month without a partition is created once and *work* runs again, so
        *work* must be safe to run twice.
        """
        if event_ats:
            with _db.translated(what):
                return insert_with_partition_retry(self._conn, self._settings, event_ats, work)
        with self._transaction(what) as tx:
            return work(tx)

    def _scope(self, scope: str | None) -> str:
        return self.scope if scope is None else check_name(scope, "scope")

    def register_dataset(self, namespace: str, name: str, *, scope: str | None = None) -> Dataset:
        """Register the dataset *namespace*/*name* in the scope; registering it again returns the existing one."""
        _check_text("namespace", namespace, NAMESPACE_MAX)
        _check_text("name", name, NAME_MAX)
        scope = self._scope(scope)
        dataset_id = _ids.uuid7()
        params = (self.project.project_id, scope, dataset_id, namespace, name, _ids.uuid7_time(dataset_id))
        with self._transaction(f"Registering dataset {namespace}/{name}") as tx:
            row = tx.execute(_INSERT_DATASET, params).fetchone()
            if row is None:  # registered before, possibly a moment ago by another job
                row = tx.execute(_SELECT_DATASET, (self.project.project_id, scope, namespace, name)).fetchone()
            if row is None:
                raise UlduarError(f"Dataset {namespace}/{name} could not be registered or found")
            return Dataset(*row)

    def get_dataset(self, namespace: str, name: str, *, scope: str | None = None) -> Dataset:
        """The registered dataset *namespace*/*name*; `UnknownRecordError` when there is none."""
        scope = self._scope(scope)
        with self._transaction(f"Reading dataset {namespace}/{name}") as tx:
            row = tx.execute(_SELECT_DATASET, (self.project.project_id, scope, namespace, name)).fetchone()
            if row is None:
                raise UnknownRecordError(f"Dataset {namespace}/{name} is not registered in scope {scope}")
            return Dataset(*row)

    def register_artifact(
        self,
        kind: str,
        checksum: bytes,
        size: int,
        *,
        origin: str | None = None,
        dataset: Dataset | None = None,
        scope: str | None = None,
        record_id: UUID | None = None,
    ) -> Artifact:
        """Record an artifact: its *kind*, SHA-256 *checksum* (32 bytes) and *size* in bytes.

        *record_id* (a UUIDv7) is the idempotency key: registering the same id again with the same content
        returns the recorded artifact, with different content raises `UlduarError`. The id's time is the
        artifact's `event_at`; a month without a partition gets one created, once, and the insert retried.
        """
        _check_artifact(kind, checksum, size, origin)
        scope = self._scope(scope)
        artifact_id = _record_id(record_id)
        event_at = _ids.uuid7_time(artifact_id)
        dataset_id = None if dataset is None else dataset.dataset_id
        project_id = self.project.project_id
        content = (scope, kind, checksum, size, origin, dataset_id)

        def insert(tx: Connection) -> Artifact:
            params = (project_id, scope, artifact_id, event_at, kind, CHECKSUM_ALGORITHM, checksum, size, origin)
            row = tx.execute(_INSERT_ARTIFACT, (*params, dataset_id)).fetchone()
            if row is not None:
                return Artifact(*row)
            existing = _select_artifact(tx, project_id, artifact_id)  # the id was recorded before
            if (
                existing is None
                or (
                    existing.scope,
                    existing.kind,
                    existing.checksum,
                    existing.size,
                    existing.origin,
                    existing.dataset_id,
                )
                != content
            ):
                raise UlduarError(f"Artifact {artifact_id} is already recorded with different content")
            return existing

        with _db.translated(f"Registering artifact {artifact_id}"):
            return insert_with_partition_retry(self._conn, self._settings, [event_at], insert)

    def get_artifact(self, artifact_id: UUID) -> Artifact:
        """The recorded artifact *artifact_id*; `UnknownRecordError` when this project has none."""
        with self._transaction(f"Reading artifact {artifact_id}") as tx:
            artifact = _select_artifact(tx, self.project.project_id, artifact_id)
            if artifact is None:
                raise UnknownRecordError(f"Artifact {artifact_id} is not recorded in project {self.project.name}")
            return artifact

    def find_artifacts(
        self,
        *,
        checksum: bytes | None = None,
        kind: str | None = None,
        dataset: Dataset | None = None,
        scope: str | None = None,
    ) -> list[Artifact]:
        """The scope's artifacts (default: the catalog's) that match every given filter, oldest first."""
        if kind is not None:
            check_kind(kind)
        if checksum is not None:
            check_checksum(checksum)
        filters = {
            "scope": self._scope(scope),
            "checksum": checksum,
            "kind": kind,
            "dataset_id": None if dataset is None else dataset.dataset_id,
        }
        given = {column: value for column, value in filters.items() if value is not None}
        conditions = sql.SQL(" AND ").join(
            [sql.SQL("project_id = %s"), *(sql.SQL("{} = %s").format(sql.Identifier(column)) for column in given)]
        )
        query = _FIND_ARTIFACTS.format(columns=_ARTIFACT_COLUMNS, table=_ARTIFACT, conditions=conditions)
        with self._transaction(f"Finding artifacts of project {self.project.name}") as tx:
            rows = tx.execute(query, (self.project.project_id, *given.values())).fetchall()
        return [Artifact(*row) for row in rows]

    def add_lineage(
        self,
        child: Artifact | Dataset | NodeRef,
        parents: Sequence[Artifact | Dataset | NodeRef],
        *,
        run_id: UUID | None = None,
        record_ids: Sequence[UUID] | None = None,
    ) -> list[LineageEdge]:
        """Record that *child* was derived from each of *parents* (all edges or none); *run_id* names the run.

        *record_ids*, one UUIDv7 per parent, make the call idempotent: an edge already recorded under its id with
        the same content is returned as stored, with other content it raises `UlduarError`; each edge's
        `event_at` is the time of its id. `ValueError` for no parents, a node derived from itself or bad
        *record_ids*; `UnknownRecordError` for a node that is not in this project; `LineageCycleError` when an
        edge would close a cycle.
        """
        return lineage.add_edges(self._conn, self._settings, self.project, child, parents, run_id, record_ids)

    def ancestors(self, node: Artifact | Dataset | NodeRef, *, max_depth: int | None = None) -> list[LineageNode]:
        """What *node* was derived from, directly or not: each node once at its minimum depth, nearest first.

        Ordered by depth, kind and id; *node* itself is not included; *max_depth* (at least 1) limits the walk.
        A node that is unknown, or has no edges, has no ancestors.
        """
        return lineage.walk(self._conn, self._settings, self.project, "ancestors", node, max_depth)

    def descendants(self, node: Artifact | Dataset | NodeRef, *, max_depth: int | None = None) -> list[LineageNode]:
        """What was derived from *node*, directly or not; ordered and limited as `ancestors`."""
        return lineage.walk(self._conn, self._settings, self.project, "descendants", node, max_depth)


def _select_artifact(tx: Connection, project_id: UUID, artifact_id: UUID) -> Artifact | None:
    row = tx.execute(_SELECT_ARTIFACT, (project_id, artifact_id)).fetchone()
    return None if row is None else Artifact(*row)


def _resolve_project(tx: Connection, name: str, scope: str) -> Project:
    """The project *name* the role belongs to (row-level security hides every other), with *scope* present."""
    row = tx.execute(sql.SQL("SELECT project_id FROM {} WHERE name = %s").format(_PROJECT), (name,)).fetchone()
    if row is None:
        raise UnknownRecordError(f"Project {name} is not onboarded for this role")
    project = Project(row[0], name)
    found = tx.execute(
        sql.SQL("SELECT 1 FROM {} WHERE project_id = %s AND scope = %s").format(_SCOPE), (project.project_id, scope)
    ).fetchone()
    if found is None:
        raise UnknownRecordError(f"Scope {scope} is not onboarded for project {name}")
    return project


@contextmanager
def open_catalog(config: Config) -> Iterator[Catalog]:
    """Open a catalog session as the recording role named by `archivum.dsn_env`; the connection closes on exit.

    Checks the schema first (`SchemaVersionError` when the Archivum is not installed or does not fit this
    library), then that the role is onboarded for `project.name` and that `project.scope` exists
    (`UnknownRecordError`). A role that was never granted the recording group is refused by the database
    (`IsolationError`) before it can look.
    """
    settings = config.archivum
    name, scope = config.project.name, config.project.scope
    with _db.open_connection(settings.dsn_env, settings) as conn:
        with _db.translated(f"Opening the catalog of project {name}"), _db.transaction(conn, settings) as tx:
            ledger.check_schema(tx, ledger.load_steps())
            project = _resolve_project(tx, name, scope)
        yield Catalog(conn, settings, project, scope)
