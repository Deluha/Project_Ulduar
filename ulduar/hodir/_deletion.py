"""The one deletion protocol: intent, its record, then delete under the address lock and record the outcome.

Input: a catalog session, a deleting store and one object (project, scope, checksum, size) with the cause of its
deletion. Output: a `DeletionOutcome`. Private: retention uses it now, the incomplete-put cleanup and the purge use it
later, so a deletion is the same protocol whatever the cause (hard rule 5, H15, D37).

Transaction 1 records the intent (or finds the open one of its own cause a failed run left) and commits; the
intent's record is then written to the store; transaction 2 takes the address lock and, when no version of the
object is left (an earlier attempt deleted it), records `confirmed`; else either records `cancelled` (a put
registered the content after the intent and relied on the object, or the object is not due any more) or deletes
every version of the object and records `confirmed`. The lock is held for the object's deletes, the check that
none is left and the outcome row only, so a put of the same content waits for the outcome and then finds the
object gone or still there, never half-deleted. The manifests of the address are deleted after the commit, best
effort. A manifest left behind names no object, which is harmless until the same content is put again (a rebuild
adopts a manifest whose object exists, so it would bring the deleted artifact back): `clean_incomplete` deletes it
once it is past the grace period, and a retried put of its artifact is refused before it stores anything. A deletion
that cannot finish (a version remains, the store refused, the Archivum was slow) leaves the intent open: the next run
resumes it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from ulduar.errors import ArchiveUnavailable, ArchivumUnavailable, IntegrityError, UlduarError
from ulduar.freya import Catalog, Connection, Project
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._layout import DeletionRecord
from ulduar.hodir._records import DeletionOutcome, Stage
from ulduar.hodir._store import DeletingStore

logger = logging.getLogger(__name__)

# What `_settle` returns: the outcome row (None when nothing was recorded), the stage reached, why, and the keys of
# the manifests to delete once the outcome is committed.
_Settled = tuple[DeletionRecord | None, Stage, str | None, list[str]]


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
    intent. An open intent of another *cause* is not this deletion's to resume: the outcome is `skipped` and nothing
    is recorded. *manifests* returns, in transaction 2, the keys of the manifests to remove after the commit. A
    store that cannot delete (an `ArchiveUnavailable`) or an Archivum that is too slow (an `ArchivumUnavailable`)
    leaves the outcome `open`; a failure to write the intent's record raises and deletes nothing; a failure to
    write an outcome's record or to delete a manifest after the commit is logged only.
    """
    short = _layout.short_checksum(checksum)

    def begin(tx: Connection) -> tuple[DeletionRecord | None, str]:
        _archivum.lock_address(tx, project.project_id, scope, checksum)
        intent = _archivum.find_open_intent(tx, project, scope, checksum)
        if intent is not None:
            if intent.cause != cause:
                return None, f"an open {intent.cause} deletion is pending"
            return intent, ""
        if not still_due(tx):
            return None, "the object is not due any more"
        return _archivum.insert_intent(tx, project, scope, checksum, size, cause, rule_id, reason), ""

    intent, why_not = catalog.transaction(f"Recording the intent to delete object {short} of scope {scope}", begin)
    if intent is None:
        return DeletionOutcome(scope, checksum, size, None, "skipped", cause, why_not)
    store.put_bytes_new(_layout.record_key(project.name, intent.deletion_id), intent.encode())
    try:
        recorded, stage, detail, leftovers = catalog.transaction(
            f"Deleting object {short} of scope {scope}",
            lambda tx: _settle(tx, store, project, intent, still_due, manifests),
        )
    except (ArchiveUnavailable, ArchivumUnavailable) as e:  # rolled back: the intent stays open
        if isinstance(e, ArchivumUnavailable):
            _require_connection(catalog, e)  # a lost connection stops the run; a timeout lets it go on
        logger.warning("deletion %s of object %s in scope %s stays open: %s", intent.deletion_id, short, scope, e)
        recorded, stage, detail, leftovers = None, "open", str(e), []
    if recorded is not None:
        _write_record(store, project, recorded)
    _delete_manifests(store, leftovers)
    return DeletionOutcome(scope, checksum, intent.size, intent.deletion_id, stage, intent.cause, detail)


def _require_connection(catalog: Catalog, error: ArchivumUnavailable) -> None:
    """Return when the catalog session answers a trivial statement; else raise *error*, the one that was caught."""
    try:
        catalog.transaction("Checking the Archivum connection", lambda tx: tx.execute("SELECT 1"))
    except UlduarError:
        raise error from None


def _settle(
    tx: Connection,
    store: DeletingStore,
    project: Project,
    intent: DeletionRecord,
    still_due: Callable[[Connection], bool],
    manifests: Callable[[Connection], list[str]],
) -> _Settled:
    """Transaction 2, under the address lock: confirm the deletion, cancel it, or delete the object and confirm it.

    No version left means a previous attempt deleted the object and failed before recording it: it is confirmed
    whatever else holds, so a half-done deletion is never cancelled while registrations stay live without their
    object. Otherwise nothing is deleted that is not due at the moment of deletion: a registration after the
    intent, or a *still_due* that now says no (a rule lengthened since, an intent resumed after the cause went
    away), cancels it.
    """
    scope, checksum = intent.scope, intent.checksum
    _archivum.lock_address(tx, project.project_id, scope, checksum)
    current = _archivum.find_open_intent(tx, project, scope, checksum)
    if current is None or current.deletion_id != intent.deletion_id:
        return None, "skipped", "another run settled the intent", []
    key = _layout.object_key(project.name, scope, checksum)
    if not store.any_version_left(key):
        return _archivum.insert_outcome(tx, project, intent, "confirmed"), "confirmed", None, manifests(tx)
    if _archivum.registered_after(tx, project.project_id, scope, checksum, intent.recorded_at):
        outcome = _archivum.insert_outcome(tx, project, intent, "cancelled")
        return outcome, "cancelled", "a put registered the object after the intent", []
    if not still_due(tx):
        outcome = _archivum.insert_outcome(tx, project, intent, "cancelled")
        return outcome, "cancelled", "the object is not due any more", []
    store.delete_every_version(key)
    if store.any_version_left(key):
        logger.warning(
            "deletion %s of object %s in scope %s stays open: a version remains",
            intent.deletion_id,
            _layout.short_checksum(checksum),
            scope,
        )
        return None, "open", "a version of the object remains", []
    return _archivum.insert_outcome(tx, project, intent, "confirmed"), "confirmed", None, manifests(tx)


def manifest_keys(tx: Connection, project: Project, scope: str, checksum: bytes) -> list[str]:
    """The keys of the manifests of every registration of the object, live or not."""
    return [
        _layout.manifest_key(project.name, scope, artifact_id)
        for artifact_id in _archivum.address_artifacts(tx, project.project_id, scope, checksum)
    ]


def _write_record(store: DeletingStore, project: Project, record: DeletionRecord) -> None:
    """Write the record of a committed outcome; the Archivum is the authority, so a failure is only logged."""
    try:
        store.put_bytes_new(_layout.record_key(project.name, record.deletion_id), record.encode())
    except (ArchiveUnavailable, IntegrityError) as e:
        logger.warning("the record of deletion %s was not written to the archive: %s", record.deletion_id, e)


def _delete_manifests(store: DeletingStore, keys: list[str]) -> None:
    """Delete the manifests of a confirmed deletion, best effort; `clean_incomplete` deletes one that stays."""
    for key in keys:
        try:
            store.delete_every_version(key)
        except (ArchiveUnavailable, IntegrityError) as e:
            logger.warning("the manifest %s was not deleted from the archive: %s", key, e)
