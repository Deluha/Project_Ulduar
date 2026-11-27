"""Hodir's SQL against the Archivum, in one place: the locks, the registration insert, the archived check, the
verification and retention reads, the deletion records and the reads the record sweep and a rebuild make.

Input: a transaction of the catalog session (`Catalog.transaction`), or of the ledger role for the retention rule,
and the ids and checksums of one put, get, verification or deletion. Output: the registration row of an archived
artifact, whether an artifact's object is still archived, the live addresses and registrations of a scope, the
rows of a check, the latest retention rules, the intent and outcome rows of a deletion, a dataset by id, and the
maintenance records (deletions, redactions, history-retention actions) of a project. Private: Hodir's
modules import it, a consumer never does. Every statement composes identifiers through Freya's `qualified` and
binds values as parameters (hard rule 3).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import sql

from ulduar import _ids
from ulduar._schema import ARCHIVE_REGISTRATION, ARTIFACT_IS_ARCHIVED, advisory_xact_lock, qualified
from ulduar.errors import UlduarError, UnknownRecordError
from ulduar.freya import Artifact, Connection, Dataset, Project
from ulduar.hodir._layout import DeletionRecord, HistoryRetentionRecord, MaintenanceRecord, RedactionRecord
from ulduar.hodir._records import PASSED, Finding, LatestRule, LiveRegistration

_INSERT_REGISTRATION = sql.SQL(
    "INSERT INTO {} (project_id, scope, artifact_id, event_at, retention_class) VALUES (%s, %s, %s, %s, %s)"
    " ON CONFLICT (project_id, artifact_id) DO NOTHING RETURNING 1"
).format(ARCHIVE_REGISTRATION)
_SELECT_CLASS = sql.SQL("SELECT retention_class FROM {} WHERE project_id = %s AND artifact_id = %s").format(
    ARCHIVE_REGISTRATION
)

_SELECT_SCOPE = sql.SQL("SELECT 1 FROM {} WHERE project_id = %s AND scope = %s").format(qualified("scope"))
_IS_ARCHIVED = sql.SQL("SELECT {}(%s, %s)").format(ARTIFACT_IS_ARCHIVED)
_LIVE = sql.SQL(
    "SELECT DISTINCT x.checksum, x.size_bytes FROM {registration} r"
    " JOIN {artifact} x ON x.project_id = r.project_id AND x.artifact_id = r.artifact_id AND x.event_at = r.event_at"
    " WHERE r.project_id = %(project_id)s AND r.scope = %(scope)s AND {archived}(r.project_id, r.artifact_id)"
).format(registration=ARCHIVE_REGISTRATION, artifact=qualified("artifact"), archived=ARTIFACT_IS_ARCHIVED)
_SELECT_LIVE = sql.SQL("{} ORDER BY checksum").format(_LIVE)
# The sample: the live addresses whose latest verification finding, of any outcome (by the database's clock), is
# oldest, never-verified first.
_SELECT_SAMPLE = sql.SQL(
    "WITH live AS ({live}) SELECT l.checksum, l.size_bytes FROM live l"
    " LEFT JOIN LATERAL (SELECT max(f.recorded_at) AS last_verified FROM {finding} f"
    " JOIN {check} c ON c.project_id = f.project_id AND c.check_id = f.check_id AND c.kind = 'verification'"
    " WHERE f.project_id = %(project_id)s AND f.scope = %(scope)s AND f.checksum = l.checksum) p ON true"
    " ORDER BY p.last_verified NULLS FIRST, l.checksum LIMIT %(n)s"
).format(live=_LIVE, finding=qualified("archive_finding"), check=qualified("archive_check"))
_INSERT_CHECK = sql.SQL(
    "INSERT INTO {} (project_id, check_id, event_at, scope, kind, mode, objects_checked, findings)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
).format(qualified("archive_check"))
_INSERT_FINDING = sql.SQL(
    "INSERT INTO {} (project_id, finding_id, event_at, check_id, scope, checksum, size_bytes, outcome)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
).format(qualified("archive_finding"))

_RULE = qualified("archive_retention_rule")
_INSERT_RULE = sql.SQL(
    "INSERT INTO {} (project_id, rule_id, event_at, retention_class, keep_days, keep_newest, shortening_confirmed)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
).format(_RULE)
# The latest rule (by when it was recorded) of each class of the project.
_LATEST_RULES = sql.SQL(
    "SELECT DISTINCT ON (retention_class) retention_class, rule_id, keep_days, keep_newest FROM {}"
    " WHERE project_id = %s ORDER BY retention_class, recorded_at DESC, rule_id DESC"
).format(_RULE)
_LIVE_REGISTRATIONS = sql.SQL(
    "SELECT r.scope, x.checksum, x.size_bytes, r.artifact_id, x.dataset_id, r.retention_class, r.recorded_at"
    " FROM {registration} r"
    " JOIN {artifact} x ON x.project_id = r.project_id AND x.artifact_id = r.artifact_id AND x.event_at = r.event_at"
    " WHERE r.project_id = %(project_id)s AND (%(scope)s::text IS NULL OR r.scope = %(scope)s)"
    " AND {archived}(r.project_id, r.artifact_id) ORDER BY r.scope, x.checksum, r.artifact_id"
).format(registration=ARCHIVE_REGISTRATION, artifact=qualified("artifact"), archived=ARTIFACT_IS_ARCHIVED)
_ADDRESS_ARTIFACTS = sql.SQL(
    "SELECT r.artifact_id FROM {registration} r"
    " JOIN {artifact} x ON x.project_id = r.project_id AND x.artifact_id = r.artifact_id AND x.event_at = r.event_at"
    " WHERE r.project_id = %s AND r.scope = %s AND x.checksum = %s ORDER BY r.artifact_id"
).format(registration=ARCHIVE_REGISTRATION, artifact=qualified("artifact"))
_REGISTERED_AFTER = sql.SQL(
    "SELECT EXISTS (SELECT FROM {registration} r"
    " JOIN {artifact} x ON x.project_id = r.project_id AND x.artifact_id = r.artifact_id AND x.event_at = r.event_at"
    " WHERE r.project_id = %s AND r.scope = %s AND x.checksum = %s AND r.recorded_at > %s)"
).format(registration=ARCHIVE_REGISTRATION, artifact=qualified("artifact"))
_DELETION = qualified("archive_deletion")
_DELETION_COLUMNS = sql.SQL(
    "deletion_id, event_at, recorded_at, scope, checksum, size_bytes, stage, intent_id, cause, rule_id, reason"
)
_INSERT_DELETION = sql.SQL(
    "INSERT INTO {} (project_id, deletion_id, event_at, scope, checksum, size_bytes, stage, intent_id, cause,"
    " rule_id, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING recorded_at"
).format(_DELETION)
_REGISTERED_IDS = sql.SQL("SELECT artifact_id FROM {} WHERE project_id = %s AND scope = %s").format(
    ARCHIVE_REGISTRATION
)
_ADDRESS_LIVE = sql.SQL(
    "SELECT EXISTS (SELECT FROM {registration} r"
    " JOIN {artifact} x ON x.project_id = r.project_id AND x.artifact_id = r.artifact_id AND x.event_at = r.event_at"
    " WHERE r.project_id = %s AND r.scope = %s AND x.checksum = %s AND {archived}(r.project_id, r.artifact_id))"
).format(registration=ARCHIVE_REGISTRATION, artifact=qualified("artifact"), archived=ARTIFACT_IS_ARCHIVED)
_DELETED_IDS = sql.SQL(
    "SELECT r.artifact_id FROM {registration} r WHERE r.project_id = %s AND r.scope = %s"
    " AND NOT {archived}(r.project_id, r.artifact_id)"
).format(registration=ARCHIVE_REGISTRATION, archived=ARTIFACT_IS_ARCHIVED)
_SELECT_SCOPES = sql.SQL("SELECT scope FROM {} WHERE project_id = %s ORDER BY scope").format(qualified("scope"))
_SELECT_DATASET_BY_ID = sql.SQL(
    "SELECT dataset_id, project_id, scope, namespace, name, event_at, recorded_at FROM {}"
    " WHERE project_id = %s AND dataset_id = %s"
).format(qualified("dataset"))
# An intent with no outcome row: one deletion started and not settled.
_OPEN_INTENT = sql.SQL(
    "SELECT {columns} FROM {deletion} i WHERE i.project_id = %s AND i.scope = %s AND i.checksum = %s"
    " AND i.stage = 'intent' AND NOT EXISTS (SELECT FROM {deletion} o"
    " WHERE o.project_id = i.project_id AND o.intent_id = i.deletion_id) ORDER BY i.recorded_at, i.deletion_id LIMIT 1"
).format(columns=_DELETION_COLUMNS, deletion=_DELETION)
# The open intents of deletions of one cause, in one scope or (a NULL scope) in every scope.
_OPEN_INTENTS = sql.SQL(
    "SELECT {columns} FROM {deletion} i WHERE i.project_id = %(project_id)s"
    " AND (%(scope)s::text IS NULL OR i.scope = %(scope)s) AND i.stage = 'intent' AND i.cause = %(cause)s"
    " AND NOT EXISTS (SELECT FROM {deletion} o WHERE o.project_id = i.project_id AND o.intent_id = i.deletion_id)"
    " ORDER BY i.scope, i.checksum, i.recorded_at, i.deletion_id"
).format(columns=_DELETION_COLUMNS, deletion=_DELETION)
_ALL_DELETIONS = sql.SQL(
    "SELECT {columns} FROM {deletion} WHERE project_id = %s ORDER BY recorded_at, deletion_id"
).format(columns=_DELETION_COLUMNS, deletion=_DELETION)
_ALL_REDACTIONS = sql.SQL(
    "SELECT redaction_id, event_at, history, record_id, field, reason FROM {} WHERE project_id = %s"
    " ORDER BY redaction_id"
).format(qualified("redaction"))
# A history-retention action with the keep_days and event_at of the rule it applied.
_ALL_ACTIONS = sql.SQL(
    "SELECT a.action_id, a.event_at, a.history, a.rule_id, r.keep_days, r.event_at, a.cutoff, a.rows_removed"
    " FROM {action} a JOIN {rule} r ON r.project_id = a.project_id AND r.rule_id = a.rule_id"
    " WHERE a.project_id = %s ORDER BY a.action_id"
).format(action=qualified("history_retention_action"), rule=qualified("history_retention_rule"))


def check_scope(tx: Connection, project: Project, scope: str) -> None:
    """`UnknownRecordError` unless *scope* is one of *project*'s scopes: refused before any storage I/O."""
    if tx.execute(_SELECT_SCOPE, (project.project_id, scope)).fetchone() is None:
        raise UnknownRecordError(f"Scope {scope} is not onboarded for project {project.name}")


def address_lock_name(project_id: UUID, scope: str, checksum: bytes) -> str:
    """The name of the advisory lock that orders a put and a deletion of one object (project, scope, content)."""
    return f"ulduar.archive:{project_id}:{scope}:{checksum.hex()}"


def lock_address(tx: Connection, project_id: UUID, scope: str, checksum: bytes) -> None:
    """Hold the address lock of the object until the transaction ends."""
    advisory_xact_lock(tx, address_lock_name(project_id, scope, checksum))


def is_archived(tx: Connection, project_id: UUID, artifact_id: UUID) -> bool:
    """True while the artifact is registered and no confirmed deletion of its content was recorded after that."""
    row = tx.execute(_IS_ARCHIVED, (project_id, artifact_id)).fetchone()
    return row is not None and bool(row[0])


def insert_registration(
    tx: Connection, project_id: UUID, scope: str, artifact: Artifact, retention_class: str | None
) -> None:
    """Register *artifact*'s object; registering it again with the same retention class changes nothing.

    `UlduarError` when the artifact is already registered with another class (a class is fixed at archive time) or
    when it is registered but its object was deleted after that (a retry of a put whose record id came too late).
    """
    params = (project_id, scope, artifact.artifact_id, artifact.event_at, retention_class)
    if tx.execute(_INSERT_REGISTRATION, params).fetchone() is not None:
        return
    row = tx.execute(_SELECT_CLASS, (project_id, artifact.artifact_id)).fetchone()
    if row is None or row[0] != retention_class:
        old = None if row is None else row[0]
        raise UlduarError(f"Artifact {artifact.artifact_id} is already archived with retention class {old!r}")
    if not is_archived(tx, project_id, artifact.artifact_id):
        raise _deleted_error(artifact.artifact_id)


def _deleted_error(artifact_id: UUID) -> UlduarError:
    return UlduarError(f"Artifact {artifact_id} was deleted from the archive; put it again under a new record id")


def check_not_deleted(tx: Connection, project_id: UUID, artifact_id: UUID) -> None:
    """`UlduarError` when the artifact is registered but its object was deleted since: a retried put came too late.

    Nothing is raised for an artifact with no registration (a put that is new, or interrupted before it registered).
    """
    registered = tx.execute(_SELECT_CLASS, (project_id, artifact_id)).fetchone() is not None
    if registered and not is_archived(tx, project_id, artifact_id):
        raise _deleted_error(artifact_id)


def live_addresses(tx: Connection, project_id: UUID, scope: str) -> list[tuple[bytes, int]]:
    """The distinct (checksum, size) of the scope's registrations whose artifact is archived, by checksum."""
    rows = tx.execute(_SELECT_LIVE, {"project_id": project_id, "scope": scope}).fetchall()
    return [(row[0], row[1]) for row in rows]


def sample_addresses(tx: Connection, project_id: UUID, scope: str, n: int) -> list[tuple[bytes, int]]:
    """The *n* live addresses whose latest verification finding (any outcome) is oldest, never-verified first."""
    rows = tx.execute(_SELECT_SAMPLE, {"project_id": project_id, "scope": scope, "n": n}).fetchall()
    return [(row[0], row[1]) for row in rows]


def insert_check(
    tx: Connection,
    project_id: UUID,
    *,
    check_id: UUID,
    scope: str,
    kind: str,
    mode: str,
    objects_checked: int,
    findings: Sequence[Finding],
) -> None:
    """Record one `archive_check` of *kind* (`verification` or `reconciliation`) and a row per finding.

    The check's `event_at` is the time inside *check_id*; its `findings` count is those not `passed`.
    """
    failed = sum(finding.outcome != PASSED for finding in findings)
    params = (project_id, check_id, _ids.uuid7_time(check_id), scope, kind, mode, objects_checked, failed)
    tx.execute(_INSERT_CHECK, params)
    with tx.cursor() as cursor:
        cursor.executemany(
            _INSERT_FINDING,
            [
                (
                    project_id,
                    f.finding_id,
                    _ids.uuid7_time(f.finding_id),
                    check_id,
                    f.scope,
                    f.checksum,
                    f.size,
                    f.outcome,
                )
                for f in findings
            ],
        )


def retention_lock_name(project_id: UUID, retention_class: str) -> str:
    """The name of the advisory lock that orders the declarations of rules of one project and retention class."""
    return f"ulduar.archive-retention:{project_id}:{retention_class}"


def lock_retention(tx: Connection, project_id: UUID, retention_class: str) -> None:
    """Hold the retention lock of the class until the transaction ends."""
    advisory_xact_lock(tx, retention_lock_name(project_id, retention_class))


def latest_rules(tx: Connection, project_id: UUID) -> dict[str, LatestRule]:
    """The latest rule of each retention class of the project, by `recorded_at DESC, rule_id DESC`."""
    return {row[0]: LatestRule(*row[1:]) for row in tx.execute(_LATEST_RULES, (project_id,)).fetchall()}


def insert_rule(
    tx: Connection,
    project_id: UUID,
    rule_id: UUID,
    retention_class: str,
    keep_days: int | None,
    keep_newest: int,
    shortening_confirmed: bool,
) -> None:
    """Append a retention rule (ledger role only: the recording role can read rules, not write them)."""
    params = (
        project_id,
        rule_id,
        _ids.uuid7_time(rule_id),
        retention_class,
        keep_days,
        keep_newest,
        shortening_confirmed,
    )
    tx.execute(_INSERT_RULE, params)


def live_registrations(tx: Connection, project_id: UUID, scope: str | None) -> list[LiveRegistration]:
    """Every registration of the scope (None: of every scope) whose artifact is archived, by scope and checksum."""
    rows = tx.execute(_LIVE_REGISTRATIONS, {"project_id": project_id, "scope": scope}).fetchall()
    return [LiveRegistration(*row) for row in rows]


def address_artifacts(tx: Connection, project_id: UUID, scope: str, checksum: bytes) -> list[UUID]:
    """The artifact ids of every registration of the object (*scope*, *checksum*), live or not."""
    return [row[0] for row in tx.execute(_ADDRESS_ARTIFACTS, (project_id, scope, checksum)).fetchall()]


def registered_after(tx: Connection, project_id: UUID, scope: str, checksum: bytes, since: datetime) -> bool:
    """True when a registration of the object was recorded (the database's clock) after *since*."""
    row = tx.execute(_REGISTERED_AFTER, (project_id, scope, checksum, since)).fetchone()
    return row is not None and bool(row[0])


def _insert_deletion(
    tx: Connection,
    project: Project,
    scope: str,
    checksum: bytes,
    size: int,
    stage: str,
    intent: DeletionRecord | None,
    cause: str,
    rule_id: UUID | None,
    reason: str | None,
) -> DeletionRecord:
    """Insert a deletion row (an intent when *intent* is None, else its outcome) and return it as recorded."""
    deletion_id = _ids.uuid7()
    event_at = _ids.uuid7_time(deletion_id)
    intent_id = None if intent is None else intent.deletion_id
    params = (
        project.project_id,
        deletion_id,
        event_at,
        scope,
        checksum,
        size,
        stage,
        intent_id,
        cause,
        rule_id,
        reason,
    )
    row = tx.execute(_INSERT_DELETION, params).fetchone()
    assert row is not None
    return DeletionRecord(
        deletion_id, event_at, row[0], project.name, scope, checksum, size, stage, intent_id, cause, rule_id, reason
    )


def insert_intent(
    tx: Connection,
    project: Project,
    scope: str,
    checksum: bytes,
    size: int,
    cause: str,
    rule_id: UUID | None,
    reason: str | None,
) -> DeletionRecord:
    """Record the intent to delete the object (*scope*, *checksum*)."""
    return _insert_deletion(tx, project, scope, checksum, size, "intent", None, cause, rule_id, reason)


def insert_outcome(tx: Connection, project: Project, intent: DeletionRecord, stage: str) -> DeletionRecord:
    """Record the outcome (`confirmed` or `cancelled`) of *intent*, with its object, cause, rule and reason."""
    return _insert_deletion(
        tx,
        project,
        intent.scope,
        intent.checksum,
        intent.size,
        stage,
        intent,
        intent.cause,
        intent.rule_id,
        intent.reason,
    )


def _deletion_record(project: Project, row: tuple[Any, ...]) -> DeletionRecord:
    """The record of a row selected with `_DELETION_COLUMNS`."""
    deletion_id, event_at, recorded_at, scope, checksum, size, stage, intent_id, cause, rule_id, reason = row
    return DeletionRecord(
        deletion_id,
        event_at,
        recorded_at,
        project.name,
        scope,
        checksum,
        size,
        stage,
        intent_id,
        cause,
        rule_id,
        reason,
    )


def find_open_intent(tx: Connection, project: Project, scope: str, checksum: bytes) -> DeletionRecord | None:
    """The oldest intent to delete the object that has no outcome yet, or None."""
    row = tx.execute(_OPEN_INTENT, (project.project_id, scope, checksum)).fetchone()
    return None if row is None else _deletion_record(project, row)


def open_intents(tx: Connection, project: Project, scope: str | None, cause: str) -> list[DeletionRecord]:
    """Every intent of a deletion of *cause* with no outcome yet, in *scope* (None: every scope)."""
    params = {"project_id": project.project_id, "scope": scope, "cause": cause}
    return [_deletion_record(project, row) for row in tx.execute(_OPEN_INTENTS, params).fetchall()]


def open_intents_outside(
    tx: Connection, project: Project, scope: str | None, cause: str, wanted: Collection[tuple[str, bytes]]
) -> list[DeletionRecord]:
    """The open intents of *cause* in *scope* (None: every scope) whose object is not among *wanted*, one per object.

    *wanted* are the (scope, checksum) addresses the caller deletes anyway; an intent left open for any other is
    one the caller must still settle. Of several intents of one object the oldest is the one returned.
    """
    found: dict[tuple[str, bytes], DeletionRecord] = {}
    for intent in open_intents(tx, project, scope, cause):
        address = (intent.scope, intent.checksum)
        if address not in wanted and address not in found:
            found[address] = intent
    return list(found.values())


def database_now(tx: Connection) -> datetime:
    """The database's clock, as of the start of the transaction: the time every grace period is measured against."""
    row = tx.execute("SELECT now()").fetchone()
    assert row is not None
    now: datetime = row[0]
    return now


def project_scopes(tx: Connection, project_id: UUID) -> list[str]:
    """The project's scopes, by name."""
    return [row[0] for row in tx.execute(_SELECT_SCOPES, (project_id,)).fetchall()]


def registered_artifact_ids(tx: Connection, project_id: UUID, scope: str) -> set[UUID]:
    """The ids of the artifacts that have a registration in the scope, live or not."""
    return {row[0] for row in tx.execute(_REGISTERED_IDS, (project_id, scope)).fetchall()}


def deleted_artifact_ids(tx: Connection, project_id: UUID, scope: str) -> set[UUID]:
    """The ids of the artifacts registered in the scope whose object a confirmed deletion removed since."""
    return {row[0] for row in tx.execute(_DELETED_IDS, (project_id, scope)).fetchall()}


def address_is_live(tx: Connection, project_id: UUID, scope: str, checksum: bytes) -> bool:
    """True when some registration of the object (*scope*, *checksum*) is archived (`artifact_is_archived`)."""
    row = tx.execute(_ADDRESS_LIVE, (project_id, scope, checksum)).fetchone()
    return row is not None and bool(row[0])


def find_dataset(tx: Connection, project_id: UUID, dataset_id: UUID) -> Dataset | None:
    """The dataset of the project with this id, or None; a manifest names its dataset by id only."""
    row = tx.execute(_SELECT_DATASET_BY_ID, (project_id, dataset_id)).fetchone()
    return None if row is None else Dataset(*row)


def deletion_records(tx: Connection, project: Project) -> list[DeletionRecord]:
    """Every deletion row of the project, in the order it was recorded."""
    return [_deletion_record(project, row) for row in tx.execute(_ALL_DELETIONS, (project.project_id,)).fetchall()]


def maintenance_records(tx: Connection, project: Project) -> list[MaintenanceRecord]:
    """Every deletion, redaction and history-retention action of the project, as the store holds them."""
    records: list[MaintenanceRecord] = [*deletion_records(tx, project)]
    for row in tx.execute(_ALL_REDACTIONS, (project.project_id,)).fetchall():
        records.append(RedactionRecord(row[0], row[1], project.name, row[2], row[3], row[4], row[5]))
    for row in tx.execute(_ALL_ACTIONS, (project.project_id,)).fetchall():
        records.append(HistoryRetentionRecord(row[0], row[1], project.name, *row[2:]))
    return records


def restore_deletion(tx: Connection, project_id: UUID, record: DeletionRecord) -> None:
    """Insert a deletion row read from the store under its own id and times (`recorded_at` is the database's now)."""
    params = (
        project_id,
        record.deletion_id,
        record.event_at,
        record.scope,
        record.checksum,
        record.size,
        record.stage,
        record.intent_id,
        record.cause,
        record.rule_id,
        record.reason,
    )
    tx.execute(_INSERT_DELETION, params)
