"""Retention of archived objects: declare how long a class is kept, list what is due, delete it with a record.

Input: a `Config` with an `[archive]` section; the ledger role declares a rule (`declare_retention`), the recording
role and the deleter credential apply them (`apply_retention`). Output: the `ArchiveRetentionRule` a declaration
appends and the `RetentionRun` of an application. Sits above Freya's two hooks (`ledger_transaction`,
`Catalog.transaction`) and the deletion protocol (`_deletion`); the facade `ulduar.hodir` re-exports it.

A put names a retention class, fixed at archive time (a registration without one is kept for ever); the latest rule
of a class says how many days its registrations are kept and how many of a dataset's newest are never deleted by
age alone. An object is due only when every registration of it has expired under its own class (H13): the longest
retention wins, and a class without a rule keeps for ever. Applying retention is a dry run unless asked, refuses to
go past `archive.max_deletions_per_run` without being told, and deletes through the one protocol every deletion shares.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import UUID

from ulduar import _ids
from ulduar.config import Config, check_name
from ulduar.errors import ConfigurationError, UlduarError
from ulduar.freya import Catalog, Connection, Project, ledger_transaction, open_catalog
from ulduar.freya.history import lookup_project_id
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._deletion import delete_address
from ulduar.hodir._records import (
    ArchiveRetentionRule,
    DeletionOutcome,
    DueAddress,
    LatestRule,
    LiveRegistration,
    OpenIntent,
    RetentionRun,
    Stage,
)
from ulduar.hodir._store import DeletingStore, open_store
from ulduar.hodir.archive import _ensure_format

logger = logging.getLogger(__name__)

_SHORT_CHECKSUM_HEX = 12
_STAGES = get_args(Stage)


def _check_int(argument: str, value: int | None, minimum: int, *, allow_none: bool = False) -> None:
    """`ValueError` unless *value* is an integer (a bool is not one) of at least *minimum*, or None when allowed."""
    if value is None and allow_none:
        return
    if value is None or isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        wanted = "a positive integer or None" if allow_none else f"an integer of at least {minimum}"
        raise ValueError(f"{argument} {value!r} must be {wanted}")


def _shortens(current: LatestRule | None, keep_days: int | None, keep_newest: int) -> bool:
    """True when a rule of *keep_days* and *keep_newest* keeps objects for less time than *current* (None: no rule)."""
    if current is None:
        return keep_days is not None
    if keep_newest < current.keep_newest:
        return True
    if keep_days is None:
        return False
    return current.keep_days is None or keep_days < current.keep_days


def declare_retention(
    config: Config,
    project: str,
    retention_class: str,
    keep_days: int | None,
    *,
    keep_newest: int = 1,
    confirm_shortening: bool = False,
) -> ArchiveRetentionRule:
    """Append a rule: *retention_class* of *project* is kept *keep_days* days (None: for ever), as the ledger role.

    The latest rule of a class wins. *keep_newest* of a dataset's newest registrations (default 1) are never
    deleted by age alone. A rule that keeps objects for less time than the class does now (no rule is for ever; a
    shorter `keep_days`, a smaller `keep_newest`) is `UlduarError` unless *confirm_shortening* is True, which the
    rule records. Nothing is deleted until `apply_retention` runs. `ValueError` for a bad argument, before any SQL;
    `UnknownRecordError` for a project that is not onboarded; `ConfigurationError` without `archivum.ledger_dsn_env`.
    """
    check_name(project, "project")
    check_name(retention_class, "retention class")
    _check_int("keep_days", keep_days, 1, allow_none=True)
    _check_int("keep_newest", keep_newest, 0)
    rule_id = _ids.uuid7()
    what = f"Declaring retention class {retention_class} of project {project}"
    with ledger_transaction(config, what, purpose="declaring archive retention") as tx:
        project_id = lookup_project_id(tx, project)
        _archivum.lock_retention(tx, project_id, retention_class)
        current = _archivum.latest_rules(tx, project_id).get(retention_class)
        shortens = _shortens(current, keep_days, keep_newest)
        if shortens and not confirm_shortening:
            raise UlduarError(
                f"Retention class {retention_class} of project {project} would keep objects for less time than it"
                " does now; declare it with confirm_shortening=True"
            )
        _archivum.insert_rule(tx, project_id, rule_id, retention_class, keep_days, keep_newest, shortens)
    logger.info(
        "declared retention class %s of project %s: keep_days %s, keep_newest %d%s",
        retention_class,
        project,
        keep_days,
        keep_newest,
        ", shortening confirmed" if shortens else "",
    )
    return ArchiveRetentionRule(
        rule_id, project, retention_class, keep_days, keep_newest, shortens, _ids.uuid7_time(rule_id)
    )


def _dataset_ranks(registrations: list[LiveRegistration]) -> dict[UUID, int]:
    """Each registration's rank among the live registrations of its dataset, newest first (0 is the newest)."""
    by_dataset: dict[UUID, list[LiveRegistration]] = {}
    for registration in registrations:
        if registration.dataset_id is not None:
            by_dataset.setdefault(registration.dataset_id, []).append(registration)
    ranks: dict[UUID, int] = {}
    for members in by_dataset.values():
        members.sort(key=lambda r: (r.artifact_event_at, r.artifact_id), reverse=True)
        ranks.update({member.artifact_id: rank for rank, member in enumerate(members)})
    return ranks


def _expiry(
    registration: LiveRegistration, rules: Mapping[str, LatestRule], ranks: Mapping[UUID, int], now: datetime
) -> tuple[datetime, UUID] | None:
    """(when the registration expired, the rule that expired it), or None while it is kept."""
    rule = None if registration.retention_class is None else rules.get(registration.retention_class)
    if rule is None or rule.keep_days is None:
        return None
    if registration.dataset_id is not None and ranks[registration.artifact_id] < rule.keep_newest:
        return None
    expires = registration.artifact_event_at + timedelta(days=rule.keep_days)
    return (expires, rule.rule_id) if expires < now else None


def due_addresses(
    registrations: Iterable[LiveRegistration], rules: Mapping[str, LatestRule], now: datetime
) -> list[DueAddress]:
    """The objects every live registration of which has expired at *now*, by scope and checksum.

    A registration expires when its class has a rule with `keep_days` and its artifact is older than that, unless
    it is among the `keep_newest` newest live registrations of its dataset (of any class; one without a dataset is
    not protected). An object is due only when all its registrations expired, so the longest retention wins; its
    `rule_id` is that of the registration that expired last.
    """
    live = list(registrations)
    ranks = _dataset_ranks(live)
    by_address: dict[tuple[str, bytes], list[LiveRegistration]] = {}
    for registration in live:
        by_address.setdefault((registration.scope, registration.checksum), []).append(registration)
    due = []
    for scope, checksum in sorted(by_address):
        group = by_address[scope, checksum]
        expiries = [_expiry(registration, rules, ranks, now) for registration in group]
        if None in expiries:
            continue
        binding = max(expiry for expiry in expiries if expiry is not None)
        artifact_ids = tuple(sorted(registration.artifact_id for registration in group))
        due.append(DueAddress(scope, checksum, group[0].size, binding[1], artifact_ids))
    return due


def _read_due(tx: Connection, project_id: UUID, scope: str | None, now: datetime) -> list[DueAddress]:
    """What is due now in *scope* (None: every scope), from the registrations and rules as the transaction sees them."""
    return due_addresses(
        _archivum.live_registrations(tx, project_id, scope), _archivum.latest_rules(tx, project_id), now
    )


def _is_due(tx: Connection, project_id: UUID, scope: str, checksum: bytes, now: datetime) -> bool:
    return any((due.scope, due.checksum) == (scope, checksum) for due in _read_due(tx, project_id, scope, now))


def _read_open_intents(tx: Connection, project: Project, scope: str | None, due: list[DueAddress]) -> list[OpenIntent]:
    """The open retention intents in *scope* whose objects are not in *due*, one per object."""
    wanted = {(address.scope, address.checksum) for address in due}
    found: dict[tuple[str, bytes], OpenIntent] = {}
    for intent in _archivum.open_retention_intents(tx, project, scope):
        key = (intent.scope, intent.checksum)
        if key not in wanted and key not in found:
            assert intent.rule_id is not None  # a retention intent names its rule (the table's CHECK)
            found[key] = OpenIntent(intent.deletion_id, intent.scope, intent.checksum, intent.size, intent.rule_id)
    return list(found.values())


def apply_retention(
    config: Config,
    *,
    dry_run: bool = True,
    allow_over_limit: bool = False,
    scope: str | None = None,
    now: datetime | None = None,
) -> RetentionRun:
    """List (dry run, the default: the Archivum is read, no store is opened) or delete what retention makes due.

    The objects due at *now* (default the current time; it must be timezone-aware) in *scope* (default: every
    scope of the project) are those whose every registration expired (`due_addresses`). A real run needs the
    deleter credential and refuses, before deleting anything, to delete more objects than
    `archive.max_deletions_per_run` unless *allow_over_limit*. Each object goes through the deletion protocol with
    cause `retention`. A real run also passes every open retention intent of those scopes whose object is not due
    (a failed run left it open and a rule was lengthened since) through the protocol, which cancels it; they are
    listed in `open_intents` (a dry run lists them too) and do not count towards the limit. An object that is no
    longer due when its turn comes, because a put or a longer rule arrived meanwhile, is skipped, and one the store
    refuses to delete stays `open`: the run goes on and the next run resumes it. `ConfigurationError` without
    `[archive]` (or, for a real run, without the deleter);
    `UnknownRecordError` for a *scope* the project lacks; `ValueError` for a bad *now* or *scope*.
    """
    settings = config.archive
    if settings is None:
        raise ConfigurationError("Configuration key `archive` is required to use the archive")
    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    if scope is not None:
        check_name(scope, "scope")
    store = None if dry_run else open_store(settings, deleting=True)
    if store is not None:
        _ensure_format(store)
    with open_catalog(config) as catalog:
        project = catalog.project

        def read(tx: Connection) -> tuple[list[DueAddress], list[OpenIntent]]:
            if scope is not None:
                _archivum.check_scope(tx, project, scope)
            found = _read_due(tx, project.project_id, scope, now)
            return found, _read_open_intents(tx, project, scope, found)

        listed, resumable = catalog.transaction("Reading what archive retention makes due", read)
        due, open_intents = tuple(listed), tuple(resumable)
        if store is None:
            logger.info("retention dry run: %d objects are due, %d open intents are not", len(due), len(open_intents))
            return RetentionRun(True, due, (), open_intents)
        if len(due) > settings.max_deletions_per_run and not allow_over_limit:
            raise UlduarError(
                f"Retention would delete {len(due)} objects, more than archive.max_deletions_per_run"
                f" ({settings.max_deletions_per_run}); pass allow_over_limit=True to go ahead"
            )
        outcomes = []
        targets: list[DueAddress | OpenIntent] = [*open_intents, *due]
        for target in targets:
            outcomes.append(_delete_one(catalog, store, project, target, now))
            _log_outcome(outcomes[-1])
    result = RetentionRun(False, due, tuple(outcomes), open_intents)
    logger.info(
        "retention applied: %d due, %d open intents resumed, %s",
        len(due),
        len(open_intents),
        ", ".join(f"{sum(o.stage == stage for o in outcomes)} {stage}" for stage in _STAGES),
    )
    return result


def _delete_one(
    catalog: Catalog, store: DeletingStore, project: Project, target: DueAddress | OpenIntent, now: datetime
) -> DeletionOutcome:
    """Delete one due object, or settle one open intent; transaction 2 re-checks that the object is due."""
    scope, checksum = target.scope, target.checksum
    return delete_address(
        catalog,
        store,
        project,
        scope,
        checksum,
        target.size,
        cause="retention",
        rule_id=target.rule_id,
        still_due=lambda tx: _is_due(tx, project.project_id, scope, checksum, now),
        manifests=lambda tx: _manifest_keys(tx, project, scope, checksum),
    )


def _manifest_keys(tx: Connection, project: Project, scope: str, checksum: bytes) -> list[str]:
    """The keys of the manifests of every registration of the object."""
    return [
        _layout.manifest_key(project.name, scope, artifact_id)
        for artifact_id in _archivum.address_artifacts(tx, project.project_id, scope, checksum)
    ]


def _log_outcome(outcome: DeletionOutcome) -> None:
    logger.info(
        "retention: object %s in scope %s is %s",
        outcome.checksum.hex()[:_SHORT_CHECKSUM_HEX],
        outcome.scope,
        outcome.stage,
    )
