"""Registering an archived object and adopting a manifest: the steps a put, a cleanup and a rebuild share.

Input: a catalog session and a writing store. Output: `register_object` (the registration under the address lock,
the one step a put, an adoption and a rebuild all end with), `unregistered_manifests` (the manifests of a scope
that no registration names, read and checked), `adopt_manifest` (the artifact and registration of one of them) and
`adopt_all` (the loop a cleanup and a rebuild share: adopt each, count those left without their dataset).
Private: Hodir's modules import it, a consumer never does. A manifest carries no origin, so an adopted artifact
has none; an artifact the catalog already holds keeps the origin it has.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import NamedTuple

from ulduar.errors import ArchiveFormatError, UlduarError, UnknownRecordError
from ulduar.freya import Artifact, Catalog, Connection, Dataset
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._store import StoredObject, WritingStore, read_small

logger = logging.getLogger(__name__)


class ListedManifest(NamedTuple):
    """A manifest read from the store, with the stored object it came from (its `modified` is the manifest's age)."""

    stored: StoredObject
    manifest: _layout.Manifest


def register_object(
    catalog: Catalog, store: WritingStore, artifact: Artifact, key: str, size: int, retention_class: str | None
) -> bool:
    """Record the registration of *artifact* under the address lock if the object at *key* is there; False if not.

    The check and the insert are one transaction under the lock a deletion of the address takes, so a deletion that
    races the registration either finishes first (the object is gone, False) or waits for it (H15).
    """
    project_id = catalog.project.project_id

    def work(tx: Connection) -> bool:
        _archivum.lock_address(tx, project_id, artifact.scope, artifact.checksum)
        stored = store.head(key)
        if stored is None or stored.size != size:
            return False
        _archivum.insert_registration(tx, project_id, artifact.scope, artifact, retention_class)
        return True

    return catalog.transaction(f"Registering archived artifact {artifact.artifact_id}", work)


def unregistered_manifests(
    catalog: Catalog, store: WritingStore, scope: str, *, since: datetime | None = None
) -> list[ListedManifest]:
    """The manifests of *scope* (from the month of *since* on) whose artifact has no registration, read and checked.

    A key under `_manifests` that is not a manifest key is skipped with a warning. A manifest that does not decode,
    or that names another project, scope or artifact than its key does, is an `ArchiveFormatError` naming the key,
    never the content: nothing is adopted or removed on the strength of a manifest that cannot be trusted.
    """
    project = catalog.project
    registered = catalog.transaction(
        f"Reading the registered artifacts of scope {scope}",
        lambda tx: _archivum.registered_artifact_ids(tx, project.project_id, scope),
    )
    found = []
    for stored in store.list(_layout.manifests_prefix(project.name, scope)):
        parsed = _layout.parse_manifest_key(stored.key.removeprefix(f"{project.name}/"))
        if parsed is None:
            logger.warning("skipped %s: not a manifest key", stored.key)
            continue
        artifact_id = parsed[1]
        if artifact_id in registered or not _layout.is_since(artifact_id, since):
            continue
        try:
            manifest = _layout.Manifest.decode(read_small(store, stored.key))
        except ArchiveFormatError as e:
            raise ArchiveFormatError(f"The manifest {stored.key} cannot be read: {e}") from None
        if (manifest.project, manifest.scope, manifest.artifact_id) != (project.name, scope, artifact_id):
            raise ArchiveFormatError(f"The manifest {stored.key} names another project, scope or artifact than its key")
        found.append(ListedManifest(stored, manifest))
    return found


def _artifact_of(catalog: Catalog, manifest: _layout.Manifest) -> Artifact:
    """The artifact the catalog holds under the manifest's id, or one registered from the manifest."""
    try:
        existing = catalog.get_artifact(manifest.artifact_id)
    except UnknownRecordError:
        existing = None
    if existing is not None:
        if (existing.scope, existing.checksum, existing.size) != (manifest.scope, manifest.checksum, manifest.size):
            raise UlduarError(f"Artifact {manifest.artifact_id} is recorded with other content than its manifest")
        return existing
    dataset = None
    if manifest.dataset_id is not None:
        project_id = catalog.project.project_id
        dataset_id = manifest.dataset_id
        found: Dataset | None = catalog.transaction(
            f"Reading dataset {dataset_id}", lambda tx: _archivum.find_dataset(tx, project_id, dataset_id)
        )
        dataset = found if found is not None and found.scope == manifest.scope else None
    return catalog.register_artifact(
        manifest.kind,
        manifest.checksum,
        manifest.size,
        dataset=dataset,
        scope=manifest.scope,
        record_id=manifest.artifact_id,
    )


def adopt_manifest(catalog: Catalog, store: WritingStore, manifest: _layout.Manifest) -> Artifact | None:
    """Register the artifact *manifest* describes, under its id, with its retention class; None if its object is gone.

    The artifact is registered from the manifest unless the catalog holds it (then its origin and dataset stay as
    they are); its dataset is the manifest's when the Archivum holds that dataset in the scope, else none (the
    caller compares `dataset_id` with the manifest's to count those).
    """
    artifact = _artifact_of(catalog, manifest)
    key = _layout.object_key(catalog.project.name, manifest.scope, manifest.checksum)
    if not register_object(catalog, store, artifact, key, manifest.size, manifest.retention_class):
        return None
    logger.info("adopted artifact %s from its manifest", artifact.artifact_id)
    return artifact


def adopt_all(
    catalog: Catalog, store: WritingStore, manifests: Iterable[_layout.Manifest]
) -> tuple[list[Artifact], int]:
    """Adopt each manifest: the artifacts registered, and how many of them lost the dataset their manifest names.

    A manifest whose object is gone is logged and left out.
    """
    adopted: list[Artifact] = []
    datasets_missing = 0
    for manifest in manifests:
        artifact = adopt_manifest(catalog, store, manifest)
        if artifact is None:
            logger.warning("manifest %s was not adopted: its object is gone", manifest.artifact_id)
            continue
        adopted.append(artifact)
        datasets_missing += manifest.dataset_id is not None and artifact.dataset_id is None
    return adopted, datasets_missing
