"""Rebuild: restore what the archive store alone can give back to an Archivum that lost it (D32, H28).

Input: a catalog session on the recording role and a writing store. Output: `rebuild`, a `Rebuild`. Private:
`Archive.rebuild` calls it.

The order matters. Whether an object is still archived compares the `recorded_at` of its registration with that of a
confirmed deletion of its content, and the database stamps both with its own clock when the row is inserted. So the
deletion records of the store are restored first, and only then are the registrations of the manifests made again:
a registration made now is newer than every restored deletion, the same as a put made after a deletion. The
redactions and history-retention actions the store holds are only read: removing recorded history is the ledger
role's, and `freya.reapply_removals` applies them afterwards.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ulduar.errors import ArchiveFormatError, UlduarError, UnknownRecordError
from ulduar.freya import Artifact, Catalog, Connection, Redaction, RetentionAction
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._layout import DeletionRecord, HistoryRetentionRecord, MaintenanceRecord, RedactionRecord
from ulduar.hodir._records import Rebuild
from ulduar.hodir._registration import adopt_all, unregistered_manifests
from ulduar.hodir._store import WritingStore, read_small

logger = logging.getLogger(__name__)


def rebuild(catalog: Catalog, store: WritingStore, since: datetime | None) -> Rebuild:
    """Restore the deletion records the Archivum lacks, then adopt every manifest without a registration.

    The `_records` and `_manifests` prefixes are listed whole; only those of the months from *since* on are decoded
    (all of them for None). A manifest whose object is gone is left. Running it again changes nothing.
    `UnknownRecordError` for a deletion record of a scope the project lacks (onboard it first); `UlduarError` for an
    outcome whose intent neither the Archivum nor the records read hold (read from an earlier month);
    `ArchiveFormatError` for a record or manifest that cannot be read, before anything is changed by it.
    """
    records = _read_records(catalog, store, since)
    deletions = [record for record in records if isinstance(record, DeletionRecord)]
    restored = catalog.transaction("Restoring the deletion records", lambda tx: _restore(tx, catalog, deletions))
    adopted: list[Artifact] = []
    datasets_missing = 0
    scopes = catalog.transaction(
        "Reading the scopes of the project", lambda tx: _archivum.project_scopes(tx, catalog.project.project_id)
    )
    for scope in scopes:
        manifests = (listed.manifest for listed in unregistered_manifests(catalog, store, scope, since=since))
        found, missing = adopt_all(catalog, store, manifests)
        adopted += found
        datasets_missing += missing
    removals = tuple(_removal(record) for record in records if not isinstance(record, DeletionRecord))
    logger.info(
        "rebuilt: %d deletion records restored, %d artifacts adopted, %d without their dataset, %d removals found",
        restored,
        len(adopted),
        datasets_missing,
        len(removals),
    )
    return Rebuild(restored, tuple(adopted), datasets_missing, removals)


def _read_records(catalog: Catalog, store: WritingStore, since: datetime | None) -> list[MaintenanceRecord]:
    """Every record of the project under `_records` from the month of *since* on, decoded, by `event_at` and id."""
    project = catalog.project
    records: list[MaintenanceRecord] = []
    for stored in store.list(f"{project.name}/_records"):
        record_id = _layout.parse_record_key(stored.key.removeprefix(f"{project.name}/"))
        if record_id is None:
            logger.warning("skipped %s: not a record key", stored.key)
            continue
        if not _layout.is_since(record_id, since):
            continue
        try:
            record = _layout.decode_record(read_small(store, stored.key))
        except ArchiveFormatError as e:
            raise ArchiveFormatError(f"The record {stored.key} cannot be read: {e}") from None
        if record.project != project.name or record.identifier != record_id:
            raise ArchiveFormatError(f"The record {stored.key} names another project or id than its key")
        records.append(record)
    return sorted(records, key=lambda record: (record.event_at, record.identifier))


def _restore(tx: Connection, catalog: Catalog, deletions: list[DeletionRecord]) -> int:
    """Insert the deletion records the Archivum lacks, intents before their outcomes; how many were inserted."""
    project = catalog.project
    known = {record.deletion_id for record in _archivum.deletion_records(tx, project)}
    scopes = set(_archivum.project_scopes(tx, project.project_id))
    missing = [record for record in deletions if record.deletion_id not in known]
    for record in missing:
        if record.scope not in scopes:
            raise UnknownRecordError(f"Deletion record {record.deletion_id} names scope {record.scope}, not onboarded")
    # An intent is older than its outcome, but sort by stage as well so the foreign key never depends on the clock.
    missing.sort(key=lambda record: (record.stage != "intent", record.event_at, record.deletion_id))
    present = known | {record.deletion_id for record in missing if record.stage == "intent"}
    for record in missing:
        if record.intent_id is not None and record.intent_id not in present:
            raise UlduarError(
                f"Deletion record {record.deletion_id} names intent {record.intent_id}, which is not held"
            )
        _archivum.restore_deletion(tx, project.project_id, record)
    return len(missing)


def _removal(record: RedactionRecord | HistoryRetentionRecord) -> Redaction | RetentionAction:
    """The Freya record `freya.reapply_removals` takes for a redaction or an action read from the store."""
    if isinstance(record, RedactionRecord):
        return Redaction(
            record.redaction_id,
            record.project,
            record.history,
            record.record_id,
            record.field,
            record.reason,
            record.event_at,
        )
    return RetentionAction(
        record.action_id,
        record.project,
        record.history,
        record.rule_id,
        record.keep_days,
        record.rule_event_at,
        record.cutoff,
        record.rows_removed,
        record.event_at,
    )
