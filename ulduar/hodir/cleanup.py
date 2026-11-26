"""The built-in incomplete-put rule: adopt what an interrupted put left registrable, remove and abort the rest (D38).

Input: a `Config` with an `[archive]` section; the recording role and, for a real run, the deleter credential.
Output: `clean_incomplete`, a `CleanupRun`. Sits above the reconciliation scan, the adoption helpers and the one
deletion protocol; the facade `ulduar.hodir` re-exports it.

A put writes object, manifest, artifact and registration in that order (D31), so an interruption leaves an object
alone, or an object with its manifest, and never a registration without its object. After the grace period
(`archive.incomplete_put_grace_hours`, the database's clock against the store's modification time) a manifest without a
registration whose object exists is adopted, an object with neither a registration nor a manifest is removed through
the deletion protocol (cause `incomplete_put`, so the removal is recorded like any deletion), and an unfinished upload
is aborted. A manifest that cannot be read stops the run before anything changes: what it names might be adoptable.
A manifest past the grace whose artifact is registered but no longer archived is a leftover of a confirmed deletion
(its delete after the commit failed): it is deleted, so a rebuild cannot bring the deleted artifact back once the
same content is put again. That is no object deletion (the registration it names is removed and the deletion is
already recorded), so no deletion record is written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from ulduar.config import Config, check_name
from ulduar.freya import Catalog, Connection, Project, open_catalog
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._deletion import delete_address
from ulduar.hodir._reconcile import Scan, scan
from ulduar.hodir._records import Adoption, CleanupRun, DeletionOutcome, Orphan, UnfinishedUpload
from ulduar.hodir._registration import adopt_all, unregistered_manifests
from ulduar.hodir._store import DeletingStore, PendingUpload, WritingStore, open_maintenance_store
from ulduar.hodir._sync import sync_records

logger = logging.getLogger(__name__)

CAUSE = "incomplete_put"


@dataclass(frozen=True, slots=True)
class _Plan:
    """What the rule would do in one scope: the manifests to adopt, the objects to remove, the uploads to abort."""

    manifests: tuple[_layout.Manifest, ...]
    remove: tuple[Orphan, ...]
    abort: tuple[PendingUpload, ...]
    open_intents: tuple[Orphan, ...]
    stale: tuple[str, ...]


def _plan(catalog: Catalog, store: WritingStore, grace_hours: int, scope: str) -> _Plan:
    """Read one scope and decide; nothing is changed."""
    project = catalog.project
    found = scan(catalog, store, grace_hours, scope)
    unregistered = unregistered_manifests(catalog, store, scope)
    stored_objects = {o.checksum: o.stored for o in found.objects if o.checksum is not None}
    # An address that has a manifest of its own, however young, is never removed: its put may be running.
    claimed = {listed.manifest.checksum for listed in unregistered}
    adopt = tuple(
        listed.manifest
        for listed in unregistered
        if found.is_old(listed.stored.modified)
        and (stored := stored_objects.get(listed.manifest.checksum)) is not None
        and stored.size == listed.manifest.size
    )
    remove = tuple(
        Orphan(scope, o.checksum, o.stored.size)
        for o in found.objects
        if o.checksum is not None
        and o.checksum not in found.live
        and o.checksum not in claimed
        and found.is_old(o.stored.modified)
    )
    abort = tuple(upload for upload in found.unfinished if found.is_old(upload.started))
    wanted = {(orphan.scope, orphan.checksum) for orphan in remove}
    intents = catalog.transaction(
        f"Reading the open incomplete-put deletions of scope {scope}",
        lambda tx: _archivum.open_intents_outside(tx, project, scope, CAUSE, wanted),
    )
    stale = _stale_manifests(catalog, store, found)
    return _Plan(adopt, remove, abort, tuple(Orphan(i.scope, i.checksum, i.size) for i in intents), stale)


def _stale_manifests(catalog: Catalog, store: WritingStore, found: Scan) -> tuple[str, ...]:
    """The keys of the manifests, past the grace, of the scope's artifacts registered whose object was deleted."""
    project = catalog.project
    deleted = catalog.transaction(
        f"Reading the deleted artifacts of scope {found.scope}",
        lambda tx: _archivum.deleted_artifact_ids(tx, project.project_id, found.scope),
    )
    if not deleted:
        return ()
    stale = []
    for stored in store.list(_layout.manifests_prefix(project.name, found.scope)):
        parsed = _layout.parse_manifest_key(stored.key.removeprefix(f"{project.name}/"))
        if parsed is not None and parsed[1] in deleted and found.is_old(stored.modified):
            stale.append(stored.key)
    return tuple(stale)


def clean_incomplete(config: Config, *, dry_run: bool = True, scope: str | None = None) -> CleanupRun:
    """List (dry run, the default) or perform the built-in incomplete-put rule in *scope* (default: every scope).

    The grace period is `archive.incomplete_put_grace_hours` against the database's clock. A manifest without a
    registration whose manifest object is older than the grace and whose object exists is adopted: the artifact is
    registered under the id of the manifest (one the catalog holds is used as it is, its origin kept) with the
    manifest's retention class, as a put registers it. An object older than the grace that no live registration
    and no manifest names is removed, with an intent and an outcome recorded and written to the store; an
    unfinished upload older than the grace is aborted (this is the only call that aborts one: `reconcile` only
    reports them). A real run also settles the open incomplete-put intents of the scopes (a failed run left them)
    through the same protocol. A manifest older than the grace whose artifact is registered but no longer archived (a
    leftover of a confirmed deletion) is deleted and listed in `stale_manifests` (its key); it is no object deletion,
    so no record is made of it. A dry run opens the writing store and writes only what any call may write: the
    format marker and `LAYOUT.txt` of a new store, and the records the Archivum holds that the store lacks (a real
    run copies those too, also when a step of it raises); it adopts, removes and aborts nothing. `ConfigurationError`
    without `[archive]` (or, for a real run, without the deleter); `UnknownRecordError` for a *scope* the project
    lacks; `ValueError` for a bad *scope*; `ArchiveFormatError` for a manifest that cannot be read.
    """
    if scope is not None:
        check_name(scope, "scope")
    settings, store, deleting = open_maintenance_store(config, deleting=not dry_run)
    with open_catalog(config) as catalog:
        scopes = [scope] if scope is not None else _all_scopes(catalog)
        plans = {name: _plan(catalog, store, settings.incomplete_put_grace_hours, name) for name in scopes}
        manifests = [m for plan in plans.values() for m in plan.manifests]
        listing = CleanupRun(
            dry_run,
            tuple(Adoption(m.artifact_id, m.scope, m.checksum, m.size) for m in manifests),
            tuple(o for plan in plans.values() for o in plan.remove),
            tuple(UnfinishedUpload(u.key, u.started) for plan in plans.values() for u in plan.abort),
            tuple(o for plan in plans.values() for o in plan.open_intents),
            stale_manifests=tuple(key for plan in plans.values() for key in plan.stale),
        )
        if deleting is None:
            logger.info(
                "incomplete-put dry run: %d to adopt, %d to remove, %d to abort, %d open intents, %d stale manifests",
                len(listing.adopt),
                len(listing.remove),
                len(listing.abort),
                len(listing.open_intents),
                len(listing.stale_manifests),
            )
            sync_records(catalog, store)
            return listing
        uploads = [u for plan in plans.values() for u in plan.abort]
        try:
            result = _perform(catalog, deleting, settings.incomplete_put_grace_hours, listing, manifests, uploads)
        finally:  # what the Archivum recorded before a step failed still reaches the store
            sync_records(catalog, store)
    return result


def _all_scopes(catalog: Catalog) -> list[str]:
    project_id = catalog.project.project_id
    return catalog.transaction("Reading the scopes of the project", lambda tx: _archivum.project_scopes(tx, project_id))


def _perform(
    catalog: Catalog,
    store: DeletingStore,
    grace_hours: int,
    listing: CleanupRun,
    manifests: list[_layout.Manifest],
    uploads: list[PendingUpload],
) -> CleanupRun:
    """Adopt, settle and remove, then abort, in that order: an adopted object is never one of those removed."""
    adopted, datasets_missing = adopt_all(catalog, store, manifests)
    outcomes = [
        _remove(catalog, store, grace_hours, orphan)
        for orphan in (*listing.open_intents, *listing.remove)  # intents a failed run left open come first
    ]
    for key in listing.stale_manifests:
        store.delete_every_version(key)
        logger.info("incomplete-put rule: deleted the stale manifest %s of a deleted artifact", key)
    for upload in uploads:
        store.abort_unfinished(upload)
    logger.info(
        "incomplete-put rule applied: %d adopted, %d deletions (%s), %d stale manifests deleted, %d uploads aborted",
        len(adopted),
        len(outcomes),
        ", ".join(sorted({outcome.stage for outcome in outcomes})) or "none",
        len(listing.stale_manifests),
        len(uploads),
    )
    return CleanupRun(
        False,
        listing.adopt,
        listing.remove,
        listing.abort,
        listing.open_intents,
        tuple(outcomes),
        tuple(adopted),
        datasets_missing,
        listing.stale_manifests,
    )


def _remove(catalog: Catalog, store: DeletingStore, grace_hours: int, orphan: Orphan) -> DeletionOutcome:
    """Delete one orphan through the protocol; transaction 2 asks again whether it is still one."""
    project = catalog.project
    scope, checksum = orphan.scope, orphan.checksum
    outcome = delete_address(
        catalog,
        store,
        project,
        scope,
        checksum,
        orphan.size,
        cause=CAUSE,
        still_due=lambda tx: _still_orphan(tx, store, project, grace_hours, orphan),
        manifests=lambda tx: [],
    )
    logger.info(
        "incomplete-put rule: object %s in scope %s is %s",
        _layout.short_checksum(checksum),
        scope,
        outcome.stage,
    )
    return outcome


def _still_orphan(tx: Connection, store: WritingStore, project: Project, grace_hours: int, orphan: Orphan) -> bool:
    """True while no registration of the object is live and the object, if the store still has it, is past the grace."""
    if _archivum.address_is_live(tx, project.project_id, orphan.scope, orphan.checksum):
        return False
    stored = store.head(_layout.object_key(project.name, orphan.scope, orphan.checksum))
    cutoff = _archivum.database_now(tx) - timedelta(hours=grace_hours)
    return stored is None or stored.modified < cutoff
