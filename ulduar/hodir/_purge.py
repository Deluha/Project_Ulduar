"""Purge: an explicit, recorded removal of an object that was archived by mistake (D39).

Input: a `Config` with an `[archive]` section, the object's scope and checksum and a reason; the recording role and
the deleter credential. Output: the `DeletionOutcome` of the deletion. Sits above the one deletion protocol
(`_deletion`) and the record sweep; the facade `ulduar.hodir` re-exports it.

A purge is the one deletion nothing declared: the caller asks for it and names why (`personal_data`, `secret` or
`recorded_by_mistake`, the reasons of a redaction). It removes the object, every version of it and the manifests of
every registration of it, and records an intent and an outcome with cause `purge` and the reason. The catalog's
entries are append-only and stay: redacting them is a separate, explicit call of Freya.
"""

from __future__ import annotations

import logging
from typing import NamedTuple
from uuid import UUID

from ulduar._schema import REDACTION_REASONS, check_checksum
from ulduar.config import Config, check_name
from ulduar.errors import UlduarError, UnknownRecordError
from ulduar.freya import Connection, open_catalog
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._deletion import delete_address, manifest_keys
from ulduar.hodir._layout import DeletionRecord
from ulduar.hodir._records import DeletionOutcome
from ulduar.hodir._store import open_maintenance_store
from ulduar.hodir._sync import sync_records

logger = logging.getLogger(__name__)

CAUSE = "purge"


class _Known(NamedTuple):
    """What the Archivum holds about one address: its registrations, whether one is live, its open intent."""

    artifact_ids: list[UUID]
    live: bool
    intent: DeletionRecord | None


def purge(config: Config, *, scope: str, checksum: bytes, reason: str) -> DeletionOutcome:
    """Remove the object (*scope*, *checksum*) with every version and manifest, and record why (*reason*).

    *reason* is one of Freya's `REDACTION_REASONS`. The object must be known to the project: it has a registration,
    live or not, or the store holds it. It goes through the deletion protocol with cause `purge` under the deleter
    credential, so an intent is committed and written to the store first, and a registration recorded after it (a put
    of the same content) cancels the purge; purge again to remove that too. A refused delete (the writer credential,
    an object lock) leaves the intent open, and purging again with the same *reason* resumes it; another *reason*
    is `UlduarError`, so a record never claims a reason it was not given, and so is an open intent of another cause
    (settle it first). An object already gone (a confirmed deletion after its last registration, nothing in the store)
    is `skipped`, and nothing is recorded. The catalog's entries are untouched; redact them with `freya.redact`.
    Records the Archivum holds that the store lacks are copied to it afterwards.
    `ConfigurationError` without `[archive]` or the deleter; `ValueError` for a bad *scope*, *checksum* or *reason*,
    before any I/O; `UnknownRecordError` for a scope the project lacks or an object it does not know.
    """
    check_name(scope, "scope")
    check_checksum(checksum)
    if reason not in REDACTION_REASONS:
        raise ValueError(f"Reason {reason!r} is not one of {', '.join(REDACTION_REASONS)}")
    short = _layout.short_checksum(checksum)
    with open_catalog(config) as catalog:
        project = catalog.project

        def read(tx: Connection) -> _Known:
            _archivum.check_scope(tx, project, scope)
            return _Known(
                _archivum.address_artifacts(tx, project.project_id, scope, checksum),
                _archivum.address_is_live(tx, project.project_id, scope, checksum),
                _archivum.find_open_intent(tx, project, scope, checksum),
            )

        known = catalog.transaction(f"Reading what is recorded of object {short} of scope {scope}", read)
        _, store, deleting = open_maintenance_store(config, deleting=True)
        assert deleting is not None  # asked for above
        stored = store.head(_layout.object_key(project.name, scope, checksum))
        if not known.artifact_ids and stored is None:
            raise UnknownRecordError(f"No archived object {short} in scope {scope}")
        intent = known.intent
        if intent is not None:
            _check_resumable(intent, reason, short, scope)
            size = intent.size
        elif known.artifact_ids:
            size = catalog.get_artifact(known.artifact_ids[0]).size
        else:
            assert stored is not None  # an address with no registration is known by its object
            size = stored.size
        if intent is None and not known.live and stored is None:
            logger.info("purge of object %s in scope %s: skipped, already gone", short, scope)
            sync_records(catalog, store)
            return DeletionOutcome(scope, checksum, size, None, "skipped", CAUSE, "the object is already gone")
        outcome = delete_address(
            catalog,
            deleting,
            project,
            scope,
            checksum,
            size,
            cause=CAUSE,
            reason=reason,
            still_due=lambda tx: True,
            manifests=lambda tx: manifest_keys(tx, project, scope, checksum),
        )
        sync_records(catalog, store)
    logger.info("purge of object %s in scope %s for %s: %s", short, scope, reason, outcome.stage)
    return outcome


def _check_resumable(intent: DeletionRecord, reason: str, short: str, scope: str) -> None:
    """`UlduarError` unless the open *intent* is a purge's with this *reason*; the protocol would settle it as is."""
    if intent.cause != CAUSE:
        raise UlduarError(
            f"Object {short} in scope {scope} has an open {intent.cause} deletion; settle it before purging the object"
        )
    if intent.reason != reason:
        raise UlduarError(
            f"Object {short} in scope {scope} has an open purge recorded with reason {intent.reason}, not {reason};"
            " purge it again with that reason"
        )
