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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from ulduar.config import Config, check_name
from ulduar.errors import ConfigurationError
from ulduar.freya import Artifact, Catalog, Connection, Project, open_catalog
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._deletion import delete_address
from ulduar.hodir._reconcile import scan
from ulduar.hodir._records import Adoption, CleanupRun, DeletionOutcome, Orphan
from ulduar.hodir._registration import adopt_manifest, unregistered_manifests
from ulduar.hodir._store import DeletingStore, UnfinishedUpload, WritingStore, open_store
from ulduar.hodir._sync import sync_records
from ulduar.hodir.archive import _ensure_format

logger = logging.getLogger(__name__)

CAUSE = "incomplete_put"


@dataclass(frozen=True, slots=True)
class _Plan:
    """What the rule would do in one scope: the manifests to adopt, the objects to remove, the uploads to abort."""

    manifests: tuple[_layout.Manifest, ...]
    remove: tuple[Orphan, ...]
    abort: tuple[UnfinishedUpload, ...]
    open_intents: tuple[Orphan, ...]


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
    intents = catalog.transaction(
        f"Reading the open incomplete-put deletions of scope {scope}",
        lambda tx: _archivum.open_intents(tx, project, scope, CAUSE),
    )
    wanted = {(orphan.scope, orphan.checksum) for orphan in remove}
    open_intents = {
        (i.scope, i.checksum): Orphan(i.scope, i.checksum, i.size)
        for i in intents
        if (i.scope, i.checksum) not in wanted
    }
    return _Plan(adopt, remove, abort, tuple(open_intents.values()))


def clean_incomplete(config: Config, *, dry_run: bool = True, scope: str | None = None) -> CleanupRun:
    """List (dry run, the default) or perform the built-in incomplete-put rule in *scope* (default: every scope).

    The grace period is `archive.incomplete_put_grace_hours` against the database's clock. A manifest without a
    registration whose manifest object is older than the grace and whose object exists is adopted: the artifact is
    registered under the id of the manifest (one the catalog holds is used as it is, its origin kept) with the
    manifest's retention class, as a put registers it. An object older than the grace that no live registration
    and no manifest names is removed, with an intent and an outcome recorded and written to the store; an
    unfinished upload older than the grace is aborted. A real run also settles the open incomplete-put intents of
    the scopes (a failed run left them) through the same protocol. A dry run opens the writing store and changes
    nothing in the archive; a real run needs the deleter credential. Records the Archivum holds that the store
    lacks are copied to it either way. `ConfigurationError` without `[archive]` (or, for a real run, without the
    deleter); `UnknownRecordError` for a *scope* the project lacks; `ValueError` for a bad *scope*;
    `ArchiveFormatError` for a manifest that cannot be read.
    """
    settings = config.archive
    if settings is None:
        raise ConfigurationError("Configuration key `archive` is required to use the archive")
    if scope is not None:
        check_name(scope, "scope")
    deleting = None if dry_run else open_store(settings, deleting=True)
    store: WritingStore = deleting if deleting is not None else open_store(settings)
    _ensure_format(store)
    with open_catalog(config) as catalog:
        scopes = [scope] if scope is not None else _all_scopes(catalog)
        plans = {name: _plan(catalog, store, settings.incomplete_put_grace_hours, name) for name in scopes}
        manifests = [m for plan in plans.values() for m in plan.manifests]
        listing = CleanupRun(
            dry_run,
            tuple(Adoption(m.artifact_id, m.scope, m.checksum, m.size) for m in manifests),
            tuple(o for plan in plans.values() for o in plan.remove),
            tuple(u for plan in plans.values() for u in plan.abort),
            tuple(o for plan in plans.values() for o in plan.open_intents),
        )
        if deleting is None:
            logger.info(
                "incomplete-put dry run: %d to adopt, %d to remove, %d to abort, %d open intents",
                len(listing.adopt),
                len(listing.remove),
                len(listing.abort),
                len(listing.open_intents),
            )
            sync_records(catalog, store)
            return listing
        result = _perform(catalog, deleting, settings.incomplete_put_grace_hours, listing, manifests)
        sync_records(catalog, store)
    return result


def _all_scopes(catalog: Catalog) -> list[str]:
    project_id = catalog.project.project_id
    return catalog.transaction("Reading the scopes of the project", lambda tx: _archivum.project_scopes(tx, project_id))


def _perform(
    catalog: Catalog, store: DeletingStore, grace_hours: int, listing: CleanupRun, manifests: list[_layout.Manifest]
) -> CleanupRun:
    """Adopt, settle and remove, then abort, in that order: an adopted object is never one of those removed."""
    adopted: list[Artifact] = []
    datasets_missing = 0
    for manifest in manifests:
        artifact = adopt_manifest(catalog, store, manifest)
        if artifact is None:
            logger.warning("manifest %s was not adopted: its object is gone", manifest.artifact_id)
            continue
        adopted.append(artifact)
        datasets_missing += manifest.dataset_id is not None and artifact.dataset_id is None
    outcomes = [
        _remove(catalog, store, grace_hours, orphan)
        for orphan in (*listing.open_intents, *listing.remove)  # intents a failed run left open come first
    ]
    for upload in listing.abort:
        store.abort_unfinished(upload)
    logger.info(
        "incomplete-put rule applied: %d adopted, %d deletions (%s), %d uploads aborted",
        len(adopted),
        len(outcomes),
        ", ".join(sorted({outcome.stage for outcome in outcomes})) or "none",
        len(listing.abort),
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
