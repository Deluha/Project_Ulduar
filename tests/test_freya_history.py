"""ulduar.freya.history: onboarding, retention rules and their application, redaction — all as the ledger role."""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from tests.conftest import (
    CHECKSUM,
    Archivum,
    count,
    lineage_lock_name,
    ms_ago,
    old_artifacts,
    onboarded,
    partition_exists,
    recorder_of,
    wait_until_blocked,
)
from ulduar import _db, _ids
from ulduar.config import Config
from ulduar.errors import ConfigurationError, SchemaVersionError, UlduarError, UnknownRecordError
from ulduar.freya import (
    Project,
    RetentionResult,
    apply_history_retention,
    declare_history_retention,
    install,
    onboard_project,
    open_catalog,
    redact,
)
from ulduar.freya.history import HISTORIES, MAINTENANCE_SETTING, retention_lock_name

OLD_DAYS = 200
OLDER_DAYS = 300


def without_ledger(config: Config) -> Config:
    return dataclasses.replace(config, archivum=dataclasses.replace(config.archivum, ledger_dsn_env=None))


def old_edge(archivum: Archivum, project: str, child: uuid.UUID, parent: uuid.UUID, days: int) -> None:
    """An old lineage edge, written by superuser: the retention test needs history of an age the library never makes."""
    edge_id = _ids.uuid7(ms_ago(days))
    with archivum.superuser() as su:
        su.execute("SELECT ulduar.ensure_partitions(%s, 0)", (_ids.uuid7_time(edge_id),))
        su.execute(
            "INSERT INTO ulduar.lineage_edge (project_id, edge_id, event_at, child_kind, child_id, parent_kind,"
            " parent_id) SELECT project_id, %s, %s, 'artifact', %s, 'artifact', %s FROM ulduar.project WHERE name = %s",
            (edge_id, _ids.uuid7_time(edge_id), child, parent, project),
        )


# --- onboarding ---


def test_onboarding_creates_the_project_its_scopes_and_the_membership(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path, project="alpha")
    install(config)
    role = archivum.recorders[-1].role
    project = onboard_project(config, "alpha", role, scopes=("default", "staging"))
    assert isinstance(project, Project)
    assert (project.name, project.project_id.version) == ("alpha", 7)
    assert count(archivum, "SELECT count(*) FROM ulduar.scope WHERE project_id = %s", project.project_id) == 2
    assert count(archivum, "SELECT count(*) FROM ulduar.project_member WHERE role_name = %s", role) == 1
    assert (
        count(
            archivum,
            "SELECT count(*) FROM pg_auth_members WHERE roleid = 'ulduar_recorder'::regrole AND member = %s::regrole",
            role,
        )
        == 1
    )
    with open_catalog(config) as catalog:
        assert catalog.project == project


def test_onboarding_twice_changes_nothing(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    install(config)
    role = archivum.recorders[-1].role
    first = onboard_project(config, "alpha", role)
    assert onboard_project(config, "alpha", role) == first
    again = onboard_project(config, "alpha", role, scopes=("default", "extra"))
    assert again == first
    assert count(archivum, "SELECT count(*) FROM ulduar.project") == 1
    assert count(archivum, "SELECT count(*) FROM ulduar.scope") == 2
    assert count(archivum, "SELECT count(*) FROM ulduar.project_member") == 1


def test_a_second_role_joins_the_same_project(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    install(config)
    first = onboard_project(config, "alpha", archivum.recorders[-1].role)
    second = onboard_project(config, "alpha", archivum.new_recorder().role)
    assert second == first
    assert count(archivum, "SELECT count(*) FROM ulduar.project_member") == 2


def test_a_role_of_another_project_is_refused_and_nothing_is_kept(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    install(config)
    role = archivum.recorders[-1].role
    onboard_project(config, "alpha", role)
    with pytest.raises(UlduarError, match=f"{role} already belongs to project alpha"):
        onboard_project(config, "beta", role, scopes=("default", "more"))
    assert count(archivum, "SELECT count(*) FROM ulduar.project WHERE name = 'beta'") == 0
    assert count(archivum, "SELECT count(*) FROM ulduar.scope") == 1


def test_scopes_given_as_one_string_is_refused_before_any_sql(archivum: Archivum, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="'raw'.*not a single string"):
        onboard_project(archivum.config(tmp_path), "alpha", "any_role", scopes="raw")


def test_an_unknown_login_role_is_refused(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    install(config)
    with pytest.raises(UnknownRecordError, match="Login role ulduar_no_such_role does not exist"):
        onboard_project(config, "alpha", "ulduar_no_such_role")
    assert count(archivum, "SELECT count(*) FROM ulduar.project") == 0


@pytest.mark.parametrize(
    ("project", "role", "scopes", "message"),
    [
        ("Alpha", "r", ("default",), "project 'Alpha'"),
        ("", "r", ("default",), "project ''"),
        ("alpha", "r", ("default", "Bad Scope"), "scope 'Bad Scope'"),
        ("alpha", "", ("default",), "login role ''"),
    ],
)
def test_onboarding_validates_names_before_any_sql(
    archivum: Archivum, tmp_path: Path, project: str, role: str, scopes: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        onboard_project(archivum.config(tmp_path), project, role, scopes=scopes)


def test_a_role_name_that_needs_quoting_is_granted_safely(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    install(config)
    quoted = 'ulduar_test_"quote'
    with archivum.superuser() as su:
        su.execute('CREATE ROLE "ulduar_test_""quote" LOGIN')
    try:
        onboard_project(config, "alpha", quoted)
        assert count(archivum, "SELECT count(*) FROM ulduar.project_member WHERE role_name = %s", quoted) == 1
    finally:
        with archivum.superuser() as su:
            su.execute('DROP ROLE "ulduar_test_""quote"')


# --- declaring and applying retention ---


def test_declaring_validates_its_arguments_and_the_project(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    for keep_days in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="keep_days"):
            declare_history_retention(config, "alpha", "artifact", keep_days)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="'dataset'"):
        declare_history_retention(config, "alpha", "dataset", 30)  # type: ignore[arg-type]
    with pytest.raises(UnknownRecordError, match="Project nowhere"):
        declare_history_retention(config, "nowhere", "artifact", 30)
    rule = declare_history_retention(config, "alpha", "lineage_edge", None)
    assert (rule.project, rule.history, rule.keep_days) == ("alpha", "lineage_edge", None)
    assert rule.event_at == _ids.uuid7_time(rule.rule_id)


def test_a_dry_run_counts_the_rows_a_rule_would_remove_and_removes_nothing(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    old_artifacts(config, OLD_DAYS, OLDER_DAYS, 1)
    rule = declare_history_retention(config, "alpha", "artifact", 30)
    now = datetime.now(UTC)
    [result] = apply_history_retention(config, now=now)
    assert result == RetentionResult("alpha", "artifact", rule.rule_id, now - timedelta(days=30), 2, True)
    assert apply_history_retention(config, dry_run=True, now=now) == [result]
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 3
    assert count(archivum, "SELECT count(*) FROM ulduar.history_retention_action") == 0


def test_applying_removes_only_the_old_rows_of_the_projects_with_a_rule(archivum: Archivum, tmp_path: Path) -> None:
    alpha, beta = onboarded(archivum, tmp_path, "alpha"), onboarded(archivum, tmp_path, "beta")
    [alpha_older, alpha_old, alpha_new] = old_artifacts(alpha, OLDER_DAYS, OLD_DAYS, 1)
    [beta_older] = old_artifacts(beta, OLDER_DAYS)
    rule = declare_history_retention(alpha, "alpha", "artifact", 30)
    now = datetime.now(UTC)
    [result] = apply_history_retention(alpha, dry_run=False, now=now)
    assert result == RetentionResult("alpha", "artifact", rule.rule_id, now - timedelta(days=30), 2, False)
    with open_catalog(alpha) as catalog:
        assert [a.artifact_id for a in catalog.find_artifacts()] == [alpha_new]
    with open_catalog(beta) as catalog:
        assert [a.artifact_id for a in catalog.find_artifacts()] == [beta_older]
    with archivum.superuser() as su:
        action = su.execute(
            "SELECT a.rule_id, a.history, a.cutoff, a.rows_removed, p.name,"
            " a.event_at = uuid_extract_timestamp(a.action_id)"
            " FROM ulduar.history_retention_action a JOIN ulduar.project p USING (project_id)"
        ).fetchall()
    assert action == [(rule.rule_id, "artifact", now - timedelta(days=30), 2, "alpha", True)]
    assert (
        count(archivum, "SELECT count(*) FROM ulduar.artifact WHERE artifact_id = ANY(%s)", [alpha_older, alpha_old])
        == 0
    )


def test_applying_drops_the_partitions_it_emptied_and_keeps_the_others(archivum: Archivum, tmp_path: Path) -> None:
    alpha, beta = onboarded(archivum, tmp_path, "alpha"), onboarded(archivum, tmp_path, "beta")
    old_artifacts(alpha, OLDER_DAYS, OLD_DAYS)
    old_artifacts(beta, OLDER_DAYS)
    assert partition_exists(archivum, "artifact", ms_ago(OLD_DAYS)) and partition_exists(
        archivum, "artifact", ms_ago(OLDER_DAYS)
    )
    declare_history_retention(alpha, "alpha", "artifact", 30)
    apply_history_retention(alpha, dry_run=False)
    assert not partition_exists(archivum, "artifact", ms_ago(OLD_DAYS))
    assert partition_exists(archivum, "artifact", ms_ago(OLDER_DAYS))  # beta still has a row in it
    assert partition_exists(archivum, "artifact", ms_ago(0))


def test_applying_a_lineage_edge_rule_removes_old_edges_by_their_own_age(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    [a, b] = old_artifacts(config, 1, 2)
    old_edge(archivum, "alpha", a, b, OLD_DAYS)
    declare_history_retention(config, "alpha", "lineage_edge", 30)
    [result] = apply_history_retention(config, dry_run=False)
    assert (result.history, result.rows) == ("lineage_edge", 1)
    assert count(archivum, "SELECT count(*) FROM ulduar.lineage_edge") == 0
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 2
    assert not partition_exists(archivum, "lineage_edge", ms_ago(OLD_DAYS))


def test_without_a_rule_nothing_is_removed(
    archivum: Archivum, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = onboarded(archivum, tmp_path)
    old_artifacts(config, OLD_DAYS)
    caplog.set_level(logging.INFO, logger="ulduar.freya.history")
    assert apply_history_retention(config) == []
    assert apply_history_retention(config, dry_run=False) == []
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 1
    assert sum("nothing to apply" in r.getMessage() for r in caplog.records) == 1


def test_the_latest_rule_wins_and_keep_for_ever_stops_retention(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    old_artifacts(config, OLD_DAYS, 1)
    declare_history_retention(config, "alpha", "artifact", 30)
    long_rule = declare_history_retention(config, "alpha", "artifact", 1000)
    [result] = apply_history_retention(config)
    assert (result.rule_id, result.rows) == (long_rule.rule_id, 0)
    declare_history_retention(config, "alpha", "artifact", 10)
    assert [r.rows for r in apply_history_retention(config)] == [1]
    declare_history_retention(config, "alpha", "artifact", None)
    assert apply_history_retention(config, dry_run=False) == []
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 2


def test_apply_needs_a_timezone_aware_now(archivum: Archivum, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        apply_history_retention(archivum.config(tmp_path), now=datetime(2030, 1, 1))  # noqa: DTZ001


# --- redaction ---


def test_redacting_an_origin_sets_it_to_null_and_records_the_redaction(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    with open_catalog(config) as catalog:
        artifact = catalog.register_artifact("raw_file", CHECKSUM, 1, origin="people/jane.csv")
        result = redact(config, "alpha", "artifact", artifact.artifact_id, "origin", "personal_data")
        after = catalog.get_artifact(artifact.artifact_id)
    assert after == dataclasses.replace(artifact, origin=None)
    assert (result.project, result.history, result.record_id, result.field, result.reason) == (
        "alpha",
        "artifact",
        artifact.artifact_id,
        "origin",
        "personal_data",
    )
    assert result.event_at == _ids.uuid7_time(result.redaction_id)
    with archivum.superuser() as su:
        recorded = su.execute("SELECT redaction_id, history, record_id, field, reason FROM ulduar.redaction").fetchall()
    assert recorded == [(result.redaction_id, "artifact", artifact.artifact_id, "origin", "personal_data")]


@pytest.mark.parametrize("field", ["name", "namespace"])
def test_redacting_a_dataset_field_replaces_it_with_the_redaction_id(
    archivum: Archivum, tmp_path: Path, field: str
) -> None:
    config = onboarded(archivum, tmp_path)
    with open_catalog(config) as catalog:
        dataset = catalog.register_dataset("people", "jane")
        result = redact(config, "alpha", "dataset", dataset.dataset_id, field, "secret")
        again = catalog.register_dataset("people", "jane")  # the original identity is free to register anew
    expected = f"redacted:{result.redaction_id}"
    with archivum.superuser() as su:
        row = su.execute(
            "SELECT namespace, name FROM ulduar.dataset WHERE dataset_id = %s", (dataset.dataset_id,)
        ).fetchone()
    assert row == ((expected, "jane") if field == "namespace" else ("people", expected))
    assert again.dataset_id != dataset.dataset_id


@pytest.mark.parametrize(
    ("history", "field", "reason"),
    [
        ("artifact", "kind", "secret"),
        ("artifact", "checksum", "secret"),
        ("dataset", "scope", "secret"),
        ("dataset", "origin", "secret"),
        ("lineage_edge", "run_id", "secret"),
        ("artifact", "origin", "because"),
    ],
)
def test_a_field_or_reason_outside_the_allowed_pairs_is_refused(
    archivum: Archivum, tmp_path: Path, history: str, field: str, reason: str
) -> None:
    config = archivum.config(tmp_path)  # refused before any connection: the Archivum need not exist
    with pytest.raises(ValueError, match="cannot be redacted|Reason"):
        redact(config, "alpha", history, uuid.uuid4(), field, reason)  # type: ignore[arg-type]


def test_redacting_an_unknown_or_foreign_record_is_refused_and_records_nothing(
    archivum: Archivum, tmp_path: Path
) -> None:
    alpha, beta = onboarded(archivum, tmp_path, "alpha"), onboarded(archivum, tmp_path, "beta")
    with open_catalog(alpha) as catalog:
        artifact = catalog.register_artifact("log", CHECKSUM, 1, origin="a")
    with pytest.raises(UnknownRecordError, match="not recorded in project alpha"):
        redact(alpha, "alpha", "artifact", uuid.uuid4(), "origin", "secret")
    with pytest.raises(UnknownRecordError, match="not recorded in project beta"):
        redact(beta, "beta", "artifact", artifact.artifact_id, "origin", "secret")
    with pytest.raises(UnknownRecordError, match="Project nowhere"):
        redact(alpha, "nowhere", "artifact", artifact.artifact_id, "origin", "secret")
    assert count(archivum, "SELECT count(*) FROM ulduar.redaction") == 0
    with open_catalog(alpha) as catalog:
        assert catalog.get_artifact(artifact.artifact_id).origin == "a"


# --- retention and redaction records are never removed ---


def maintenance_failure(config: Config, mode: str, statement: str) -> psycopg.Error:
    """The driver error of *statement* as the ledger role with the maintenance setting *mode* in force."""
    assert config.archivum.ledger_dsn_env
    with _db.open_connection(config.archivum.ledger_dsn_env, config.archivum) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as tx:
            tx.execute("SELECT set_config(%s, %s, true)", (MAINTENANCE_SETTING, mode))
            tx.execute(statement)
    return caught.value


@pytest.mark.parametrize("mode", ["retention", "redaction"])
@pytest.mark.parametrize("table", ["history_retention_rule", "history_retention_action", "redaction"])
@pytest.mark.parametrize("statement", ["DELETE FROM ulduar.{}", "UPDATE ulduar.{} SET recorded_at = now()"])
def test_retention_and_redaction_records_cannot_be_changed_even_in_maintenance_mode(
    archivum: Archivum, tmp_path: Path, table: str, mode: str, statement: str
) -> None:
    config = onboarded(archivum, tmp_path)
    old, recent = old_artifacts(config, OLD_DAYS, 1)
    declare_history_retention(config, "alpha", "artifact", 30)
    apply_history_retention(config, dry_run=False)
    redact(config, "alpha", "artifact", recent, "origin", "secret")
    assert count(archivum, f"SELECT count(*) FROM ulduar.{table}") == 1
    error = maintenance_failure(config, mode, statement.format(table))
    assert error.sqlstate == "42501"
    assert "append-only" in (error.diag.message_primary or "")
    assert old != recent


# --- the ledger role is required ---


def test_every_ledger_call_without_a_ledger_variable_is_a_configuration_error(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = without_ledger(onboarded(archivum, tmp_path))
    record_id = uuid.uuid4()
    calls = [
        lambda: onboard_project(config, "alpha", "any_role"),
        lambda: declare_history_retention(config, "alpha", "artifact", 30),
        lambda: apply_history_retention(config),
        lambda: redact(config, "alpha", "artifact", record_id, "origin", "secret"),
    ]
    for call in calls:
        with pytest.raises(ConfigurationError, match="archivum.ledger_dsn_env is not set"):
            call()


def test_a_ledger_call_before_install_is_a_schema_version_error(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    with pytest.raises(SchemaVersionError, match="not installed"):
        apply_history_retention(config)
    with pytest.raises(SchemaVersionError, match="not installed"):
        onboard_project(config, "alpha", archivum.recorders[-1].role)


# --- a maintenance mode passes only its own operation ---


@pytest.mark.parametrize(
    ("mode", "statement"),
    [
        ("redaction", "DELETE FROM ulduar.artifact"),
        ("retention", "UPDATE ulduar.artifact SET origin = 'x'"),
        ("redaction", "DELETE FROM ulduar.dataset"),
        ("retention", "UPDATE ulduar.dataset SET name = 'x'"),
        ("redaction", "DELETE FROM ulduar.lineage_edge"),
        ("redaction", "UPDATE ulduar.lineage_edge SET run_id = NULL"),
        ("retention", "UPDATE ulduar.lineage_edge SET run_id = NULL"),
    ],
)
def test_a_maintenance_mode_does_not_allow_the_other_modes_operation(
    archivum: Archivum, tmp_path: Path, mode: str, statement: str
) -> None:
    config = onboarded(archivum, tmp_path)
    with open_catalog(config) as catalog:
        child, parent = catalog.register_artifact("log", CHECKSUM, 1, origin="a"), catalog.register_dataset("n", "d")
        catalog.add_lineage(child, [parent])
    error = maintenance_failure(config, mode, statement)
    assert error.sqlstate == "42501"
    assert "append-only" in (error.diag.message_primary or "")
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact WHERE origin = 'a'") == 1


# --- retention races: a recorder holding locks, and a rule declared meanwhile ---


def insert_current_artifact(tx: psycopg.Connection, project_id: uuid.UUID) -> None:
    artifact_id = _ids.uuid7()
    tx.execute(
        "INSERT INTO ulduar.artifact (project_id, scope, artifact_id, event_at, kind, checksum_algorithm, checksum,"
        " size_bytes) VALUES (%s, 'default', %s, %s, 'log', 'sha256', %s, 1)",
        (project_id, artifact_id, _ids.uuid7_time(artifact_id), CHECKSUM),
    )


def test_retention_does_not_deadlock_with_a_recorder_that_holds_locks_and_reads_every_partition(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = onboarded(archivum, tmp_path, lock_timeout_s=30)
    old_artifacts(config, OLD_DAYS)
    declare_history_retention(config, "alpha", "artifact", 30)
    with open_catalog(config) as catalog:
        project_id = catalog.project.project_id
    holding = threading.Event()

    def recorder() -> int:
        with _db.open_connection(recorder_of(archivum, config).env, config.archivum) as conn:
            with _db.transaction(conn, config.archivum) as tx:
                insert_current_artifact(tx, project_id)  # the locks of an insert: the parent and this month's partition
                holding.set()
                wait_until_blocked(archivum)  # retention is queued for the parent
                row = tx.execute("SELECT count(*) FROM ulduar.artifact").fetchone()  # every partition, pruned none
        assert row is not None
        return int(row[0])

    with ThreadPoolExecutor(max_workers=2) as pool:
        recording = pool.submit(recorder)
        assert holding.wait(timeout=30)
        applying = pool.submit(apply_history_retention, config, dry_run=False)
        assert recording.result(timeout=60) == 1  # its own row; a deadlock would end one of the two with an error
        [result] = applying.result(timeout=60)
    assert (result.history, result.rows) == ("artifact", 1)
    assert not partition_exists(archivum, "artifact", ms_ago(OLD_DAYS))
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 1


def test_retention_does_not_deadlock_with_an_edge_recorder_waiting_for_its_project_lock(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = onboarded(archivum, tmp_path, lock_timeout_s=30)
    with open_catalog(config) as catalog:
        child, parent = catalog.register_dataset("n", "child"), catalog.register_dataset("n", "parent")
        lineage_lock = lineage_lock_name(catalog.project.project_id)
    old_artifacts(config, OLD_DAYS)
    declare_history_retention(config, "alpha", "artifact", 30)

    def record_edge() -> int:
        with open_catalog(config) as catalog:
            return len(catalog.add_lineage(child, [parent]))

    with archivum.superuser() as holder, ThreadPoolExecutor(max_workers=2) as pool:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lineage_lock,))
        edge = pool.submit(record_edge)
        wait_until_blocked(archivum)  # the edge holds its table lock and waits for the project's lineage lock
        applying = pool.submit(apply_history_retention, config, dry_run=False)
        wait_until_blocked(archivum, 2)  # retention's partition drop waits for that table lock
        holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lineage_lock,))
        # a deadlock ends one of the two with ArchivumUnavailable; a timeout fails the test instead of hanging it
        assert edge.result(timeout=60) == 1
        [result] = applying.result(timeout=60)
    assert (result.history, result.rows) == ("artifact", 1)
    assert not partition_exists(archivum, "artifact", ms_ago(OLD_DAYS))
    assert count(archivum, "SELECT count(*) FROM ulduar.lineage_edge") == 1


def test_a_dry_run_does_not_deadlock_with_a_recorder_making_a_partition(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path, lock_timeout_s=30)
    old_artifacts(config, OLD_DAYS)
    declare_history_retention(config, "alpha", "artifact", 30)
    declare_history_retention(config, "alpha", "lineage_edge", 30)
    long_ago = 430  # a month without a partition: recording there makes the recorder retry through ensure_partitions

    with archivum.superuser() as holder, ThreadPoolExecutor(max_workers=2) as pool:
        with holder.transaction():
            holder.execute("LOCK TABLE ulduar.lineage_edge IN ACCESS EXCLUSIVE MODE")
            recording = pool.submit(old_artifacts, config, long_ago)
            wait_until_blocked(archivum)  # ensure_partitions is queued first for lineage_edge
            counting = pool.submit(apply_history_retention, config)
            # the dry run counted the artifacts and now waits for lineage_edge behind the recorder; the recorder
            # then needs artifact, which a count still in its transaction would hold: a deadlock
            wait_until_blocked(archivum, 2)
        # a deadlock ends one of the two with ArchivumUnavailable; a timeout fails the test instead of hanging it
        assert len(recording.result(timeout=60)) == 1
        results = counting.result(timeout=60)
    assert [(r.history, r.rows) for r in results] == [("artifact", 1), ("lineage_edge", 0)]
    assert partition_exists(archivum, "artifact", ms_ago(long_ago))


@pytest.mark.parametrize("later_keep_days", [None, 1000])
def test_applying_honours_a_rule_declared_after_it_read_the_rules(
    archivum: Archivum, tmp_path: Path, later_keep_days: int | None
) -> None:
    config = onboarded(archivum, tmp_path, lock_timeout_s=30)
    old_artifacts(config, OLD_DAYS)
    declare_history_retention(config, "alpha", "artifact", 30)
    project_id = count_project_id(archivum)
    lock = retention_lock_name(project_id, "artifact")
    later_rule = _ids.uuid7()
    with archivum.superuser() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock,))
        applying = pool.submit(apply_history_retention, config, dry_run=False)
        wait_until_blocked(archivum)  # it read keep 30 and waits for the lock before deleting
        holder.execute(
            "INSERT INTO ulduar.history_retention_rule (project_id, rule_id, event_at, history, keep_days)"
            " VALUES (%s, %s, %s, 'artifact', %s)",
            (project_id, later_rule, _ids.uuid7_time(later_rule), later_keep_days),
        )
        holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock,))
        results = applying.result(timeout=30)
    assert count(archivum, "SELECT count(*) FROM ulduar.artifact") == 1  # the 200-day-old row stays under both
    assert count(archivum, "SELECT count(*) FROM ulduar.history_retention_action") == (
        0 if later_keep_days is None else 1
    )
    if later_keep_days is None:
        assert results == []
    else:
        [result] = results
        assert (result.rule_id, result.rows) == (later_rule, 0)
        assert result.cutoff < datetime.now(UTC) - timedelta(days=999)


def count_project_id(archivum: Archivum) -> uuid.UUID:
    with archivum.superuser() as su:
        row = su.execute("SELECT project_id FROM ulduar.project WHERE name = 'alpha'").fetchone()
    assert row is not None
    project_id: uuid.UUID = row[0]
    return project_id


def test_declaring_waits_for_the_retention_lock_of_its_project_and_history(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path, lock_timeout_s=30)
    lock = retention_lock_name(count_project_id(archivum), "artifact")
    with archivum.superuser() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock,))
        declaring = pool.submit(declare_history_retention, config, "alpha", "artifact", 30)
        wait_until_blocked(archivum)
        assert count(archivum, "SELECT count(*) FROM ulduar.history_retention_rule") == 0
        holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock,))
        assert declaring.result(timeout=30).keep_days == 30
    assert count(archivum, "SELECT count(*) FROM ulduar.history_retention_rule") == 1


def test_among_declares_serialised_by_the_retention_lock_the_one_that_commits_last_wins(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = onboarded(archivum, tmp_path, lock_timeout_s=30)
    project_id = count_project_id(archivum)
    lock = retention_lock_name(project_id, "artifact")
    earlier = _ids.uuid7()
    with archivum.superuser() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock,))
        declaring = pool.submit(declare_history_retention, config, "alpha", "artifact", 10)
        wait_until_blocked(archivum)  # the waiter's transaction began here, before the holder's
        with holder.transaction():  # so the holder's now() is later than the waiter's, though it commits first
            holder.execute(
                "INSERT INTO ulduar.history_retention_rule (project_id, rule_id, event_at, history, keep_days)"
                " VALUES (%s, %s, %s, 'artifact', 20)",
                (project_id, earlier, _ids.uuid7_time(earlier)),
            )
        holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock,))
        waiter = declaring.result(timeout=30)
    [result] = apply_history_retention(config)
    assert result.rule_id == waiter.rule_id  # the later insert, not the later transaction start, is the latest rule


# --- artifacts whose object is archived, and the check histories (step 2) ---


def register_archived(config: Config, artifact_ids: list[uuid.UUID]) -> None:
    """Register the artifacts' objects as archived, as the recording role does through the catalog's hook."""

    def work(tx: psycopg.Connection) -> None:
        for artifact_id in artifact_ids:
            tx.execute(
                "INSERT INTO ulduar.archive_registration (project_id, scope, artifact_id, event_at)"
                " SELECT project_id, scope, artifact_id, event_at FROM ulduar.artifact WHERE artifact_id = %s",
                (artifact_id,),
            )

    with open_catalog(config) as catalog:
        catalog.transaction("Registering test objects", work)


def confirm_deletion(config: Config) -> None:
    """Record, as the recording role, an intent and its confirmation for the content of `CHECKSUM` in `default`."""

    def work(tx: psycopg.Connection) -> None:
        intent, outcome = _ids.uuid7(), _ids.uuid7()
        insert = (
            "INSERT INTO ulduar.archive_deletion (project_id, deletion_id, event_at, scope, checksum, size_bytes,"
            " stage, intent_id, cause) SELECT project_id, %s, %s, 'default', %s, 1, %s, %s, 'incomplete_put'"
            " FROM ulduar.project WHERE name = 'alpha'"
        )
        tx.execute(insert, (intent, _ids.uuid7_time(intent), CHECKSUM, "intent", None))
        tx.execute(insert, (outcome, _ids.uuid7_time(outcome), CHECKSUM, "confirmed", intent))

    with open_catalog(config) as catalog:
        catalog.transaction("Recording a test deletion", work)


def test_an_artifact_whose_object_is_archived_does_not_age_out_until_a_deletion_is_confirmed(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = onboarded(archivum, tmp_path)
    [archived, plain, recent] = old_artifacts(config, OLDER_DAYS, OLD_DAYS, 1)
    register_archived(config, [archived, recent])
    rule = declare_history_retention(config, "alpha", "artifact", 30)
    now = datetime.now(UTC)
    [result] = apply_history_retention(config, now=now)
    assert (result.rule_id, result.rows) == (rule.rule_id, 1)  # only the one never archived
    [result] = apply_history_retention(config, dry_run=False, now=now)
    assert result.rows == 1
    with open_catalog(config) as catalog:
        assert {a.artifact_id for a in catalog.find_artifacts()} == {archived, recent}
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_registration") == 2
    confirm_deletion(config)
    [result] = apply_history_retention(config, dry_run=False, now=now)
    assert result.rows == 1  # the old one, its object gone: its registration goes with it
    with open_catalog(config) as catalog:
        assert [a.artifact_id for a in catalog.find_artifacts()] == [recent]
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_registration WHERE artifact_id = %s", archived) == 0
    assert count(archivum, "SELECT count(*) FROM ulduar.archive_registration") == 1
    assert count(archivum, "SELECT sum(rows_removed) FROM ulduar.history_retention_action") == 2


def test_a_registration_after_a_confirmed_deletion_archives_the_content_again(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = onboarded(archivum, tmp_path)
    [before, after] = old_artifacts(config, OLDER_DAYS, OLD_DAYS)
    register_archived(config, [before])
    confirm_deletion(config)
    register_archived(config, [after])  # the same content, stored again after the deletion
    is_archived = (
        "SELECT ulduar.artifact_is_archived(project_id, artifact_id) FROM ulduar.artifact WHERE artifact_id = %s"
    )
    assert count(archivum, f"SELECT ({is_archived})::int", before) == 0
    assert count(archivum, f"SELECT ({is_archived})::int", after) == 1


def old_check_rows(archivum: Archivum, project: str, days: int) -> None:
    """An old archive_check and archive_finding, written by superuser (the library never makes history of that age)."""
    check_id, finding_id = _ids.uuid7(ms_ago(days)), _ids.uuid7(ms_ago(days))
    with archivum.superuser() as su:
        su.execute("SELECT ulduar.ensure_partitions(%s, 0)", (_ids.uuid7_time(check_id),))
        su.execute(
            "INSERT INTO ulduar.archive_check (project_id, check_id, event_at, scope, kind, mode, objects_checked,"
            " findings) SELECT project_id, %s, %s, 'default', 'verification', 'full', 1, 0 FROM ulduar.project"
            " WHERE name = %s",
            (check_id, _ids.uuid7_time(check_id), project),
        )
        su.execute(
            "INSERT INTO ulduar.archive_finding (project_id, finding_id, event_at, check_id, scope, outcome)"
            " SELECT project_id, %s, %s, %s, 'default', 'passed' FROM ulduar.project WHERE name = %s",
            (finding_id, _ids.uuid7_time(finding_id), check_id, project),
        )


@pytest.mark.parametrize("history", ["archive_check", "archive_finding"])
def test_the_check_histories_age_out_by_their_own_age_and_their_partitions_are_dropped(
    archivum: Archivum, tmp_path: Path, history: str
) -> None:
    config = onboarded(archivum, tmp_path)
    old_check_rows(archivum, "alpha", OLD_DAYS)
    old_check_rows(archivum, "alpha", 1)
    declare_history_retention(config, "alpha", history, 30)  # type: ignore[arg-type]
    [dry] = apply_history_retention(config)
    assert (dry.history, dry.rows, dry.dry_run) == (history, 1, True)
    [result] = apply_history_retention(config, dry_run=False)
    assert (result.history, result.rows) == (history, 1)
    assert count(archivum, f"SELECT count(*) FROM ulduar.{history}") == 1
    other = "archive_finding" if history == "archive_check" else "archive_check"
    assert count(archivum, f"SELECT count(*) FROM ulduar.{other}") == 2  # a rule covers its own history only
    assert not partition_exists(archivum, history, ms_ago(OLD_DAYS))
    assert partition_exists(archivum, other, ms_ago(OLD_DAYS))


def test_the_histories_that_age_out_are_the_ones_the_schema_accepts(archivum: Archivum, tmp_path: Path) -> None:
    onboarded(archivum, tmp_path)
    with archivum.superuser() as su:
        row = su.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'ulduar.history_retention_rule'::regclass AND contype = 'c'"
            " AND conname = 'history_retention_rule_history_check'"
        ).fetchone()
    assert row is not None
    assert sorted(re.findall(r"'([a-z_]+)'", row[0])) == sorted(HISTORIES)
