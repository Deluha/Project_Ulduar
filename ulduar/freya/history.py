"""Ledger-role calls on the Archivum's history: onboarding, retention rules and their application, redaction.

Input: a `Config` whose `archivum.ledger_dsn_env` names the ledger role's connection string
(`ConfigurationError` otherwise). Output: the frozen records `RetentionRule`, `RetentionResult`, `Redaction`
and the `Project` an onboarding returns. Each call runs in one transaction after the schema check.

This module is the only code path that issues `UPDATE` or `DELETE` against recorded history (hard rule 5):
a retention delete under `ulduar.maintenance` = `retention`, and a redaction under `redaction`, both
recorded by an appended record in the same transaction as the change. An artifact whose object is still
archived (`ulduar.artifact_is_archived`) never ages out, and the registrations of the artifacts that do go with
them. The triggers of step 1 and step 2 refuse every other change, including these from any other role, outside
the maintenance setting, or in another mode's operation (a mode passes only its own operation:
`DELETE:retention`, `UPDATE:redaction`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args
from uuid import UUID

from psycopg import Cursor, sql
from psycopg.rows import TupleRow

from ulduar import _db, _ids
from ulduar.config import DEFAULT_SCOPE, ArchivumSettings, Config, check_name
from ulduar.errors import UlduarError, UnknownRecordError
from ulduar.freya import ledger
from ulduar.freya._records import Connection, Project, qualified

logger = logging.getLogger(__name__)

RetentionHistory = Literal["artifact", "lineage_edge", "archive_check", "archive_finding"]
RedactionHistory = Literal["artifact", "dataset"]
RedactionReason = Literal["personal_data", "secret", "recorded_by_mistake"]
# The histories that age out, the (history, field) pairs that may be redacted, and why: the schema's
# CHECK constraints spell the same sets.
HISTORIES: tuple[str, ...] = get_args(RetentionHistory)
REDACTABLE_FIELDS: dict[str, tuple[str, ...]] = {"artifact": ("origin",), "dataset": ("name", "namespace")}
REDACTION_REASONS: tuple[str, ...] = get_args(RedactionReason)
# The transaction-scoped setting the ledger role sets so the append-only triggers let a change through.
MAINTENANCE_SETTING = "ulduar.maintenance"
RETENTION_MODE = "retention"
REDACTION_MODE = "redaction"
# The group role step 1 creates; a project's login role joins it when onboarded.
RECORDER_GROUP = "ulduar_recorder"
_ID_COLUMNS = {"artifact": "artifact_id", "dataset": "dataset_id"}
_REDACTED_PREFIX = "redacted:"

_PROJECT = qualified("project")
_SCOPE = qualified("scope")
_MEMBER = qualified("project_member")
_RULE = qualified("history_retention_rule")
_ACTION = qualified("history_retention_action")
_REDACTION = qualified("redaction")
_REGISTRATION = qualified("archive_registration")
_IS_ARCHIVED = qualified("artifact_is_archived")
_SELECT_PROJECT_ID = sql.SQL("SELECT project_id FROM {} WHERE name = %s").format(_PROJECT)
_INSERT_PROJECT = sql.SQL(
    "INSERT INTO {} (project_id, name) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING RETURNING project_id"
).format(_PROJECT)
_INSERT_SCOPE = sql.SQL("INSERT INTO {} (project_id, scope) VALUES (%s, %s) ON CONFLICT DO NOTHING").format(_SCOPE)
_INSERT_MEMBER = sql.SQL("INSERT INTO {} (role_name, project_id) VALUES (%s, %s) ON CONFLICT DO NOTHING").format(
    _MEMBER
)
_SELECT_MEMBER_PROJECT = sql.SQL(
    "SELECT p.name FROM {member} m JOIN {project} p USING (project_id) WHERE m.role_name = %s"
).format(member=_MEMBER, project=_PROJECT)
_INSERT_RULE = sql.SQL(
    "INSERT INTO {} (project_id, rule_id, event_at, history, keep_days) VALUES (%s, %s, %s, %s, %s)"
).format(_RULE)
# The latest rule (by when it was recorded) of each project and history; a NULL argument stands for every one.
_LATEST_RULES = sql.SQL(
    "SELECT DISTINCT ON (r.project_id, r.history) r.project_id, p.name, r.history, r.rule_id, r.keep_days"
    " FROM {rule} r JOIN {project} p USING (project_id)"
    " WHERE r.project_id = coalesce(%s::uuid, r.project_id) AND r.history = coalesce(%s::text, r.history)"
    " ORDER BY r.project_id, r.history, r.recorded_at DESC, r.event_at DESC, r.rule_id DESC"
).format(rule=_RULE, project=_PROJECT)
_INSERT_ACTION = sql.SQL(
    "INSERT INTO {} (project_id, action_id, event_at, rule_id, history, cutoff, rows_removed)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
).format(_ACTION)
_INSERT_REDACTION = sql.SQL(
    "INSERT INTO {} (project_id, redaction_id, event_at, history, record_id, field, reason)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
).format(_REDACTION)


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """A declared retention rule: *history* of *project* is kept *keep_days* days (None: for ever)."""

    rule_id: UUID
    project: str
    history: RetentionHistory
    keep_days: int | None
    event_at: datetime


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """What applying the latest rule of one project and history did, or (dry run) would do."""

    project: str
    history: RetentionHistory
    rule_id: UUID
    cutoff: datetime
    rows: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class Redaction:
    """A recorded redaction of one field of one record."""

    redaction_id: UUID
    project: str
    history: RedactionHistory
    record_id: UUID
    field: str
    reason: RedactionReason
    event_at: datetime


@contextmanager
def _ledger_transaction(config: Config, purpose: str, what: str) -> Iterator[Connection]:
    """One transaction as the ledger role, schema checked first; driver errors are raised as the package's."""
    settings = config.archivum
    with (
        ledger.open_ledger_connection(config, purpose) as conn,
        _db.translated_transaction(conn, settings, what) as tx,
    ):
        ledger.check_schema(tx, ledger.load_steps())
        yield tx


def _scalar(cursor: Cursor[TupleRow]) -> Any:
    """The first column of the one row a statement just returned."""
    row = cursor.fetchone()
    assert row is not None
    return row[0]


def _project_id(tx: Connection, project: str) -> UUID:
    row = tx.execute(_SELECT_PROJECT_ID, (project,)).fetchone()
    if row is None:
        raise UnknownRecordError(f"Project {project} is not onboarded")
    project_id: UUID = row[0]
    return project_id


def retention_lock_name(project_id: UUID, history: str) -> str:
    """The name of the advisory lock that keeps a rule and its application for one project and history apart."""
    return f"ulduar.retention:{project_id}:{history}"


def _lock_retention(tx: Connection, project_id: UUID, history: str) -> None:
    """Hold the retention lock of *project_id* and *history* until the transaction ends."""
    tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (retention_lock_name(project_id, history),))


def _maintenance(tx: Connection, mode: str) -> None:
    """Allow the append-only triggers to pass a change of *mode* for the rest of this transaction only."""
    tx.execute("SELECT set_config(%s, %s, true)", (MAINTENANCE_SETTING, mode))


def onboard_project(
    config: Config, project: str, login_role: str, *, scopes: Sequence[str] = (DEFAULT_SCOPE,)
) -> Project:
    """Onboard *project* for the database login role *login_role*, with *scopes*; repeating it changes nothing.

    Creates the project (a new UUIDv7 id) and its scopes when absent, makes the role a member of the project
    and grants it the recording group. One login role belongs to one project: a role already in another
    project raises `UlduarError`; a role that does not exist, `UnknownRecordError`.
    """
    if isinstance(scopes, str):
        raise ValueError(f"The scopes {scopes!r} must be a sequence of scope names, not a single string")
    check_name(project, "project")
    for scope in scopes:
        check_name(scope, "scope")
    if not isinstance(login_role, str) or not login_role:
        raise ValueError(f"The login role {login_role!r} must be a non-empty string")
    with _ledger_transaction(config, "onboarding", f"Onboarding project {project}") as tx:
        if tx.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (login_role,)).fetchone() is None:
            raise UnknownRecordError(f"Login role {login_role} does not exist")
        project_id = _ids.uuid7()
        row = tx.execute(_INSERT_PROJECT, (project_id, project)).fetchone()
        project_id = row[0] if row is not None else _project_id(tx, project)
        for scope in scopes:
            tx.execute(_INSERT_SCOPE, (project_id, scope))
        tx.execute(_INSERT_MEMBER, (login_role, project_id))
        member_of = _scalar(tx.execute(_SELECT_MEMBER_PROJECT, (login_role,)))
        if member_of != project:
            raise UlduarError(f"Login role {login_role} already belongs to project {member_of}")
        tx.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(RECORDER_GROUP), sql.Identifier(login_role)))
    logger.info("onboarded login role to project %s", project)
    return Project(project_id, project)


def declare_history_retention(
    config: Config, project: str, history: RetentionHistory, keep_days: int | None
) -> RetentionRule:
    """Append a retention rule: *history* of *project* is kept *keep_days* days; None keeps it for ever.

    The latest rule recorded for a project and history wins. Nothing is removed until
    `apply_history_retention` runs; it and this call take the same lock per project and history, so an
    application that is running finishes first or sees the new rule before it deletes.
    """
    if history not in HISTORIES:
        raise ValueError(f"History {history!r} is not one of {', '.join(HISTORIES)}")
    if keep_days is not None and (isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1):
        raise ValueError(f"keep_days {keep_days!r} must be a positive integer or None")
    rule_id = _ids.uuid7()
    event_at = _ids.uuid7_time(rule_id)
    what = f"Declaring {history} retention of project {project}"
    with _ledger_transaction(config, "declaring retention", what) as tx:
        project_id = _project_id(tx, project)
        _lock_retention(tx, project_id, history)
        tx.execute(_INSERT_RULE, (project_id, rule_id, event_at, history, keep_days))
    return RetentionRule(rule_id, project, history, keep_days, event_at)


@dataclass(frozen=True, slots=True)
class _LimitedRule:
    """The latest rule of a project and history, when it keeps the history a limited time."""

    project_id: UUID
    project: str
    history: RetentionHistory
    rule_id: UUID
    keep_days: int

    def cutoff(self, now: datetime) -> datetime:
        return now - timedelta(days=self.keep_days)


def _limited_rules(tx: Connection, project_id: UUID | None = None, history: str | None = None) -> list[_LimitedRule]:
    """The latest rule of every project and history (or of the one given), without the keep-for-ever ones."""
    rows = tx.execute(_LATEST_RULES, (project_id, history)).fetchall()
    return [_LimitedRule(*row) for row in rows if row[4] is not None]


def _table(history: str) -> sql.Identifier:
    """The table of a validated *history* name."""
    if history not in HISTORIES and history not in REDACTABLE_FIELDS:
        raise ValueError(f"History {history!r} is unknown")
    return qualified(history)


def _count_older(tx: Connection, rule: _LimitedRule, cutoff: datetime) -> int:
    """The rows `_delete_older` would remove: an artifact whose object is still archived is not counted (H25)."""
    query = sql.SQL("SELECT count(*) FROM {} WHERE project_id = %s AND event_at < %s").format(_table(rule.history))
    if rule.history == "artifact":
        query = sql.SQL("{} AND NOT {}(project_id, artifact_id)").format(query, _IS_ARCHIVED)
    count: int = _scalar(tx.execute(query, (rule.project_id, cutoff)))
    return count


# An artifact whose object is still archived stays, and the registrations of the artifacts that go are removed in
# the same statement, so a registration never outlives its artifact.
_DELETE_ARTIFACTS = sql.SQL(
    "WITH gone AS ("
    "DELETE FROM {artifact} WHERE project_id = %s AND event_at < %s AND NOT {archived}(project_id, artifact_id)"
    " RETURNING project_id, artifact_id), "
    "registrations AS (DELETE FROM {registration} r USING gone"
    " WHERE r.project_id = gone.project_id AND r.artifact_id = gone.artifact_id) "
    "SELECT count(*) FROM gone"
).format(artifact=qualified("artifact"), archived=_IS_ARCHIVED, registration=_REGISTRATION)


def _delete_older(tx: Connection, rule: _LimitedRule, cutoff: datetime) -> int:
    """Delete the project rows of the rule history older than *cutoff* and append the action record, atomically."""
    _maintenance(tx, RETENTION_MODE)
    if rule.history == "artifact":
        removed: int = _scalar(tx.execute(_DELETE_ARTIFACTS, (rule.project_id, cutoff)))
    else:
        query = sql.SQL("DELETE FROM {} WHERE project_id = %s AND event_at < %s").format(_table(rule.history))
        removed = tx.execute(query, (rule.project_id, cutoff)).rowcount
    action_id = _ids.uuid7()
    params = (rule.project_id, action_id, _ids.uuid7_time(action_id), rule.rule_id, rule.history, cutoff, removed)
    tx.execute(_INSERT_ACTION, params)
    return removed


def _count_rule(conn: Connection, settings: ArchivumSettings, rule: _LimitedRule, now: datetime) -> RetentionResult:
    """What applying *rule* would remove, counted in a transaction of its own.

    One count per transaction keeps the lock order of every other path: a transaction that counted the
    artifacts and then the edges would hold `artifact` while it waits for `lineage_edge`, the reverse of
    the order of the partition functions and of an edge recorder.
    """
    cutoff = rule.cutoff(now)
    with _db.translated_transaction(conn, settings, f"Counting {rule.history} of project {rule.project}") as tx:
        rows = _count_older(tx, rule, cutoff)
    return RetentionResult(rule.project, rule.history, rule.rule_id, cutoff, rows, True)


def _apply_rule(
    conn: Connection, settings: ArchivumSettings, rule: _LimitedRule, now: datetime
) -> RetentionResult | None:
    """Apply *rule* in one transaction, after re-reading the latest rule under the retention lock.

    A rule declared since *rule* was read that keeps for ever stops the application (None); one that keeps
    longer replaces *rule*; a shorter one never deletes more than *rule* showed. The result names what was applied.
    """
    what = f"Applying {rule.history} retention of project {rule.project}"
    with _db.translated_transaction(conn, settings, what) as tx:
        _lock_retention(tx, rule.project_id, rule.history)
        latest = _limited_rules(tx, rule.project_id, rule.history)
        if not latest:
            logger.info("%s retention of project %s now keeps for ever — nothing removed", rule.history, rule.project)
            return None
        if latest[0].keep_days > rule.keep_days:
            rule = latest[0]
        cutoff = rule.cutoff(now)
        removed = _delete_older(tx, rule, cutoff)
    logger.info("removed %d %s rows older than %s from project %s", removed, rule.history, cutoff, rule.project)
    return RetentionResult(rule.project, rule.history, rule.rule_id, cutoff, removed, False)


def apply_history_retention(
    config: Config, *, dry_run: bool = True, now: datetime | None = None
) -> list[RetentionResult]:
    """Apply the latest retention rule of every project and history that keeps a limited time.

    The cutoff is *now* (default: the current time, UTC) minus `keep_days`; rows whose `event_at` is older
    go. By default nothing changes and `rows` counts what would go. Otherwise each project and history is
    one transaction that, under the retention lock, re-reads the latest rule (one declared meanwhile that
    keeps for ever, or longer, wins), deletes the rows and appends a `history_retention_action`; the results
    list what was applied. Afterwards the emptied monthly partitions that ended before the earliest cutoff
    are dropped. Without a rule, or with `keep_days` None, nothing happens.
    """
    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    settings = config.archivum
    with ledger.open_ledger_connection(config, "applying retention") as conn:
        with _db.translated_transaction(conn, settings, "Reading the retention rules") as tx:
            ledger.check_schema(tx, ledger.load_steps())
            rules = _limited_rules(tx)
        if dry_run:
            return [_count_rule(conn, settings, rule, now) for rule in rules]
        results = [result for rule in rules if (result := _apply_rule(conn, settings, rule, now)) is not None]
        if not results:
            logger.info("no retention rule keeps a history for a limited time — nothing to apply")
            return []
        drop = sql.SQL("SELECT {}(%s)").format(qualified("drop_empty_partitions"))
        with _db.translated_transaction(conn, settings, "Dropping the emptied partitions") as tx:
            tx.execute(drop, (min(r.cutoff for r in results),))
    return results


def redact(
    config: Config,
    project: str,
    history: RedactionHistory,
    record_id: UUID,
    field: str,
    reason: RedactionReason,
) -> Redaction:
    """Redact one field of one record and record the redaction; the record itself stays.

    Allowed: `artifact.origin` (becomes NULL), `dataset.name` and `dataset.namespace` (become
    `redacted:<redaction id>`); anything else is a `ValueError`. `UnknownRecordError` when the project has no
    such record. The change and the appended `redaction` record commit together.
    """
    if field not in REDACTABLE_FIELDS.get(history, ()):
        raise ValueError(f"{history}.{field} cannot be redacted")
    if reason not in REDACTION_REASONS:
        raise ValueError(f"Reason {reason!r} is not one of {', '.join(REDACTION_REASONS)}")
    redaction_id = _ids.uuid7()
    value = None if history == "artifact" else f"{_REDACTED_PREFIX}{redaction_id}"
    with _ledger_transaction(config, "redaction", f"Redacting {history} {record_id}") as tx:
        project_id = _project_id(tx, project)
        _maintenance(tx, REDACTION_MODE)
        update = sql.SQL("UPDATE {table} SET {field} = %s WHERE project_id = %s AND {id} = %s").format(
            table=_table(history), field=sql.Identifier(field), id=sql.Identifier(_ID_COLUMNS[history])
        )
        if tx.execute(update, (value, project_id, record_id)).rowcount != 1:
            raise UnknownRecordError(f"The {history} {record_id} is not recorded in project {project}")
        tx.execute(
            _INSERT_REDACTION,
            (project_id, redaction_id, _ids.uuid7_time(redaction_id), history, record_id, field, reason),
        )
    logger.info("redacted %s.%s of project %s", history, field, project)
    return Redaction(redaction_id, project, history, record_id, field, reason, _ids.uuid7_time(redaction_id))
