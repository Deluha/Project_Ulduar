"""ulduar.hodir purge: the explicit, recorded removal of an object archived by mistake (H16, D39).

Runs on a real Archivum and, per backend, a real directory, an unversioned bucket and a versioned bucket of the local
object store (only the bucket cases are marked `s3`; the purge must remove every version). The D37 race injects a put
of the same content, in another thread, through a wrapper that forwards to the real deleting store (hard rule 9, H22).
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict

from tests.conftest import (
    ARCHIVE_BACKENDS,
    ARCHIVE_SETTINGS_FIXTURES,
    Archivum,
    archive_config,
    count,
    digest,
)
from ulduar import _ids
from ulduar.config import ArchiveSettings, Config
from ulduar.errors import ConfigurationError, UlduarError, UnknownRecordError
from ulduar.freya import Artifact
from ulduar.hodir import _layout, open_archive, purge
from ulduar.hodir import _purge as purge_module
from ulduar.hodir._store import DeletingStore, open_store

BACKENDS = [*ARCHIVE_BACKENDS, pytest.param("s3-versioned", id="s3-versioned", marks=pytest.mark.s3)]
SETTINGS_FIXTURES = {**ARCHIVE_SETTINGS_FIXTURES, "s3-versioned": "archive_settings_s3_versioned"}
MISTAKE = b"archived by mistake"
REASON = "recorded_by_mistake"
_FILES = itertools.count()


@pytest.fixture(params=BACKENDS)
def config(request: pytest.FixtureRequest, archivum: Archivum, tmp_path: Path) -> Config:
    """An onboarded config (project `alpha`) with the archive of each backend in turn, a versioned bucket included."""
    return archive_config(archivum, tmp_path, request.getfixturevalue(SETTINGS_FIXTURES[request.param]))


# --- helpers ---


def put(config: Config, tmp_path: Path, content: bytes = MISTAKE, *, scope: str | None = None) -> Artifact:
    """Archive *content* as a new artifact in its own session."""
    path = tmp_path / f"content-{next(_FILES)}.bin"
    path.write_bytes(content)
    with open_archive(config) as session:
        return session.put_file(path, "report", scope=scope)


def purge_it(config: Config, content: bytes = MISTAKE, *, reason: str = REASON, scope: str = "default") -> Any:
    return purge(config, scope=scope, checksum=digest_of(content), reason=reason)


def digest_of(content: bytes) -> bytes:
    return hashlib.sha256(content).digest()


def store_of(config: Config) -> DeletingStore:
    assert config.archive is not None
    return open_store(config.archive, deleting=True)


def object_key_of(artifact: Artifact) -> str:
    return _layout.object_key("alpha", artifact.scope, artifact.checksum)


def manifest_key_of(artifact: Artifact) -> str:
    return _layout.manifest_key("alpha", artifact.scope, artifact.artifact_id)


def deletion_rows(archivum: Archivum) -> list[tuple[Any, ...]]:
    with archivum.superuser() as su:
        return su.execute(
            "SELECT deletion_id, stage, intent_id, cause, rule_id, scope, checksum, size_bytes, reason"
            " FROM ulduar.archive_deletion ORDER BY recorded_at, deletion_id"
        ).fetchall()


def stages(archivum: Archivum) -> list[str]:
    return [row[1] for row in deletion_rows(archivum)]


def stored_records(config: Config) -> list[_layout.DeletionRecord]:
    """Every deletion record the store holds, decoded."""
    store = store_of(config)
    found = []
    for stored in store.list("alpha/_records"):
        with store.read(stored.key) as stream:
            found.append(_layout.DeletionRecord.decode(stream.read()))
    return found


def gone(store: DeletingStore, *keys: str) -> bool:
    """True when nothing of any of the keys remains, not even a version."""
    return all(store.head(key) is None and not store.any_version_left(key) for key in keys)


class HookedStore:
    """A deleting store that forwards to a real one and runs a hook before the intent's record is written."""

    def __init__(self, store: DeletingStore, on_first_record: Callable[[], None]) -> None:
        self._store = store
        self._on_first_record: Callable[[], None] | None = on_first_record

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def put_bytes_new(self, key: str, data: bytes) -> bool:
        if "/_records/" in key and self._on_first_record is not None:
            hook, self._on_first_record = self._on_first_record, None
            hook()
        return self._store.put_bytes_new(key, data)


class UntouchedStore:
    """A store that fails the test on any call: what a refused request must never reach."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the store was called: {name}")


# --- what a purge removes and records ---


def test_a_purge_removes_the_object_every_version_and_the_manifest_and_records_both_stages(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    mistake = put(config, tmp_path)
    keep = put(config, tmp_path, b"keep this")
    outcome = purge_it(config)
    assert (outcome.stage, outcome.cause, outcome.detail, outcome.scope) == ("confirmed", "purge", None, "default")
    assert (outcome.checksum, outcome.size) == (mistake.checksum, mistake.size)
    store = store_of(config)
    assert gone(store, object_key_of(mistake), manifest_key_of(mistake))
    assert store.head(object_key_of(keep)) is not None and store.head(manifest_key_of(keep)) is not None
    intent, confirmed = deletion_rows(archivum)
    assert (intent[1], intent[2], confirmed[1], confirmed[2]) == ("intent", None, "confirmed", intent[0])
    for row in (intent, confirmed):
        assert (row[3], row[4], row[5], row[6], row[7], row[8]) == (
            "purge",
            None,
            "default",
            mistake.checksum,
            mistake.size,
            REASON,
        )
    records = sorted(stored_records(config), key=lambda record: record.deletion_id)
    assert [(r.stage, r.cause, r.reason) for r in records] == [
        ("intent", "purge", REASON),
        ("confirmed", "purge", REASON),
    ]
    assert outcome.deletion_id == intent[0]
    with open_archive(config) as session:
        with pytest.raises(UnknownRecordError, match="is not in the archive"):
            session.get(mistake, tmp_path / "gone")
        session.get(keep, tmp_path / "kept")
        assert not [f for f in session.reconcile().findings if f.checksum == mistake.checksum]
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact WHERE artifact_id = %s", mistake.artifact_id) == 1


def test_an_object_registered_twice_is_purged_for_both_artifacts(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    first, second = put(config, tmp_path), put(config, tmp_path)
    assert first.artifact_id != second.artifact_id and first.checksum == second.checksum
    outcome = purge_it(config)
    assert outcome.stage == "confirmed"
    assert gone(store_of(config), object_key_of(first), manifest_key_of(first), manifest_key_of(second))
    assert stages(archivum) == ["intent", "confirmed"]
    with open_archive(config) as session:
        for artifact in (first, second):
            with pytest.raises(UnknownRecordError):
                session.get(artifact, tmp_path / f"gone-{artifact.artifact_id}")


def test_an_object_in_the_store_that_no_registration_names_is_purged(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    key = _layout.object_key("alpha", "default", digest_of(MISTAKE))
    assert config.archive is not None
    assert open_store(config.archive).put_bytes_new(key, MISTAKE)
    outcome = purge_it(config)
    assert (outcome.stage, outcome.size) == ("confirmed", len(MISTAKE))
    assert gone(store_of(config), key)
    assert stages(archivum) == ["intent", "confirmed"]


# --- what is refused ---


def test_an_address_the_project_does_not_know_is_refused(config: Config, archivum: Archivum, tmp_path: Path) -> None:
    put(config, tmp_path, b"something else")
    short = _layout.short_checksum(digest_of(MISTAKE))
    with pytest.raises(UnknownRecordError, match=f"No archived object {short} in scope default"):
        purge_it(config)
    assert stages(archivum) == []


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        ({"reason": "because"}, ValueError, "personal_data, secret, recorded_by_mistake"),
        ({"checksum": b"short"}, ValueError, "SHA-256 digest of 32 bytes, got 5 bytes"),
        ({"checksum": "text"}, ValueError, "got a str"),
        ({"scope": "Not A Name!"}, ValueError, "scope"),
        ({"scope": "other"}, UnknownRecordError, "Scope other is not onboarded"),
    ],
)
def test_a_bad_argument_is_refused_before_any_store_call(
    config: Config,
    archivum: Archivum,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    put(config, tmp_path)
    monkeypatch.setattr(purge_module, "open_store", lambda settings, *, deleting=False: UntouchedStore())
    given: dict[str, Any] = {"scope": "default", "checksum": digest_of(MISTAKE), "reason": REASON} | arguments
    with pytest.raises(error, match=message):
        purge(config, **given)
    assert stages(archivum) == []


def test_a_config_without_an_archive_is_refused(config: Config) -> None:
    with pytest.raises(ConfigurationError, match="archive"):
        purge(replace(config, archive=None), scope="default", checksum=digest("x"), reason=REASON)


def test_a_purge_of_an_object_already_purged_is_skipped_and_records_nothing(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    put(config, tmp_path)
    assert purge_it(config).stage == "confirmed"
    again = purge_it(config)
    assert (again.stage, again.deletion_id, again.cause) == ("skipped", None, "purge")
    assert again.detail is not None and "already gone" in again.detail
    assert stages(archivum) == ["intent", "confirmed"]
    assert len(stored_records(config)) == 2


# --- an object that cannot be deleted stays an open intent ---


def test_the_writer_credential_leaves_the_intent_open_and_the_deleter_resumes_it_with_the_same_reason(
    archive_settings_s3: ArchiveSettings, archivum: Archivum, tmp_path: Path
) -> None:
    config = archive_config(archivum, tmp_path, archive_settings_s3)
    mistake = put(config, tmp_path)
    assert archive_settings_s3.writer is not None
    as_writer = replace(config, archive=replace(archive_settings_s3, deleter=archive_settings_s3.writer))
    opened = purge_it(as_writer)
    assert opened.stage == "open" and opened.detail is not None and "AccessDenied" in opened.detail
    assert store_of(config).head(object_key_of(mistake)) is not None
    assert stages(archivum) == ["intent"]
    assert [record.stage for record in stored_records(config)] == ["intent"]
    with pytest.raises(UlduarError, match=rf"reason {REASON}, not personal_data"):
        purge_it(config, reason="personal_data")
    assert stages(archivum) == ["intent"]
    assert store_of(config).head(object_key_of(mistake)) is not None
    done = purge_it(config)
    assert (done.stage, done.deletion_id) == ("confirmed", opened.deletion_id)
    assert stages(archivum) == ["intent", "confirmed"]
    assert gone(store_of(config), object_key_of(mistake), manifest_key_of(mistake))
    assert {row[8] for row in deletion_rows(archivum)} == {REASON}


def test_an_open_intent_of_another_cause_is_not_settled_as_a_purge(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    mistake = put(config, tmp_path)
    deletion_id = _ids.uuid7()
    with archivum.superuser() as su:
        su.execute(
            "INSERT INTO ulduar.archive_deletion (project_id, deletion_id, event_at, scope, checksum, size_bytes,"
            " stage, cause) SELECT project_id, %s, %s, 'default', %s, %s, 'intent', 'incomplete_put'"
            " FROM ulduar.project WHERE name = 'alpha'",
            (deletion_id, _ids.uuid7_time(deletion_id), mistake.checksum, mistake.size),
        )
    with pytest.raises(UlduarError, match="open incomplete_put deletion"):
        purge_it(config)
    assert stages(archivum) == ["intent"]
    assert store_of(config).head(object_key_of(mistake)) is not None


# --- a purge against a put of the same content (D37) ---


def test_a_put_between_the_intent_and_the_deletion_cancels_the_purge_and_a_second_purge_removes_it(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = put(config, tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        racing: list[Artifact] = []

        def put_the_same() -> None:
            racing.append(pool.submit(put, config, tmp_path).result(timeout=30))

        monkeypatch.setattr(
            purge_module,
            "open_store",
            lambda settings, *, deleting=False: HookedStore(open_store(settings, deleting=deleting), put_the_same),
        )
        outcome = purge_it(config)
    assert outcome.stage == "cancelled" and outcome.detail is not None
    assert store_of(config).head(object_key_of(old)) is not None
    assert stages(archivum) == ["intent", "cancelled"]
    with open_archive(config) as session:
        session.get(racing[0], tmp_path / "new")
        session.get(old, tmp_path / "old")
    monkeypatch.setattr(purge_module, "open_store", open_store)
    second = purge_it(config)
    assert second.stage == "confirmed" and second.deletion_id != outcome.deletion_id
    assert gone(store_of(config), object_key_of(old), manifest_key_of(old), manifest_key_of(racing[0]))
    assert stages(archivum) == ["intent", "cancelled", "intent", "confirmed"]


# --- secrets ---


def test_no_credential_appears_in_the_log_records_of_a_purge(
    config: Config, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert config.archive is not None
    names = [config.archivum.dsn_env, config.archivum.ledger_dsn_env]
    for credential in (config.archive.writer, config.archive.deleter):
        names += [] if credential is None else [credential.access_key_env, credential.secret_key_env]
    secrets = [os.environ[name] for name in names if name]
    secrets += [conninfo_to_dict(dsn)["password"] for dsn in secrets[:2]]  # the connection strings come first
    assert all(secrets)
    mistake = put(config, tmp_path)
    caplog.set_level(logging.DEBUG, logger="ulduar")
    purge_it(config)
    records = [r for r in caplog.records if r.name.startswith("ulduar")]
    messages = [r.getMessage() for r in records if r.name == "ulduar.hodir._purge"]
    short = _layout.short_checksum(mistake.checksum)
    assert messages == [f"purge of object {short} in scope default for {REASON}: confirmed"]
    text = "\n".join(f"{r.getMessage()} {r.exc_text or ''} {r.args}" for r in records)
    assert not [secret for secret in secrets if secret in text]
