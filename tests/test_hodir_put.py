"""ulduar.hodir: the object layout, the format marker, `open_archive`, `put_file` and `put_stream`.

The layout and manifest tests are pure. The put tests run on a real Archivum and, per backend, a real directory and
an unversioned bucket of the local object store (only the bucket cases are marked `s3`); the store under test is built
by `open_store` from the fixtures' settings. Fault injection (hard rule 9, H22): `FaultyStore` forwards every call to
the real store and raises after a chosen one, so the ordering guarantee of a put is tested against a failure at each
step, with the Archivum and the store both real.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from tests.conftest import (
    CONTENT,
    CONTENT_CHECKSUM,
    Archivum,
    Fault,
    count,
    failure,
    faulty_archive,
    ms_ago,
    object_key,
    onboarded,
    recorder_of,
)
from ulduar import _ids
from ulduar.config import Config
from ulduar.errors import (
    ArchiveFormatError,
    ConfigurationError,
    IntegrityError,
    UlduarError,
    UnknownRecordError,
)
from ulduar.freya import Artifact, Connection, open_catalog
from ulduar.hodir import PutSink, _archivum, _layout, archive, open_archive
from ulduar.hodir._layout import Manifest
from ulduar.hodir._store import WritingStore, open_store

ARCHIVE_TABLES = (
    "archive_registration",
    "archive_retention_rule",
    "archive_deletion",
    "archive_check",
    "archive_finding",
)
FOREIGN_MARKERS = [
    pytest.param(b'{"format":"something-else","version":1}\n', "foreign format marker", id="other format"),
    pytest.param(b"not json at all", "foreign format marker", id="unparseable"),
    pytest.param(
        b'{"format":"ulduar-archive","version":2}\n', "format version 2; this library knows version 1", id="v2"
    ),
]


def spool_of(config: Config) -> Path:
    assert config.archive is not None and config.archive.spool_dir is not None
    return config.archive.spool_dir


def store_of(config: Config) -> WritingStore:
    assert config.archive is not None
    return open_store(config.archive)


def keys_under(config: Config, prefix: str) -> list[str]:
    return [stored.key for stored in store_of(config).list(prefix)]


def stored_bytes(config: Config, key: str) -> bytes:
    with store_of(config).read(key) as stream:
        return bytes(stream.read())


def registrations(archivum: Archivum) -> list[tuple[uuid.UUID, str | None]]:
    with archivum.superuser() as su:
        rows = su.execute("SELECT artifact_id, retention_class FROM ulduar.archive_registration ORDER BY 1").fetchall()
    return [(r[0], r[1]) for r in rows]


# --- the layout and the manifest (pure) ---


def test_the_keys_follow_the_layout() -> None:
    digest = CONTENT_CHECKSUM.hex()
    assert _layout.object_key("alpha", "raw", CONTENT_CHECKSUM) == f"alpha/raw/_objects/sha256/{digest[:2]}/{digest}"
    assert _layout.objects_prefix("alpha", "raw") == "alpha/raw/_objects/sha256"
    artifact_id = _ids.uuid7(int(datetime(2026, 3, 5, 23, 59, tzinfo=UTC).timestamp() * 1000))
    assert _layout.manifest_key("alpha", "raw", artifact_id) == f"alpha/raw/_manifests/2026/03/{artifact_id}.json"
    assert _layout.manifests_prefix("alpha", "raw") == "alpha/raw/_manifests"
    assert _layout.record_key("alpha", artifact_id) == f"alpha/_records/2026/03/{artifact_id}.json"
    assert _layout.FORMAT_MARKER_KEY == "ulduar-archive.json" and _layout.LAYOUT_KEY == "LAYOUT.txt"
    assert _layout.LAYOUT_TEXT.endswith("\n")


def test_a_name_that_could_leave_its_prefix_is_refused_by_every_key_builder() -> None:
    for bad in ("..", "a/b", "_objects", "", "A"):
        with pytest.raises(ValueError):
            _layout.object_key(bad, "raw", CONTENT_CHECKSUM)
        with pytest.raises(ValueError):
            _layout.object_key("alpha", bad, CONTENT_CHECKSUM)
        with pytest.raises(ValueError):
            _layout.manifests_prefix("alpha", bad)
        with pytest.raises(ValueError):
            _layout.record_key(bad, _ids.uuid7())
    with pytest.raises(ValueError):
        _layout.manifest_key("alpha", "raw", uuid.uuid4())  # not a version 7 id


def test_an_object_key_is_parsed_back_to_its_scope_and_checksum() -> None:
    key = _layout.object_key("alpha", "raw", CONTENT_CHECKSUM)
    assert _layout.parse_object_key(key.removeprefix("alpha/")) == ("raw", CONTENT_CHECKSUM)
    digest = CONTENT_CHECKSUM.hex()
    for other in (
        "raw/_objects/sha256/00/" + digest,  # the head does not match the hash
        f"raw/_objects/sha256/{digest[:2]}/{digest[:-1]}",
        f"raw/_objects/sha1/{digest[:2]}/{digest}",
        f"_x/_objects/sha256/{digest[:2]}/{digest}",
        f"raw/_manifests/2026/03/{uuid.uuid4()}.json",
        "",
    ):
        assert _layout.parse_object_key(other) is None, other


def manifest(**changes: Any) -> Manifest:
    artifact_id = _ids.uuid7(ms_ago(40))
    fields: dict[str, Any] = {
        "artifact_id": artifact_id,
        "event_at": _ids.uuid7_time(artifact_id),
        "project": "alpha",
        "scope": "raw",
        "kind": "report",
        "checksum": CONTENT_CHECKSUM,
        "size": len(CONTENT),
        "dataset_id": _ids.uuid7(),
        "retention_class": "short",
    }
    return Manifest(**{**fields, **changes})


@pytest.mark.parametrize("changes", [{}, {"dataset_id": None, "retention_class": None, "size": 0}])
def test_a_manifest_round_trips_and_is_canonical(changes: dict[str, Any]) -> None:
    original = manifest(**changes)
    data = original.encode()
    assert Manifest.decode(data) == original
    assert data.endswith(b"\n") and b" " not in data
    raw = json.loads(data)
    assert list(raw) == sorted(raw) and raw["format"] == 1
    assert raw["checksum"] == "sha256:" + CONTENT_CHECKSUM.hex()
    assert raw["event_at"].endswith("Z") and len(raw["event_at"]) == len("2026-01-01T00:00:00.000Z")
    assert set(raw) == {
        "format", "artifact_id", "event_at", "project", "scope", "kind", "checksum", "size", "dataset_id",
        "retention_class",
    }  # fmt: skip
    assert Manifest.decode(data).encode() == data


def mutated(**changes: Any) -> bytes:
    raw = json.loads(manifest().encode())
    for name, value in changes.items():
        if value is KeyError:
            del raw[name]
        else:
            raw[name] = value
    return json.dumps(raw).encode()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff\xfe",
        b"[]",
        b"{not json",
        mutated(format=2),
        mutated(format="1"),
        mutated(format=True),
        mutated(artifact_id=KeyError),
        mutated(extra="x"),
        mutated(artifact_id="not-a-uuid"),
        mutated(artifact_id=str(uuid.uuid4())),
        mutated(event_at="2026-01-01T00:00:00Z"),
        mutated(event_at="2026-13-01T00:00:00.000Z"),
        mutated(project="Not A Name"),
        mutated(scope="a/b"),
        mutated(kind=""),
        mutated(kind=5),
        mutated(checksum="sha256:abc"),
        mutated(checksum="md5:" + CONTENT_CHECKSUM.hex()),
        mutated(checksum="sha256:" + CONTENT_CHECKSUM.hex().upper()),
        mutated(size=-1),
        mutated(size=1.5),
        mutated(size=True),
        mutated(dataset_id=7),
        mutated(retention_class="Bad Class"),
    ],
)
def test_a_manifest_that_is_not_exactly_format_one_is_refused_without_quoting_it(data: bytes) -> None:
    with pytest.raises(ArchiveFormatError) as caught:
        Manifest.decode(data)
    assert "not-a-uuid" not in str(caught.value) and "Not A Name" not in str(caught.value)


def test_the_marker_is_canonical_and_checked() -> None:
    assert _layout.marker_bytes() == b'{"format":"ulduar-archive","version":1}\n'
    _layout.check_marker(_layout.marker_bytes())
    with pytest.raises(ArchiveFormatError, match="^The archive root holds a foreign format marker$"):
        _layout.check_marker(b'{"format":"x"}')
    with pytest.raises(ArchiveFormatError, match="^The archive has format version 2; this library knows version 1$"):
        _layout.check_marker(b'{"format":"ulduar-archive","version":2}')
    with pytest.raises(ArchiveFormatError, match="foreign"):
        _layout.check_marker(b'{"format":"ulduar-archive","version":"two"}')


def test_the_facade_exports_exactly_the_session_surface() -> None:
    from ulduar import hodir

    assert sorted(hodir.__all__) == ["Archive", "Finding", "PutSink", "Verification", "open_archive"]


# --- opening: the format marker ---


def test_the_first_open_writes_the_marker_and_the_layout_text_and_a_second_open_accepts(config: Config) -> None:
    with open_archive(config) as first:
        assert (first.project.name, first.scope) == ("alpha", "default")
    assert stored_bytes(config, _layout.FORMAT_MARKER_KEY) == _layout.marker_bytes()
    assert stored_bytes(config, _layout.LAYOUT_KEY) == _layout.LAYOUT_TEXT.encode()
    with open_archive(config):
        pass
    assert stored_bytes(config, _layout.LAYOUT_KEY) == _layout.LAYOUT_TEXT.encode()


@pytest.mark.parametrize(("marker", "message"), FOREIGN_MARKERS)
def test_a_foreign_marker_or_another_version_is_refused_on_open(config: Config, marker: bytes, message: str) -> None:
    store_of(config).put_bytes_new(_layout.FORMAT_MARKER_KEY, marker)
    with pytest.raises(ArchiveFormatError, match=message), open_archive(config):
        pass
    assert keys_under(config, "") == [_layout.FORMAT_MARKER_KEY]  # nothing else was written


def test_opening_without_an_archive_section_is_a_configuration_error(archivum: Archivum, tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="`archive` is required"), open_archive(onboarded(archivum, tmp_path)):
        pass


# --- put_file ---


def test_a_file_is_stored_manifested_registered_and_returned(config: Config, archivum: Archivum, source: Path) -> None:
    record_id = _ids.uuid7(ms_ago(100))  # a month the Archivum has no partition for yet
    with open_archive(config) as session, open_catalog(config) as catalog:
        dataset = catalog.register_dataset("ns", "name")
        stored = session.put_file(
            source, "report", dataset=dataset, origin="a/path", retention_class="short", record_id=record_id
        )
        assert catalog.get_artifact(record_id) == stored
    assert (stored.artifact_id, stored.kind, stored.checksum, stored.size) == (
        record_id,
        "report",
        CONTENT_CHECKSUM,
        len(CONTENT),
    )
    assert (stored.scope, stored.origin, stored.dataset_id) == ("default", "a/path", dataset.dataset_id)
    assert stored_bytes(config, object_key()) == CONTENT
    [manifest_key] = keys_under(config, _layout.manifests_prefix("alpha", "default"))
    assert manifest_key == _layout.manifest_key("alpha", "default", record_id)
    assert Manifest.decode(stored_bytes(config, manifest_key)) == Manifest(
        record_id,
        _ids.uuid7_time(record_id),
        "alpha",
        "default",
        "report",
        CONTENT_CHECKSUM,
        len(CONTENT),
        dataset.dataset_id,
        "short",
    )
    assert registrations(archivum) == [(record_id, "short")]
    assert b"a/path" not in stored_bytes(config, manifest_key)  # the origin is never in a manifest (F18)


def test_a_file_without_a_class_is_registered_to_be_kept_for_ever_and_a_scope_may_be_chosen(
    config: Config, archivum: Archivum, source: Path
) -> None:
    with open_archive(config) as session:
        stored = session.put_file(source, "log")
    assert registrations(archivum) == [(stored.artifact_id, None)]
    assert keys_under(config, _layout.objects_prefix("alpha", "default")) == [object_key()]


def test_the_same_content_twice_is_one_object_with_two_manifests_artifacts_and_registrations(
    config: Config, archivum: Archivum, source: Path
) -> None:
    with open_archive(config) as session:
        first = session.put_file(source, "raw_file", retention_class="a")
        second = session.put_file(source, "raw_file", retention_class="b")
    assert first.artifact_id != second.artifact_id
    assert keys_under(config, _layout.objects_prefix("alpha", "default")) == [object_key()]
    assert len(keys_under(config, _layout.manifests_prefix("alpha", "default"))) == 2
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 2
    assert registrations(archivum) == sorted([(first.artifact_id, "a"), (second.artifact_id, "b")])


def test_the_same_record_id_twice_changes_nothing_and_returns_the_same_artifact(
    config: Config, archivum: Archivum, source: Path, caplog: pytest.LogCaptureFixture
) -> None:
    record_id = _ids.uuid7()
    caplog.set_level(logging.INFO, logger="ulduar.hodir")
    with open_archive(config) as session:
        first = session.put_file(source, "raw_file", retention_class="a", record_id=record_id)
        assert session.put_file(source, "raw_file", retention_class="a", record_id=record_id) == first
    assert len(keys_under(config, _layout.manifests_prefix("alpha", "default"))) == 1
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 1
    assert registrations(archivum) == [(record_id, "a")]
    messages = [r.getMessage() for r in caplog.records if r.name == "ulduar.hodir.archive"]
    assert len(messages) == 2 and "object stored" in messages[0] and "object already stored" in messages[1]
    assert all(
        CONTENT_CHECKSUM.hex()[:12] in m and CONTENT_CHECKSUM.hex() not in m and "source.bin" not in m for m in messages
    )


def test_the_same_record_id_with_another_retention_class_is_refused_and_changes_nothing(
    config: Config, archivum: Archivum, source: Path
) -> None:
    record_id = _ids.uuid7()
    with open_archive(config) as session:
        session.put_file(source, "raw_file", retention_class="a", record_id=record_id)
        with pytest.raises(UlduarError, match=f"Artifact {record_id} already has a different manifest"):
            session.put_file(source, "raw_file", retention_class="b", record_id=record_id)
        with pytest.raises(UlduarError, match="already has a different manifest"):
            session.put_file(source, "log", retention_class="a", record_id=record_id)
    assert registrations(archivum) == [(record_id, "a")]


def test_a_registration_with_another_class_is_refused_by_the_registration_insert(
    config: Config, archivum: Archivum, source: Path
) -> None:
    with open_archive(config) as session:
        stored = session.put_file(source, "raw_file", retention_class="a")
    with open_catalog(config) as catalog:
        project_id = catalog.project.project_id

        def work(tx: Connection) -> None:
            _archivum.insert_registration(tx, project_id, stored.scope, stored, "a")  # the same: nothing happens
            _archivum.insert_registration(tx, project_id, stored.scope, stored, "b")

        with pytest.raises(UlduarError, match=r"already archived with retention class 'a'"):
            catalog.transaction("Registering", work)
    assert registrations(archivum) == [(stored.artifact_id, "a")]


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"kind": "movie"}, "Artifact kind 'movie'"),
        ({"scope": "Bad Scope"}, "scope"),
        ({"retention_class": "a/b"}, "retention class"),
        ({"record_id": uuid.uuid4()}, "version 7"),
        ({"origin": ""}, "origin"),
    ],
)
def test_a_bad_argument_is_refused_before_anything_is_stored(
    config: Config, source: Path, arguments: dict[str, Any], problem: str
) -> None:
    with open_archive(config) as session:
        with pytest.raises(ValueError, match=problem):
            session.put_file(source, **{"kind": "raw_file", **arguments})
        with pytest.raises(ValueError, match=problem):
            with session.put_stream(**{"kind": "raw_file", **arguments}):
                pass
    assert keys_under(config, "alpha") == []
    assert list(spool_of(config).iterdir()) == []


def test_a_source_that_is_not_a_regular_file_is_refused_before_anything_is_stored(
    config: Config, tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing")
    with open_archive(config) as session:
        with pytest.raises(ValueError, match="symbolic link"):
            session.put_file(link, "log")
        with pytest.raises(ValueError, match="not a regular file"):
            session.put_file(tmp_path, "log")
        with pytest.raises(FileNotFoundError):
            session.put_file(tmp_path / "missing", "log")
    assert keys_under(config, "alpha") == []


def test_an_artifact_of_a_scope_the_project_lacks_is_unknown(config: Config, source: Path) -> None:
    with open_archive(config) as session, pytest.raises(UnknownRecordError):
        session.put_file(source, "log", scope="elsewhere")


# --- put_stream ---


def test_a_committed_stream_is_stored_like_a_file_and_commit_is_repeatable(config: Config, archivum: Archivum) -> None:
    spool = spool_of(config)
    with open_archive(config) as session:
        with session.put_stream("model", retention_class="a") as sink:
            assert isinstance(sink, PutSink)
            sink.write(CONTENT[:7])
            sink.write(CONTENT[7:])
            [spooled] = spool.iterdir()
            assert spooled.name.startswith("ulduar-spool-") and stat.S_IMODE(spooled.stat().st_mode) == 0o600
            stored = sink.commit()
            assert sink.commit() == stored
            with pytest.raises(UlduarError, match="closed"):
                sink.write(b"more")
        assert list(spool.iterdir()) == []
    assert (stored.kind, stored.checksum, stored.size) == ("model", CONTENT_CHECKSUM, len(CONTENT))
    assert stored_bytes(config, object_key()) == CONTENT
    assert registrations(archivum) == [(stored.artifact_id, "a")]
    with open_archive(config) as session:
        with session.put_stream("model") as late:
            pass
        with pytest.raises(UlduarError, match="closed"):  # a sink does not outlive its block
            late.write(b"x")


def test_a_stream_left_without_commit_or_with_an_exception_stores_nothing_and_leaves_no_spool(
    config: Config, archivum: Archivum
) -> None:
    spool = spool_of(config)
    with open_archive(config) as session:
        with session.put_stream("model") as sink:
            sink.write(CONTENT)
        with pytest.raises(UlduarError, match="closed"):
            sink.commit()
        with pytest.raises(Fault), session.put_stream("model") as sink:
            sink.write(CONTENT)
            raise Fault
        with session.put_stream("model"):  # nothing written at all
            pass
    assert list(spool.iterdir()) == []
    assert keys_under(config, "alpha") == []
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 0


def test_a_stream_past_the_spool_limit_is_refused_and_stores_nothing(
    config: Config, archivum: Archivum, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive, "BYTES_PER_GIB", 16)  # the limit of 1 GiB is 16 bytes
    assert config.archive is not None
    config = replace(config, archive=replace(config.archive, spool_limit_gib=1))
    spool = spool_of(config)
    with open_archive(config) as session:
        with pytest.raises(UlduarError, match="^The stream is larger than the spool limit of 1 GiB$"):
            with session.put_stream("model") as sink:
                sink.write(b"x" * 10)
                sink.write(b"y" * 7)
        with session.put_stream("model") as sink:  # exactly the limit is fine
            sink.write(b"z" * 16)
            sink.commit()
        with session.put_stream("model") as sink:
            with pytest.raises(UlduarError, match="spool limit"):
                sink.write(b"z" * 17)
            with pytest.raises(UlduarError, match="closed"):
                sink.commit()
    assert list(spool.iterdir()) == []
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 1


def test_a_large_file_is_hashed_in_chunks_and_a_stream_of_it_matches(config: Config, tmp_path: Path) -> None:
    data = os.urandom(3 * 1024**2 + 17)
    big = tmp_path / "big.bin"
    big.write_bytes(data)
    with open_archive(config) as session:
        from_file = session.put_file(big, "database_dump")
        with session.put_stream("database_dump") as sink:
            for start in range(0, len(data), 700_000):
                sink.write(data[start : start + 700_000])
            from_stream = sink.commit()
    assert from_file.checksum == from_stream.checksum == hashlib.sha256(data).digest()
    assert keys_under(config, _layout.objects_prefix("alpha", "default")) == [
        _layout.object_key("alpha", "default", hashlib.sha256(data).digest())
    ]


# --- fault injection: a failure between any two steps of a put ---


@pytest.mark.parametrize(
    ("method", "objects", "manifests", "artifacts"),
    [("put_new", 1, 0, 0), ("put_bytes_new", 1, 1, 0), ("head", 1, 1, 1)],
    ids=["after the object", "after the manifest", "inside the registration transaction"],
)
def test_a_failure_leaves_an_object_without_an_entry_never_an_entry_without_an_object_and_a_rerun_completes(
    config: Config, archivum: Archivum, source: Path, method: str, objects: int, manifests: int, artifacts: int
) -> None:
    record_id = _ids.uuid7()
    with faulty_archive(config, method=method) as (session, _), pytest.raises(Fault):
        session.put_file(source, "raw_file", retention_class="a", record_id=record_id)
    assert len(keys_under(config, _layout.objects_prefix("alpha", "default"))) == objects
    assert len(keys_under(config, _layout.manifests_prefix("alpha", "default"))) == manifests
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == artifacts
    assert registrations(archivum) == []  # never a registration without its object, and none before the end
    with open_archive(config) as session:
        done = session.put_file(source, "raw_file", retention_class="a", record_id=record_id)
    assert done.artifact_id == record_id
    assert keys_under(config, _layout.objects_prefix("alpha", "default")) == [object_key()]
    assert len(keys_under(config, _layout.manifests_prefix("alpha", "default"))) == 1
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 1
    assert registrations(archivum) == [(record_id, "a")]


def test_an_object_that_vanished_before_its_registration_is_stored_again_once(
    config: Config, archivum: Archivum, source: Path
) -> None:
    with faulty_archive(config, hide_head=1) as (session, store):
        stored = session.put_file(source, "raw_file")
        assert store.calls["put_new"] == 2
    assert registrations(archivum) == [(stored.artifact_id, None)]


def test_an_object_that_vanishes_twice_is_an_integrity_error_and_nothing_is_registered(
    config: Config, archivum: Archivum, source: Path
) -> None:
    with faulty_archive(config, hide_head=2) as (session, _):
        with pytest.raises(IntegrityError, match=f"^Object {object_key()} disappeared while it was being registered$"):
            session.put_file(source, "raw_file")
    assert registrations(archivum) == []


# --- concurrency, isolation, secrets ---


def test_two_sessions_putting_the_same_content_at_once_both_succeed_with_one_object(
    config: Config, archivum: Archivum, source: Path
) -> None:
    barrier = threading.Barrier(2)

    def put() -> Artifact:
        with open_archive(config) as session:
            barrier.wait(timeout=30)
            return session.put_file(source, "raw_file")

    with ThreadPoolExecutor(max_workers=2) as pool:
        stored = [call.result(timeout=60) for call in [pool.submit(put), pool.submit(put)]]
    assert stored[0].artifact_id != stored[1].artifact_id
    assert keys_under(config, _layout.objects_prefix("alpha", "default")) == [object_key()]
    assert len(registrations(archivum)) == 2


def test_another_projects_recorder_sees_and_registers_none_of_the_registrations(
    config: Config, archivum: Archivum, source: Path, tmp_path: Path
) -> None:
    with open_archive(config) as session:
        stored = session.put_file(source, "raw_file")
    other = onboarded(archivum, tmp_path, "beta")
    insert = (
        "INSERT INTO ulduar.archive_registration (project_id, scope, artifact_id, event_at)"
        " SELECT project_id, 'default', %s, %s FROM ulduar.project WHERE name = 'beta'"
    )
    with open_catalog(other) as catalog:
        seen = catalog.transaction(
            "Counting", lambda tx: tx.execute("SELECT count(*) FROM ulduar.archive_registration")
        )
        assert seen.fetchone() == (0,)
        with pytest.raises(UnknownRecordError):
            catalog.transaction("Registering", lambda tx: tx.execute(insert, (stored.artifact_id, stored.event_at)))
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_registration") == 1


@pytest.mark.parametrize("table", ARCHIVE_TABLES)
def test_the_recorder_cannot_update_delete_or_truncate_the_archive_tables(
    archivum: Archivum, config: Config, table: str
) -> None:
    env = recorder_of(archivum, config).env
    for statement in (
        f"UPDATE ulduar.{table} SET recorded_at = now()",
        f"DELETE FROM ulduar.{table}",
        f"TRUNCATE ulduar.{table}",
    ):
        error = failure(config, env, statement)
        assert isinstance(error, psycopg.errors.InsufficientPrivilege), statement


def test_the_recorder_cannot_declare_a_retention_rule(archivum: Archivum, config: Config) -> None:
    insert = (
        "INSERT INTO ulduar.archive_retention_rule (project_id, rule_id, event_at, retention_class, keep_newest,"
        " shortening_confirmed) SELECT project_id, %s, %s, 'a', 1, false FROM ulduar.project"
    )
    rule_id = _ids.uuid7()
    error = failure(config, recorder_of(archivum, config).env, insert, (rule_id, _ids.uuid7_time(rule_id)))
    assert isinstance(error, psycopg.errors.InsufficientPrivilege)


def test_no_credential_appears_in_the_log_records_of_a_put(
    config: Config, source: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert config.archive is not None
    names = [config.archivum.dsn_env, config.archivum.ledger_dsn_env]
    for credential in (config.archive.writer, config.archive.deleter):
        names += [] if credential is None else [credential.access_key_env, credential.secret_key_env]
    secrets = [os.environ[name] for name in names if name]
    secrets += [conninfo_to_dict(dsn)["password"] for dsn in secrets[:2]]  # the connection strings come first
    assert all(secrets)
    caplog.set_level(logging.DEBUG, logger="ulduar")
    with open_archive(config) as session:
        session.put_file(source, "raw_file")
        with session.put_stream("raw_file") as sink:
            sink.write(b"another")
            sink.commit()
    records = [r for r in caplog.records if r.name.startswith("ulduar")]
    assert any(r.name == "ulduar.hodir.archive" for r in records)
    text = "\n".join(f"{r.getMessage()} {r.exc_text or ''} {r.args}" for r in records)
    assert not [secret for secret in secrets if secret in text]
