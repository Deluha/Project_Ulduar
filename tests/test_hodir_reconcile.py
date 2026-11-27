"""ulduar.hodir reconcile, the incomplete-put rule, rebuild and the record sweep (H4, H11, H28).

The new record types, the key readers and the month filter are pure tests. Everything else runs on a real Archivum
and, per backend, a real directory or an unversioned bucket of the local object store (only the bucket cases are
marked `s3`). The grace period is measured by the database's clock against the store's modification time, which a
test can set only on a directory (`os.utime`) and never on a bucket: so the cases that need an object older than the
grace period run on the directory backend alone, and the bucket runs what needs no age (an entry without its object,
a young orphan or upload skipped and counted, the rule that leaves young things alone, rebuild, the sweep). A rebuild
is tested into a second, empty Archivum that onboards the same project name and points at the same store.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    CONTENT,
    CONTENT_CHECKSUM,
    Archivum,
    Fault,
    archive_config,
    backdate_registration,
    count,
    faulty_archive,
    ms_ago,
    onboarded,
    recorder_of,
)
from ulduar import _ids
from ulduar.config import Config
from ulduar.errors import (
    ArchiveFormatError,
    ArchiveUnavailable,
    ConfigurationError,
    UlduarError,
    UnknownRecordError,
)
from ulduar.freya import (
    Artifact,
    Dataset,
    ReapplyResult,
    Redaction,
    RetentionAction,
    apply_history_retention,
    declare_history_retention,
    onboard_project,
    open_catalog,
    reapply_removals,
    redact,
)
from ulduar.hodir import (
    Adoption,
    Archive,
    CleanupRun,
    Orphan,
    Rebuild,
    Reconciliation,
    UnfinishedUpload,
    _archivum,
    _layout,
    apply_retention,
    clean_incomplete,
    declare_retention,
    open_archive,
)
from ulduar.hodir import _store as store_module
from ulduar.hodir._store import open_store
from ulduar.hodir._sync import sync_records

_FILES = itertools.count()
NOW = datetime(2026, 6, 1, 12, 0, 0, 345000, tzinfo=UTC)
CUTOFF = datetime(2026, 5, 2, 12, 0, 0, 345678, tzinfo=UTC)
OLD_DAYS = 10  # past the default grace of 168 hours
RECORDS = "alpha/_records"


# --- helpers ---


def put(
    config: Config,
    tmp_path: Path,
    content: bytes,
    *,
    days: int = 0,
    retention_class: str | None = None,
    dataset: Dataset | None = None,
    origin: str | None = None,
    scope: str | None = None,
) -> Artifact:
    """Archive *content* as an artifact *days* old in its own session."""
    path = tmp_path / f"content-{next(_FILES)}.bin"
    path.write_bytes(content)
    with open_archive(config) as session:
        return session.put_file(
            path,
            "report",
            dataset=dataset,
            origin=origin,
            scope=scope,
            retention_class=retention_class,
            record_id=_ids.uuid7(ms_ago(days)),
        )


def put_aged(
    archivum: Archivum, config: Config, tmp_path: Path, content: bytes, days: int, **keywords: Any
) -> Artifact:
    """`put` of an artifact whose registration is *days* old (back-dated: the age retention counts)."""
    artifact = put(config, tmp_path, content, days=days, **keywords)
    backdate_registration(archivum, artifact, days)
    return artifact


def interrupted(config: Config, tmp_path: Path, content: bytes, method: str, **keywords: Any) -> uuid.UUID:
    """Start a put of *content* and let it fail after the store's *method* call; the artifact id it was made under."""
    path = tmp_path / f"content-{next(_FILES)}.bin"
    path.write_bytes(content)
    record_id = _ids.uuid7()
    with faulty_archive(config, method=method) as (session, _), pytest.raises(Fault):
        session.put_file(path, "report", record_id=record_id, **keywords)
    return record_id


def key_of(content: bytes, scope: str = "default") -> str:
    return _layout.object_key("alpha", scope, _checksum(content))


def _checksum(content: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(content).digest()


def manifest_key_of(artifact_id: uuid.UUID, scope: str = "default") -> str:
    return _layout.manifest_key("alpha", scope, artifact_id)


def store_of(config: Config) -> Any:
    assert config.archive is not None
    return open_store(config.archive, deleting=True)


def keys(config: Config, prefix: str) -> list[str]:
    return [stored.key for stored in store_of(config).list(prefix)]


def path_of(config: Config, key: str) -> Path:
    """The file of *key* in a directory archive."""
    settings = config.archive
    assert settings is not None and settings.path is not None
    return settings.path / settings.prefix / key


def backdate(config: Config, key: str, days: int = OLD_DAYS) -> None:
    """Make the file of *key* (directory archive) *days* old: the one way a test can age a stored object."""
    moment = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path_of(config, key), (moment, moment))


def stray_temporary(config: Config, *, days: int = 0) -> str:
    """Leave a temporary file where an interrupted directory put would, *days* old; its key."""
    key = f"{_layout.objects_prefix('alpha', 'default')}/ab/.tmp-{uuid.uuid4().hex}"
    path = path_of(config, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"partial")
    if days:
        backdate(config, key, days)
    return key


def check_rows(archivum: Archivum) -> list[tuple[Any, ...]]:
    with archivum.superuser() as su:
        return su.execute(
            "SELECT kind, mode, scope, objects_checked, findings FROM ulduar.archive_check ORDER BY recorded_at"
        ).fetchall()


def finding_rows(archivum: Archivum) -> list[tuple[Any, ...]]:
    with archivum.superuser() as su:
        return su.execute(
            "SELECT outcome, checksum, size_bytes FROM ulduar.archive_finding ORDER BY outcome, checksum"
        ).fetchall()


def deletion_rows(archivum: Archivum) -> list[tuple[Any, ...]]:
    with archivum.superuser() as su:
        return su.execute(
            "SELECT deletion_id, stage, intent_id, cause, rule_id, checksum FROM ulduar.archive_deletion"
            " ORDER BY recorded_at, deletion_id"
        ).fetchall()


def registered(archivum: Archivum) -> list[tuple[Any, ...]]:
    with archivum.superuser() as su:
        return su.execute(
            "SELECT artifact_id, retention_class FROM ulduar.archive_registration ORDER BY artifact_id"
        ).fetchall()


def stored_records(config: Config) -> list[_layout.MaintenanceRecord]:
    store = store_of(config)
    found = []
    for stored in store.list(RECORDS):
        with store.read(stored.key) as stream:
            found.append(_layout.decode_record(stream.read()))
    return found


@pytest.fixture
def dir_config(archivum: Archivum, tmp_path: Path, archive_settings_dir: Any) -> Config:
    """An onboarded config over a directory archive: the backend whose objects a test can age."""
    return archive_config(archivum, tmp_path, archive_settings_dir)


# --- the new records and the key readers (pure) ---


def redaction_record(**changes: Any) -> _layout.RedactionRecord:
    fields: dict[str, Any] = {
        "redaction_id": _ids.uuid7(1_700_000_000_000),
        "event_at": datetime.fromtimestamp(1_700_000_000, UTC),
        "project": "alpha",
        "history": "dataset",
        "record_id": _ids.uuid7(1_600_000_000_000),
        "field": "name",
        "reason": "secret",
    }
    return _layout.RedactionRecord(**{**fields, **changes})


def action_record(**changes: Any) -> _layout.HistoryRetentionRecord:
    fields: dict[str, Any] = {
        "action_id": _ids.uuid7(1_700_000_000_000),
        "event_at": datetime.fromtimestamp(1_700_000_000, UTC),
        "project": "alpha",
        "history": "artifact",
        "rule_id": _ids.uuid7(1_690_000_000_000),
        "keep_days": 30,
        "rule_event_at": datetime.fromtimestamp(1_690_000_000, UTC),
        "cutoff": CUTOFF,
        "rows_removed": 4,
    }
    return _layout.HistoryRetentionRecord(**{**fields, **changes})


def altered(record: Any, **changes: Any) -> bytes:
    """The record's JSON with *changes* applied (a key set to `...` is removed), for the strict readers to refuse."""
    raw = json.loads(record.encode())
    for name, value in changes.items():
        if value is ...:
            del raw[name]
        else:
            raw[name] = value
    return json.dumps(raw).encode()


def test_a_redaction_record_is_canonical_json_and_round_trips() -> None:
    record = redaction_record()
    data = record.encode()
    assert data.endswith(b"\n") and b" " not in data
    assert json.loads(data) == {
        "format": 1,
        "type": "redaction",
        "redaction_id": str(record.redaction_id),
        "event_at": "2023-11-14T22:13:20.000Z",
        "project": "alpha",
        "history": "dataset",
        "record_id": str(record.record_id),
        "field": "name",
        "reason": "secret",
    }
    assert _layout.RedactionRecord.decode(data) == record
    assert record.identifier == record.redaction_id != record.record_id


def test_a_history_retention_record_keeps_its_cutoff_to_the_microsecond() -> None:
    record = action_record()
    data = record.encode()
    assert json.loads(data)["cutoff"] == "2026-05-02T12:00:00.345678Z"
    assert json.loads(data)["rule_event_at"] == "2023-07-22T04:26:40.000Z"
    decoded = _layout.HistoryRetentionRecord.decode(data)
    assert decoded == record and decoded.cutoff == CUTOFF
    forever = action_record(keep_days=None)
    assert _layout.HistoryRetentionRecord.decode(forever.encode()) == forever
    assert record.identifier == record.action_id


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"type": "archive_deletion"}, "not of type redaction"),
        ({"format": 2}, "not format 1"),
        ({"history": "lineage_edge"}, "`history` must be one of"),
        ({"history": "artifact", "field": "name"}, "`field` must be one of"),
        ({"reason": "because"}, "`reason` must be one of"),
        ({"redaction_id": str(uuid.uuid4())}, "`redaction_id` must be a version 7 UUID"),
        ({"record_id": 7}, "`record_id` must be a non-empty string"),
        ({"event_at": "2023-11-14T22:13:20.000000Z"}, "`event_at` must be an ISO 8601 UTC time with milliseconds"),
        ({"project": "Alpha"}, "`project` must be a name"),
        ({"reason": ...}, "lacks a field"),
        ({"extra": 1}, "lacks a field or holds one"),
    ],
)
def test_a_redaction_record_that_is_not_format_one_is_refused_without_quoting_it(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ArchiveFormatError, match=message) as caught:
        _layout.RedactionRecord.decode(altered(redaction_record(), **changes))
    assert "alpha" not in str(caught.value)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"type": "redaction"}, "not of type history_retention_action"),
        ({"history": "dataset"}, "`history` must be one of"),
        ({"keep_days": 0}, "`keep_days` must be an integer of at least 1"),
        ({"keep_days": True}, "`keep_days` must be an integer of at least 1"),
        ({"rows_removed": -1}, "`rows_removed` must be an integer of at least 0"),
        ({"cutoff": "2026-05-02T12:00:00.345Z"}, "`cutoff` must be an ISO 8601 UTC time with microseconds"),
        ({"rule_event_at": "2026-05-02T12:00:00.345678Z"}, "`rule_event_at` must be an ISO 8601 UTC time with milli"),
        ({"rule_id": "x"}, "`rule_id` must be a version 7 UUID"),
    ],
)
def test_a_history_retention_record_that_is_not_format_one_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ArchiveFormatError, match=message):
        _layout.HistoryRetentionRecord.decode(altered(action_record(), **changes))


def test_decode_record_dispatches_on_the_type_and_refuses_an_unknown_one() -> None:
    deletion = _layout.DeletionRecord(
        _ids.uuid7(), NOW, NOW, "alpha", "default", CONTENT_CHECKSUM, 3, "intent", None, "incomplete_put", None, None
    )
    for record in (deletion, redaction_record(), action_record()):
        assert _layout.decode_record(record.encode()) == record
    with pytest.raises(ArchiveFormatError, match="no type this library knows"):
        _layout.decode_record(altered(redaction_record(), type="purge"))
    with pytest.raises(ArchiveFormatError, match="no type this library knows"):
        _layout.decode_record(b"[1]")
    with pytest.raises(ArchiveFormatError, match="not valid JSON"):
        _layout.decode_record(b"{")


def test_a_manifest_names_an_artifact_kind() -> None:
    manifest = _layout.Manifest(_ids.uuid7(), NOW, "alpha", "default", "report", CONTENT_CHECKSUM, 1, None, None)
    assert _layout.Manifest.decode(manifest.encode()) == manifest
    with pytest.raises(ArchiveFormatError, match="`kind` must be one of"):
        _layout.Manifest.decode(altered(manifest, kind="spreadsheet"))


def test_a_manifest_key_and_a_record_key_read_back_and_a_wrong_month_does_not() -> None:
    artifact_id = _ids.uuid7(1_700_000_000_000)
    relative = _layout.manifest_key("alpha", "default", artifact_id).removeprefix("alpha/")
    assert _layout.parse_manifest_key(relative) == ("default", artifact_id)
    assert _layout.parse_manifest_key(relative.replace("/2023/11/", "/2023/10/")) is None
    assert _layout.parse_manifest_key(relative.replace("default", "Default")) is None
    assert _layout.parse_manifest_key(relative.replace(".json", ".tmp")) is None
    assert _layout.parse_manifest_key(f"default/_manifests/2023/11/{uuid.uuid4()}.json") is None
    record = _layout.record_key("alpha", artifact_id).removeprefix("alpha/")
    assert _layout.parse_record_key(record) == artifact_id
    assert _layout.parse_record_key(record.replace("/11/", "/12/")) is None
    assert _layout.parse_record_key("_records/2023/11/readme.txt") is None


def test_is_since_compares_months_in_utc() -> None:
    record_id = _ids.uuid7(1_700_000_000_000)  # 2023-11-14
    assert _layout.is_since(record_id, None)
    assert _layout.is_since(record_id, datetime(2023, 11, 30, 23, tzinfo=UTC))
    assert not _layout.is_since(record_id, datetime(2023, 12, 1, tzinfo=UTC))


# --- reconcile ---


def test_a_clean_archive_reconciles_without_findings_and_the_check_is_recorded(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    put(config, tmp_path, b"one")
    put(config, tmp_path, b"two")
    with open_archive(config) as session:
        result = session.reconcile()
    assert isinstance(result, Reconciliation)
    assert (result.scope, result.objects_listed, result.skipped_young, result.findings) == ("default", 2, 0, ())
    assert result.check_id.version == 7 and result.event_at == _ids.uuid7_time(result.check_id)
    assert check_rows(archivum) == [("reconciliation", "full", "default", 2, 0)]
    assert finding_rows(archivum) == []


def test_a_live_address_whose_object_is_gone_is_an_entry_without_an_object(
    config: Config, archivum: Archivum, tmp_path: Path, tamper: Callable[[str, bytes | None], None]
) -> None:
    gone = put(config, tmp_path, b"gone")
    kept = put(config, tmp_path, b"kept")
    tamper(key_of(b"gone"), None)  # no grace period applies: a put writes the object before its registration
    with open_archive(config) as session:
        result = session.reconcile()
    assert result.objects_listed == 1
    [finding] = result.findings
    assert (finding.scope, finding.checksum, finding.size) == ("default", gone.checksum, gone.size)
    assert finding.outcome == "entry_without_object" and finding.checksum != kept.checksum
    assert check_rows(archivum) == [("reconciliation", "full", "default", 1, 1)]
    assert finding_rows(archivum) == [("entry_without_object", gone.checksum, gone.size)]


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_a_live_key_that_is_a_link_or_a_directory_is_an_entry_without_an_object(
    dir_config: Config, archivum: Archivum, tmp_path: Path, replacement: str
) -> None:
    [redaction] = make_redactions(dir_config, tmp_path)  # a record the store lacks: the sweep must still run
    replaced = put(dir_config, tmp_path, b"replaced")
    path = path_of(dir_config, key_of(b"replaced"))
    path.parent.chmod(0o755)
    path.unlink()
    if replacement == "symlink":
        target = tmp_path / "elsewhere.bin"
        target.write_bytes(b"replaced")
        path.symlink_to(target)
    else:
        path.mkdir()
    with open_archive(dir_config) as session:
        result = session.reconcile()  # the store refuses the key: it must not raise
    assert [(f.outcome, f.checksum) for f in result.findings] == [("entry_without_object", replaced.checksum)]
    assert check_rows(archivum) == [("reconciliation", "full", "default", 1, 1)]
    assert finding_rows(archivum) == [("entry_without_object", replaced.checksum, replaced.size)]
    assert [record.identifier for record in stored_records(dir_config)] == [redaction.redaction_id]


def test_an_address_whose_lock_is_held_too_long_is_counted_not_reported(
    config: Config, archivum: Archivum, tmp_path: Path, tamper: Callable[[str, bytes | None], None]
) -> None:
    held = put(config, tmp_path, b"held")
    gone = put(config, tmp_path, b"gone")
    tamper(key_of(b"held"), None)
    tamper(key_of(b"gone"), None)
    config = replace(config, archivum=replace(config.archivum, lock_timeout_s=1))
    with archivum.superuser() as su:
        project_id = su.execute("SELECT project_id FROM ulduar.project WHERE name = 'alpha'").fetchone()[0]
        name = _archivum.address_lock_name(project_id, "default", held.checksum)
        su.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (name,))  # another session holds the address
        try:
            with open_archive(config) as session:
                result = session.reconcile()
        finally:
            su.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (name,))
    assert [f.checksum for f in result.findings] == [gone.checksum]  # the unchecked address is no finding
    assert result.skipped_busy == 1
    assert check_rows(archivum) == [("reconciliation", "full", "default", 0, 1)]


def test_a_young_orphan_is_skipped_and_counted_not_reported(config: Config, archivum: Archivum, tmp_path: Path) -> None:
    interrupted(config, tmp_path, b"half", "put_new")  # the object, no manifest and no entry
    with open_archive(config) as session:
        result = session.reconcile()
    assert (result.objects_listed, result.skipped_young, result.findings) == (1, 1, ())
    assert check_rows(archivum) == [("reconciliation", "full", "default", 1, 0)]


def test_a_scope_is_reconciled_alone_and_an_unknown_one_is_refused_before_any_listing(
    config: Config, archivum: Archivum, tmp_path: Path, tamper: Callable[[str, bytes | None], None]
) -> None:
    onboard_project(config, "alpha", recorder_of(archivum, config).role, scopes=("default", "staging"))
    put(config, tmp_path, b"here")
    put(config, tmp_path, b"there", scope="staging")
    tamper(key_of(b"here"), None)
    with open_archive(config) as session:
        assert session.reconcile(scope="staging").findings == ()
        assert [f.outcome for f in session.reconcile().findings] == ["entry_without_object"]
        with pytest.raises(UnknownRecordError, match="Scope elsewhere is not onboarded"):
            session.reconcile(scope="elsewhere")
        with pytest.raises(ValueError, match="scope"):
            session.reconcile(scope="../x")
    assert [row[2] for row in check_rows(archivum)] == ["staging", "default"]


def test_a_young_unfinished_bucket_upload_is_skipped_and_counted(
    archivum: Archivum, tmp_path: Path, archive_settings_s3: Any, s3_client: Any
) -> None:
    config = archive_config(archivum, tmp_path, archive_settings_s3)
    s3_client.create_multipart_upload(
        Bucket=archive_settings_s3.bucket, Key=f"{archive_settings_s3.prefix}/{key_of(CONTENT)}"
    )
    with open_archive(config) as session:
        result = session.reconcile()
    assert (result.objects_listed, result.skipped_young, result.findings) == (0, 1, ())
    assert [u.key for u in open_store(archive_settings_s3).list_unfinished("alpha")] == [key_of(CONTENT)]


def test_an_orphan_older_than_the_grace_period_is_an_object_without_an_entry(
    dir_config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    interrupted(dir_config, tmp_path, b"bare", "put_new")
    interrupted(dir_config, tmp_path, b"manifested", "put_bytes_new")
    interrupted(dir_config, tmp_path, b"young", "put_new")
    put(dir_config, tmp_path, b"registered")
    stray = f"{_layout.objects_prefix('alpha', 'default')}/zz/stray.bin"
    path_of(dir_config, stray).parent.mkdir(parents=True)
    path_of(dir_config, stray).write_bytes(b"?")
    for key in (key_of(b"bare"), key_of(b"manifested"), stray):
        backdate(dir_config, key)
    backdate(dir_config, key_of(b"registered"))  # a registered object is never an orphan, however old
    with open_archive(dir_config) as session:
        result = session.reconcile()
    assert (result.objects_listed, result.skipped_young) == (5, 1)
    found = {(f.outcome, f.checksum, f.size) for f in result.findings}
    assert found == {
        ("object_without_entry", _checksum(b"bare"), 4),
        ("object_without_entry", _checksum(b"manifested"), 10),
        ("object_without_entry", None, None),  # an unparseable key under `_objects`
    }
    assert check_rows(archivum) == [("reconciliation", "full", "default", 5, 3)]


def test_an_unfinished_upload_is_reported_once_it_is_older_than_the_grace_period(
    dir_config: Config, archivum: Archivum
) -> None:
    young = stray_temporary(dir_config)
    with open_archive(dir_config) as session:
        first = session.reconcile()
        assert (first.objects_listed, first.skipped_young, first.findings) == (0, 1, ())
        backdate(dir_config, young)
        second = session.reconcile()
    assert second.skipped_young == 0
    [finding] = second.findings
    assert (finding.outcome, finding.checksum, finding.size) == ("unfinished_upload", None, None)
    assert finding_rows(archivum) == [("unfinished_upload", None, None)]
    assert path_of(dir_config, young).exists()  # reconcile only reports: `clean_incomplete` aborts


# --- the built-in incomplete-put rule ---


def leftovers(config: Config, tmp_path: Path) -> dict[str, Any]:
    """On a directory archive: one orphan, two adoptable puts, a young orphan and an old temporary file."""
    with open_catalog(config) as catalog:
        dataset = catalog.register_dataset("ns", "events")
    found: dict[str, Any] = {"dataset": dataset}
    found["orphan"] = interrupted(config, tmp_path, b"orphan", "put_new")
    found["manifested"] = interrupted(
        config, tmp_path, b"manifested", "put_bytes_new", dataset=dataset, retention_class="short"
    )
    # The artifact is recorded (with its origin) but the registration never was: the put stopped at the head check.
    found["recorded"] = interrupted(config, tmp_path, b"recorded", "head", origin="src/recorded.csv")
    found["young"] = interrupted(config, tmp_path, b"young", "put_new")
    for content in (b"orphan", b"manifested", b"recorded"):
        backdate(config, key_of(content))
    for artifact_id in (found["manifested"], found["recorded"]):
        backdate(config, manifest_key_of(artifact_id))
    found["temporary"] = stray_temporary(config, days=OLD_DAYS)
    return found


def test_a_dry_run_lists_what_the_rule_would_do_and_changes_nothing(
    dir_config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    found = leftovers(dir_config, tmp_path)
    before = keys(dir_config, "alpha")
    run = clean_incomplete(dir_config)
    assert isinstance(run, CleanupRun) and run.dry_run is True
    assert {a.artifact_id for a in run.adopt} == {found["manifested"], found["recorded"]}
    assert all(isinstance(a, Adoption) for a in run.adopt)
    assert run.remove == (Orphan("default", _checksum(b"orphan"), 6),)
    [upload] = run.abort
    assert isinstance(upload, UnfinishedUpload) and upload.key == found["temporary"]
    assert [field.name for field in fields(UnfinishedUpload)] == [
        "key",
        "started",
    ]  # the store's upload id stays private
    assert upload.started < datetime.now(UTC) - timedelta(days=OLD_DAYS - 1)
    assert (run.open_intents, run.outcomes, run.adopted, run.datasets_missing) == ((), (), (), 0)
    assert keys(dir_config, "alpha") == before
    assert path_of(dir_config, found["temporary"]).exists()
    assert registered(archivum) == [] and deletion_rows(archivum) == []


def test_a_real_run_adopts_removes_and_aborts_and_records_the_removal(
    dir_config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    found = leftovers(dir_config, tmp_path)
    run = clean_incomplete(dir_config, dry_run=False)
    assert run.dry_run is False
    adopted = {a.artifact_id: a for a in run.adopted}
    assert set(adopted) == {found["manifested"], found["recorded"]}
    assert adopted[found["manifested"]].dataset_id == found["dataset"].dataset_id  # the manifest's dataset, held
    assert adopted[found["recorded"]].origin == "src/recorded.csv"  # the catalog's own entry, origin kept
    assert run.datasets_missing == 0
    assert dict(registered(archivum)) == {found["manifested"]: "short", found["recorded"]: None}
    with open_archive(dir_config) as session:
        for name, content in (("manifested", b"manifested"), ("recorded", b"recorded")):
            assert session.get(adopted[found[name]], tmp_path / name).read_bytes() == content
    [outcome] = run.outcomes
    assert (outcome.stage, outcome.cause, outcome.checksum, outcome.size) == (
        "confirmed",
        "incomplete_put",
        _checksum(b"orphan"),
        6,
    )
    assert [(row[1], row[3], row[4]) for row in deletion_rows(archivum)] == [
        ("intent", "incomplete_put", None),
        ("confirmed", "incomplete_put", None),
    ]
    assert {r.stage for r in stored_records(dir_config)} == {"intent", "confirmed"}  # both written to the store
    assert not path_of(dir_config, key_of(b"orphan")).exists()
    assert path_of(dir_config, key_of(b"young")).exists()  # younger than the grace period: untouched
    assert not path_of(dir_config, found["temporary"]).exists()
    again = clean_incomplete(dir_config, dry_run=False)
    assert (again.adopt, again.remove, again.abort, again.outcomes, again.adopted) == ((), (), (), (), ())


def test_an_object_older_than_the_grace_period_with_a_young_manifest_is_left_for_its_put(
    dir_config: Config, tmp_path: Path
) -> None:
    interrupted(dir_config, tmp_path, b"running", "put_bytes_new")  # the manifest is young, the object is not
    backdate(dir_config, key_of(b"running"))
    run = clean_incomplete(dir_config, dry_run=False)
    assert (run.adopt, run.remove, run.outcomes) == ((), (), ())
    assert path_of(dir_config, key_of(b"running")).exists()


def test_young_leftovers_are_untouched_by_a_real_run_on_every_backend(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    interrupted(config, tmp_path, b"bare", "put_new")
    interrupted(config, tmp_path, b"manifested", "put_bytes_new")
    before = keys(config, "alpha")
    run = clean_incomplete(config, dry_run=False)
    assert (run.adopt, run.remove, run.abort, run.open_intents, run.outcomes, run.adopted) == ((),) * 6
    assert keys(config, "alpha") == before
    assert registered(archivum) == [] and deletion_rows(archivum) == []


def test_a_manifest_that_cannot_be_read_stops_the_rule_before_anything_changes(config: Config, tmp_path: Path) -> None:
    interrupted(config, tmp_path, b"bare", "put_new")
    bad = manifest_key_of(_ids.uuid7())
    store_of(config).put_bytes_new(bad, b"not-a-manifest-secret-text")
    for dry_run in (True, False):
        with pytest.raises(ArchiveFormatError, match="cannot be read") as caught:
            clean_incomplete(config, dry_run=dry_run)
        assert bad in str(caught.value) and "secret-text" not in str(caught.value)
    assert store_of(config).head(key_of(b"bare")) is not None


def test_an_open_incomplete_put_intent_is_resumed_by_the_next_run(
    dir_config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupted(dir_config, tmp_path, b"stuck", "put_new")
    backdate(dir_config, key_of(b"stuck"))

    class Refusing:
        def __init__(self, store: Any) -> None:
            self._store = store

        def __getattr__(self, name: str) -> Any:
            return getattr(self._store, name)

        def delete_every_version(self, key: str) -> None:
            raise ArchiveUnavailable(f"Deleting {key} failed: injected")

    with monkeypatch.context() as patch:
        patch.setattr(
            store_module,
            "open_store",
            lambda settings, *, deleting=False: Refusing(open_store(settings, deleting=True)),
        )
        [outcome] = clean_incomplete(dir_config, dry_run=False).outcomes
    assert outcome.stage == "open"
    assert [row[1] for row in deletion_rows(archivum)] == ["intent"]
    [resumed] = clean_incomplete(dir_config, dry_run=False).outcomes
    assert (resumed.stage, resumed.deletion_id) == ("confirmed", outcome.deletion_id)
    assert [row[1] for row in deletion_rows(archivum)] == ["intent", "confirmed"]


def test_an_open_intent_whose_object_was_registered_since_is_listed_and_cancelled(
    dir_config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupted(dir_config, tmp_path, b"stuck", "put_new")
    backdate(dir_config, key_of(b"stuck"))

    class Refusing:
        def __init__(self, store: Any) -> None:
            self._store = store

        def __getattr__(self, name: str) -> Any:
            return getattr(self._store, name)

        def delete_every_version(self, key: str) -> None:
            raise ArchiveUnavailable(f"Deleting {key} failed: injected")

    with monkeypatch.context() as patch:
        patch.setattr(
            store_module,
            "open_store",
            lambda settings, *, deleting=False: Refusing(open_store(settings, deleting=True)),
        )
        clean_incomplete(dir_config, dry_run=False)
    put(dir_config, tmp_path, b"stuck")  # the content is registered after the intent
    dry = clean_incomplete(dir_config)
    assert [(o.checksum, o.size) for o in dry.open_intents] == [(_checksum(b"stuck"), 5)] and dry.remove == ()
    [outcome] = clean_incomplete(dir_config, dry_run=False).outcomes
    assert outcome.stage == "cancelled"
    assert path_of(dir_config, key_of(b"stuck")).exists()
    assert [row[1] for row in deletion_rows(archivum)] == ["intent", "cancelled"]


def test_a_dry_run_needs_no_deleter_and_a_real_run_needs_one(
    archive_settings_s3: Any, archivum: Archivum, tmp_path: Path
) -> None:
    config = archive_config(archivum, tmp_path, replace(archive_settings_s3, deleter=None))
    assert clean_incomplete(config).dry_run is True
    with pytest.raises(ConfigurationError, match="archive.deleter"):
        clean_incomplete(config, dry_run=False)


def test_the_rule_refuses_a_missing_archive_section_and_a_bad_or_unknown_scope(config: Config) -> None:
    with pytest.raises(ConfigurationError, match="`archive` is required"):
        clean_incomplete(replace(config, archive=None))
    with pytest.raises(ValueError, match="scope"):
        clean_incomplete(config, scope="a/b")
    with pytest.raises(UnknownRecordError, match="Scope elsewhere is not onboarded"):
        clean_incomplete(config, scope="elsewhere")


def test_a_retry_with_an_origin_of_a_put_adopted_from_its_manifest_returns_the_adopted_artifact(
    dir_config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    record_id = interrupted(dir_config, tmp_path, b"half", "put_bytes_new", origin="src/half.csv")
    backdate(dir_config, key_of(b"half"))
    backdate(dir_config, manifest_key_of(record_id))
    [adopted] = clean_incomplete(dir_config, dry_run=False).adopted  # a manifest holds no origin
    assert (adopted.artifact_id, adopted.origin) == (record_id, None)
    retry = tmp_path / "retry.bin"
    retry.write_bytes(b"half")
    with open_archive(dir_config) as session:
        assert session.put_file(retry, "report", origin="src/half.csv", record_id=record_id) == adopted
        with pytest.raises(UlduarError, match="already has a different manifest"):
            session.put_file(retry, "raw_file", origin="src/half.csv", record_id=record_id)
    assert [row[0] for row in registered(archivum)] == [record_id]


def test_a_stale_manifest_of_a_confirmed_deletion_is_removed_and_a_rebuild_adopts_only_the_new_artifact(
    dir_config: Config, archivum: Archivum, second_archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = put_aged(archivum, dir_config, tmp_path, b"again", 100, retention_class="short")
    declare_retention(dir_config, "alpha", "short", 30, confirm_shortening=True)

    class ManifestsStay:
        def __init__(self, store: Any) -> None:
            self._store = store

        def __getattr__(self, name: str) -> Any:
            return getattr(self._store, name)

        def delete_every_version(self, key: str) -> None:
            if "/_manifests/" in key:
                raise ArchiveUnavailable(f"Deleting {key} failed: injected")
            self._store.delete_every_version(key)

    with monkeypatch.context() as patch:
        patch.setattr(
            store_module,
            "open_store",
            lambda settings, *, deleting=False: ManifestsStay(open_store(settings, deleting=True)),
        )
        [outcome] = apply_retention(dir_config, dry_run=False).outcomes
    assert outcome.stage == "confirmed" and path_of(dir_config, manifest_key_of(old.artifact_id)).exists()
    new = put(dir_config, tmp_path, b"again")  # the same content, put again under a new record id
    deletions = deletion_rows(archivum)
    young = clean_incomplete(dir_config, dry_run=False)  # the stale manifest is younger than the grace: kept
    assert (young.stale_manifests, young.outcomes) == ((), ())
    assert path_of(dir_config, manifest_key_of(old.artifact_id)).exists()
    backdate(dir_config, manifest_key_of(old.artifact_id))
    dry = clean_incomplete(dir_config)
    assert dry.stale_manifests == (manifest_key_of(old.artifact_id),)
    assert path_of(dir_config, manifest_key_of(old.artifact_id)).exists()  # a dry run deletes nothing
    run = clean_incomplete(dir_config, dry_run=False)
    assert run.stale_manifests == (manifest_key_of(old.artifact_id),)
    assert (run.adopt, run.remove, run.outcomes) == ((), (), ())
    assert not path_of(dir_config, manifest_key_of(old.artifact_id)).exists()
    assert path_of(dir_config, manifest_key_of(new.artifact_id)).exists()  # the live artifact keeps its manifest
    assert deletion_rows(archivum) == deletions  # no deletion record for a manifest
    assert clean_incomplete(dir_config).stale_manifests == ()
    second = rebuilt_config(second_archivum, tmp_path, dir_config)
    with open_archive(second) as session:
        adopted = session.rebuild().adopted
    assert [a.artifact_id for a in adopted] == [new.artifact_id]  # the deleted artifact stays gone


def test_the_record_sweep_runs_even_when_a_step_of_a_real_run_raises(
    dir_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stray_temporary(dir_config, days=OLD_DAYS)  # an unfinished upload to abort, which the store refuses
    [redaction] = make_redactions(dir_config, tmp_path)  # a record the Archivum holds and the store lacks
    assert stored_records(dir_config) == []

    class Refusing:
        def __init__(self, store: Any) -> None:
            self._store = store

        def __getattr__(self, name: str) -> Any:
            return getattr(self._store, name)

        def abort_unfinished(self, upload: Any) -> None:
            raise ArchiveUnavailable("Aborting the upload failed: injected")

    monkeypatch.setattr(
        store_module, "open_store", lambda settings, *, deleting=False: Refusing(open_store(settings, deleting=True))
    )
    with pytest.raises(ArchiveUnavailable, match="injected"):
        clean_incomplete(dir_config, dry_run=False)
    assert [record.identifier for record in stored_records(dir_config)] == [redaction.redaction_id]


# --- the record sweep ---


def make_redactions(config: Config, tmp_path: Path, n: int = 1) -> list[Redaction]:
    return [
        redact(
            config,
            "alpha",
            "artifact",
            put(config, tmp_path, f"r{i}".encode(), origin="o").artifact_id,
            "origin",
            "secret",
        )
        for i in range(n)
    ]


def test_every_record_is_in_the_store_exactly_once_after_verify_retention_and_redaction(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    put_aged(archivum, config, tmp_path, b"old", 100, retention_class="short")
    declare_retention(config, "alpha", "short", 30, confirm_shortening=True)
    apply_retention(config, dry_run=False)  # an intent and a confirmation, both written by the protocol itself
    [redaction] = make_redactions(config, tmp_path)
    declare_history_retention(config, "alpha", "artifact", 1)
    [applied] = apply_history_retention(config, dry_run=False, now=datetime.now(UTC) + timedelta(days=10))
    assert applied.rows == 1
    assert len(keys(config, RECORDS)) == 2
    with open_archive(config) as session:
        session.verify()
    records = stored_records(config)
    assert sorted(type(r).__name__ for r in records) == [
        "DeletionRecord",
        "DeletionRecord",
        "HistoryRetentionRecord",
        "RedactionRecord",
    ]
    [copy] = [r for r in records if isinstance(r, _layout.RedactionRecord)]
    assert (copy.redaction_id, copy.record_id, copy.field, copy.reason) == (
        redaction.redaction_id,
        redaction.record_id,
        "origin",
        "secret",
    )
    [action] = [r for r in records if isinstance(r, _layout.HistoryRetentionRecord)]
    assert (action.history, action.keep_days, action.rows_removed, action.cutoff) == ("artifact", 1, 1, applied.cutoff)
    with open_archive(config) as session:  # nothing is written twice, whatever the call
        session.verify()
        session.reconcile()
    apply_retention(config)
    clean_incomplete(config)
    assert len(keys(config, RECORDS)) == 4
    with open_catalog(config) as catalog:
        assert sync_records(catalog, open_store(config.archive)) == 0  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda config, session: session.verify(), id="verify"),
        pytest.param(lambda config, session: session.reconcile(), id="reconcile"),
        pytest.param(lambda config, session: clean_incomplete(config), id="clean_incomplete dry run"),
        pytest.param(lambda config, session: clean_incomplete(config, dry_run=False), id="clean_incomplete"),
        pytest.param(lambda config, session: apply_retention(config), id="apply_retention dry run"),
        pytest.param(lambda config, session: apply_retention(config, dry_run=False), id="apply_retention"),
    ],
)
def test_each_maintenance_call_copies_the_records_the_store_lacks(
    config: Config, tmp_path: Path, call: Callable[[Config, Archive], Any]
) -> None:
    [redaction] = make_redactions(config, tmp_path)
    assert keys(config, RECORDS) == []
    with open_archive(config) as session:
        call(config, session)
    assert keys(config, RECORDS) == [_layout.record_key("alpha", redaction.redaction_id)]


class RecordsRefused:
    """A writing store that refuses to write a record, as an unreachable store would, and forwards the rest."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def put_bytes_new(self, key: str, data: bytes) -> bool:
        if "/_records/" in key:
            raise ArchiveUnavailable(f"Putting {key} failed: injected")
        return self._store.put_bytes_new(key, data)


def test_a_sweep_that_fails_does_not_fail_the_call_and_the_next_call_catches_up(
    config: Config, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    [redaction] = make_redactions(config, tmp_path)
    assert config.archive is not None
    caplog.set_level(logging.WARNING, logger="ulduar.hodir")
    with open_catalog(config) as catalog:
        session = Archive(catalog, RecordsRefused(open_store(config.archive)), config.archive)  # type: ignore[arg-type]
        assert session.verify().failed == ()  # the call's own result stands
        assert session.reconcile().findings == ()
    assert any("were not all copied to the archive" in r.getMessage() for r in caplog.records)
    assert keys(config, RECORDS) == []
    with open_archive(config) as session:
        session.verify()
    assert keys(config, RECORDS) == [_layout.record_key("alpha", redaction.redaction_id)]


# --- rebuild ---


@pytest.fixture
def second_archivum(db_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Archivum]:
    """A second, empty Archivum database: the one a lost Archivum is rebuilt into."""
    instance = Archivum(db_dsn, monkeypatch)
    try:
        instance.create()
        yield instance
    finally:
        instance.drop()


def rebuilt_config(second: Archivum, tmp_path: Path, config: Config) -> Config:
    """A config of the same project name on the second Archivum, pointing at the first one's store."""
    directory = tmp_path / "second"
    directory.mkdir()
    return replace(onboarded(second, directory), archive=config.archive)


def populate(archivum: Archivum, config: Config, tmp_path: Path) -> dict[str, Any]:
    """Archive a dataset's objects, delete one by retention, archive its content again, redact and age history out."""
    with open_catalog(config) as catalog:
        dataset = catalog.register_dataset("ns", "events")
    made: dict[str, Any] = {"dataset": dataset}
    made["a"] = put(config, tmp_path, b"a", dataset=dataset, retention_class="short", origin="src/a.csv")
    made["b"] = put(config, tmp_path, b"b", dataset=dataset, retention_class="short")
    made["c_old"] = put_aged(archivum, config, tmp_path, b"c", 100, retention_class="short")
    declare_retention(config, "alpha", "short", 30, confirm_shortening=True)
    [outcome] = apply_retention(config, dry_run=False).outcomes
    assert outcome.stage == "confirmed"
    made["c_new"] = put(config, tmp_path, b"c")  # the same content, registered after its deletion
    made["redactions"] = [
        redact(config, "alpha", "artifact", made["a"].artifact_id, "origin", "personal_data"),
        redact(config, "alpha", "dataset", dataset.dataset_id, "name", "secret"),
    ]
    declare_history_retention(config, "alpha", "artifact", 1)
    [made["action"]] = apply_history_retention(config, dry_run=False, now=datetime.now(UTC) + timedelta(days=10))
    with open_archive(config) as session:
        session.verify()  # copies the redactions and the action to the store
    return made


def test_rebuild_restores_deletions_first_then_registrations_and_lists_the_removals(
    config: Config, archivum: Archivum, second_archivum: Archivum, tmp_path: Path
) -> None:
    made = populate(archivum, config, tmp_path)
    second = rebuilt_config(second_archivum, tmp_path, config)
    with open_archive(second) as session:
        assert session.rebuild(since=datetime.now(UTC) + timedelta(days=62)) == Rebuild(0, (), 0, ())
        with pytest.raises(ValueError, match="timezone-aware"):
            session.rebuild(since=datetime(2026, 1, 1))
        result = session.rebuild()
        wanted = {made["a"].artifact_id, made["b"].artifact_id, made["c_new"].artifact_id}
        assert isinstance(result, Rebuild)
        assert result.deletions_restored == 2
        assert {a.artifact_id for a in result.adopted} == wanted
        assert result.datasets_missing == 2  # the dataset's id is unknown to the new Archivum
        assert count(second_archivum, "SELECT count(*) FROM ulduar.artifact") == 3  # the deleted one stays gone
        original = {row[0] for row in deletion_rows(archivum)}
        assert {row[0] for row in deletion_rows(second_archivum)} == original
        assert dict(registered(second_archivum)) == {
            made["a"].artifact_id: "short",
            made["b"].artifact_id: "short",
            made["c_new"].artifact_id: None,
        }
        # The content deleted by retention and then archived again is live: its deletion was restored first.
        assert session.get(made["c_new"], tmp_path / "c").read_bytes() == b"c"
        assert session.get(made["a"], tmp_path / "a").read_bytes() == b"a"
        adopted = {a.artifact_id: a for a in result.adopted}[made["a"].artifact_id]
        assert (adopted.origin, adopted.dataset_id, adopted.checksum, adopted.event_at) == (
            None,
            None,
            made["a"].checksum,
            made["a"].event_at,
        )
        again = session.rebuild()
    assert (again.deletions_restored, again.adopted, again.datasets_missing) == (0, (), 0)
    assert again.removals == result.removals
    first, second_redaction = made["redactions"]
    action = made["action"]
    assert [type(r) for r in result.removals] == [Redaction, Redaction, RetentionAction]
    assert result.removals[:2] == (first, second_redaction)
    [removal] = [r for r in result.removals if isinstance(r, RetentionAction)]
    assert (removal.history, removal.rule_id, removal.keep_days, removal.rows_removed) == (
        "artifact",
        action.rule_id,
        1,
        1,
    )
    assert removal.cutoff == action.cutoff  # to the microsecond
    # The recording role only listed them: the ledger role applies them, and again changes nothing.
    assert count(second_archivum, "SELECT count(*) FROM ulduar.redaction") == 0
    assert reapply_removals(second, result.removals) == ReapplyResult(2, 0, 1)
    assert reapply_removals(second, result.removals) == ReapplyResult(0, 2, 1)
    assert count(second_archivum, "SELECT count(*) FROM ulduar.redaction WHERE history = 'artifact'") == 1
    assert count(second_archivum, "SELECT count(*) FROM ulduar.redaction WHERE history = 'dataset'") == 0
    assert count(second_archivum, "SELECT count(*) FROM ulduar.artifact WHERE origin IS NULL") == 3
    assert count(second_archivum, "SELECT count(*) FROM ulduar.history_retention_rule") == 1
    with second_archivum.superuser() as su:
        row = su.execute("SELECT rows_removed, cutoff, rule_id FROM ulduar.history_retention_action").fetchone()
    assert row == (1, action.cutoff, removal.rule_id)
    assert count(second_archivum, "SELECT count(*) FROM ulduar.artifact") == 3  # the archived ones were skipped


def test_rebuild_leaves_a_manifest_whose_object_is_gone_and_refuses_a_record_it_cannot_read(
    config: Config, second_archivum: Archivum, tmp_path: Path, tamper: Callable[[str, bytes | None], None]
) -> None:
    put(config, tmp_path, b"kept")
    put(config, tmp_path, b"lost")
    tamper(key_of(b"lost"), None)
    second = rebuilt_config(second_archivum, tmp_path, config)
    store_of(config).put_bytes_new(_layout.record_key("alpha", _ids.uuid7()), b'{"type":"redaction"}')
    with open_archive(second) as session:
        with pytest.raises(ArchiveFormatError, match="cannot be read"):
            session.rebuild()
        assert count(second_archivum, "SELECT count(*) FROM ulduar.artifact") == 0  # nothing was changed
    for stored in store_of(config).list(RECORDS):
        store_of(config).delete_every_version(stored.key)
    with open_archive(second) as session:
        [adopted] = session.rebuild().adopted
    assert adopted.checksum == _checksum(b"kept")


def test_rebuild_refuses_a_deletion_of_a_scope_the_new_archivum_lacks(
    config: Config, archivum: Archivum, second_archivum: Archivum, tmp_path: Path
) -> None:
    onboard_project(config, "alpha", recorder_of(archivum, config).role, scopes=("default", "staging"))
    put_aged(archivum, config, tmp_path, b"staged", 100, retention_class="short", scope="staging")
    declare_retention(config, "alpha", "short", 30, confirm_shortening=True)
    apply_retention(config, dry_run=False)
    second = rebuilt_config(second_archivum, tmp_path, config)
    with open_archive(second) as session, pytest.raises(UnknownRecordError, match="scope staging, not onboarded"):
        session.rebuild()
    assert count(second_archivum, "SELECT count(*) FROM ulduar.archive_deletion") == 0
