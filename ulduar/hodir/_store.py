"""The archive's storage interface: what Hodir needs from a store, one key validator, and the choice of backend.

Input: an `ArchiveSettings`. Output: a `WritingStore` (put, head, read, list, abort an unfinished upload) or a
`DeletingStore` (adds the two deletion calls). The writing classes have no delete method at all, so the
credential that writes cannot delete, in the type system as well as in the permissions (hard rule 5). Two backends
implement it: a local directory (`_directory`) and an S3-compatible bucket (`_s3`). Private: Hodir's facade
imports it, a consumer never does. No message built here carries a credential's value (hard rule 11).
"""

from __future__ import annotations

import errno
import os
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, overload

from ulduar.config import ArchiveSettings
from ulduar.errors import ConfigurationError

MIB = 1024**2
# An object this size or larger is stored in parts; a part is never smaller than the minimum.
MULTIPART_THRESHOLD = 64 * MIB
MIN_PART_SIZE = 64 * MIB
MAX_PARTS = 10_000
CHUNK_SIZE = MIB
MAX_OBJECT_SIZE = 5 * 1024**4
MAX_KEY_BYTES = 1024
SHA256_BYTES = 32


@dataclass(frozen=True, slots=True)
class StoredObject:
    """An object the store holds: its key, size and last-modified time (UTC); `immediate` is False while it is cold."""

    key: str
    size: int
    modified: datetime
    immediate: bool


@dataclass(frozen=True, slots=True)
class UnfinishedUpload:
    """What an interrupted put left behind: S3 an unfinished multipart upload, a directory its temporary file."""

    key: str
    upload_id: str
    started: datetime


class WritingStore(Protocol):
    """Everything but deletion. A key is `/`-joined segments (`check_key`), relative to the store's own root."""

    def put_new(self, key: str, source: Path, size: int, sha256: bytes) -> bool:
        """Store the regular file *source* under *key* unless *key* exists: True when stored, False when it existed.

        Never overwrites. The bytes stored must hash to *sha256* and be *size* long, else `IntegrityError` and
        nothing stays stored under *key*.
        """
        ...

    def put_bytes_new(self, key: str, data: bytes) -> bool:
        """`put_new` for a small payload held in memory (a marker, a manifest, a record)."""
        ...

    def head(self, key: str) -> StoredObject | None:
        """The object at *key*, or None."""
        ...

    def read(self, key: str) -> AbstractContextManager[BinaryIO]:
        """The object's bytes as a stream; a missing *key* is an `IntegrityError`."""
        ...

    def list(self, prefix: str) -> Iterator[StoredObject]:
        """Every object whose key starts with *prefix* and `/` (all of them for ""), recursively, sorted by key."""
        ...

    def list_unfinished(self, prefix: str) -> Iterator[UnfinishedUpload]:
        """Every unfinished upload under *prefix*."""
        ...

    def abort_unfinished(self, upload: UnfinishedUpload) -> None:
        """Remove what one interrupted put left behind; an upload already gone is not an error."""
        ...


class DeletingStore(WritingStore, Protocol):
    """A writing store that can also delete; only the retention and purge paths are given one."""

    def delete_every_version(self, key: str) -> None:
        """Remove the object and, on a versioned bucket, every version and delete marker of exactly this key."""
        ...

    def any_version_left(self, key: str) -> bool:
        """True while any version (a delete marker does not count) of exactly this key remains."""
        ...


def check_key(key: str, *, allow_root: bool = False) -> str:
    """*key* when it is a valid object key; otherwise `ValueError` naming it. *allow_root* accepts "" (the root).

    A key is `/`-joined segments; no segment is empty, `.` or `..` or holds a backslash or a control character
    (NUL included); no leading or trailing `/`; at most `MAX_KEY_BYTES` bytes of UTF-8. Raised before any I/O, so a
    caller's name can never leave its prefix or root.
    """
    if allow_root and key == "":
        return key
    problem = _key_problem(key)
    if problem:
        raise ValueError(f"The object key {key!r} {problem}")
    return key


def _key_problem(key: object) -> str | None:
    if not isinstance(key, str):
        return "must be a string"
    try:
        encoded = key.encode("utf-8")
    except UnicodeEncodeError:
        return "is not valid UTF-8"
    if not 0 < len(encoded) <= MAX_KEY_BYTES:
        return f"must be 1 to {MAX_KEY_BYTES} bytes long"
    if "\\" in key or any(unicodedata.category(char) == "Cc" for char in key):
        return "must not hold a backslash or a control character"
    if any(segment in ("", ".", "..") for segment in key.split("/")):
        return "must not hold an empty segment, `.` or `..`"
    return None


def check_size(size: int) -> int:
    """*size* when an object can have it; otherwise `ValueError`, before any request."""
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_OBJECT_SIZE:
        raise ValueError(f"The object size {size!r} must be an integer from 0 to {MAX_OBJECT_SIZE} bytes")
    return size


def part_size(size: int) -> int:
    """The size of each part of a multipart put of *size* bytes: whole MiB, at least `MIN_PART_SIZE`."""
    check_size(size)
    return max(MIN_PART_SIZE, ceil(ceil(size / MAX_PARTS) / MIB) * MIB)


def open_regular(source: Path) -> BinaryIO:
    """Open *source* for reading when it is a regular file that is not a symbolic link; `ValueError` otherwise."""
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError(f"The source {source} is a symbolic link, not a regular file") from e
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"The source {source} is not a regular file")
    return os.fdopen(fd, "rb")


@overload
def open_store(settings: ArchiveSettings, *, deleting: Literal[False] = False) -> WritingStore: ...
@overload
def open_store(settings: ArchiveSettings, *, deleting: Literal[True]) -> DeletingStore: ...
def open_store(settings: ArchiveSettings, *, deleting: bool = False) -> WritingStore | DeletingStore:
    """The store *settings* names; a deleting one only when *deleting* (S3: with the `deleter` credential)."""
    if settings.backend == "directory":
        from ulduar.hodir._directory import open_directory_store

        return open_directory_store(settings, deleting=deleting)
    try:
        from ulduar.hodir._s3 import open_s3_store
    except ImportError as e:
        raise ConfigurationError("The s3 backend needs the extra project-ulduar[s3] (boto3)") from e
    return open_s3_store(settings, deleting=deleting)
