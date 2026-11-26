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
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from ulduar import _ids
from ulduar.config import ArchiveSettings, Config, check_name
from ulduar.errors import ConfigurationError, IntegrityError, UlduarError, UnknownRecordError
from ulduar.freya import Artifact, Catalog, Connection, Dataset, Project, open_catalog
from ulduar.freya.catalog import check_kind, check_origin
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._store import CHUNK_SIZE, WritingStore, open_regular, open_store

logger = logging.getLogger(__name__)

BYTES_PER_GIB = 1024**3
# What is read back of the marker and of a manifest: both are small, a larger object is not one.
_SMALL_OBJECT_LIMIT = 1024**2
_SHORT_CHECKSUM_HEX = 12


def _read_small(store: WritingStore, key: str) -> bytes:
    with store.read(key) as stream:
        return stream.read(_SMALL_OBJECT_LIMIT + 1)[:_SMALL_OBJECT_LIMIT]


def _ensure_format(store: WritingStore) -> None:
    """Write the format marker and the layout text on first use, then check the marker the store holds."""
    if store.head(_layout.FORMAT_MARKER_KEY) is None:
        store.put_bytes_new(_layout.FORMAT_MARKER_KEY, _layout.marker_bytes())
        store.put_bytes_new(_layout.LAYOUT_KEY, _layout.LAYOUT_TEXT.encode("utf-8"))
    _layout.check_marker(_read_small(store, _layout.FORMAT_MARKER_KEY))


@contextmanager
def open_archive(config: Config) -> Iterator[Archive]:
    """Open the archive of `[archive]` for writing, with a catalog session on the recording role.

    `ConfigurationError` without an `[archive]` section; `ArchiveFormatError` when the store's format marker is
    foreign or of another version (a new store gets its marker and layout text); the catalog session's errors
    as `freya.open_catalog` raises them. The catalog connection closes on exit.
    """
    if config.archive is None:
        raise ConfigurationError("Configuration key `archive` is required to use the archive")
    with open_catalog(config) as catalog:
        store = open_store(config.archive)
        _ensure_format(store)
        yield Archive(catalog, store, config.archive)


@dataclass(frozen=True, slots=True)
class _Request:
    """What one put records, validated before any I/O."""

    kind: str
    dataset: Dataset | None
    origin: str | None
    scope: str
    retention_class: str | None
    artifact_id: UUID


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
        if not self._open:
            raise UlduarError("The stream is closed — nothing was stored")
        self._close()
        self._artifact = self._store(self._path, self._size, self._hash.digest())
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
        return _Request(kind, dataset, origin, scope, retention_class, artifact_id)

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
        argument or a source that is not a regular file (a symbolic link included), before any storage I/O;
        `UlduarError` when *record_id* names an artifact with other content, manifest or retention class.
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

        The bytes are written to a partial file beside the destination (`.<name>.ulduar-partial-<hex>`, mode 0o666
        under the umask), hashed while written, and the destination appears, by a hard link, only after they match
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
            partial = f".{dest.name}.ulduar-partial-{uuid4().hex}"
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
        digest = hashlib.sha256()
        size = 0
        with self._store.read(key) as stream:
            while chunk := stream.read(CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
                write(chunk)
        if size != artifact.size:
            raise IntegrityError(f"Object {key} does not match its recorded size")
        if digest.digest() != artifact.checksum:
            raise IntegrityError(f"Object {key} does not match its recorded checksum")
        short = artifact.checksum.hex()[:_SHORT_CHECKSUM_HEX]
        logger.info("got artifact %s: %s, %d bytes", artifact.artifact_id, short, size)
        return size

    def _put(self, request: _Request, path: Path, size: int, sha256: bytes) -> Artifact:
        """Store *path* (*size* bytes hashing to *sha256*) and register it: object, manifest, artifact, registration."""
        project = self.project
        key = _layout.object_key(project.name, request.scope, sha256)
        stored_new = self._store.put_new(key, path, size, sha256)
        self._write_manifest(request, sha256, size)
        artifact = self._catalog.register_artifact(
            request.kind,
            sha256,
            size,
            origin=request.origin,
            dataset=request.dataset,
            scope=request.scope,
            record_id=request.artifact_id,
        )
        if not self._register(artifact, key, size, request.retention_class):
            # The object was deleted between the put and the registration: store it again, once.
            stored_new = self._store.put_new(key, path, size, sha256)
            if not self._register(artifact, key, size, request.retention_class):
                raise IntegrityError(f"Object {key} disappeared while it was being registered")
        logger.info(
            "archived %s (%d bytes) in scope %s, %s",
            sha256.hex()[:_SHORT_CHECKSUM_HEX],
            size,
            request.scope,
            "object stored" if stored_new else "object already stored",
        )
        return artifact

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
        if _layout.Manifest.decode(_read_small(self._store, key)) != manifest:
            raise UlduarError(f"Artifact {manifest.artifact_id} already has a different manifest")

    def _register(self, artifact: Artifact, key: str, size: int, retention_class: str | None) -> bool:
        """Record the registration under the address lock if the object is there; False when it is not."""
        project_id = self.project.project_id

        def work(tx: Connection) -> bool:
            _archivum.lock_address(tx, project_id, artifact.scope, artifact.checksum)
            stored = self._store.head(key)
            if stored is None or stored.size != size:
                return False
            _archivum.insert_registration(tx, project_id, artifact.scope, artifact, retention_class)
            return True

        return self._catalog.transaction(f"Registering archived artifact {artifact.artifact_id}", work)
