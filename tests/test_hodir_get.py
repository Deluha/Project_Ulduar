"""ulduar.hodir: `get` and `get_stream`, on a real Archivum over a real directory and an unversioned bucket.

Each test puts through `open_archive` and reads back through the same facade. A stored object is tampered with (or
removed) as the test, never the library: a directory object is made writable and overwritten, a bucket object is
overwritten or deleted through the root `s3_client`. A wrapper that forwards every call to the real store proves a
refusal happens before any read, and makes a destination appear while a get is reading (H9, H22, D36).
"""

from __future__ import annotations

import errno
import hashlib
import io
import logging
import os
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    CONTENT,
    CONTENT_CHECKSUM,
    Archivum,
    Fault,
    archive_file,
    faulty_archive,
    object_key,
    onboarded,
    record_confirmed_deletion,
)
from ulduar import _ids
from ulduar.config import Config
from ulduar.errors import IntegrityError, UlduarError, UnknownRecordError
from ulduar.freya import open_catalog
from ulduar.hodir import Archive, open_archive
from ulduar.hodir._store import CHUNK_SIZE, WritingStore, open_store

NOT_THE_CONTENT = bytes(len(CONTENT))  # as long as the content, hashing to something else


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """An empty directory to get into."""
    path = tmp_path / "target"
    path.mkdir()
    return path


class RacingStore:
    """A store that forwards to a real one but makes *path* appear as a get starts reading."""

    def __init__(self, store: WritingStore, path: Path) -> None:
        self._store = store
        self._path = path

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def read(self, key: str) -> Any:
        self._path.write_bytes(b"first")
        return self._store.read(key)


def leftovers(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


# --- the bytes come back exactly ---


def test_an_archived_object_is_got_to_a_file_by_artifact_and_by_id(config: Config, source: Path, target: Path) -> None:
    stored = archive_file(config, source)
    with open_archive(config) as session:
        by_artifact = session.get(stored, target / "by-artifact.bin")
        by_id = session.get(stored.artifact_id, str(target / "by-id.bin"))
    assert by_artifact == target / "by-artifact.bin" and by_id == target / "by-id.bin"
    assert by_artifact.read_bytes() == CONTENT and by_id.read_bytes() == CONTENT
    assert leftovers(target) == ["by-artifact.bin", "by-id.bin"]  # no partial file stays
    umask = os.umask(0)
    os.umask(umask)
    assert stat.S_IMODE(by_id.stat().st_mode) == 0o666 & ~umask
    assert by_id.stat().st_nlink == 1


def test_a_relative_destination_is_written_in_the_working_directory(
    config: Config, source: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = archive_file(config, source)
    monkeypatch.chdir(target)
    with open_archive(config) as session:
        assert session.get(stored, "here.bin") == Path("here.bin")
    assert leftovers(target) == ["here.bin"] and (target / "here.bin").read_bytes() == CONTENT


def test_an_object_of_several_chunks_is_got_whole_to_a_file_and_to_a_stream(
    config: Config, tmp_path: Path, target: Path
) -> None:
    data = os.urandom(2 * CHUNK_SIZE + 17)
    big = tmp_path / "big.bin"
    big.write_bytes(data)
    stored = archive_file(config, big)
    out = io.BytesIO()
    with open_archive(config) as session:
        assert session.get(stored, target / "big.bin").read_bytes() == data
        assert session.get_stream(stored.artifact_id, out) == len(data)
    assert out.getvalue() == data


def test_a_stream_get_returns_the_size_and_writes_the_bytes(config: Config, source: Path) -> None:
    stored = archive_file(config, source)
    by_artifact, by_id = io.BytesIO(), io.BytesIO()
    with open_archive(config) as session:
        assert session.get_stream(stored, by_artifact) == len(CONTENT)
        assert session.get_stream(stored.artifact_id, by_id) == len(CONTENT)
    assert by_artifact.getvalue() == by_id.getvalue() == CONTENT


class FullDisk(io.RawIOBase):
    """A destination whose every write fails as a full disk does."""

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


def test_a_destination_that_fails_to_write_surfaces_as_the_callers_oserror(config: Config, source: Path) -> None:
    stored = archive_file(config, source)
    with open_archive(config) as session:
        with pytest.raises(OSError, match="No space left on device") as caught:
            session.get_stream(stored, FullDisk())  # type: ignore[arg-type]
    assert caught.value.errno == errno.ENOSPC
    assert not isinstance(caught.value, UlduarError)  # not the store's ArchiveUnavailable


def test_a_get_logs_the_artifact_the_short_checksum_and_the_size_but_no_destination(
    config: Config, source: Path, target: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stored = archive_file(config, source)
    caplog.set_level(logging.INFO, logger="ulduar.hodir")
    with open_archive(config) as session:
        session.get(stored, target / "private-folder-name.bin")
        session.get_stream(stored, io.BytesIO())
    messages = [r.getMessage() for r in caplog.records if r.name == "ulduar.hodir.archive" and "got" in r.getMessage()]
    assert len(messages) == 2
    for message in messages:
        assert str(stored.artifact_id) in message and f"{len(CONTENT)} bytes" in message
        assert CONTENT_CHECKSUM.hex()[:12] in message and CONTENT_CHECKSUM.hex() not in message
        assert "private-folder-name" not in message and str(target) not in message


def test_a_destination_name_of_the_longest_length_is_got_because_the_partial_file_carries_no_name(
    config: Config, source: Path, target: Path
) -> None:
    stored = archive_file(config, source)
    with open_archive(config) as session:
        destination = session.get(stored, target / ("n" * 250))
    assert destination.read_bytes() == CONTENT and leftovers(target) == ["n" * 250]


# --- verification: a tampered or vanished object is never handed over ---


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        pytest.param(NOT_THE_CONTENT, "recorded checksum", id="same size, other bytes"),
        pytest.param(CONTENT[:-1], "recorded size", id="shorter"),
        pytest.param(CONTENT + b"!", "recorded size", id="longer"),
    ],
)
def test_a_corrupted_object_is_an_integrity_error_and_no_file_or_partial_file_remains(
    config: Config,
    source: Path,
    target: Path,
    tamper: Callable[[str, bytes | None], None],
    data: bytes,
    problem: str,
) -> None:
    stored = archive_file(config, source)
    tamper(object_key(), data)
    out = io.BytesIO()
    with open_archive(config) as session:
        with pytest.raises(IntegrityError, match=f"^Object {object_key()} does not match its {problem}$"):
            session.get(stored, target / "out.bin")
        assert leftovers(target) == []
        with pytest.raises(IntegrityError, match=f"does not match its {problem}"):
            session.get_stream(stored, out)
    assert out.getvalue() == data  # a stream got its bytes before the end told they were wrong


def test_an_object_removed_from_the_store_is_an_integrity_error(
    config: Config, source: Path, target: Path, tamper: Callable[[str, bytes | None], None]
) -> None:
    stored = archive_file(config, source)
    tamper(object_key(), None)
    with open_archive(config) as session:
        with pytest.raises(IntegrityError):
            session.get(stored, target / "out.bin")
        with pytest.raises(IntegrityError):
            session.get_stream(stored.artifact_id, io.BytesIO())
    assert leftovers(target) == []


# --- the destination ---


def test_an_existing_destination_is_refused_before_any_read_and_left_as_it_was(
    config: Config, source: Path, target: Path
) -> None:
    stored = archive_file(config, source)
    (target / "file.bin").write_bytes(b"mine")
    (target / "dangling").symlink_to(target / "nowhere")
    with faulty_archive(config) as (session, store):
        for name in ("file.bin", "dangling", "."):
            with pytest.raises(FileExistsError) as caught:
                session.get(stored, target / name)
            assert caught.value.filename == str(target / name)
        assert "read" not in store.calls
    assert (target / "file.bin").read_bytes() == b"mine"
    assert not (target / "nowhere").exists() and (target / "dangling").is_symlink()
    assert leftovers(target) == ["dangling", "file.bin"]


def test_a_destination_without_an_existing_directory_to_hold_it_is_refused_before_any_read(
    config: Config, source: Path, target: Path
) -> None:
    stored = archive_file(config, source)
    (target / "a-file").write_bytes(b"x")
    with faulty_archive(config) as (session, store):
        with pytest.raises(FileNotFoundError):
            session.get(stored, target / "missing" / "out.bin")
        with pytest.raises(NotADirectoryError):
            session.get(stored, target / "a-file" / "out.bin")
        assert "read" not in store.calls
    assert leftovers(target) == ["a-file"]


def test_a_destination_that_appears_during_the_get_is_not_replaced_and_no_partial_file_remains(
    config: Config, source: Path, target: Path
) -> None:
    stored = archive_file(config, source)
    assert config.archive is not None
    with open_catalog(config) as catalog:
        racing = RacingStore(open_store(config.archive), target / "out.bin")
        session = Archive(catalog, racing, config.archive)  # type: ignore[arg-type]
        with pytest.raises(FileExistsError):
            session.get(stored, target / "out.bin")
    assert (target / "out.bin").read_bytes() == b"first"
    assert leftovers(target) == ["out.bin"]


# --- only what is archived can be got ---


def test_an_artifact_that_was_recorded_without_an_object_is_not_in_the_archive(config: Config, target: Path) -> None:
    with open_catalog(config) as catalog:
        recorded = catalog.register_artifact("log", CONTENT_CHECKSUM, len(CONTENT))
    with faulty_archive(config) as (session, store):
        for artifact in (recorded, recorded.artifact_id):
            with pytest.raises(UnknownRecordError, match=f"^Artifact {recorded.artifact_id} is not in the archive$"):
                session.get(artifact, target / "out.bin")
            with pytest.raises(UnknownRecordError, match="is not in the archive"):
                session.get_stream(artifact, io.BytesIO())
        assert "read" not in store.calls
    assert leftovers(target) == []


def test_an_unknown_id_is_an_unknown_record(config: Config, target: Path) -> None:
    unknown = _ids.uuid7()
    with open_archive(config) as session:
        with pytest.raises(UnknownRecordError, match=str(unknown)):
            session.get(unknown, target / "out.bin")
        with pytest.raises(UnknownRecordError, match=str(unknown)):
            session.get_stream(unknown, io.BytesIO())
    assert leftovers(target) == []


def test_only_the_id_of_an_artifact_counts_the_recorded_checksum_size_and_scope_are_used(
    config: Config, source: Path, target: Path
) -> None:
    stored = archive_file(config, source)
    forged = replace(stored, checksum=hashlib.sha256(b"other").digest(), size=5, scope="elsewhere")
    out = io.BytesIO()
    with faulty_archive(config) as (session, store):
        assert session.get(forged, target / "out.bin").read_bytes() == CONTENT
        assert session.get_stream(forged, out) == len(CONTENT)
        assert store.calls["read"] == 2
    assert out.getvalue() == CONTENT
    unknown = replace(stored, artifact_id=_ids.uuid7())
    with open_archive(config) as session:
        with pytest.raises(UnknownRecordError, match=str(unknown.artifact_id)):
            session.get(unknown, target / "other.bin")
        with pytest.raises(UnknownRecordError, match=str(unknown.artifact_id)):
            session.get_stream(unknown, io.BytesIO())
    assert leftovers(target) == ["out.bin"]


def test_an_artifact_of_another_project_is_not_recorded_in_this_one(
    config: Config, archivum: Archivum, source: Path, target: Path, tmp_path: Path
) -> None:
    stored = archive_file(config, source)
    other = replace(onboarded(archivum, tmp_path, "beta"), archive=config.archive)
    with open_archive(other) as session:
        for artifact in (stored, stored.artifact_id):
            with pytest.raises(UnknownRecordError, match="is not recorded in project beta"):
                session.get(artifact, target / "out.bin")


def test_an_object_whose_deletion_was_confirmed_after_its_registration_is_not_in_the_archive_until_archived_again(
    config: Config, archivum: Archivum, source: Path, target: Path
) -> None:
    first = archive_file(config, source)
    record_confirmed_deletion(archivum, first)
    with open_archive(config) as session:
        for artifact in (first, first.artifact_id):
            with pytest.raises(UnknownRecordError, match=f"^Artifact {first.artifact_id} is not in the archive$"):
                session.get(artifact, target / "out.bin")
        with pytest.raises(UnknownRecordError, match="is not in the archive"):
            session.get_stream(first, io.BytesIO())
        assert leftovers(target) == []
        again = session.put_file(source, "report")  # registered after the deletion: archived again
        assert session.get(again, target / "out.bin").read_bytes() == CONTENT
        with pytest.raises(UnknownRecordError):
            session.get(first, target / "other.bin")


def test_a_failure_after_the_partial_file_was_made_removes_it(config: Config, source: Path, target: Path) -> None:
    stored = archive_file(config, source)
    with faulty_archive(config, method="read") as (session, _), pytest.raises(Fault):
        session.get(stored, target / "out.bin")
    assert leftovers(target) == []
