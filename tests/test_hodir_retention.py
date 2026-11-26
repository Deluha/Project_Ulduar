"""ulduar.hodir.retention: declared retention classes, what is due, and the recorded deletion applying them (H12-H15).

The due logic and the deletion record are pure tests. Declaring a rule, applying retention and the races of a
deletion against a put run on a real Archivum and, per backend, a real directory, an unversioned bucket and a
versioned bucket of the local object store (only the bucket cases are marked `s3`; the deletion must remove every
version). Races use two recording sessions and a wrapper that forwards to the real deleting store and runs a put in
another thread at a chosen step (hard rule 9, H22); the object lock is MinIO's own.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict

from dev_tools.services import S3_DELETER_ACCESS_KEY_VARIABLE, S3_DELETER_SECRET_KEY_VARIABLE
from tests.conftest import (
    ARCHIVE_BACKENDS,
    ARCHIVE_SETTINGS_FIXTURES,
    TEST_BUCKET_PREFIX,
    Archivum,
    archive_config,
    count,
    digest,
    ms_ago,
    onboarded,
    s3_archive_settings,
    wait_until_blocked,
)
from ulduar import _ids
from ulduar.config import ArchiveSettings, Config
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
    apply_history_retention,
    declare_history_retention,
    open_catalog,
)
from ulduar.hodir import (
    DueAddress,
    OpenIntent,
    RetentionRun,
    _archivum,
    _layout,
    apply_retention,
    declare_retention,
    open_archive,
)
from ulduar.hodir import retention as retention_module
from ulduar.hodir._records import LatestRule, LiveRegistration
from ulduar.hodir._store import DeletingStore, open_store
from ulduar.hodir.retention import _shortens, due_addresses

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
RULE_SHORT, RULE_LONG, RULE_FOREVER = (_ids.uuid7() for _ in range(3))
RULES = {
    "short": LatestRule(RULE_SHORT, 30, 1),
    "long": LatestRule(RULE_LONG, 365, 1),
    "forever": LatestRule(RULE_FOREVER, None, 1),
}
BACKENDS = [*ARCHIVE_BACKENDS, pytest.param("s3-versioned", id="s3-versioned", marks=pytest.mark.s3)]
SETTINGS_FIXTURES = {**ARCHIVE_SETTINGS_FIXTURES, "s3-versioned": "archive_settings_s3_versioned"}
OLD = 100  # days: past the 30 of "short", inside the 365 of "long"
_FILES = itertools.count()


@pytest.fixture(params=BACKENDS)
def config(request: pytest.FixtureRequest, archivum: Archivum, tmp_path: Path) -> Config:
    """An onboarded config (project `alpha`) with the archive of each backend in turn, a versioned bucket included."""
    return archive_config(archivum, tmp_path, request.getfixturevalue(SETTINGS_FIXTURES[request.param]))


# --- helpers ---


def registration(
    *,
    days: int = OLD,
    checksum: bytes | None = None,
    retention_class: str | None = "short",
    dataset: uuid.UUID | None = None,
    scope: str = "default",
) -> LiveRegistration:
    """A live registration of an artifact *days* before `NOW`."""
    moment = NOW - timedelta(days=days)
    artifact_id = _ids.uuid7(int(moment.timestamp() * 1000))
    return LiveRegistration(
        scope, checksum or digest("content"), 7, artifact_id, moment, dataset, retention_class, moment
    )


def put(
    config: Config,
    tmp_path: Path,
    content: bytes,
    *,
    days: int = 0,
    retention_class: str | None = "short",
    dataset: Dataset | None = None,
    scope: str | None = None,
) -> Artifact:
    """Archive *content* as an artifact *days* old (its id carries the time) in its own session."""
    path = tmp_path / f"content-{next(_FILES)}.bin"
    path.write_bytes(content)
    with open_archive(config) as session:
        return session.put_file(
            path,
            "report",
            dataset=dataset,
            scope=scope,
            retention_class=retention_class,
            record_id=_ids.uuid7(ms_ago(days)),
        )


def declare(config: Config, retention_class: str, keep_days: int | None, **keywords: Any) -> Any:
    """Declare a rule, confirming the shortening a first rule is."""
    return declare_retention(config, "alpha", retention_class, keep_days, confirm_shortening=True, **keywords)


def store_of(config: Config) -> DeletingStore:
    assert config.archive is not None
    return open_store(config.archive, deleting=True)


def object_key_of(artifact: Artifact) -> str:
    return _layout.object_key("alpha", artifact.scope, artifact.checksum)


def manifest_key_of(artifact: Artifact) -> str:
    return _layout.manifest_key("alpha", artifact.scope, artifact.artifact_id)


def rule_rows(archivum: Archivum) -> list[tuple[Any, ...]]:
    with archivum.superuser() as su:
        return su.execute(
            "SELECT retention_class, keep_days, keep_newest, shortening_confirmed, rule_id, event_at"
            " FROM ulduar.archive_retention_rule ORDER BY recorded_at, rule_id"
        ).fetchall()


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


def sorted_by_checksum(*artifacts: Artifact) -> list[Artifact]:
    return sorted(artifacts, key=lambda artifact: artifact.checksum)


class HookedStore:
    """A deleting store that forwards to a real one and runs a test's hook at one chosen step.

    `on_first_record` runs before the first record is written (the intent's), `on_first_delete` before the first
    object is deleted, `fail_first_delete` makes that delete an `ArchiveUnavailable` instead; each fires once.
    """

    def __init__(
        self,
        store: DeletingStore,
        *,
        on_first_record: Callable[[], None] | None = None,
        on_first_delete: Callable[[], None] | None = None,
        fail_first_delete: bool = False,
    ) -> None:
        self._store = store
        self._on_first_record = on_first_record
        self._on_first_delete = on_first_delete
        self._fail_first_delete = fail_first_delete

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def put_bytes_new(self, key: str, data: bytes) -> bool:
        if "/_records/" in key and self._on_first_record is not None:
            hook, self._on_first_record = self._on_first_record, None
            hook()
        return self._store.put_bytes_new(key, data)

    def delete_every_version(self, key: str) -> None:
        if "/_objects/" in key and self._on_first_delete is not None:
            hook, self._on_first_delete = self._on_first_delete, None
            hook()
        if "/_objects/" in key and self._fail_first_delete:
            self._fail_first_delete = False
            raise ArchiveUnavailable(f"Deleting {key} failed: injected")
        self._store.delete_every_version(key)


def hook_store(monkeypatch: pytest.MonkeyPatch, **hooks: Any) -> None:
    """Make `apply_retention` open its store through a `HookedStore` over the real one."""
    monkeypatch.setattr(
        retention_module,
        "open_store",
        lambda settings, *, deleting=False: HookedStore(open_store(settings, deleting=deleting), **hooks),
    )


# --- the pure due logic ---


def test_without_a_class_a_rule_or_a_limit_nothing_is_due() -> None:
    kept = [
        registration(retention_class=None),
        registration(retention_class="no-rule", checksum=digest("b")),
        registration(retention_class="forever", checksum=digest("c")),
    ]
    assert due_addresses(kept, RULES, NOW) == []
    assert due_addresses([], RULES, NOW) == []


def test_a_registration_expires_when_its_artifact_is_older_than_keep_days() -> None:
    expired, young = registration(days=31), registration(days=29, checksum=digest("b"))
    assert [d.checksum for d in due_addresses([expired, young], RULES, NOW)] == [expired.checksum]
    exactly = registration(days=30)
    assert due_addresses([exactly], RULES, NOW) == []


def test_keep_newest_protects_the_newest_registrations_of_a_dataset_whatever_their_class() -> None:
    dataset = uuid.uuid4()
    series = [registration(days=d, dataset=dataset, checksum=digest(str(d))) for d in (100, 90, 80)]
    assert sorted(d.checksum for d in due_addresses(series, RULES, NOW)) == sorted(s.checksum for s in series[:2])
    two = {"short": LatestRule(RULE_SHORT, 30, 2)}
    assert [d.checksum for d in due_addresses(series, two, NOW)] == [series[0].checksum]
    none = {"short": LatestRule(RULE_SHORT, 30, 0)}
    assert len(due_addresses(series, none, NOW)) == 3
    # the newest of the dataset is of no class, yet it holds the one protected place
    other = registration(days=1, dataset=dataset, retention_class=None, checksum=digest("other"))
    assert len(due_addresses([*series, other], RULES, NOW)) == 3


def test_a_registration_without_a_dataset_is_not_protected_by_keep_newest() -> None:
    many = {"short": LatestRule(RULE_SHORT, 30, 50)}
    assert len(due_addresses([registration(days=60), registration(days=90, checksum=digest("b"))], many, NOW)) == 2


def test_an_object_registered_under_two_classes_is_due_when_the_longer_has_expired() -> None:
    first = registration(days=100, retention_class="short")
    second = registration(days=100, retention_class="long")
    assert due_addresses([first, second], RULES, NOW) == []
    shorter_long = {**RULES, "long": LatestRule(RULE_LONG, 90, 1)}
    [due] = due_addresses([first, second], shorter_long, NOW)
    assert (due.rule_id, due.size, due.artifact_ids) == (
        RULE_LONG,
        7,
        tuple(sorted((first.artifact_id, second.artifact_id))),
    )
    assert due_addresses([first, second, registration(days=1)], shorter_long, NOW) == []  # a young one keeps it


def test_the_binding_rule_is_that_of_the_registration_that_expires_last() -> None:
    old = registration(days=200, retention_class="long")
    older_short = registration(days=300, retention_class="short")
    [due] = due_addresses([old, older_short], {**RULES, "long": LatestRule(RULE_LONG, 100, 1)}, NOW)
    assert due.rule_id == RULE_LONG  # 200 days old plus 100 expires after 300 days old plus 30
    [due] = due_addresses([old, older_short], {**RULES, "long": LatestRule(RULE_LONG, 40, 1)}, NOW)
    assert due.rule_id == RULE_LONG  # 200 days old plus 40 is still later than 300 plus 30


def test_due_addresses_are_ordered_by_scope_and_checksum() -> None:
    a, b, c = (registration(checksum=digest(name)) for name in "abc")
    elsewhere = registration(scope="aaa-first", checksum=digest("z"))
    due = due_addresses([c, b, elsewhere, a], RULES, NOW)
    assert [(d.scope, d.checksum) for d in due] == [
        ("aaa-first", digest("z")),
        *(("default", checksum) for checksum in sorted(r.checksum for r in (a, b, c))),
    ]


@pytest.mark.parametrize(
    ("current", "new", "shortens"),
    [
        (None, (None, 1), False),
        (None, (30, 1), True),
        ((30, 1), (60, 1), False),
        ((30, 1), (None, 1), False),
        ((30, 1), (30, 1), False),
        ((30, 1), (29, 1), True),
        ((None, 1), (30, 1), True),
        ((None, 1), (None, 1), False),
        ((30, 2), (60, 1), True),
        ((None, 2), (None, 1), True),
        ((30, 1), (60, 5), False),
    ],
)
def test_which_rules_shorten(
    current: tuple[int | None, int] | None, new: tuple[int | None, int], shortens: bool
) -> None:
    rule = None if current is None else LatestRule(RULE_SHORT, *current)
    assert _shortens(rule, *new) is shortens


# --- the deletion record ---


def sample_record(**changes: Any) -> _layout.DeletionRecord:
    deletion_id, intent_id = _ids.uuid7(), _ids.uuid7()
    fields: dict[str, Any] = {
        "deletion_id": deletion_id,
        "event_at": _ids.uuid7_time(deletion_id),
        "recorded_at": datetime(2026, 6, 1, 12, 0, 0, 123000, tzinfo=UTC),
        "project": "alpha",
        "scope": "default",
        "checksum": digest("x"),
        "size": 12,
        "stage": "confirmed",
        "intent_id": intent_id,
        "cause": "retention",
        "rule_id": RULE_SHORT,
        "reason": None,
    }
    return _layout.DeletionRecord(**{**fields, **changes})


def test_a_deletion_record_is_canonical_json_and_round_trips() -> None:
    record = sample_record()
    data = record.encode()
    assert data.endswith(b"\n") and b" " not in data
    assert _layout.DeletionRecord.decode(data) == record
    assert f'"checksum":"sha256:{record.checksum.hex()}"'.encode() in data
    assert b'"recorded_at":"2026-06-01T12:00:00.123Z"' in data
    assert b'"format":1,' in data and b'"type":"archive_deletion"' in data
    intent = sample_record(stage="intent", intent_id=None, cause="purge", rule_id=None, reason="secret")
    assert _layout.DeletionRecord.decode(intent.encode()) == intent


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda raw: raw.update(format=2), id="format"),
        pytest.param(lambda raw: raw.update(type="other"), id="type"),
        pytest.param(lambda raw: raw.update(stage="done"), id="stage"),
        pytest.param(lambda raw: raw.update(cause="whim"), id="cause"),
        pytest.param(lambda raw: raw.update(checksum="md5:abc"), id="checksum"),
        pytest.param(lambda raw: raw.update(size=-1), id="size"),
        pytest.param(lambda raw: raw.update(size=True), id="size bool"),
        pytest.param(lambda raw: raw.update(recorded_at="yesterday"), id="time"),
        pytest.param(lambda raw: raw.update(deletion_id="not-a-uuid"), id="id"),
        pytest.param(lambda raw: raw.update(scope="A/B"), id="scope"),
        pytest.param(lambda raw: raw.update(extra=1), id="unknown field"),
        pytest.param(lambda raw: raw.pop("reason"), id="missing field"),
        pytest.param(lambda raw: raw.update(rule_id=None), id="retention without a rule"),
        pytest.param(lambda raw: raw.update(intent_id=None), id="outcome without an intent"),
        pytest.param(lambda raw: raw.update(reason="secret"), id="reason without a purge"),
    ],
)
def test_a_deletion_record_that_is_not_format_one_is_refused_without_quoting_it(
    mutate: Callable[[Any], object],
) -> None:
    import json

    raw = json.loads(sample_record().encode())
    mutate(raw)
    with pytest.raises(ArchiveFormatError) as caught:
        _layout.DeletionRecord.decode(json.dumps(raw).encode())
    assert "secret" not in str(caught.value) and "not-a-uuid" not in str(caught.value)


def test_a_deletion_record_that_is_not_json_is_refused() -> None:
    for data in (b"nope", b"[]"):
        with pytest.raises(ArchiveFormatError, match="deletion record"):
            _layout.DeletionRecord.decode(data)


# --- declare_retention ---


def test_a_first_finite_rule_needs_confirmation_and_records_it(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    with pytest.raises(
        UlduarError,
        match="Retention class short of project alpha would keep objects for less time than it does now;"
        " declare it with confirm_shortening=True",
    ):
        declare_retention(config, "alpha", "short", 30)
    assert rule_rows(archivum) == []
    rule = declare_retention(config, "alpha", "short", 30, confirm_shortening=True)
    assert (rule.project, rule.retention_class, rule.keep_days, rule.keep_newest) == ("alpha", "short", 30, 1)
    assert rule.shortening_confirmed is True
    assert rule.event_at == _ids.uuid7_time(rule.rule_id)
    assert rule_rows(archivum) == [("short", 30, 1, True, rule.rule_id, rule.event_at)]


def test_a_lengthening_rule_needs_no_confirmation_and_is_not_marked(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    declare(config, "short", 30)
    assert declare_retention(config, "alpha", "short", 60).shortening_confirmed is False
    assert declare_retention(config, "alpha", "short", None).shortening_confirmed is False
    assert declare_retention(config, "alpha", "other", None).shortening_confirmed is False  # for ever: no shortening
    assert [row[:4] for row in rule_rows(archivum)] == [
        ("short", 30, 1, True),
        ("short", 60, 1, False),
        ("short", None, 1, False),
        ("other", None, 1, False),
    ]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"keep_days": 60}, {"keep_days": 30}),
        ({"keep_days": None}, {"keep_days": 30}),
        ({"keep_days": 30, "keep_newest": 3}, {"keep_days": 30, "keep_newest": 2}),
        ({"keep_days": None, "keep_newest": 3}, {"keep_days": None, "keep_newest": 0}),
    ],
)
def test_a_shortening_rule_is_refused_unless_confirmed_and_then_recorded(
    archivum: Archivum, tmp_path: Path, first: dict[str, Any], second: dict[str, Any]
) -> None:
    config = onboarded(archivum, tmp_path)
    declare(config, "short", first["keep_days"], keep_newest=first.get("keep_newest", 1))
    with pytest.raises(UlduarError, match="confirm_shortening=True"):
        declare_retention(config, "alpha", "short", second["keep_days"], keep_newest=second.get("keep_newest", 1))
    assert len(rule_rows(archivum)) == 1
    confirmed = declare(config, "short", second["keep_days"], keep_newest=second.get("keep_newest", 1))
    assert confirmed.shortening_confirmed is True
    assert [row[3] for row in rule_rows(archivum)][1:] == [True]


def test_the_latest_rule_of_a_class_is_the_one_read_back(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    declare(config, "short", 30)
    later = declare_retention(config, "alpha", "short", 90, keep_newest=4)
    other = declare(config, "other", 10)
    with open_catalog(config) as catalog:
        project_id = catalog.project.project_id
        rules = catalog.transaction("Reading the rules", lambda tx: _archivum.latest_rules(tx, project_id))
    assert rules == {"short": LatestRule(later.rule_id, 90, 4), "other": LatestRule(other.rule_id, 10, 1)}


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        (("Bad Project", "short", 30), "project"),
        (("alpha", "Bad Class", 30), "retention class"),
        (("alpha", "a/b", 30), "retention class"),
        (("alpha", "short", 0), "keep_days 0"),
        (("alpha", "short", -3), "keep_days -3"),
        (("alpha", "short", True), "keep_days True"),
        (("alpha", "short", 1.5), "keep_days 1.5"),
    ],
)
def test_a_bad_argument_is_refused_before_any_sql(
    archivum: Archivum, tmp_path: Path, arguments: tuple[Any, ...], problem: str
) -> None:
    config = onboarded(archivum, tmp_path)
    config = replace(config, archivum=replace(config.archivum, ledger_dsn_env=None))
    with pytest.raises(ValueError, match=problem):  # not the ConfigurationError the missing ledger variable would give
        declare_retention(config, *arguments)


@pytest.mark.parametrize("keep_newest", [-1, True, 1.5, None])
def test_a_bad_keep_newest_is_refused(archivum: Archivum, tmp_path: Path, keep_newest: Any) -> None:
    config = onboarded(archivum, tmp_path)
    with pytest.raises(ValueError, match="keep_newest"):
        declare_retention(config, "alpha", "short", 30, keep_newest=keep_newest, confirm_shortening=True)
    assert rule_rows(archivum) == []


def test_a_project_that_is_not_onboarded_is_unknown(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    with pytest.raises(UnknownRecordError, match="Project beta is not onboarded"):
        declare_retention(config, "beta", "short", 30, confirm_shortening=True)
    assert rule_rows(archivum) == []


def test_declaring_needs_the_ledger_role(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    config = replace(config, archivum=replace(config.archivum, ledger_dsn_env=None))
    with pytest.raises(ConfigurationError, match="archivum.ledger_dsn_env is not set"):
        declare_retention(config, "alpha", "short", 30, confirm_shortening=True)


def test_declaring_waits_for_the_retention_lock_of_its_project_and_class(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path, lock_timeout_s=30)
    with archivum.superuser() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        row = holder.execute("SELECT project_id FROM ulduar.project WHERE name = 'alpha'").fetchone()
        assert row is not None
        lock = _archivum.retention_lock_name(row[0], "short")
        assert lock == f"ulduar.archive-retention:{row[0]}:short"
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock,))
        declaring = pool.submit(declare, config, "short", 30)
        wait_until_blocked(archivum)
        assert rule_rows(archivum) == []
        holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock,))
        assert declaring.result(timeout=30).keep_days == 30
    assert len(rule_rows(archivum)) == 1


# --- apply_retention: arguments and the dry run ---


def test_apply_needs_the_archive_section_a_timezone_aware_now_and_a_known_scope(
    archivum: Archivum, tmp_path: Path
) -> None:
    bare = onboarded(archivum, tmp_path)
    with pytest.raises(ConfigurationError, match="`archive` is required"):
        apply_retention(bare)


def test_a_naive_now_and_a_bad_or_unknown_scope_are_refused(config: Config) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        apply_retention(config, now=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="scope"):
        apply_retention(config, scope="Bad Scope")
    with pytest.raises(UnknownRecordError, match="Scope elsewhere is not onboarded for project alpha"):
        apply_retention(config, scope="elsewhere")


def test_a_dry_run_lists_what_is_due_and_deletes_nothing(config: Config, archivum: Archivum, tmp_path: Path) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    put(config, tmp_path, b"young", days=1)
    put(config, tmp_path, b"classless", days=OLD, retention_class=None)
    rule = declare(config, "short", 30)
    run = apply_retention(config)
    assert isinstance(run, RetentionRun)
    assert run.dry_run is True and run.outcomes == ()
    assert run.due == (DueAddress("default", due.checksum, 3, rule.rule_id, (due.artifact_id,)),)
    assert store_of(config).head(object_key_of(due)) is not None
    assert deletion_rows(archivum) == []


def test_a_dry_run_opens_no_store_and_a_real_run_needs_the_deleter(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    assert config.archive is not None
    if config.archive.backend == "s3":
        for variable in (S3_DELETER_ACCESS_KEY_VARIABLE, S3_DELETER_SECRET_KEY_VARIABLE):
            monkeypatch.delenv(variable)
        with pytest.raises(ConfigurationError, match="Environment variable"):
            apply_retention(config, dry_run=False)
    else:
        assert config.archive.path is not None
        config = replace(config, archive=replace(config.archive, path=config.archive.path / "gone"))
        with pytest.raises(ConfigurationError, match="does not exist"):
            apply_retention(config, dry_run=False)
    assert [d.checksum for d in apply_retention(config).due] == [due.checksum]


def test_a_config_without_a_deleter_is_refused_by_a_real_run(
    archive_settings_s3: ArchiveSettings, archivum: Archivum, tmp_path: Path
) -> None:
    config = archive_config(archivum, tmp_path, replace(archive_settings_s3, deleter=None))
    with pytest.raises(ConfigurationError, match="archive.deleter"):
        apply_retention(config, dry_run=False)
    assert apply_retention(config).due == ()


# --- apply_retention: a real run ---


def test_a_real_run_deletes_the_due_objects_and_their_manifests_and_records_each_deletion(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    classless = put(config, tmp_path, b"classless", days=OLD, retention_class=None)
    longer = put(config, tmp_path, b"longer", days=OLD, retention_class="long")
    young = put(config, tmp_path, b"young", days=1)
    rule = declare(config, "short", 30)
    declare(config, "long", 365)
    run = apply_retention(config, dry_run=False)
    assert run.dry_run is False and [d.checksum for d in run.due] == [due.checksum]
    [outcome] = run.outcomes
    assert (outcome.stage, outcome.cause, outcome.detail) == ("confirmed", "retention", None)
    assert (outcome.scope, outcome.checksum, outcome.size) == ("default", due.checksum, 3)
    store = store_of(config)
    assert store.head(object_key_of(due)) is None and store.head(manifest_key_of(due)) is None
    assert not store.any_version_left(object_key_of(due))
    for kept in (classless, longer, young):
        assert store.head(object_key_of(kept)) is not None and store.head(manifest_key_of(kept)) is not None
    intent, confirmed = deletion_rows(archivum)
    assert intent[1:5] == ("intent", None, "retention", rule.rule_id)
    assert confirmed[1:5] == ("confirmed", intent[0], "retention", rule.rule_id)
    assert (intent[0], outcome.deletion_id) == (intent[0], intent[0])
    assert (confirmed[5:8], intent[5:8]) == (("default", due.checksum, 3), ("default", due.checksum, 3))
    with open_archive(config) as session:
        with pytest.raises(UnknownRecordError, match="is not in the archive"):
            session.get_stream(due, open(os.devnull, "wb"))
        for kept in (classless, longer, young):
            session.get(kept, tmp_path / f"out-{kept.artifact_id}")


def test_both_deletion_records_are_in_the_store_and_decode_to_the_rows(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    apply_retention(config, dry_run=False)
    records = {record.stage: record for record in stored_records(config)}
    assert set(records) == {"intent", "confirmed"}
    for row in deletion_rows(archivum):
        record = records[row[1]]
        assert (record.deletion_id, record.stage, record.intent_id, record.cause, record.rule_id) == row[:5]
        assert (record.scope, record.checksum, record.size, record.reason, record.project) == (
            row[5],
            row[6],
            row[7],
            row[8],
            "alpha",
        )
    assert records["confirmed"].recorded_at >= records["intent"].recorded_at
    assert records["intent"].checksum == due.checksum
    assert all(key.startswith("alpha/_records/") for key in (s.key for s in store_of(config).list("alpha/_records")))


def test_every_version_of_an_object_goes(
    config: Config, archivum: Archivum, tmp_path: Path, tamper: Callable[[str, bytes | None], None]
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    tamper(object_key_of(due), b"overwritten by someone else: a second version on a versioned bucket")
    run = apply_retention(config, dry_run=False)
    assert [o.stage for o in run.outcomes] == ["confirmed"]
    store = store_of(config)
    assert store.head(object_key_of(due)) is None and not store.any_version_left(object_key_of(due))


def test_keep_newest_leaves_the_newest_of_a_dataset(config: Config, archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(config) as catalog:
        dataset = catalog.register_dataset("ns", "series")
    artifacts = [put(config, tmp_path, f"v{d}".encode(), days=d, dataset=dataset) for d in (90, 80, 70)]
    declare(config, "short", 30)
    run = apply_retention(config, dry_run=False)
    assert sorted(o.checksum for o in run.outcomes) == sorted(a.checksum for a in artifacts[:2])
    assert store_of(config).head(object_key_of(artifacts[2])) is not None
    declare(config, "short", 30, keep_newest=0)
    assert [d.checksum for d in apply_retention(config).due] == [artifacts[2].checksum]


def test_a_second_run_finds_nothing_more(config: Config, archivum: Archivum, tmp_path: Path) -> None:
    put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    assert len(apply_retention(config, dry_run=False).outcomes) == 1
    again = apply_retention(config, dry_run=False)
    assert (again.due, again.outcomes) == ((), ())
    assert stages(archivum) == ["intent", "confirmed"]


def test_a_scope_limits_the_run(config: Config, archivum: Archivum, tmp_path: Path) -> None:
    from ulduar.freya import onboard_project

    role = next(r.role for r in archivum.recorders if r.env == config.archivum.dsn_env)
    onboard_project(config, "alpha", role, scopes=("default", "other"))
    here = put(config, tmp_path, b"here", days=OLD)
    there = put(config, tmp_path, b"there", days=OLD, scope="other")
    declare(config, "short", 30)
    assert sorted(d.scope for d in apply_retention(config).due) == ["default", "other"]
    run = apply_retention(config, dry_run=False, scope="other")
    assert [o.scope for o in run.outcomes] == ["other"]
    store = store_of(config)
    assert store.head(object_key_of(there)) is None and store.head(object_key_of(here)) is not None


def test_history_retention_can_age_the_artifact_of_a_deleted_object_out(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    kept = put(config, tmp_path, b"kept", days=OLD, retention_class=None)
    declare(config, "short", 30)
    declare_history_retention(config, "alpha", "artifact", 30)
    before = apply_history_retention(config, dry_run=False)
    assert [r.rows for r in before] == [0]  # both objects are still archived
    apply_retention(config, dry_run=False)
    [after] = apply_history_retention(config, dry_run=False)
    assert after.rows == 1
    with archivum.superuser() as su:
        found = su.execute("SELECT artifact_id FROM ulduar.artifact").fetchall()
    assert [row[0] for row in found] == [kept.artifact_id] and due.artifact_id not in [row[0] for row in found]


# --- the safeguard on the number of deletions ---


def test_a_run_over_the_limit_deletes_nothing_unless_told_to(
    config: Config, archivum: Archivum, tmp_path: Path
) -> None:
    assert config.archive is not None
    limited = replace(config, archive=replace(config.archive, max_deletions_per_run=1))
    first, second = (put(config, tmp_path, name, days=OLD) for name in (b"one", b"two"))
    declare(config, "short", 30)
    with pytest.raises(
        UlduarError,
        match=r"Retention would delete 2 objects, more than archive.max_deletions_per_run \(1\);"
        " pass allow_over_limit=True to go ahead",
    ):
        apply_retention(limited, dry_run=False)
    assert deletion_rows(archivum) == []
    assert all(store_of(config).head(object_key_of(a)) is not None for a in (first, second))
    assert len(apply_retention(limited).due) == 2  # a dry run lists every object, the limit or not
    run = apply_retention(limited, dry_run=False, allow_over_limit=True)
    assert [o.stage for o in run.outcomes] == ["confirmed", "confirmed"]
    assert all(store_of(config).head(object_key_of(a)) is None for a in (first, second))


def test_a_run_of_exactly_the_limit_goes_ahead(config: Config, tmp_path: Path) -> None:
    assert config.archive is not None
    put(config, tmp_path, b"one", days=OLD)
    declare(config, "short", 30)
    run = apply_retention(replace(config, archive=replace(config.archive, max_deletions_per_run=1)), dry_run=False)
    assert [o.stage for o in run.outcomes] == ["confirmed"]


# --- an object that cannot be deleted stays an open intent ---


def test_an_unavailable_store_leaves_the_intent_open_and_the_run_goes_on_and_the_next_run_resumes(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = sorted_by_checksum(*(put(config, tmp_path, name, days=OLD) for name in (b"one", b"two")))
    declare(config, "short", 30)
    hook_store(monkeypatch, fail_first_delete=True)
    run = apply_retention(config, dry_run=False)
    assert [(o.checksum, o.stage) for o in run.outcomes] == [(first.checksum, "open"), (second.checksum, "confirmed")]
    assert run.outcomes[0].detail is not None and "injected" in run.outcomes[0].detail
    store = store_of(config)
    assert store.head(object_key_of(first)) is not None and store.head(object_key_of(second)) is None
    assert stages(archivum) == ["intent", "intent", "confirmed"]
    intent_id = run.outcomes[0].deletion_id
    monkeypatch.setattr(retention_module, "open_store", open_store)
    resumed = apply_retention(config, dry_run=False)
    [outcome] = resumed.outcomes
    assert (outcome.stage, outcome.deletion_id) == ("confirmed", intent_id)
    assert stages(archivum) == ["intent", "intent", "confirmed", "confirmed"]  # no second intent for the first object
    assert store.head(object_key_of(first)) is None


def test_the_writer_credential_cannot_run_a_real_retention_and_the_deleter_resumes_the_intent(
    archive_settings_s3: ArchiveSettings, archivum: Archivum, tmp_path: Path
) -> None:
    config = archive_config(archivum, tmp_path, archive_settings_s3)
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    assert archive_settings_s3.writer is not None
    as_writer = replace(config, archive=replace(archive_settings_s3, deleter=archive_settings_s3.writer))
    run = apply_retention(as_writer, dry_run=False)
    [outcome] = run.outcomes
    assert outcome.stage == "open" and outcome.detail is not None and "AccessDenied" in outcome.detail
    assert store_of(config).head(object_key_of(due)) is not None
    assert stages(archivum) == ["intent"]
    assert [record.stage for record in stored_records(config)] == ["intent"]
    resumed = apply_retention(config, dry_run=False)
    [done] = resumed.outcomes
    assert (done.stage, done.deletion_id) == ("confirmed", outcome.deletion_id)
    assert stages(archivum) == ["intent", "confirmed"]
    assert store_of(config).head(object_key_of(due)) is None


@pytest.fixture
def locked_archive_settings(
    s3_client: Any, s3_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ArchiveSettings]:
    """Settings of an archive in a bucket with object lock enabled; teardown bypasses the governance retention."""
    name = TEST_BUCKET_PREFIX + uuid.uuid4().hex[:12]
    s3_client.create_bucket(Bucket=name, ObjectLockEnabledForBucket=True)
    try:
        yield s3_archive_settings(name, s3_settings, monkeypatch)
    finally:
        for page in s3_client.get_paginator("list_object_versions").paginate(Bucket=name):
            for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
                s3_client.delete_object(
                    Bucket=name, Key=item["Key"], VersionId=item["VersionId"], BypassGovernanceRetention=True
                )
        s3_client.delete_bucket(Bucket=name)


def test_an_object_under_object_lock_stays_and_its_intent_stays_open(
    locked_archive_settings: ArchiveSettings, s3_client: Any, archivum: Archivum, tmp_path: Path
) -> None:
    config = archive_config(archivum, tmp_path, locked_archive_settings)
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    full_key = f"{locked_archive_settings.prefix}/{object_key_of(due)}"
    [version] = s3_client.list_object_versions(Bucket=locked_archive_settings.bucket, Prefix=full_key)["Versions"]
    s3_client.put_object_retention(
        Bucket=locked_archive_settings.bucket,
        Key=full_key,
        VersionId=version["VersionId"],
        Retention={"Mode": "GOVERNANCE", "RetainUntilDate": datetime.now(UTC) + timedelta(days=1)},
    )
    [outcome] = apply_retention(config, dry_run=False).outcomes
    assert outcome.stage == "open" and outcome.detail is not None and "failed" in outcome.detail
    assert store_of(config).head(object_key_of(due)) is not None
    assert store_of(config).head(manifest_key_of(due)) is not None
    assert stages(archivum) == ["intent"]
    with open_archive(config) as session:
        session.get(due, tmp_path / "still-there")


# --- the object is due no longer when its turn comes ---


@pytest.mark.parametrize("change", ["a put", "a longer rule"])
def test_an_object_that_stops_being_due_meanwhile_is_skipped(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    first, second = sorted_by_checksum(*(put(config, tmp_path, name, days=OLD) for name in (b"one", b"two")))
    declare(config, "short", 30)
    second_content = b"one" if second.checksum == digest("one") else b"two"

    def meanwhile() -> None:
        if change == "a put":
            put(config, tmp_path, second_content, days=0)
        else:
            declare_retention(config, "alpha", "short", 365)

    hook_store(monkeypatch, on_first_delete=meanwhile)
    run = apply_retention(config, dry_run=False)
    assert [(o.checksum, o.stage) for o in run.outcomes] == [
        (first.checksum, "confirmed"),
        (second.checksum, "skipped"),
    ]
    skipped = run.outcomes[1]
    assert skipped.deletion_id is None and skipped.detail is not None
    assert store_of(config).head(object_key_of(second)) is not None
    assert stages(archivum) == ["intent", "confirmed"]


# --- a deletion against a put of the same content (D37) ---


def test_a_put_between_the_intent_and_the_deletion_cancels_it(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    with ThreadPoolExecutor(max_workers=1) as pool:
        racing: list[Artifact] = []

        def put_the_same() -> None:
            racing.append(pool.submit(put, config, tmp_path, b"due", days=0).result(timeout=30))

        hook_store(monkeypatch, on_first_record=put_the_same)  # the intent is committed, the object not yet deleted
        run = apply_retention(config, dry_run=False)
    [outcome] = run.outcomes
    assert outcome.stage == "cancelled" and outcome.detail is not None
    assert store_of(config).head(object_key_of(old)) is not None
    assert stages(archivum) == ["intent", "cancelled"]
    assert [record.stage for record in stored_records(config)].count("cancelled") == 1
    with open_archive(config) as session:
        session.get(racing[0], tmp_path / "new")
        session.get(old, tmp_path / "old")  # nothing was deleted, so the old registration still stands


def test_a_put_that_starts_while_the_deletion_holds_the_lock_uploads_the_object_again(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []

        def start_a_put_and_wait_for_the_lock() -> None:
            futures.append(pool.submit(put, config, tmp_path, b"due", days=0))
            wait_until_blocked(archivum)

        hook_store(monkeypatch, on_first_delete=start_a_put_and_wait_for_the_lock)
        run = apply_retention(config, dry_run=False)
        new = futures[0].result(timeout=30)
    assert [o.stage for o in run.outcomes] == ["confirmed"]
    assert stages(archivum) == ["intent", "confirmed"]
    assert store_of(config).head(object_key_of(old)) is not None  # the put found the object gone and stored it again
    with open_archive(config) as session:
        session.get(new, tmp_path / "new")
        with pytest.raises(UnknownRecordError, match="is not in the archive"):
            session.get(old, tmp_path / "old")


def test_a_rule_lengthened_between_the_intent_and_the_deletion_cancels_it(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    # the intent is committed, its record not yet written: the rule changes in between
    hook_store(monkeypatch, on_first_record=lambda: declare_retention(config, "alpha", "short", 365))
    [outcome] = apply_retention(config, dry_run=False).outcomes
    assert (outcome.stage, outcome.detail) == ("cancelled", "the object is not due any more")
    assert store_of(config).head(object_key_of(due)) is not None
    assert store_of(config).head(manifest_key_of(due)) is not None
    assert stages(archivum) == ["intent", "cancelled"]
    assert sorted(record.stage for record in stored_records(config)) == ["cancelled", "intent"]
    assert apply_retention(config, dry_run=False).outcomes == ()  # not due, no open intent: nothing to do


def test_an_open_intent_whose_object_is_no_longer_due_is_listed_by_a_dry_run_and_cancelled_by_a_real_one(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    hook_store(monkeypatch, fail_first_delete=True)
    [refused] = apply_retention(config, dry_run=False).outcomes
    assert refused.stage == "open" and stages(archivum) == ["intent"]
    monkeypatch.setattr(retention_module, "open_store", open_store)
    assert apply_retention(config).open_intents == ()  # still due: the due list carries it
    rule = declare_retention(config, "alpha", "short", 365)
    dry = apply_retention(config)
    assert dry.due == () and dry.outcomes == ()
    assert dry.open_intents == (
        OpenIntent(refused.deletion_id, "default", due.checksum, 3, dry.open_intents[0].rule_id),
    )
    assert stages(archivum) == ["intent"]  # a dry run settles nothing
    run = apply_retention(config, dry_run=False)
    assert (run.due, run.open_intents) == ((), dry.open_intents)
    [cancelled] = run.outcomes
    assert (cancelled.stage, cancelled.deletion_id) == ("cancelled", refused.deletion_id)
    assert stages(archivum) == ["intent", "cancelled"]  # the same intent, no second one
    assert store_of(config).head(object_key_of(due)) is not None
    assert store_of(config).head(manifest_key_of(due)) is not None
    assert [record.stage for record in stored_records(config)].count("cancelled") == 1
    assert apply_retention(config, dry_run=False).outcomes == ()
    assert rule.rule_id != dry.open_intents[0].rule_id  # the intent keeps the rule it was recorded under


def test_a_resumed_intent_is_cancelled_when_the_rule_is_lengthened_as_it_is_recorded_again(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    hook_store(monkeypatch, fail_first_delete=True)
    [refused] = apply_retention(config, dry_run=False).outcomes
    hook_store(monkeypatch, on_first_record=lambda: declare_retention(config, "alpha", "short", 365))
    [resumed] = apply_retention(config, dry_run=False).outcomes  # due when listed, so resumed through the due list
    assert (resumed.stage, resumed.deletion_id) == ("cancelled", refused.deletion_id)
    assert stages(archivum) == ["intent", "cancelled"]
    assert store_of(config).head(object_key_of(due)) is not None


def test_open_intents_do_not_count_towards_the_limit_and_other_causes_are_left_alone(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert config.archive is not None
    first, _ = sorted_by_checksum(*(put(config, tmp_path, name, days=OLD) for name in (b"one", b"two")))
    declare(config, "short", 30)
    hook_store(monkeypatch, fail_first_delete=True)
    [refused, _] = apply_retention(config, dry_run=False).outcomes
    assert refused.checksum == first.checksum
    monkeypatch.setattr(retention_module, "open_store", open_store)
    with archivum.superuser() as su:  # an open intent of another cause is not retention's to settle
        su.execute(
            "INSERT INTO ulduar.archive_deletion (project_id, deletion_id, event_at, scope, checksum, size_bytes,"
            " stage, cause) SELECT project_id, %s, %s, 'default', %s, 3, 'intent', 'incomplete_put'"
            " FROM ulduar.project",
            (*(lambda i: (i, _ids.uuid7_time(i)))(_ids.uuid7()), digest("stray")),
        )
    declare(config, "short", 365)  # the first object's intent is open and not due now
    third = put(config, tmp_path, b"three", days=OLD, retention_class="other")
    declare(config, "other", 30)
    limited = replace(config, archive=replace(config.archive, max_deletions_per_run=1))
    run = apply_retention(limited, dry_run=False)  # one due object is the limit; the open intent is on top of it
    assert [o.stage for o in run.outcomes] == ["cancelled", "confirmed"]
    assert [o.checksum for o in run.outcomes] == [first.checksum, third.checksum]
    assert len(run.open_intents) == 1 and len(run.due) == 1
    stray = [row[1] for row in deletion_rows(archivum) if row[3] == "incomplete_put"]
    assert stray == ["intent"]  # the open intent of another cause stays open and untouched
    assert store_of(config).head(object_key_of(first)) is not None
    assert store_of(config).head(object_key_of(third)) is None


def test_an_intent_another_run_settled_meanwhile_is_not_settled_twice(
    config: Config, archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    fired: list[bool] = []
    with ThreadPoolExecutor(max_workers=1) as pool:

        def another_run_resumes_the_open_intent() -> None:
            if not fired:
                fired.append(True)
                [outcome] = pool.submit(apply_retention, config, dry_run=False).result(timeout=30).outcomes
                assert outcome.stage == "confirmed"

        hook_store(monkeypatch, on_first_record=another_run_resumes_the_open_intent)
        [outcome] = apply_retention(config, dry_run=False).outcomes
    assert outcome.stage == "skipped" and outcome.deletion_id is not None
    assert stages(archivum) == ["intent", "confirmed"]
    assert store_of(config).head(object_key_of(due)) is None


# --- secrets ---


def test_no_credential_appears_in_the_log_records_of_a_real_run(
    config: Config, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert config.archive is not None
    names = [config.archivum.dsn_env, config.archivum.ledger_dsn_env]
    for credential in (config.archive.writer, config.archive.deleter):
        names += [] if credential is None else [credential.access_key_env, credential.secret_key_env]
    secrets = [os.environ[name] for name in names if name]
    secrets += [conninfo_to_dict(dsn)["password"] for dsn in secrets[:2]]  # the connection strings come first
    assert all(secrets)
    put(config, tmp_path, b"due", days=OLD)
    caplog.set_level(logging.DEBUG, logger="ulduar")
    declare(config, "short", 30)
    apply_retention(config)
    apply_retention(config, dry_run=False)
    records = [r for r in caplog.records if r.name.startswith("ulduar")]
    messages = [r.getMessage() for r in records if r.name.startswith("ulduar.hodir.retention")]
    assert any(m.startswith("declared retention class short") for m in messages)
    assert any("is confirmed" in m for m in messages)
    assert any(m.startswith("retention applied: 1 due") for m in messages)
    text = "\n".join(f"{r.getMessage()} {r.exc_text or ''} {r.args}" for r in records)
    assert not [secret for secret in secrets if secret in text]


def test_a_record_that_cannot_be_written_after_the_commit_is_only_logged(
    config: Config,
    archivum: Archivum,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    due = put(config, tmp_path, b"due", days=OLD)
    declare(config, "short", 30)
    writes = threading.Event()

    class FailingRecords(HookedStore):
        def put_bytes_new(self, key: str, data: bytes) -> bool:
            if "/_records/" in key and writes.is_set():
                raise ArchiveUnavailable(f"Putting {key} failed: injected")
            if "/_records/" in key:
                writes.set()  # the intent's record is written; the outcome's is not
            return super().put_bytes_new(key, data)

    monkeypatch.setattr(
        retention_module,
        "open_store",
        lambda settings, *, deleting=False: FailingRecords(open_store(settings, deleting=deleting)),
    )
    caplog.set_level(logging.WARNING, logger="ulduar.hodir")
    run = apply_retention(config, dry_run=False)
    assert [o.stage for o in run.outcomes] == ["confirmed"]
    assert stages(archivum) == ["intent", "confirmed"]
    assert [record.stage for record in stored_records(config)] == ["intent"]
    assert any("was not written to the archive" in r.getMessage() for r in caplog.records)
    assert store_of(config).head(object_key_of(due)) is None
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_deletion") == 2
