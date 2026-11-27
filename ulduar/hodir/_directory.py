"""The local-directory backend of the storage interface: an archive kept as files under one root directory.

Input: the `ArchiveSettings` of a `directory` archive. Output: a `DirectoryStore` (writing) or a
`DirectoryDeletingStore`. An object is written to a temporary file `.tmp-<hex>` beside its final name, flushed to
disk and then hard-linked into place, so the final name never holds a partial file and an existing name is never
replaced. No symbolic link below the root is ever followed: every segment of a key is opened relative to its parent
directory with `O_NOFOLLOW`, and a link is an `IntegrityError`. Private: reached through `open_store` only.

NOTE: the mode `0o444` of a stored file is the weaker guarantee of this backend: the process that wrote a file
can still remove it, so the immutability of a directory archive rests on that process, not on the store itself.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

from ulduar.config import ArchiveSettings
from ulduar.errors import ArchiveUnavailable, ConfigurationError, IntegrityError
from ulduar.hodir._store import (
    CHUNK_SIZE,
    DeletingStore,
    PendingUpload,
    StoredObject,
    WritingStore,
    check_key,
    check_size,
    open_regular,
)

logger = logging.getLogger(__name__)

TEMP_PREFIX = ".tmp-"
DIRECTORY_MODE = 0o755
FILE_MODE = 0o444
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# O_NONBLOCK: opening a named pipe that was put where an object should be must not hang; `fstat` then refuses it.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_NOT_A_DIRECTORY = (errno.ELOOP, errno.ENOTDIR)
# (key, status) of the regular files found by a scan.
_Found = list[tuple[str, os.stat_result]]


def _not_regular(key: str) -> IntegrityError:
    return IntegrityError(
        f"Object {key} is not stored as a regular file: a directory, link or special file is in its way"
    )


@contextmanager
def _translated(operation: str, key: str) -> Iterator[None]:
    """Re-raise an operating-system error as `ArchiveUnavailable`, worded `<operation> <key> failed: <reason>`."""
    try:
        yield
    except OSError as e:
        raise ArchiveUnavailable(f"{operation} {key} failed: {e.strerror or type(e).__name__}") from e


def _step(parent: int, name: str, key: str, create: bool) -> int | None:
    """The directory *name* opened under the directory descriptor *parent*, created when *create*, else None if absent.

    A symbolic link is never followed (`IntegrityError`); a file where a directory belongs is absent for a lookup.
    """
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            return None
    except OSError as e:
        if e.errno not in _NOT_A_DIRECTORY:
            raise
        if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
            raise _not_regular(key) from e
        if not create:
            return None
        raise
    try:
        os.mkdir(name, DIRECTORY_MODE, dir_fd=parent)
    except FileExistsError:  # another writer made it first
        pass
    else:
        os.fsync(parent)  # the new directory entry survives a crash
    return _step(parent, name, key, create=False)


def _lstat(directory: int, name: str, key: str) -> os.stat_result | None:
    """The status of the regular file *name* in *directory*; None when absent.

    A directory, symbolic link or special file at the name is an `IntegrityError`, as `read` reports it.
    """
    try:
        status = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise _not_regular(key)
    return status


def _stored(key: str, status: os.stat_result) -> StoredObject:
    return StoredObject(key, status.st_size, datetime.fromtimestamp(status.st_mtime, UTC), True)


def _split(key: str) -> tuple[list[str], str]:
    """(directory segments, file name) of a valid *key*; no segment may look like a temporary file."""
    segments = check_key(key).split("/")
    if any(segment.startswith(TEMP_PREFIX) for segment in segments):
        raise ValueError(f"The object key {key!r} must not start a segment with {TEMP_PREFIX!r}")
    return segments[:-1], segments[-1]


class _Reader:
    """An object's open file as `read` hands it out: only `read` is offered, and its operating-system errors are
    `ArchiveUnavailable`. An error raised by the caller's own code around it (a full destination disk) is not."""

    def __init__(self, handle: BinaryIO, key: str) -> None:
        self._handle = handle
        self._key = key

    def read(self, size: int = -1) -> bytes:
        with _translated("Reading", self._key):
            return self._handle.read(size)


class DirectoryStore:
    """A writing store over the directory *base* and the key prefix *prefix* below it (the store's own root)."""

    def __init__(self, base: Path, prefix: str = "") -> None:
        self._base = base
        self._prefix = [segment for segment in prefix.split("/") if segment]

    def create_root(self) -> None:
        """Create the prefix directories below the base that are not there yet."""
        with _translated("Opening", str(self._base)), self._directory([], "", create=True):
            pass

    def _descend(self, segments: Sequence[str], key: str, create: bool) -> int | None:
        parent = os.open(self._base, os.O_RDONLY | os.O_DIRECTORY)
        for segment in [*self._prefix, *segments]:
            try:
                child = _step(parent, segment, key, create)
            finally:
                os.close(parent)
            if child is None:
                return None
            parent = child
        return parent

    @contextmanager
    def _directory(self, segments: Sequence[str], key: str, *, create: bool) -> Iterator[int | None]:
        """A descriptor of the directory holding *key*'s file (None when absent and not *create*), closed on exit."""
        descriptor = self._descend(segments, key, create)
        try:
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def put_new(self, key: str, source: Path, size: int, sha256: bytes) -> bool:
        directory, name = _split(key)
        check_size(size)
        with open_regular(source) as handle:
            chunks = iter(lambda: handle.read(CHUNK_SIZE), b"")
            return self._put(key, directory, name, chunks, size, sha256)

    def put_bytes_new(self, key: str, data: bytes) -> bool:
        directory, name = _split(key)
        return self._put(key, directory, name, iter([data]), len(data), hashlib.sha256(data).digest())

    def _put(
        self, key: str, directory: Sequence[str], name: str, chunks: Iterable[bytes], size: int, sha256: bytes
    ) -> bool:
        with _translated("Storing", key), self._directory(directory, key, create=True) as parent:
            assert parent is not None
            if _lstat(parent, name, key) is not None:
                return False
            temporary = TEMP_PREFIX + uuid.uuid4().hex
            try:
                self._write_temporary(parent, temporary, key, chunks, size, sha256)
                try:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    return False
                os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
            finally:
                with suppress(OSError):  # best effort: the temporary file is gone after a successful link
                    os.unlink(temporary, dir_fd=parent)
            logger.debug("stored object %s (%d bytes)", key, size)
            return True

    @staticmethod
    def _write_temporary(
        parent: int, temporary: str, key: str, chunks: Iterable[bytes], size: int, sha256: bytes
    ) -> None:
        """Write *chunks* to the new file *temporary*, check size and hash, and flush it to disk."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(temporary, flags, FILE_MODE, dir_fd=parent), "wb") as out:
            digest = hashlib.sha256()
            written = 0
            for chunk in chunks:
                out.write(chunk)
                digest.update(chunk)
                written += len(chunk)
            if written != size or digest.digest() != sha256:
                raise IntegrityError(f"Object {key} does not match its recorded checksum and size: nothing was stored")
            out.flush()
            os.fsync(out.fileno())

    def head(self, key: str) -> StoredObject | None:
        directory, name = _split(key)
        with _translated("Reading", key), self._directory(directory, key, create=False) as parent:
            status = None if parent is None else _lstat(parent, name, key)
            return None if status is None else _stored(key, status)

    def read(self, key: str) -> AbstractContextManager[BinaryIO]:
        directory, name = _split(key)
        return self._read(key, directory, name)

    @contextmanager
    def _read(self, key: str, directory: Sequence[str], name: str) -> Iterator[BinaryIO]:
        with _translated("Reading", key), self._directory(directory, key, create=False) as parent:
            handle = self._open_file(parent, name, key)
        with handle:  # outside the translation: an error of the caller's own code is not the store's
            yield cast(BinaryIO, _Reader(handle, key))

    @staticmethod
    def _open_file(parent: int | None, name: str, key: str) -> BinaryIO:
        missing = IntegrityError(f"Object {key} is missing from the archive")
        if parent is None:
            raise missing
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            raise missing from None
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise _not_regular(key) from e
            raise
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise _not_regular(key)
        return os.fdopen(descriptor, "rb")

    def list(self, prefix: str) -> Iterator[StoredObject]:
        check_key(prefix, allow_root=True)
        found = [_stored(key, status) for key, status in self._files(prefix) if not _is_temporary(key)]
        return iter(sorted(found, key=lambda stored: stored.key))

    def list_unfinished(self, prefix: str) -> Iterator[PendingUpload]:
        check_key(prefix, allow_root=True)
        found = [
            PendingUpload(key, "", datetime.fromtimestamp(status.st_mtime, UTC))
            for key, status in self._files(prefix)
            if _is_temporary(key)
        ]
        return iter(sorted(found, key=lambda upload: upload.key))

    def abort_unfinished(self, upload: PendingUpload) -> None:
        """Remove the temporary file; refuses any key that is not one, so a stored object can never go this way."""
        segments = check_key(upload.key).split("/")
        if not segments[-1].startswith(TEMP_PREFIX):
            raise ValueError(f"The key {upload.key!r} is not an unfinished upload")
        with _translated("Aborting", upload.key), self._directory(segments[:-1], upload.key, create=False) as parent:
            if parent is not None:
                with suppress(FileNotFoundError):
                    os.unlink(segments[-1], dir_fd=parent)

    def _files(self, prefix: str) -> _Found:
        """(key, status) of every regular file under *prefix*, temporary files included."""
        segments = prefix.split("/") if prefix else []
        found: _Found = []
        with _translated("Listing", prefix or "the archive root"):
            with self._directory(segments, prefix, create=False) as top:
                if top is not None:
                    self._scan(top, f"{prefix}/" if prefix else "", found)
        return found

    def _scan(self, directory: int, key_prefix: str, found: _Found) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                key = key_prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    child = _step(directory, entry.name, key, create=False)
                    if child is not None:
                        try:
                            self._scan(child, key + "/", found)
                        finally:
                            os.close(child)
                elif entry.is_file(follow_symlinks=False):
                    found.append((key, entry.stat(follow_symlinks=False)))
                else:
                    logger.warning("skipped %s: a symbolic link or special file is not an archived object", key)


def _is_temporary(key: str) -> bool:
    return key.rsplit("/", 1)[-1].startswith(TEMP_PREFIX)


class DirectoryDeletingStore(DirectoryStore):
    """A `DirectoryStore` that can also delete."""

    def delete_every_version(self, key: str) -> None:
        directory, name = _split(key)
        with _translated("Deleting", key), self._directory(directory, key, create=False) as parent:
            if parent is not None:
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=parent)
                os.fsync(parent)

    def any_version_left(self, key: str) -> bool:
        return self.head(key) is not None


def open_directory_store(settings: ArchiveSettings, *, deleting: bool) -> WritingStore | DeletingStore:
    """The directory store of *settings*: its `path` must be an existing directory; the prefix below it is created."""
    path = settings.path
    if path is None or not path.is_dir():
        raise ConfigurationError(f"Archive directory {path} does not exist or is not a directory")
    store = DirectoryDeletingStore(path, settings.prefix) if deleting else DirectoryStore(path, settings.prefix)
    store.create_root()
    return store
