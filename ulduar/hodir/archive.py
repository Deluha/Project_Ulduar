"""The archive session: store a file or a stream as an immutable, content-addressed object and get it back.

Input: a `Config` with an `[archive]` section and the recording role's Archivum connection. Output:
`open_archive`, which yields an `Archive` holding the catalog session and the writing store, the `Artifact`
each put returns, and the verified bytes of a get. Sits above Freya (the catalog, one connection per job) and
the storage interface; the facade `ulduar.hodir` re-exports it.

A put writes in the order object, manifest, artifact, registration (D31): a failure leaves at worst an object
without an entry, never an entry without its object, and running the same put again with the same `record_id`
completes it. The registration is written under the object's address lock (H15), after a check that the object is
there, so a deletion that races a put cannot remove an object a registration then names.

A get is verified (D36): the object's bytes are hashed and counted while they are read, and a file is handed over
only after they matched the artifact's checksum and size.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import UUID, uuid4

from ulduar import _ids
from ulduar._schema import check_kind, check_origin
from ulduar.config import ArchiveSettings, Config, check_name
from ulduar.errors import IntegrityError, UlduarError, UnknownRecordError
from ulduar.freya import Artifact, Catalog, Connection, Dataset, Project, open_catalog
from ulduar.hodir import _archivum, _layout, _rebuild, _reconcile
from ulduar.hodir._records import PASSED, Finding, Rebuild, Reconciliation, Verification
from ulduar.hodir._registration import register_object
from ulduar.hodir._store import CHUNK_SIZE, WritingStore, open_maintenance_store, open_regular, read_small
from ulduar.hodir._sync import sync_records

logger = logging.getLogger(__name__)

BYTES_PER_GIB = 1024**3


def _hash_object(store: WritingStore, key: str, write: Callable[[bytes], object] | None = None) -> tuple[bytes, int]:
    """Read the object at *key* in chunks, hashing and counting them (and passing each to *write*); (digest, size)."""
    digest = hashlib.sha256()
    size = 0
    with store.read(key) as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
            if write is not None:
                write(chunk)
    return digest.digest(), size


@contextmanager
def open_archive(config: Config) -> Iterator[Archive]:
    """Open the archive of `[archive]` for writing, with a catalog session on the recording role.

    `ConfigurationError` without an `[archive]` section; `ArchiveFormatError` when the store's format marker is
    foreign or of another version (a new store gets its marker and layout text); the catalog session's errors
    as `freya.open_catalog` raises them. The catalog connection closes on exit.
    """
    settings, store, _ = open_maintenance_store(config)
    with open_catalog(config) as catalog:
        yield Archive(catalog, store, settings)


@dataclass(frozen=True, slots=True)
class _Request:
    """What one put records, validated before any I/O."""

    kind: str
    dataset: Dataset | None
    origin: str | None
    scope: str
    retention_class: str | None
    artifact_id: UUID
    explicit_id: bool  # the caller named the id: the put may be a retry of one that was interrupted


class PutSink:
    """Where a stream put collects its bytes: written to a local spool while hashed, stored by `commit`.

    Made by `Archive.put_stream`; it refuses writes after `commit`, after the block ends and after the spool
    limit was passed.
    """

    def __init__(
        self, spool: BinaryIO, path: Path, limit_gib: int, store: Callable[[Path, int, bytes], Artifact]
    ) -> None:
        self._spool = spool
        self._path = path
        self._limit_gib = limit_gib
        self._store = store
        self._hash = hashlib.sha256()
        self._size = 0
        self._open = True
        self._failed = False
        self._artifact: Artifact | None = None

    def write(self, data: bytes) -> None:
        """Append *data* to the stream; `UlduarError` once closed, or past the spool limit (the sink then closes)."""
        if not self._open:
            raise UlduarError("The stream is closed — nothing more can be written to it")
        if self._size + len(data) > self._limit_gib * BYTES_PER_GIB:
            self._close()
            raise UlduarError(f"The stream is larger than the spool limit of {self._limit_gib} GiB")
        self._spool.write(data)
        self._hash.update(data)
        self._size += len(data)

    def commit(self) -> Artifact:
        """Store what was written and register it; called again, it returns the same artifact."""
        if self._artifact is not None:
            return self._artifact
        if self._failed:
            raise UlduarError("The put did not complete — put the stream again, with the same record_id to finish it")
        if not self._open:
            raise UlduarError("The stream is closed — nothing was stored")
        self._close()
        self._failed = True
        self._artifact = self._store(self._path, self._size, self._hash.digest())
        self._failed = False
        return self._artifact

    def _close(self) -> None:
        self._open = False
        self._spool.close()

    def __enter__(self) -> PutSink:
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()


class Archive:
    """One project's archive session: the catalog session and the writing store; made by `open_archive`.

    Not thread-safe: the catalog's connection serves one call at a time, so a job holds one session, and a
    thread of its own opens its own.
    """

    def __init__(self, catalog: Catalog, store: WritingStore, settings: ArchiveSettings) -> None:
        self._catalog = catalog
        self._store = store
        self._settings = settings

    @property
    def project(self) -> Project:
        return self._catalog.project

    @property
    def scope(self) -> str:
        """The default scope: the configuration's."""
        return self._catalog.scope

    def _request(
        self,
        kind: str,
        dataset: Dataset | None,
        origin: str | None,
        scope: str | None,
        retention_class: str | None,
        record_id: UUID | None,
    ) -> _Request:
        check_kind(kind)
        check_origin(origin)
        scope = self.scope if scope is None else check_name(scope, "scope")
        if retention_class is not None:
            check_name(retention_class, "retention class")
        artifact_id = _ids.uuid7() if record_id is None else record_id
        _ids.uuid7_time(artifact_id)  # ValueError naming the id when it is not a version 7 UUID
        project = self.project
        self._catalog.transaction(f"Checking scope {scope}", lambda tx: _archivum.check_scope(tx, project, scope))
        return _Request(kind, dataset, origin, scope, retention_class, artifact_id, record_id is not None)

    def put_file(
        self,
        path: Path,
        kind: str,
        *,
        dataset: Dataset | None = None,
        origin: str | None = None,
        scope: str | None = None,
        retention_class: str | None = None,
        record_id: UUID | None = None,
    ) -> Artifact:
        """Store the regular file *path* in the archive, register it as an artifact of *kind* and return it.

        The object is stored under its SHA-256 (the same content twice is one object, and each put its own
        artifact and manifest); *retention_class* is fixed now and names the retention rule that will apply;
        *origin* is a path or id within the source. *record_id* (a UUIDv7) is the idempotency key: a failed
        put repeated with it completes, a finished one returns the same artifact. `ValueError` for a bad
        argument or a source that is not a regular file (a symbolic link included), and `UnknownRecordError` for a
        scope the project lacks, both before any storage I/O;
        `UlduarError` when *record_id* names an artifact with other content, manifest or retention class, or one
        whose object was deleted from the archive since (nothing is stored then: put it again under a new record id).
        """
        request = self._request(kind, dataset, origin, scope, retention_class, record_id)
        digest = hashlib.sha256()
        size = 0
        with open_regular(Path(path)) as source:
            while chunk := source.read(CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
        return self._put(request, Path(path), size, digest.digest())

    def put_stream(
        self,
        kind: str,
        *,
        dataset: Dataset | None = None,
        origin: str | None = None,
        scope: str | None = None,
        retention_class: str | None = None,
        record_id: UUID | None = None,
    ) -> AbstractContextManager[PutSink]:
        """A sink to write a stream into; `commit()` stores it as `put_file` does and returns the artifact.

        The bytes are spooled to a local file (in `spool_dir`) while hashed, up to `spool_limit_gib`; leaving the
        block without `commit`, or with an exception, deletes the spool and stores nothing. The arguments are
        those of `put_file` and are checked before the spool is made.
        """
        request = self._request(kind, dataset, origin, scope, retention_class, record_id)
        return self._spooled(request)

    @contextmanager
    def _spooled(self, request: _Request) -> Iterator[PutSink]:
        fd, name = tempfile.mkstemp(prefix="ulduar-spool-", dir=self._settings.spool_dir)
        path = Path(name)
        try:
            with PutSink(
                os.fdopen(fd, "wb"),
                path,
                self._settings.spool_limit_gib,
                lambda spool, size, sha256: self._put(request, spool, size, sha256),
            ) as sink:
                yield sink
        finally:
            with suppress(FileNotFoundError):
                path.unlink()

    def get(self, artifact: Artifact | UUID, destination: str | os.PathLike[str]) -> Path:
        """Write the archived object of *artifact* (an `Artifact` or its id) to the new file *destination*; return it.

        Only the artifact's id is used: the checksum, size and scope are re-read from the catalog.

        The bytes are written to a partial file beside the destination (`.ulduar-partial-<hex>`, mode 0o666
        under the umask; no part of the destination's name is in it, so a long name never overflows the file system's
        limit), hashed while written, and the destination appears, by a hard link, only after they match
        the artifact's checksum and size and were synced; a failure at any point removes the partial file. A
        destination that exists (a dangling symbolic link included) is `FileExistsError`, and one whose parent is
        not a directory `FileNotFoundError` or `NotADirectoryError`, both before any read of the store.
        `UnknownRecordError` when the artifact is not recorded or its object is no longer in the archive;
        `IntegrityError` when the object is missing, unreadable or does not match.
        """
        artifact = self._archived(artifact)
        dest = Path(destination)
        try:
            os.lstat(dest)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "The destination already exists", str(dest))
        directory = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            partial = f".ulduar-partial-{uuid4().hex}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            file = os.fdopen(os.open(partial, flags, 0o666, dir_fd=directory), "wb")
            try:
                with file:
                    self._read_verified(artifact, file.write)
                    file.flush()
                    os.fsync(file.fileno())
                os.link(partial, dest.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.unlink(partial, dir_fd=directory)
            except BaseException:
                with suppress(OSError):
                    os.unlink(partial, dir_fd=directory)
                raise
            os.fsync(directory)
        finally:
            os.close(directory)
        return dest

    def get_stream(self, artifact: Artifact | UUID, out: BinaryIO) -> int:
        """Write the archived object of *artifact* to *out* as it is read; return the number of bytes written.

        The bytes reach *out* before they are verified: when this raises `IntegrityError` (a checksum or size
        mismatch, found at the end), the caller must discard what it received. Other errors as `get`.
        """
        return self._read_verified(self._archived(artifact), out.write)

    def _archived(self, artifact: Artifact | UUID) -> Artifact:
        """The artifact as the catalog recorded it, once it is known to be in the archive.

        Only the id of *artifact* is used: its checksum, size and scope are the recorded ones, never the
        caller's. `UnknownRecordError` when the id is not recorded or the object is not in the archive.
        """
        artifact_id = artifact if isinstance(artifact, UUID) else artifact.artifact_id
        recorded = self._catalog.get_artifact(artifact_id)
        project_id = self.project.project_id
        if not self._catalog.transaction(
            f"Checking that artifact {artifact_id} is archived",
            lambda tx: _archivum.is_archived(tx, project_id, artifact_id),
        ):
            raise UnknownRecordError(f"Artifact {artifact_id} is not in the archive")
        return recorded

    def _read_verified(self, artifact: Artifact, write: Callable[[bytes], object]) -> int:
        """Pass the object's bytes to *write* chunk by chunk, hashing and counting them; verify at the end."""
        key = _layout.object_key(self.project.name, artifact.scope, artifact.checksum)
        digest, size = _hash_object(self._store, key, write)
        if size != artifact.size:
            raise IntegrityError(f"Object {key} does not match its recorded size")
        if digest != artifact.checksum:
            raise IntegrityError(f"Object {key} does not match its recorded checksum")
        short = _layout.short_checksum(artifact.checksum)
        logger.info("got artifact %s: %s, %d bytes", artifact.artifact_id, short, size)
        return size

    def verify(self, *, sample: int | None = None, scope: str | None = None) -> Verification:
        """Re-read the archived objects of *scope* (default: the session's), compare them with their records, record it.

        Every live address of the scope (a registered object with no confirmed deletion after it) is judged:
        `missing` (absent, or gone between the listing and the read), `not_immediate` (a storage class that
        cannot be read now; not read), `unreadable` (a link or special file where the object belongs, or a read
        the store refused as an `IntegrityError`), `mismatch` (the SHA-256 or the size differs) or `passed`.
        With *sample* N (an integer of at least 1) only the N addresses whose latest verification finding, of any
        outcome, is oldest are judged, those never verified first, so repeated samples rotate through every object
        (a failing one included); otherwise all are.
        One check and one finding per judged address are recorded in one catalog transaction at the end; an
        `ArchiveUnavailable` propagates and records nothing. A scope of the project nothing was archived in records a
        check of 0 objects.
        `ValueError` for a bad *sample* or *scope*, before any I/O; `UnknownRecordError` for a scope the project lacks.
        """
        scope = self.scope if scope is None else check_name(scope, "scope")
        if sample is not None and (isinstance(sample, bool) or not isinstance(sample, int) or sample < 1):
            raise ValueError(f"The sample {sample!r} must be an integer of at least 1")
        project_id = self.project.project_id
        check_id = _ids.uuid7()

        def listing(tx: Connection) -> list[tuple[bytes, int]]:
            _archivum.check_scope(tx, self.project, scope)
            if sample is None:
                return _archivum.live_addresses(tx, project_id, scope)
            return _archivum.sample_addresses(tx, project_id, scope, sample)

        addresses = self._catalog.transaction(f"Listing the objects of scope {scope} to verify", listing)
        findings = tuple(self._judge(scope, checksum, size) for checksum, size in addresses)
        mode: Literal["full", "sample"] = "full" if sample is None else "sample"
        check = Verification(check_id, scope, mode, len(findings), findings, _ids.uuid7_time(check_id))
        times = [check.event_at, *(_ids.uuid7_time(finding.finding_id) for finding in findings)]
        self._catalog.transaction(
            f"Recording verification {check_id}",
            lambda tx: _archivum.insert_check(
                tx,
                project_id,
                check_id=check_id,
                scope=scope,
                kind="verification",
                mode=mode,
                objects_checked=len(findings),
                findings=findings,
            ),
            event_ats=times,
        )
        logger.info("verified scope %s (%s): %d checked, %d failed", scope, mode, len(findings), len(check.failed))
        sync_records(self._catalog, self._store)
        return check

    def reconcile(self, *, scope: str | None = None) -> Reconciliation:
        """Compare the store's objects in *scope* (default: the session's) with the live addresses, and record it.

        `object_without_entry`: an object no live registration names, older than `archive.incomplete_put_grace_hours`
        (an unparseable key under `_objects` too, with no checksum). `entry_without_object`: a live address the store
        does not hold. `unfinished_upload`: an interrupted put's leftovers, older than the grace. Younger objects and
        uploads are skipped and counted. The grace period is measured by the database's clock against the store's
        modification time. It only reports: unfinished uploads are listed, never aborted (`clean_incomplete` does
        that). An address the listing did not show is reported only if, asked again under its address lock after the
        listing, it is still live and the store still lacks its object, so a deletion in progress is not a finding.
        One check of kind `reconciliation` and a finding per disagreement are recorded in one transaction; records
        the Archivum holds that the store lacks are copied to it afterwards.
        `ValueError` for a bad *scope*; `UnknownRecordError` for a scope the project lacks, before any listing.
        """
        scope = self.scope if scope is None else check_name(scope, "scope")
        result = _reconcile.reconcile(self._catalog, self._store, self._settings.incomplete_put_grace_hours, scope)
        sync_records(self._catalog, self._store)
        return result

    def rebuild(self, *, since: datetime | None = None) -> Rebuild:
        """Restore, from the store alone, what a lost Archivum held: its deletion records, then its registrations.

        First the deletion records of `<project>/_records` the Archivum lacks are inserted under their ids and
        `event_at`; only then is every manifest without a registration, whose object exists, registered again
        under its artifact id, retention class and kind (no grace period; its dataset when the Archivum holds
        that id, else none, counted in `datasets_missing`; no origin, which a manifest never holds). The order
        matters: whether an object is archived compares the database's `recorded_at` of its registration with a
        confirmed deletion's, so a deletion restored after a registration would delete it again. The redactions and
        history-retention actions found are returned in `removals`, not applied (the recording role cannot; pass
        them to `freya.reapply_removals`). *since* (timezone-aware) limits what is decoded to the months from its
        month on; the whole `_records` and `_manifests` prefixes are still listed.
        Running it again changes nothing. The scopes of the project are the Archivum's: onboard them first
        (`UnknownRecordError` for a deletion record of another). `ValueError` for a naive *since*; `ArchiveFormatError`
        for a record or manifest that cannot be read.
        """
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be a timezone-aware datetime")
        return _rebuild.rebuild(self._catalog, self._store, since)

    def _judge(self, scope: str, checksum: bytes, size: int) -> Finding:
        key = _layout.object_key(self.project.name, scope, checksum)
        outcome = self._outcome(key, checksum, size)
        if outcome != PASSED:
            logger.warning(
                "verification of scope %s: object %s is %s",
                scope,
                _layout.short_checksum(checksum),
                outcome,
            )
        return Finding(_ids.uuid7(), scope, checksum, size, outcome)

    def _outcome(self, key: str, checksum: bytes, size: int) -> str:
        try:
            stored = self._store.head(key)
            if stored is None:
                return "missing"
            if not stored.immediate:
                return "not_immediate"
            digest, read = _hash_object(self._store, key)
        except IntegrityError:  # the store refused: a link or special file in the way, or gone since the head
            return "missing" if self._is_gone(key) else "unreadable"
        return "passed" if digest == checksum and read == size else "mismatch"

    def _is_gone(self, key: str) -> bool:
        try:
            return self._store.head(key) is None
        except IntegrityError:
            return False

    def _put(self, request: _Request, path: Path, size: int, sha256: bytes) -> Artifact:
        """Store *path* (*size* bytes hashing to *sha256*) and register it: object, manifest, artifact, registration."""
        project = self.project
        if request.explicit_id:  # a retry whose artifact was deleted since is refused before anything is stored
            self._catalog.transaction(
                f"Checking that artifact {request.artifact_id} was not deleted",
                lambda tx: _archivum.check_not_deleted(tx, project.project_id, request.artifact_id),
            )
        key = _layout.object_key(project.name, request.scope, sha256)
        stored_new = self._store.put_new(key, path, size, sha256)
        self._write_manifest(request, sha256, size)
        artifact = self._register_artifact(request, sha256, size)
        if not register_object(self._catalog, self._store, artifact, key, size, request.retention_class):
            # The object was deleted between the put and the registration: store it again, once.
            stored_new = self._store.put_new(key, path, size, sha256)
            if not register_object(self._catalog, self._store, artifact, key, size, request.retention_class):
                found = self._store.head(key)
                if found is not None:  # there, but not the bytes its address names
                    raise IntegrityError(f"Object {key} holds other bytes than its address")
                raise IntegrityError(f"Object {key} disappeared while it was being registered")
        logger.info(
            "archived %s (%d bytes) in scope %s, %s",
            _layout.short_checksum(sha256),
            size,
            request.scope,
            "object stored" if stored_new else "object already stored",
        )
        return artifact

    def _register_artifact(self, request: _Request, sha256: bytes, size: int) -> Artifact:
        """The artifact the put records: new, or the one the retried `record_id` names.

        A manifest carries no origin, so an artifact adopted from one has none: a retry that names an origin
        accepts it when everything else the catalog holds matches. Anything else that differs is Freya's refusal.
        """
        if request.explicit_id and request.origin is not None:
            dataset_id = None if request.dataset is None else request.dataset.dataset_id
            with suppress(UnknownRecordError):
                held = self._catalog.get_artifact(request.artifact_id)
                content = (held.kind, held.checksum, held.size, held.scope, held.dataset_id)
                if held.origin is None and content == (request.kind, sha256, size, request.scope, dataset_id):
                    return held
        return self._catalog.register_artifact(
            request.kind,
            sha256,
            size,
            origin=request.origin,
            dataset=request.dataset,
            scope=request.scope,
            record_id=request.artifact_id,
        )

    def _write_manifest(self, request: _Request, sha256: bytes, size: int) -> None:
        """Write the artifact's manifest once; one already stored under its id must be the same."""
        manifest = _layout.Manifest(
            artifact_id=request.artifact_id,
            event_at=_ids.uuid7_time(request.artifact_id),
            project=self.project.name,
            scope=request.scope,
            kind=request.kind,
            checksum=sha256,
            size=size,
            dataset_id=None if request.dataset is None else request.dataset.dataset_id,
            retention_class=request.retention_class,
        )
        key = _layout.manifest_key(manifest.project, manifest.scope, manifest.artifact_id)
        if self._store.put_bytes_new(key, manifest.encode()):
            return
        if _layout.Manifest.decode(read_small(self._store, key)) != manifest:
            raise UlduarError(f"Artifact {manifest.artifact_id} already has a different manifest")
