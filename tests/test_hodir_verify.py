"""ulduar.hodir: `verify` on a real Archivum over a real directory and an unversioned bucket (H10).

Objects are archived through `open_archive` and tampered with as the test (see `tamper` in conftest); the check and
finding rows are read back as the superuser. A wrapper that forwards to the real store makes an object cold, or the
store unreachable. The outcome vocabulary is held equal to the CHECK of step 2's `archive_finding`.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    CONTENT,
    Archivum,
    archive_file,
    count,
    faulty_archive,
    onboarded,
    record_confirmed_deletion,
)
from ulduar import _ids
from ulduar.config import ArchiveSettings, Config
from ulduar.errors import ArchiveUnavailable
from ulduar.freya import Artifact, open_catalog
from ulduar.hodir import Archive, Finding, Verification, _layout, open_archive
from ulduar.hodir._records import CHECK_OUTCOMES
from ulduar.hodir._store import WritingStore, open_store

SAME_SIZE_OTHER_BYTES = bytes(8)


class WrappedStore:
    """A store that forwards to a real one, counting reads, and reports every object cold or fails every read."""

    def __init__(self, store: WritingStore, *, cold: bool = False, unavailable: bool = False) -> None:
        self._store = store
        self._cold = cold
        self._unavailable = unavailable
        self.reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def head(self, key: str) -> Any:
        stored = self._store.head(key)
        return replace(stored, immediate=False) if self._cold and stored is not None else stored

    def read(self, key: str) -> Any:
        self.reads += 1
        if self._unavailable:
            raise ArchiveUnavailable(f"Reading {key} failed: timed out")
        return self._store.read(key)


def session_over(config: Config, store_wrapper: Callable[[WritingStore], WrappedStore]) -> tuple[Any, WrappedStore]:
    """(context manager of an `Archive` over the wrapped real store, the wrapper)."""
    assert config.archive is not None
    wrapper = store_wrapper(open_store(config.archive))

    class Session:
        def __enter__(self) -> Archive:
            self.catalog = open_catalog(config)
            return Archive(self.catalog.__enter__(), wrapper, config.archive)  # type: ignore[arg-type]

        def __exit__(self, *exc: object) -> None:
            self.catalog.__exit__(None, None, None)

    return Session(), wrapper


def stage(config: Config, tmp_path: Path, *contents: bytes) -> list[Artifact]:
    """Archive one file per content, in the default scope."""
    stored = []
    for number, data in enumerate(contents):
        path = tmp_path / f"object-{number}.bin"
        path.write_bytes(data)
        stored.append(archive_file(config, path))
    return stored


def key_of(artifact: Artifact) -> str:
    return _layout.object_key("alpha", artifact.scope, artifact.checksum)


def outcomes(check: Verification) -> dict[bytes, str]:
    return {finding.checksum: finding.outcome for finding in check.findings if finding.checksum is not None}


def check_rows(archivum: Archivum) -> list[tuple[Any, ...]]:
    with archivum.superuser() as su:
        rows = su.execute(
            "SELECT check_id, scope, kind, mode, objects_checked, findings, event_at FROM ulduar.archive_check"
            " ORDER BY event_at, check_id"
        ).fetchall()
    return [tuple(row) for row in rows]


def finding_rows(archivum: Archivum) -> list[tuple[Any, ...]]:
    with archivum.superuser() as su:
        rows = su.execute(
            "SELECT finding_id, check_id, scope, checksum, size_bytes, outcome, event_at FROM ulduar.archive_finding"
            " ORDER BY event_at, finding_id"
        ).fetchall()
    return [tuple(row) for row in rows]


# --- a full verification ---


def test_a_full_verify_of_three_objects_passes_each_and_records_the_check_and_its_findings(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    stored = stage(config, tmp_path, b"one", b"two", b"three")
    with open_archive(config) as session:
        check = session.verify()
    assert (check.scope, check.mode, check.objects_checked, check.failed) == ("default", "full", 3, ())
    assert check.event_at == _ids.uuid7_time(check.check_id)
    assert outcomes(check) == {artifact.checksum: "passed" for artifact in stored}
    assert [f.checksum for f in check.findings] == sorted(a.checksum for a in stored)  # by checksum
    assert {f.size for f in check.findings} == {3, 5}
    assert check_rows(archivum) == [(check.check_id, "default", "verification", "full", 3, 0, check.event_at)]
    assert sorted(finding_rows(archivum)) == sorted(
        (f.finding_id, check.check_id, "default", f.checksum, f.size, "passed", _ids.uuid7_time(f.finding_id))
        for f in check.findings
    )
    assert all(isinstance(f, Finding) for f in check.findings)


def test_the_same_content_registered_twice_is_verified_once(config: Config, archivum: Archivum, source: Path) -> None:
    archive_file(config, source)
    archive_file(config, source)
    with open_archive(config) as session:
        assert session.verify().objects_checked == 1
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_finding") == 1


def test_an_empty_scope_verifies_nothing_and_still_records_the_check(config: Config, archivum: Archivum) -> None:
    with open_archive(config) as session:
        full, sampled = session.verify(), session.verify(sample=5)
    assert (full.objects_checked, full.findings, full.failed) == (0, (), ())
    assert (sampled.mode, sampled.objects_checked) == ("sample", 0)
    assert [row[1:6] for row in check_rows(archivum)] == [
        ("default", "verification", "full", 0, 0),
        ("default", "verification", "sample", 0, 0),
    ]
    assert finding_rows(archivum) == []


# --- what a failure looks like ---


@pytest.mark.parametrize("other", [SAME_SIZE_OTHER_BYTES, b"short"], ids=["same size", "other size"])
def test_a_corrupted_object_is_a_mismatch_a_removed_one_missing_and_the_others_still_pass(
    config: Config,
    archivum: Archivum,
    tmp_path: Path,
    tamper: Callable[[str, bytes | None], None],
    caplog: pytest.LogCaptureFixture,
    other: bytes,
) -> None:
    corrupted, removed, intact = stage(config, tmp_path, b"corrupt me", b"remove me", b"keep me")
    tamper(key_of(corrupted), other)
    tamper(key_of(removed), None)
    caplog.set_level(logging.INFO, logger="ulduar.hodir")
    with open_archive(config) as session:
        check = session.verify()
    assert outcomes(check) == {corrupted.checksum: "mismatch", removed.checksum: "missing", intact.checksum: "passed"}
    assert sorted(f.outcome for f in check.failed) == ["mismatch", "missing"]
    assert check_rows(archivum)[0][4:6] == (3, 2)  # three objects judged, two findings that are not `passed`
    assert sorted(row[5] for row in finding_rows(archivum)) == ["mismatch", "missing", "passed"]
    messages = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "ulduar.hodir.archive"]
    warnings = [m for level, m in messages if level == logging.WARNING]
    assert sorted(warnings) == sorted(
        f"verification of scope default: object {a.checksum.hex()[:12]} is {o}"
        for a, o in ((corrupted, "mismatch"), (removed, "missing"))
    )
    assert [m for level, m in messages if level == logging.INFO][
        -1
    ] == "verified scope default (full): 3 checked, 2 failed"
    assert not any(a.checksum.hex() in m for a in (corrupted, removed) for _, m in messages)


def test_a_link_in_place_of_an_object_is_unreadable(
    archivum: Archivum, archive_settings_dir: ArchiveSettings, tmp_path: Path, source: Path
) -> None:
    config = replace(onboarded(archivum, tmp_path), archive=archive_settings_dir)
    stored = archive_file(config, source)
    assert archive_settings_dir.path is not None
    path = archive_settings_dir.path / archive_settings_dir.prefix / key_of(stored)
    path.unlink()
    path.symlink_to(source)  # a regular file with the right bytes, but not where an object may be a link
    with open_archive(config) as session:
        check = session.verify()
    assert outcomes(check) == {stored.checksum: "unreadable"}
    assert check_rows(archivum)[0][4:6] == (1, 1)


def test_an_object_that_cannot_be_read_now_is_not_read_and_is_a_finding(
    config: Config, archivum: Archivum, source: Path
) -> None:
    stored = archive_file(config, source)
    manager, wrapper = session_over(config, lambda store: WrappedStore(store, cold=True))
    with manager as session:
        check = session.verify()
    assert outcomes(check) == {stored.checksum: "not_immediate"} and wrapper.reads == 0
    assert check_rows(archivum)[0][4:6] == (1, 1)


def test_an_unavailable_store_propagates_and_nothing_is_recorded(
    config: Config, archivum: Archivum, source: Path
) -> None:
    archive_file(config, source)
    manager, wrapper = session_over(config, lambda store: WrappedStore(store, unavailable=True))
    with manager as session, pytest.raises(ArchiveUnavailable):
        session.verify()
    assert wrapper.reads == 1
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_check") == 0
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_finding") == 0


# --- the rotation of a sample ---


def test_a_sample_of_one_visits_every_object_once_then_the_one_passed_longest_ago(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    stored = stage(config, tmp_path, b"one", b"two", b"three")
    with open_archive(config) as session:
        visited = [session.verify(sample=1) for _ in range(3)]
        fourth = session.verify(sample=1)
    assert [check.mode for check in visited] == ["sample"] * 3 and all(check.objects_checked == 1 for check in visited)
    first_each = [check.findings[0].checksum for check in visited]
    assert sorted(first_each) == sorted(a.checksum for a in stored)  # each exactly once
    assert fourth.findings[0].checksum == first_each[0]
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_check") == 4


def test_a_failed_object_has_no_pass_to_its_name_so_it_stays_at_the_front_of_every_sample(
    config: Config, tmp_path: Path, tamper: Callable[[str, bytes | None], None]
) -> None:
    stored = stage(config, tmp_path, b"one", b"two", b"three")
    tamper(key_of(stored[1]), SAME_SIZE_OTHER_BYTES)
    with open_archive(config) as session:
        session.verify()  # the two intact objects now have a pass; the corrupted one has none
        picked = [session.verify(sample=1).findings[0] for _ in range(3)]
    assert [(f.checksum, f.outcome) for f in picked] == [(stored[1].checksum, "mismatch")] * 3


def test_a_sample_larger_than_the_scope_judges_every_address(config: Config, tmp_path: Path) -> None:
    stage(config, tmp_path, b"one", b"two")
    with open_archive(config) as session:
        assert session.verify(sample=50).objects_checked == 2


# --- arguments ---


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"sample": 0}, "sample 0"),
        ({"sample": True}, "sample True"),
        ({"sample": -1}, "sample -1"),
        ({"sample": 1.5}, "sample 1.5"),
        ({"scope": "Bad Scope"}, "scope"),
        ({"scope": "a/b"}, "scope"),
    ],
)
def test_a_bad_argument_is_refused_before_any_io(
    config: Config, archivum: Archivum, source: Path, arguments: dict[str, Any], problem: str
) -> None:
    archive_file(config, source)
    with faulty_archive(config) as (session, store):
        with pytest.raises(ValueError, match=problem):
            session.verify(**arguments)
        assert store.calls == {}
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_check") == 0


def test_a_scope_without_any_registration_is_an_empty_scope(config: Config, archivum: Archivum) -> None:
    """The check tables hold no foreign key to the scopes, so a scope nothing was archived in verifies 0 objects."""
    with open_archive(config) as session:
        check = session.verify(scope="elsewhere")
    assert (check.scope, check.objects_checked) == ("elsewhere", 0)
    assert [row[1:6] for row in check_rows(archivum)] == [("elsewhere", "verification", "full", 0, 0)]


# --- what counts as archived, and whose it is ---


def test_an_address_with_a_confirmed_deletion_after_its_registration_is_not_verified(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    deleted, kept = stage(config, tmp_path, b"deleted", b"kept")
    record_confirmed_deletion(archivum, deleted)
    with open_archive(config) as session:
        full, sampled = session.verify(), session.verify(sample=5)
    assert outcomes(full) == outcomes(sampled) == {kept.checksum: "passed"}


def test_another_projects_recorder_sees_none_of_the_checks_and_verifies_none_of_the_objects(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    stage(config, tmp_path, b"one")
    with open_archive(config) as session:
        session.verify()
    other = replace(onboarded(archivum, tmp_path, "beta"), archive=config.archive)
    with open_catalog(other) as catalog:
        for query in ("SELECT count(*) FROM ulduar.archive_check", "SELECT count(*) FROM ulduar.archive_finding"):
            seen = catalog.transaction("Counting", lambda tx, query=query: tx.execute(query))
            assert seen.fetchone() == (0,)
    with open_archive(other) as session:
        assert session.verify().objects_checked == 0
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_check") == 2


def test_the_outcomes_are_the_ones_step_two_accepts(archivum: Archivum, tmp_path: Path) -> None:
    onboarded(archivum, tmp_path)
    with archivum.superuser() as su:
        rows = su.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'ulduar.archive_finding'::regclass AND contype = 'c'"
            " AND pg_get_constraintdef(oid) LIKE '%outcome%'"
        ).fetchall()
    assert len(rows) == 1
    assert sorted(re.findall(r"'([a-z_]+)'", rows[0][0])) == sorted(CHECK_OUTCOMES)


def test_the_content_of_an_object_is_never_in_what_a_verification_returns(config: Config, source: Path) -> None:
    stored = archive_file(config, source)
    with open_archive(config) as session:
        check = session.verify()
    assert repr(check).count(CONTENT.decode()) == 0
    assert check.findings[0].checksum == hashlib.sha256(CONTENT).digest() == stored.checksum
