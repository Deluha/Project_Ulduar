"""Reconciliation: compare one scope's store listing with the Archivum's live addresses, and record what differs.

Input: a catalog session, a writing store, the grace period and a scope. Output: `scan` (what the store holds in the
scope and what the Archivum registers there, with the database's clock and the grace cutoff, one reading shared by
the reconciliation and the incomplete-put cleanup) and `reconcile` (the findings of a scan, recorded as one
`archive_check`). Private: `Archive.reconcile` and `clean_incomplete` call it.

Order of reading (D31): the Archivum first, then the store. A put writes its object before its registration, so an
object listed after the registrations were read is either registered, or younger than the grace period, or an
orphan; reading the store first would report a put that finished meanwhile as an entry without its object.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple

from ulduar import _ids
from ulduar.errors import ArchivumUnavailable, IntegrityError
from ulduar.freya import Catalog, Connection
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._records import Finding, Reconciliation
from ulduar.hodir._store import PendingUpload, StoredObject, WritingStore

logger = logging.getLogger(__name__)


class ListedObject(NamedTuple):
    """An object under a scope's object prefix: what the store says of it and the checksum its key names (or None)."""

    stored: StoredObject
    checksum: bytes | None


@dataclass(frozen=True, slots=True)
class Scan:
    """One scope as the Archivum and the store showed it; *cutoff* is the database's now minus the grace period."""

    scope: str
    cutoff: datetime
    live: dict[bytes, int]
    objects: tuple[ListedObject, ...]
    unfinished: tuple[PendingUpload, ...]

    def is_old(self, moment: datetime) -> bool:
        return moment < self.cutoff


def scan(catalog: Catalog, store: WritingStore, grace_hours: int, scope: str) -> Scan:
    """Read the live addresses and the database's clock, then list the scope's objects and unfinished uploads.

    `UnknownRecordError` for a scope the project lacks, before the store is touched.
    """
    project = catalog.project

    def read(tx: Connection) -> tuple[datetime, dict[bytes, int]]:
        _archivum.check_scope(tx, project, scope)
        return _archivum.database_now(tx), dict(_archivum.live_addresses(tx, project.project_id, scope))

    now, live = catalog.transaction(f"Reading the objects of scope {scope} that are registered", read)
    objects = tuple(
        ListedObject(stored, _checksum_of(project.name, scope, stored.key))
        for stored in store.list(_layout.objects_prefix(project.name, scope))
    )
    unfinished = tuple(store.list_unfinished(f"{project.name}/{scope}"))
    return Scan(scope, now - timedelta(hours=grace_hours), live, objects, unfinished)


def _still_missing(catalog: Catalog, store: WritingStore, scope: str, checksum: bytes) -> bool | None:
    """True when the address, listed as live before the store was, is still live and the store has no object for it.

    Asked under the address lock, so a deletion whose transaction is running has committed (the address is no longer
    live) or not begun (the object is there) by the time it answers: a deletion in progress is never a finding. A
    key the store refuses to read (a link or a directory where the object belongs) is no object Hodir can serve, so
    the address counts as missing. None when the lock could not be had in time: the address is left unchecked.
    """
    project = catalog.project
    key = _layout.object_key(project.name, scope, checksum)

    def work(tx: Connection) -> bool:
        _archivum.lock_address(tx, project.project_id, scope, checksum)
        if not _archivum.address_is_live(tx, project.project_id, scope, checksum):
            return False
        try:
            return store.head(key) is None
        except IntegrityError:
            return True

    try:
        return catalog.transaction(f"Re-reading whether object {_layout.short_checksum(checksum)} is missing", work)
    except ArchivumUnavailable as e:  # a lock or statement timeout rolled the transaction back: the session goes on
        logger.warning(
            "reconciliation of scope %s: object %s was not checked: %s", scope, _layout.short_checksum(checksum), e
        )
        return None


def _checksum_of(project: str, scope: str, key: str) -> bytes | None:
    """The checksum a listed object key names in *scope*, or None for a key that is not an object's."""
    parsed = _layout.parse_object_key(key.removeprefix(f"{project}/"))
    return parsed[1] if parsed is not None and parsed[0] == scope else None


def reconcile(catalog: Catalog, store: WritingStore, grace_hours: int, scope: str) -> Reconciliation:
    """Record and return what differs between the store and the Archivum in *scope*.

    `object_without_entry`: an object that is not live and is older than the grace period (an unparseable key under
    `_objects` too, with no checksum). `entry_without_object`: a live address the store does not hold (no grace: a
    put writes its object before its registration), still live when asked again after the listing; an address whose
    lock is held too long to ask is counted in `skipped_busy`, never reported, and a key the store refuses to read
    (a link or directory in its place) is an entry without an object. `unfinished_upload`:
    older than the grace period; it is reported, never aborted. Younger objects and uploads are only counted. One
    `archive_check` (kind `reconciliation`) and a finding per disagreement are recorded in one transaction; an
    agreement records no finding.
    """
    project = catalog.project
    found = scan(catalog, store, grace_hours, scope)
    findings: list[Finding] = []
    skipped = 0
    busy = 0
    listed = set()
    for stored, checksum in found.objects:
        if checksum is not None:
            listed.add(checksum)
            if checksum in found.live:
                continue
        if not found.is_old(stored.modified):
            skipped += 1
            continue
        size = None if checksum is None else stored.size
        findings.append(Finding(_ids.uuid7(), scope, checksum, size, "object_without_entry"))
    for checksum, size in found.live.items():
        if checksum in listed:
            continue
        missing = _still_missing(catalog, store, scope, checksum)
        if missing is None:
            busy += 1
        elif missing:
            findings.append(Finding(_ids.uuid7(), scope, checksum, size, "entry_without_object"))
    for upload in found.unfinished:
        if found.is_old(upload.started):
            checksum = _checksum_of(project.name, scope, upload.key)  # None for a directory's temporary file
            findings.append(Finding(_ids.uuid7(), scope, checksum, None, "unfinished_upload"))
        else:
            skipped += 1
    check_id = _ids.uuid7()
    times = [_ids.uuid7_time(check_id), *(_ids.uuid7_time(finding.finding_id) for finding in findings)]
    catalog.transaction(
        f"Recording reconciliation {check_id}",
        lambda tx: _archivum.insert_check(
            tx,
            project.project_id,
            check_id=check_id,
            scope=scope,
            kind="reconciliation",
            mode="full",
            objects_checked=len(found.objects),
            findings=findings,
        ),
        event_ats=times,
    )
    for finding in findings:
        logger.warning(
            "reconciliation of scope %s: %s %s",
            scope,
            finding.outcome,
            "(no checksum)" if finding.checksum is None else _layout.short_checksum(finding.checksum),
        )
    logger.info(
        "reconciled scope %s: %d objects listed, %d skipped as young, %d unchecked as busy, %d findings",
        scope,
        len(found.objects),
        skipped,
        busy,
        len(findings),
    )
    return Reconciliation(
        check_id, scope, len(found.objects), skipped, tuple(findings), _ids.uuid7_time(check_id), busy
    )
