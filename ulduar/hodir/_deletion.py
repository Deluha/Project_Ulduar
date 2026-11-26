"""The one deletion protocol: intent, its record, then delete under the address lock and record the outcome.

Input: a catalog session, a deleting store and one object (project, scope, checksum, size) with the cause of its
deletion. Output: a `DeletionOutcome`. Private: retention uses it now, the incomplete-put cleanup and the purge use it
later, so a deletion is the same protocol whatever the cause (hard rule 5, H15, D37).

Transaction 1 records the intent (or finds the open one a failed run left) and commits; the intent's record is then
written to the store; transaction 2 takes the address lock, and either records `cancelled` (a put registered the
content after the intent and relied on the object, or the object is not due any more) or deletes every version of
the object and its manifests and records `confirmed`. The lock is held while the store deletes, so a put of the
same content waits for the outcome and then finds the object gone or still there, never half-deleted. A deletion
that cannot finish (a version remains, the store refused) leaves the intent open: the next run resumes it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from ulduar.errors import ArchiveUnavailable, IntegrityError
from ulduar.freya import Catalog, Connection, Project
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._layout import DeletionRecord
from ulduar.hodir._records import DeletionOutcome, Stage
from ulduar.hodir._store import DeletingStore

logger = logging.getLogger(__name__)

_SHORT_CHECKSUM_HEX = 12
# What `_settle` returns: the outcome row (None when nothing was recorded), the stage reached and why.
_Settled = tuple[DeletionRecord | None, Stage, str | None]


def delete_address(
    catalog: Catalog,
    store: DeletingStore,
    project: Project,
    scope: str,
    checksum: bytes,
    size: int,
    *,
    cause: str,
    rule_id: UUID | None = None,
    reason: str | None = None,
    still_due: Callable[[Connection], bool],
    manifests: Callable[[Connection], list[str]],
) -> DeletionOutcome:
    """Delete the object (*scope*, *checksum*) under the protocol above; the stage reached is the outcome's.

    *still_due* is asked in transaction 1, under the address lock, unless an open intent is resumed: False records
    nothing and the outcome is `skipped`. It is asked again in transaction 2, also under the lock: False cancels the
    intent. *manifests* returns, in transaction 2, the keys of the manifests to remove
    with the object. A store that cannot delete (an `ArchiveUnavailable`) leaves the outcome `open`; a failure to
    write the intent's record raises and deletes nothing; a failure to write an outcome's record after its commit is
    logged only.
    """
    short = checksum.hex()[:_SHORT_CHECKSUM_HEX]

    def begin(tx: Connection) -> DeletionRecord | None:
        _archivum.lock_address(tx, project.project_id, scope, checksum)
        intent = _archivum.find_open_intent(tx, project, scope, checksum)
        if intent is not None:
            return intent
        if not still_due(tx):
            return None
        return _archivum.insert_intent(tx, project, scope, checksum, size, cause, rule_id, reason)

    intent = catalog.transaction(f"Recording the intent to delete object {short} of scope {scope}", begin)
    if intent is None:
        return DeletionOutcome(scope, checksum, size, None, "skipped", cause, "the object is not due any more")
    store.put_bytes_new(_layout.record_key(project.name, intent.deletion_id), intent.encode())
    try:
        recorded, stage, detail = catalog.transaction(
            f"Deleting object {short} of scope {scope}",
            lambda tx: _settle(tx, store, project, intent, still_due, manifests),
        )
    except ArchiveUnavailable as e:  # the transaction rolled back: the intent stays open and the run goes on
        logger.warning("deletion %s of object %s in scope %s stays open: %s", intent.deletion_id, short, scope, e)
        recorded, stage, detail = None, "open", str(e)
    if recorded is not None:
        _write_record(store, project, recorded)
    return DeletionOutcome(scope, checksum, intent.size, intent.deletion_id, stage, intent.cause, detail)


def _settle(
    tx: Connection,
    store: DeletingStore,
    project: Project,
    intent: DeletionRecord,
    still_due: Callable[[Connection], bool],
    manifests: Callable[[Connection], list[str]],
) -> _Settled:
    """Transaction 2, under the address lock: cancel the deletion, or delete the object and confirm it.

    Nothing is deleted that is not due at the moment of deletion: a registration after the intent, or a *still_due*
    that now says no (a rule lengthened since, an intent resumed after the cause went away), cancels it.
    """
    scope, checksum = intent.scope, intent.checksum
    _archivum.lock_address(tx, project.project_id, scope, checksum)
    current = _archivum.find_open_intent(tx, project, scope, checksum)
    if current is None or current.deletion_id != intent.deletion_id:
        return None, "skipped", "another run settled the intent"
    if _archivum.registered_after(tx, project.project_id, scope, checksum, intent.recorded_at):
        outcome = _archivum.insert_outcome(tx, project, intent, "cancelled")
        return outcome, "cancelled", "a put registered the object after the intent"
    if not still_due(tx):
        outcome = _archivum.insert_outcome(tx, project, intent, "cancelled")
        return outcome, "cancelled", "the object is not due any more"
    key = _layout.object_key(project.name, scope, checksum)
    store.delete_every_version(key)
    for manifest in manifests(tx):
        store.delete_every_version(manifest)
    if store.any_version_left(key):
        logger.warning(
            "deletion %s of object %s in scope %s stays open: a version remains",
            intent.deletion_id,
            checksum.hex()[:_SHORT_CHECKSUM_HEX],
            scope,
        )
        return None, "open", "a version of the object remains"
    return _archivum.insert_outcome(tx, project, intent, "confirmed"), "confirmed", None


def _write_record(store: DeletingStore, project: Project, record: DeletionRecord) -> None:
    """Write the record of a committed outcome; the Archivum is the authority, so a failure is only logged."""
    try:
        store.put_bytes_new(_layout.record_key(project.name, record.deletion_id), record.encode())
    except (ArchiveUnavailable, IntegrityError) as e:
        logger.warning("the record of deletion %s was not written to the archive: %s", record.deletion_id, e)
