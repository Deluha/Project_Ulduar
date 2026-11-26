"""Hodir's storage interface: one contract, checked on a real directory and on the local object store.

Every contract test runs over the directory backend, an unversioned bucket and a versioned bucket; each backend's
own behaviour (symbolic links, unfinished uploads, deletion of versions, the transport guard, the credential
handling) has its own tests. No mock: the store under test is built by `open_store` from the fixtures' settings.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import math
import os
import secrets
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dev_tools.services import (
    S3_DELETER_SECRET_KEY_VARIABLE,
    S3_ENDPOINT_VARIABLE,
    S3_WRITER_ACCESS_KEY_VARIABLE,
    S3_WRITER_SECRET_KEY_VARIABLE,
)
from tests.conftest import S3Settings
from ulduar.config import ArchiveSettings
from ulduar.errors import ArchiveUnavailable, ConfigurationError, IntegrityError
from ulduar.hodir import _store
from ulduar.hodir._s3 import is_immediate
from ulduar.hodir._store import (
    CHUNK_SIZE,
    MAX_OBJECT_SIZE,
    MIB,
    MIN_PART_SIZE,
    MULTIPART_THRESHOLD,
    PendingUpload,
    StoredObject,
    check_key,
    open_store,
    part_size,
)

BACKENDS = [
    pytest.param("directory", id="directory"),
    pytest.param("s3", id="s3", marks=pytest.mark.s3),
    pytest.param("s3-versioned", id="s3-versioned", marks=pytest.mark.s3),
]
SETTINGS_FIXTURES = {
    "directory": "archive_settings_dir",
    "s3": "archive_settings_s3",
    "s3-versioned": "archive_settings_s3_versioned",
}
S3_MINIMUM_PART = 5 * MIB
BAD_KEYS = ["", "..", "a/../b", ".", "a/./b", "a//b", "/a", "a/", "a\\b", "a\x00b", "a\nb", "a\x7fb", "x" * 1025]
FLOOR = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture(params=BACKENDS)
def settings(request: pytest.FixtureRequest) -> ArchiveSettings:
    """The settings of each backend in turn; only the object-store cases are marked `s3`."""
    found: ArchiveSettings = request.getfixturevalue(SETTINGS_FIXTURES[request.param])
    return found


def blob(directory: Path, data: bytes, name: str = "source.bin") -> tuple[Path, int, bytes]:
    """(path, size, sha256) of a source file holding *data*."""
    path = directory / name
    path.write_bytes(data)
    return path, len(data), hashlib.sha256(data).digest()


def stored_bytes(store: Any, key: str) -> bytes:
    with store.read(key) as stream:
        return bytes(stream.read())


def test_put_new_stores_once_and_never_overwrites(settings: ArchiveSettings, tmp_path: Path) -> None:
    store = open_store(settings)
    path, size, digest = blob(tmp_path, b"vault")
    assert store.put_new("a/one", path, size, digest) is True
    assert stored_bytes(store, "a/one") == b"vault"
    assert store.put_new("a/one", path, size, digest) is False  # the same bytes
    other, other_size, other_digest = blob(tmp_path, b"different", "other.bin")
    assert store.put_new("a/one", other, other_size, other_digest) is False
    assert stored_bytes(store, "a/one") == b"vault"


def test_put_bytes_new_stores_once_and_never_overwrites(settings: ArchiveSettings) -> None:
    store = open_store(settings)
    assert store.put_bytes_new("marker", b"v1") is True
    assert store.put_bytes_new("marker", b"v1") is False
    assert store.put_bytes_new("marker", b"v2") is False
    assert stored_bytes(store, "marker") == b"v1"
    assert store.put_bytes_new("empty", b"") is True
    assert stored_bytes(store, "empty") == b""


def test_an_object_of_several_chunks_round_trips(settings: ArchiveSettings, tmp_path: Path) -> None:
    store = open_store(settings)
    data = os.urandom(CHUNK_SIZE * 2 + 17)
    path, size, digest = blob(tmp_path, data)
    assert store.put_new("big", path, size, digest) is True
    assert stored_bytes(store, "big") == data


@pytest.mark.parametrize("flaw", ["checksum", "short", "long"])
def test_a_wrong_checksum_or_size_stores_nothing(settings: ArchiveSettings, tmp_path: Path, flaw: str) -> None:
    store = open_store(settings)
    path, size, digest = blob(tmp_path, b"vault")
    claim = {
        "checksum": (size, hashlib.sha256(b"other").digest()),
        "short": (size - 1, digest),
        "long": (size + 1, digest),
    }[flaw]
    with pytest.raises(IntegrityError):
        store.put_new("a/one", path, *claim)
    assert store.head("a/one") is None
    assert list(store.list("")) == []
    assert list(store.list_unfinished("")) == []  # a directory leaves no temporary file behind


def test_a_source_that_is_not_a_regular_file_is_refused(settings: ArchiveSettings, tmp_path: Path) -> None:
    store = open_store(settings)
    path, size, digest = blob(tmp_path, b"vault")
    link = tmp_path / "link.bin"
    link.symlink_to(path)
    for source in (link, tmp_path):
        with pytest.raises(ValueError, match=str(source)):
            store.put_new("a/one", source, size, digest)
    assert store.head("a/one") is None


def test_an_oversized_object_is_refused_before_any_request(settings: ArchiveSettings, tmp_path: Path) -> None:
    store = open_store(settings)
    path, _, digest = blob(tmp_path, b"vault")
    for size in (MAX_OBJECT_SIZE + 1, -1):
        with pytest.raises(ValueError, match=str(size)):
            store.put_new("a/one", path, size, digest)


def test_part_size_is_whole_mebibytes_within_the_part_limit() -> None:
    assert part_size(0) == part_size(MULTIPART_THRESHOLD) == MIN_PART_SIZE
    largest = part_size(MAX_OBJECT_SIZE)
    assert largest % MIB == 0 and largest * _store.MAX_PARTS >= MAX_OBJECT_SIZE
    assert largest == math.ceil(MAX_OBJECT_SIZE / _store.MAX_PARTS / MIB) * MIB
    with pytest.raises(ValueError):
        part_size(MAX_OBJECT_SIZE + 1)


@pytest.fixture
def small_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multipart from 5 MiB (the object store's minimum part size) instead of 64 MiB."""
    monkeypatch.setattr(_store, "MULTIPART_THRESHOLD", S3_MINIMUM_PART)
    monkeypatch.setattr(_store, "MIN_PART_SIZE", S3_MINIMUM_PART)


@pytest.mark.usefixtures("small_parts")
def test_a_multipart_object_is_stored_and_read_back(settings: ArchiveSettings, tmp_path: Path) -> None:
    store = open_store(settings)
    data = os.urandom(S3_MINIMUM_PART * 2 + 12345)  # two full parts and a short last one
    path, size, digest = blob(tmp_path, data)
    assert store.put_new("big/object", path, size, digest) is True
    assert stored_bytes(store, "big/object") == data
    found = store.head("big/object")
    assert found is not None and found.size == size
    assert list(store.list_unfinished("")) == []


@pytest.mark.usefixtures("small_parts")
def test_a_multipart_put_onto_an_existing_key_leaves_nothing_unfinished(
    settings: ArchiveSettings, tmp_path: Path
) -> None:
    store = open_store(settings)
    assert store.put_bytes_new("big/object", b"first") is True
    path, size, digest = blob(tmp_path, os.urandom(S3_MINIMUM_PART + 1))
    assert store.put_new("big/object", path, size, digest) is False
    assert stored_bytes(store, "big/object") == b"first"
    assert list(store.list_unfinished("")) == []


@pytest.mark.usefixtures("small_parts")
def test_a_multipart_put_that_loses_the_race_leaves_nothing_unfinished(
    settings: ArchiveSettings, tmp_path: Path
) -> None:
    # Eight writers of one key at once: whoever loses at completion aborts its own upload.
    data = os.urandom(S3_MINIMUM_PART + 1)
    path, size, digest = blob(tmp_path, data)
    results = _race(settings, lambda store: store.put_new("big/object", path, size, digest))
    assert results.count(True) == 1 and results.count(False) == 7
    store = open_store(settings)
    assert stored_bytes(store, "big/object") == data
    assert list(store.list_unfinished("")) == []


@pytest.mark.usefixtures("small_parts")
@pytest.mark.parametrize("flaw", ["checksum", "size"])
def test_a_multipart_put_with_a_wrong_checksum_or_size_stores_nothing(
    settings: ArchiveSettings, tmp_path: Path, flaw: str
) -> None:
    store = open_store(settings)
    path, size, digest = blob(tmp_path, os.urandom(S3_MINIMUM_PART + 1))
    claim = (size, hashlib.sha256(b"other").digest()) if flaw == "checksum" else (size + 1, digest)
    with pytest.raises(IntegrityError):
        store.put_new("big/object", path, *claim)
    assert store.head("big/object") is None
    assert list(store.list_unfinished("")) == []


def _race(settings: ArchiveSettings, action: Any, writers: int = 8) -> list[bool]:
    """Run *action* on *writers* stores of *settings* at once; every outcome."""
    with ThreadPoolExecutor(max_workers=writers) as pool:
        futures = [pool.submit(action, open_store(settings)) for _ in range(writers)]
        return [future.result() for future in futures]


def test_only_one_of_several_simultaneous_writers_wins(settings: ArchiveSettings) -> None:
    outcomes = _race(settings, lambda store: store.put_bytes_new("race", secrets.token_bytes(64)))
    assert outcomes.count(True) == 1 and outcomes.count(False) == 7
    assert list(open_store(settings).list_unfinished("")) == []


def test_head_reports_size_and_time(settings: ArchiveSettings) -> None:
    store = open_store(settings)
    assert store.head("a/one") is None
    store.put_bytes_new("a/one", b"vault")
    found = store.head("a/one")
    assert found is not None
    assert (found.key, found.size, found.immediate) == ("a/one", 5, True)
    assert found.modified.tzinfo is not None and found.modified.utcoffset() == UTC.utcoffset(None)
    assert FLOOR < found.modified <= datetime.now(UTC)


def test_read_of_a_missing_key_is_an_integrity_error(settings: ArchiveSettings) -> None:
    store = open_store(settings)
    for key in ("absent", "a/absent"):
        with pytest.raises(IntegrityError, match=f"Object {key} is missing from the archive"):
            with store.read(key):
                pass
    store.put_bytes_new("a/one", b"x")
    with pytest.raises(IntegrityError, match="a/one/deeper is missing"):
        with store.read("a/one/deeper"):
            pass


def test_list_is_recursive_sorted_and_bounded_by_the_prefix(settings: ArchiveSettings) -> None:
    store = open_store(settings)
    keys = ["a/b/two", "a/one", "a.txt", "a/b/one", "ab/x", "z", "a/bc/x"]
    for key in keys:
        store.put_bytes_new(key, key.encode())
    everything = list(store.list(""))
    assert [item.key for item in everything] == sorted(keys)
    assert all(isinstance(item, StoredObject) and item.size == len(item.key) for item in everything)
    assert [item.key for item in store.list("a")] == ["a/b/one", "a/b/two", "a/bc/x", "a/one"]
    assert [item.key for item in store.list("a/b")] == ["a/b/one", "a/b/two"]
    assert list(store.list("a/b/one")) == []  # an object is not a prefix
    assert list(store.list("nothing/here")) == []


def test_list_excludes_unfinished_uploads_of_a_directory(archive_settings_dir: ArchiveSettings) -> None:
    store = open_store(archive_settings_dir)
    store.put_bytes_new("a/one", b"x")
    root = archive_settings_dir.path
    assert root is not None
    (root / archive_settings_dir.prefix / "a" / ".tmp-abc").write_bytes(b"partial")
    assert [item.key for item in store.list("")] == ["a/one"]
    assert [item.key for item in store.list("a")] == ["a/one"]


def test_a_stray_temporary_file_is_an_unfinished_upload_of_a_directory(archive_settings_dir: ArchiveSettings) -> None:
    store = open_store(archive_settings_dir)
    store.put_bytes_new("a/one", b"x")
    root = archive_settings_dir.path
    assert root is not None
    stray = root / archive_settings_dir.prefix / "a" / ".tmp-abc"
    stray.write_bytes(b"partial")
    unfinished = list(store.list_unfinished(""))
    assert [(u.key, u.upload_id) for u in unfinished] == [("a/.tmp-abc", "")]
    assert FLOOR < unfinished[0].started <= datetime.now(UTC)
    assert [u.key for u in store.list_unfinished("a")] == ["a/.tmp-abc"]
    assert list(store.list_unfinished("b")) == []
    with pytest.raises(ValueError, match="not an unfinished upload"):  # a stored object can never go this way
        store.abort_unfinished(PendingUpload("a/one", "", FLOOR))
    store.abort_unfinished(unfinished[0])
    store.abort_unfinished(unfinished[0])  # already gone: not an error
    assert not stray.exists() and list(store.list_unfinished("")) == []
    assert stored_bytes(store, "a/one") == b"x"


def test_an_unfinished_multipart_upload_is_listed_and_aborted(
    archive_settings_s3: ArchiveSettings, s3_client: Any
) -> None:
    store = open_store(archive_settings_s3)
    bucket, prefix = archive_settings_s3.bucket, archive_settings_s3.prefix
    created = s3_client.create_multipart_upload(Bucket=bucket, Key=f"{prefix}/a/big")
    s3_client.create_multipart_upload(Bucket=bucket, Key="outside/the/prefix")
    unfinished = list(store.list_unfinished(""))
    assert [(u.key, u.upload_id) for u in unfinished] == [("a/big", created["UploadId"])]
    assert FLOOR < unfinished[0].started <= datetime.now(UTC)
    assert [u.key for u in store.list_unfinished("a")] == ["a/big"]
    assert list(store.list_unfinished("b")) == []
    store.abort_unfinished(unfinished[0])
    store.abort_unfinished(unfinished[0])  # already gone: not an error
    assert list(store.list_unfinished("")) == []


PREFIXES = ["", "ulduar", "one/two"]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_the_store_prefix_is_never_seen_by_the_caller(settings: ArchiveSettings, prefix: str) -> None:
    store = open_store(replace(settings, prefix=prefix))
    store.put_bytes_new("a/one", b"x")
    store.put_bytes_new("two", b"y")
    assert [item.key for item in store.list("")] == ["a/one", "two"]
    assert [item.key for item in store.list("a")] == ["a/one"]
    assert store.head("a/one") is not None


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_directory_object_lies_under_the_prefix(archive_settings_dir: ArchiveSettings, prefix: str) -> None:
    open_store(replace(archive_settings_dir, prefix=prefix)).put_bytes_new("a/one", b"x")
    assert archive_settings_dir.path is not None
    assert (archive_settings_dir.path / prefix / "a" / "one").read_bytes() == b"x"


@pytest.mark.parametrize("prefix", PREFIXES)
def test_an_s3_object_lies_under_the_prefix(archive_settings_s3: ArchiveSettings, s3_client: Any, prefix: str) -> None:
    open_store(replace(archive_settings_s3, prefix=prefix)).put_bytes_new("a/one", b"x")
    listed = s3_client.list_objects_v2(Bucket=archive_settings_s3.bucket)["Contents"]
    assert [item["Key"] for item in listed] == [f"{prefix}/a/one" if prefix else "a/one"]


def test_a_deleting_store_is_the_only_one_that_can_delete(settings: ArchiveSettings) -> None:
    writing = open_store(settings)
    deleting = open_store(settings, deleting=True)
    for name in ("delete_every_version", "any_version_left"):
        assert not hasattr(writing, name)
        assert hasattr(deleting, name)


def test_delete_every_version_removes_the_object(settings: ArchiveSettings) -> None:
    store = open_store(settings, deleting=True)
    assert store.any_version_left("a/one") is False
    store.delete_every_version("a/one")  # nothing there: not an error
    store.put_bytes_new("a/one", b"x")
    store.put_bytes_new("a/two", b"y")
    assert store.any_version_left("a/one") is True
    store.delete_every_version("a/one")
    assert store.any_version_left("a/one") is False
    assert store.head("a/one") is None
    assert [item.key for item in store.list("")] == ["a/two"]  # only that key went


def test_delete_every_version_removes_versions_and_delete_markers(
    archive_settings_s3_versioned: ArchiveSettings, s3_client: Any
) -> None:
    settings = archive_settings_s3_versioned
    store = open_store(settings, deleting=True)
    full = f"{settings.prefix}/a/one"
    other = f"{settings.prefix}/a/one-more"
    store.put_bytes_new("a/one", b"first")
    s3_client.delete_object(Bucket=settings.bucket, Key=full)  # a delete marker on top of version one
    assert store.head("a/one") is None
    assert store.put_bytes_new("a/one", b"second") is True  # version two, above the marker
    s3_client.put_object(Bucket=settings.bucket, Key=other, Body=b"keep")
    store.delete_every_version("a/one")
    assert store.any_version_left("a/one") is False
    listed = s3_client.list_object_versions(Bucket=settings.bucket)
    assert [item["Key"] for item in listed.get("Versions", [])] == [other]
    assert listed.get("DeleteMarkers", []) == []


def test_a_delete_marker_alone_is_not_a_version_left(
    archive_settings_s3_versioned: ArchiveSettings, s3_client: Any
) -> None:
    settings = archive_settings_s3_versioned
    store = open_store(settings, deleting=True)
    store.put_bytes_new("a/one", b"x")
    full = f"{settings.prefix}/a/one"
    version = s3_client.list_object_versions(Bucket=settings.bucket, Prefix=full)["Versions"][0]["VersionId"]
    s3_client.delete_object(Bucket=settings.bucket, Key=full)
    assert store.any_version_left("a/one") is True
    s3_client.delete_object(Bucket=settings.bucket, Key=full, VersionId=version)
    assert store.any_version_left("a/one") is False
    store.delete_every_version("a/one")  # the marker goes too
    assert s3_client.list_object_versions(Bucket=settings.bucket).get("DeleteMarkers", []) == []


def test_a_deleter_that_is_the_writers_credential_is_refused_by_the_store(
    archive_settings_s3: ArchiveSettings,
) -> None:
    open_store(archive_settings_s3).put_bytes_new("a/one", b"x")
    as_writer = replace(archive_settings_s3, deleter=archive_settings_s3.writer)
    store = open_store(as_writer, deleting=True)
    with pytest.raises(ArchiveUnavailable, match="AccessDenied"):
        store.delete_every_version("a/one")
    assert stored_bytes(open_store(archive_settings_s3), "a/one") == b"x"


def test_the_writing_credential_cannot_delete_but_the_writing_store_can_still_read_and_list(
    archive_settings_s3_versioned: ArchiveSettings,
) -> None:
    as_writer = replace(archive_settings_s3_versioned, deleter=archive_settings_s3_versioned.writer)
    store = open_store(as_writer, deleting=True)
    store.put_bytes_new("a/one", b"x")
    assert store.any_version_left("a/one") is True  # listing versions is allowed to the writer
    assert [item.key for item in store.list("")] == ["a/one"]
    with pytest.raises(ArchiveUnavailable, match="AccessDenied"):
        store.delete_every_version("a/one")
    assert store.any_version_left("a/one") is True


# --- the directory backend ---


def object_path(settings: ArchiveSettings, key: str) -> Path:
    assert settings.path is not None
    return settings.path / settings.prefix / key


def test_a_directory_object_is_read_only_and_laid_out_under_the_prefix(archive_settings_dir: ArchiveSettings) -> None:
    store = open_store(archive_settings_dir)
    store.put_bytes_new("a/b/one", b"x")
    path = object_path(archive_settings_dir, "a/b/one")
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert stat.S_IMODE(object_path(archive_settings_dir, "a/b").stat().st_mode) == 0o755
    assert [p.name for p in path.parent.iterdir()] == ["one"]  # no temporary file left


def test_the_directory_must_exist(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    with pytest.raises(ConfigurationError, match=str(missing)):
        open_store(ArchiveSettings(backend="directory", path=missing))
    file = tmp_path / "file"
    file.write_bytes(b"")
    with pytest.raises(ConfigurationError, match=str(file)):
        open_store(ArchiveSettings(backend="directory", path=file))


def test_the_prefix_directories_are_created_when_the_store_opens(tmp_path: Path) -> None:
    open_store(ArchiveSettings(backend="directory", path=tmp_path, prefix="one/two"))
    assert stat.S_IMODE((tmp_path / "one" / "two").stat().st_mode) == 0o755
    open_store(ArchiveSettings(backend="directory", path=tmp_path, prefix="one/two"))  # again: not an error


def test_a_symbolic_link_as_the_object_is_an_integrity_error(
    archive_settings_dir: ArchiveSettings, tmp_path: Path
) -> None:
    store = open_store(archive_settings_dir)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    object_path(archive_settings_dir, "a").mkdir()
    object_path(archive_settings_dir, "a/link").symlink_to(outside)
    for action in (
        lambda: store.head("a/link"),
        lambda: store.read("a/link").__enter__(),
        lambda: store.put_bytes_new("a/link", b"x"),
    ):
        with pytest.raises(IntegrityError, match="a/link"):
            action()
    assert outside.read_bytes() == b"secret"


def test_a_symbolic_link_in_the_key_path_is_never_followed(
    archive_settings_dir: ArchiveSettings, tmp_path: Path
) -> None:
    store = open_store(archive_settings_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "one").write_bytes(b"secret")
    object_path(archive_settings_dir, "a").symlink_to(outside, target_is_directory=True)
    for action in (
        lambda: store.head("a/one"),
        lambda: store.read("a/one").__enter__(),
        lambda: store.put_bytes_new("a/one", b"x"),
        lambda: store.put_bytes_new("a/deeper/one", b"x"),
        lambda: list(store.list("a")),
        lambda: list(store.list_unfinished("a")),
    ):
        with pytest.raises(IntegrityError, match="Object a"):
            action()
    assert sorted(p.name for p in outside.iterdir()) == ["one"]
    assert (outside / "one").read_bytes() == b"secret"
    with pytest.raises(IntegrityError):
        open_store(archive_settings_dir, deleting=True).delete_every_version("a/one")
    assert (outside / "one").read_bytes() == b"secret"


def test_a_listing_skips_links_and_special_files_with_a_warning(
    archive_settings_dir: ArchiveSettings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = open_store(archive_settings_dir)
    store.put_bytes_new("a/one", b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inside").write_bytes(b"secret")
    object_path(archive_settings_dir, "a/link").symlink_to(outside / "inside")
    object_path(archive_settings_dir, "a/dirlink").symlink_to(outside, target_is_directory=True)
    os.mkfifo(object_path(archive_settings_dir, "a/pipe"))
    with caplog.at_level(logging.WARNING, logger="ulduar"):
        assert [item.key for item in store.list("")] == ["a/one"]
    skipped = {record.getMessage().split()[1].rstrip(":") for record in caplog.records}
    assert skipped == {"a/link", "a/dirlink", "a/pipe"}
    assert "secret" not in caplog.text


def test_a_special_file_is_not_an_object(archive_settings_dir: ArchiveSettings) -> None:
    store = open_store(archive_settings_dir)
    os.mkfifo(object_path(archive_settings_dir, "pipe"))
    with pytest.raises(IntegrityError, match="pipe"):
        store.head("pipe")
    with pytest.raises(IntegrityError, match="pipe"):
        store.read("pipe").__enter__()
    with pytest.raises(IntegrityError, match="pipe"):
        store.put_bytes_new("pipe", b"x")


def test_a_directory_at_an_objects_name_is_reported_alike_by_every_call(archive_settings_dir: ArchiveSettings) -> None:
    store = open_store(archive_settings_dir)
    object_path(archive_settings_dir, "a/one").mkdir(parents=True)
    for action in (
        lambda: store.head("a/one"),
        lambda: store.read("a/one").__enter__(),
        lambda: store.put_bytes_new("a/one", b"x"),
    ):
        with pytest.raises(IntegrityError, match="Object a/one is not stored as a regular file"):
            action()
    assert object_path(archive_settings_dir, "a/one").is_dir()


def test_an_error_of_the_callers_own_code_inside_a_read_is_not_an_archive_error(
    settings: ArchiveSettings,
) -> None:
    store = open_store(settings)
    store.put_bytes_new("a/one", b"vault")
    with pytest.raises(OSError, match="No space left") as caught:
        with store.read("a/one") as stream:
            assert stream.read(2) == b"va"
            raise OSError(errno.ENOSPC, "No space left on device")
    assert not isinstance(caught.value, ArchiveUnavailable)


def test_the_cold_storage_classes_and_archive_tiers_are_not_immediate() -> None:
    for storage_class in (None, "STANDARD", "INTELLIGENT_TIERING", "STANDARD_IA"):
        assert is_immediate(storage_class)
        assert is_immediate(storage_class, None)
    for storage_class in ("GLACIER", "DEEP_ARCHIVE"):
        assert not is_immediate(storage_class)
    for archive_status in ("ARCHIVE_ACCESS", "DEEP_ARCHIVE_ACCESS"):
        assert not is_immediate("INTELLIGENT_TIERING", archive_status)


def test_a_directory_key_may_not_look_like_a_temporary_file(archive_settings_dir: ArchiveSettings) -> None:
    store = open_store(archive_settings_dir)
    for key in (".tmp-abc", "a/.tmp-abc/x"):
        with pytest.raises(ValueError, match="start a segment"):
            store.put_bytes_new(key, b"x")


def test_an_operating_system_error_is_an_archive_error(archive_settings_dir: ArchiveSettings) -> None:
    store = open_store(archive_settings_dir)
    object_path(archive_settings_dir, "a").write_bytes(b"a file where a directory belongs")
    with pytest.raises(ArchiveUnavailable, match="Storing a/one failed: Not a directory"):
        store.put_bytes_new("a/one", b"x")
    assert store.head("a/one") is None  # for a lookup the key is simply absent


# --- keys, before any I/O ---


@pytest.mark.parametrize("key", BAD_KEYS)
def test_a_bad_key_is_refused_by_every_call_before_any_io(settings: ArchiveSettings, tmp_path: Path, key: str) -> None:
    store = open_store(settings, deleting=True)
    path, size, digest = blob(tmp_path, b"x")
    calls = [
        lambda: store.put_new(key, path, size, digest),
        lambda: store.put_bytes_new(key, b"x"),
        lambda: store.head(key),
        lambda: store.read(key),
        lambda: store.abort_unfinished(PendingUpload(key, "", FLOOR)),
        lambda: store.delete_every_version(key),
        lambda: store.any_version_left(key),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="object key"):
            call()
    if key:  # "" names the root: a valid prefix
        with pytest.raises(ValueError, match="object key"):
            store.list(key)
        with pytest.raises(ValueError, match="object key"):
            store.list_unfinished(key)
    assert list(store.list("")) == []


def test_the_root_is_a_valid_prefix_but_not_a_key() -> None:
    assert check_key("", allow_root=True) == ""
    assert check_key("a/b.c-d_e/ü") == "a/b.c-d_e/ü"
    assert check_key("x" * 1024) == "x" * 1024
    with pytest.raises(ValueError, match="1 to 1024 bytes"):
        check_key("ü" * 513)  # 1026 bytes
    with pytest.raises(ValueError, match="UTF-8"):
        check_key("a\ud800")


# --- the S3 backend: transport, credentials, errors ---


def s3_settings_for(endpoint_variable: str | None = None, **changes: Any) -> ArchiveSettings:
    return ArchiveSettings(backend="s3", bucket="ulduar-test-none", endpoint_url_env=endpoint_variable, **changes)


@pytest.mark.parametrize("endpoint", ["http://example.com", "http://10.1.2.3:9000", "http://[2001:db8::1]"])
def test_a_remote_endpoint_needs_https(endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHIVE_ENDPOINT", endpoint)
    with pytest.raises(ConfigurationError, match="is not on this machine — a remote endpoint needs https"):
        open_store(s3_settings_for("ARCHIVE_ENDPOINT"))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://s3.example.com",
        "https://example.com:9000",
        "http://localhost:9",
        "http://127.0.0.1:9",
        "http://[::1]:9",
    ],
)
def test_an_https_or_loopback_endpoint_is_accepted_without_a_request(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHIVE_ENDPOINT", endpoint)
    assert open_store(s3_settings_for("ARCHIVE_ENDPOINT")) is not None


@pytest.mark.parametrize("endpoint", ["ftp://localhost", "localhost:9000", "http://", "https://"])
def test_an_endpoint_that_is_not_a_url_is_refused_without_echoing_it(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHIVE_ENDPOINT", endpoint)
    with pytest.raises(ConfigurationError, match="http or https URL") as caught:
        open_store(s3_settings_for("ARCHIVE_ENDPOINT"))
    assert endpoint not in str(caught.value)


def test_the_client_ignores_environment_proxies_and_uses_a_named_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy.invalid:3128")
    monkeypatch.setenv("ARCHIVE_ENDPOINT", "https://s3.example.com")
    plain = open_store(s3_settings_for("ARCHIVE_ENDPOINT"))
    assert plain._client.meta.config.proxies == {}  # type: ignore[attr-defined]
    monkeypatch.setenv("ARCHIVE_PROXY", "http://proxy.invalid:3128")
    named = open_store(s3_settings_for("ARCHIVE_ENDPOINT", proxy_env="ARCHIVE_PROXY"))
    assert named._client.meta.config.proxies == {  # type: ignore[attr-defined]
        "http": "http://proxy.invalid:3128",
        "https": "http://proxy.invalid:3128",
    }


def test_a_named_variable_that_is_unset_is_named_and_nothing_else(
    archive_settings_s3: ArchiveSettings, monkeypatch: pytest.MonkeyPatch, s3_settings: S3Settings
) -> None:
    monkeypatch.delenv(S3_WRITER_SECRET_KEY_VARIABLE)
    with pytest.raises(ConfigurationError, match=S3_WRITER_SECRET_KEY_VARIABLE) as caught:
        open_store(archive_settings_s3)
    assert s3_settings.writer_secret_key not in str(caught.value)
    monkeypatch.setenv(S3_WRITER_SECRET_KEY_VARIABLE, "")
    with pytest.raises(ConfigurationError, match=S3_WRITER_SECRET_KEY_VARIABLE):
        open_store(archive_settings_s3)


def test_an_unset_endpoint_variable_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHIVE_ENDPOINT", raising=False)
    with pytest.raises(ConfigurationError, match="ARCHIVE_ENDPOINT"):
        open_store(s3_settings_for("ARCHIVE_ENDPOINT"))


def test_deleting_needs_a_deleter(archive_settings_s3: ArchiveSettings) -> None:
    with pytest.raises(ConfigurationError, match="`archive.deleter` is required to delete from the archive"):
        open_store(replace(archive_settings_s3, deleter=None), deleting=True)
    assert open_store(replace(archive_settings_s3, deleter=None)) is not None  # writing needs none


def test_the_s3_backend_needs_its_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing boto3 is simulated by the import system: sys.modules[name] = None makes `import name` fail.
    monkeypatch.delitem(sys.modules, "ulduar.hodir._s3", raising=False)
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(ConfigurationError, match=r"project-ulduar\[s3\] \(boto3\)"):
        open_store(s3_settings_for())


def test_a_refused_credential_is_an_archive_error_that_never_shows_a_secret(
    archive_settings_s3: ArchiveSettings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    s3_settings: S3Settings,
) -> None:
    wrong = secrets.token_hex(16)
    monkeypatch.setenv(S3_WRITER_SECRET_KEY_VARIABLE, wrong)
    caplog.set_level(logging.DEBUG)
    store = open_store(archive_settings_s3)
    failures: list[BaseException] = []
    for call in (
        lambda: store.put_bytes_new("a/one", b"x"),
        lambda: store.head("a/one"),
        lambda: list(store.list("")),
    ):
        with pytest.raises(ArchiveUnavailable) as caught:
            call()
        failures.append(caught.value)
    secrets_and_keys = (wrong, s3_settings.writer_secret_key, s3_settings.writer_access_key, s3_settings.secret_key)
    ours = [record.getMessage() for record in caplog.records if record.name.startswith("ulduar")]
    for text in [*(str(failure) for failure in failures), *ours]:
        assert not any(value in text for value in secrets_and_keys)
    assert all("a/one" in str(failure) or "the archive root" in str(failure) for failure in failures)
    assert os.environ[S3_WRITER_ACCESS_KEY_VARIABLE] == s3_settings.writer_access_key  # the variable was only read


def test_an_unreachable_store_is_an_archive_error(
    archive_settings_s3: ArchiveSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(S3_ENDPOINT_VARIABLE, "http://127.0.0.1:9")  # nothing listens on the discard port
    store = open_store(archive_settings_s3)
    with pytest.raises(ArchiveUnavailable, match=r"Reading a/one failed: \w+Error") as caught:
        store.head("a/one")
    assert "127.0.0.1" not in str(caught.value)


def test_the_credentials_are_never_in_the_settings_only_their_names(archive_settings_s3: ArchiveSettings) -> None:
    text = repr(archive_settings_s3)
    assert S3_WRITER_SECRET_KEY_VARIABLE in text and S3_DELETER_SECRET_KEY_VARIABLE in text
    assert os.environ[S3_WRITER_SECRET_KEY_VARIABLE] not in text
