"""Ledger-role calls on the Archivum's history: onboarding, retention rules and their application, redaction.

Input: a `Config` whose `archivum.ledger_dsn_env` names the ledger role's connection string
(`ConfigurationError` otherwise). Output: the frozen records `RetentionRule`, `RetentionResult`, `Redaction`
and the `Project` an onboarding returns. Each call runs in one transaction after the schema check.

This module is the only code path that issues `UPDATE` or `DELETE` against recorded history (hard rule 5):
a retention delete under `ulduar.maintenance` = `retention`, and a redaction under `redaction`, both
recorded by an appended record in the same transaction as the change. The triggers of step 1 refuse every
other change, including these from any other role or outside the maintenance setting.
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
from ulduar.config import DEFAULT_SCOPE, NAME_PATTERN, Config
from ulduar.errors import UlduarError, UnknownRecordError
from ulduar.freya import ledger
from ulduar.freya.catalog import Connection, Project

logger = logging.getLogger(__name__)

RetentionHistory = Literal["artifact", "lineage_edge"]
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


def _check_name(argument: str, value: str) -> None:
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise ValueError(f"The {argument} {value!r} must match {NAME_PATTERN.pattern}")


@contextmanager
def _ledger_transaction(config: Config, purpose: str, what: str) -> Iterator[Connection]:
    """One transaction as the ledger role, schema checked first; driver errors are raised as the package's."""
    settings = config.archivum
    with (
        ledger.open_ledger_connection(config, purpose) as conn,
        _db.translated(what),
        _db.transaction(conn, settings) as tx,
    ):
        ledger.check_schema(tx, ledger.load_steps())
        yield tx


def _scalar(cursor: Cursor[TupleRow]) -> Any:
    """The first column of the one row a statement just returned."""
    row = cursor.fetchone()
    assert row is not None
    return row[0]


def _project_id(tx: Connection, project: str) -> UUID:
    row = tx.execute("SELECT project_id FROM ulduar.project WHERE name = %s", (project,)).fetchone()
    if row is None:
        raise UnknownRecordError(f"Project {project} is not onboarded")
    project_id: UUID = row[0]
    return project_id


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
    _check_name("project", project)
    for scope in scopes:
        _check_name("scope", scope)
    if not isinstance(login_role, str) or not login_role:
        raise ValueError(f"The login role {login_role!r} must be a non-empty string")
    with _ledger_transaction(config, "onboarding", f"Onboarding project {project}") as tx:
        if tx.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (login_role,)).fetchone() is None:
            raise UnknownRecordError(f"Login role {login_role} does not exist")
        project_id = _ids.uuid7()
        row = tx.execute(
            "INSERT INTO ulduar.project (project_id, name) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING"
            " RETURNING project_id",
            (project_id, project),
        ).fetchone()
        project_id = row[0] if row is not None else _project_id(tx, project)
        for scope in scopes:
            tx.execute(
                "INSERT INTO ulduar.scope (project_id, scope) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (project_id, scope),
            )
        tx.execute(
            "INSERT INTO ulduar.project_member (role_name, project_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (login_role, project_id),
        )
        member_of = _scalar(
            tx.execute(
                "SELECT p.name FROM ulduar.project_member m JOIN ulduar.project p USING (project_id)"
                " WHERE m.role_name = %s",
                (login_role,),
            )
        )
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
    `apply_history_retention` runs.
    """
    if history not in HISTORIES:
        raise ValueError(f"History {history!r} is not one of {', '.join(HISTORIES)}")
    if keep_days is not None and (isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1):
        raise ValueError(f"keep_days {keep_days!r} must be a positive integer or None")
    rule_id = _ids.uuid7()
    event_at = _ids.uuid7_time(rule_id)
    what = f"Declaring {history} retention of project {project}"
    with _ledger_transaction(config, "declaring retention", what) as tx:
        tx.execute(
            "INSERT INTO ulduar.history_retention_rule (project_id, rule_id, event_at, history, keep_days)"
            " VALUES (%s, %s, %s, %s, %s)",
            (_project_id(tx, project), rule_id, event_at, history, keep_days),
        )
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


def _limited_rules(tx: Connection) -> list[_LimitedRule]:
    """The latest rule (by when it was recorded) of every project and history, without the keep-for-ever ones."""
    rows = tx.execute(
        "SELECT DISTINCT ON (r.project_id, r.history) r.project_id, p.name, r.history, r.rule_id, r.keep_days"
        " FROM ulduar.history_retention_rule r JOIN ulduar.project p USING (project_id)"
        " ORDER BY r.project_id, r.history, r.recorded_at DESC, r.event_at DESC, r.rule_id DESC"
    ).fetchall()
    return [_LimitedRule(*row) for row in rows if row[4] is not None]


def _table(history: str) -> sql.Identifier:
    """The table of a validated *history* name."""
    if history not in HISTORIES and history not in REDACTABLE_FIELDS:
        raise ValueError(f"History {history!r} is unknown")
    return sql.Identifier("ulduar", history)


def _count_older(tx: Connection, rule: _LimitedRule, cutoff: datetime) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE project_id = %s AND event_at < %s").format(_table(rule.history))
    count: int = _scalar(tx.execute(query, (rule.project_id, cutoff)))
    return count


def _delete_older(tx: Connection, rule: _LimitedRule, cutoff: datetime) -> int:
    """Delete the project rows of the rule history older than *cutoff* and append the action record, atomically."""
    _maintenance(tx, RETENTION_MODE)
    query = sql.SQL("DELETE FROM {} WHERE project_id = %s AND event_at < %s").format(_table(rule.history))
    removed = tx.execute(query, (rule.project_id, cutoff)).rowcount
    action_id = _ids.uuid7()
    tx.execute(
        "INSERT INTO ulduar.history_retention_action"
        " (project_id, action_id, event_at, rule_id, history, cutoff, rows_removed)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (rule.project_id, action_id, _ids.uuid7_time(action_id), rule.rule_id, rule.history, cutoff, removed),
    )
    return removed


def apply_history_retention(
    config: Config, *, dry_run: bool = True, now: datetime | None = None
) -> list[RetentionResult]:
    """Apply the latest retention rule of every project and history that keeps a limited time.

    The cutoff is *now* (default: the current time, UTC) minus `keep_days`; rows whose `event_at` is older
    go. By default nothing changes and `rows` counts what would go. Otherwise each project and history is
    one transaction that deletes the rows and appends a `history_retention_action`; afterwards the emptied
    monthly partitions that ended before the earliest cutoff are dropped. Without a rule, or with
    `keep_days` None, nothing happens.
    """
    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    settings = config.archivum
    with ledger.open_ledger_connection(config, "applying retention") as conn:
        with _db.translated("Reading the retention rules"), _db.transaction(conn, settings) as tx:
            ledger.check_schema(tx, ledger.load_steps())
            rules = _limited_rules(tx)
            if dry_run:
                return [
                    RetentionResult(
                        r.project, r.history, r.rule_id, r.cutoff(now), _count_older(tx, r, r.cutoff(now)), True
                    )
                    for r in rules
                ]
        if not rules:
            logger.info("no retention rule keeps a history for a limited time — nothing to apply")
            return []
        results = []
        for rule in rules:
            cutoff = rule.cutoff(now)
            with _db.translated(f"Applying {rule.history} retention of project {rule.project}"):
                with _db.transaction(conn, settings) as tx:
                    removed = _delete_older(tx, rule, cutoff)
            results.append(RetentionResult(rule.project, rule.history, rule.rule_id, cutoff, removed, False))
            logger.info("removed %d %s rows older than %s from project %s", removed, rule.history, cutoff, rule.project)
        with _db.translated("Dropping the emptied partitions"), _db.transaction(conn, settings) as tx:
            tx.execute("SELECT ulduar.drop_empty_partitions(%s)", (min(r.cutoff for r in results),))
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
            "INSERT INTO ulduar.redaction (project_id, redaction_id, event_at, history, record_id, field, reason)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (project_id, redaction_id, _ids.uuid7_time(redaction_id), history, record_id, field, reason),
        )
    logger.info("redacted %s.%s of project %s", history, field, project)
    return Redaction(redaction_id, project, history, record_id, field, reason, _ids.uuid7_time(redaction_id))
