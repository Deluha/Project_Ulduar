"""ulduar.freya.ledger: the schema ledger, what steps 1 and 2 build, and the refusals of an unsupported schema.

The changing-own-schema checklist: "applies every step in order to an empty database" and "running the
ledger again applies no step" are covered below, and step 2 is applied over an Archivum installed with step 1
only, whose rows must survive (no release has shipped a step yet, so step 1 is the previous schema).
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow

from tests.conftest import PARTITION_LOCK_KEY, Archivum, count, wait_until_blocked
from ulduar import _db
from ulduar._ids import uuid7, uuid7_time
from ulduar.config import Config
from ulduar.errors import (
    ConfigurationError,
    IsolationError,
    LedgerError,
    LineageCycleError,
    SchemaVersionError,
    UlduarError,
    UnknownRecordError,
)
from ulduar.freya.ledger import (
    LEDGER_LOCK_KEY,
    SchemaStatus,
    Step,
    _install,
    check_schema,
    install,
    library_release,
    load_steps,
    schema_status,
)

STEP_ONE_FILE = "0001_catalog.sql"
STEP_TWO_FILE = "0002_archive.sql"
APPLIED_LOG = "applied Archivum schema step"
PARTITIONED = ("lineage_edge", "artifact", "archive_check", "archive_finding")
BREAKING_TWO = (
    "-- ulduar-kind: breaking\n-- ulduar-min-library: 0.1.0\nCREATE TABLE ulduar.synthetic_two (x integer);\n"
)
ADDITIVE_TWO = (
    "-- ulduar-kind: additive\n-- ulduar-min-library: 0.1.0\nCREATE TABLE ulduar.synthetic_two (x integer);\n"
)
Connection = psycopg.Connection[TupleRow]


def real_step_one() -> bytes:
    return (files("ulduar.freya") / "steps" / STEP_ONE_FILE).read_bytes()


def synthetic_steps(directory: Path, *extra: tuple[str, str]) -> tuple[Step, ...]:
    """The real step 1 plus *extra* `(file name, text)` steps, written under *directory* and loaded."""
    directory.mkdir()
    (directory / STEP_ONE_FILE).write_bytes(real_step_one())
    for name, text in extra:
        (directory / name).write_text(text, encoding="utf-8")
    return load_steps(directory)


@contextmanager
def ledger_connection(config: Config) -> Iterator[Connection]:
    assert config.archivum.ledger_dsn_env
    with _db.open_connection(config.archivum.ledger_dsn_env, config.archivum) as conn:
        yield conn


def ledger_rows(archivum: Archivum) -> list[tuple[int, str, str, bytes, str, str]]:
    with archivum.superuser() as su:
        rows = su.execute(
            "SELECT step, name, kind, checksum, min_library_version, applied_by_version"
            " FROM ulduar.schema_ledger ORDER BY step"
        ).fetchall()
    return [(r[0], r[1], r[2], bytes(r[3]), r[4], r[5]) for r in rows]


def applied_log_count(caplog: pytest.LogCaptureFixture) -> int:
    return sum(APPLIED_LOG in record.getMessage() for record in caplog.records)


def join_group(archivum: Archivum, role: str) -> None:
    """What onboarding will do for a recording role: make it a member of the `ulduar_recorder` group."""
    with archivum.superuser() as su:
        su.execute(psycopg.sql.SQL("GRANT ulduar_recorder TO {}").format(psycopg.sql.Identifier(role)))


@pytest.fixture
def installed(archivum: Archivum, tmp_path: Path) -> Config:
    """A config for an Archivum that has had the real steps installed."""
    config = archivum.config(tmp_path)
    install(config)
    return config


# --- library_release and load_steps (no schema needed) ---


@pytest.mark.parametrize(
    ("version", "release"),
    [
        ("0.1.0", (0, 1, 0)),
        ("0.1.0.dev0", (0, 1, 0)),
        ("12.34.5rc1", (12, 34, 5)),
        ("0+unknown", (0, 0, 0)),
        ("", (0, 0, 0)),
    ],
)
def test_library_release_reads_the_leading_release(version: str, release: tuple[int, int, int]) -> None:
    assert library_release(version) == release


def test_packaged_steps_load_numbered_from_one() -> None:
    steps = load_steps()
    assert [s.number for s in steps] == list(range(1, len(steps) + 1))
    first = steps[0]
    assert (first.name, first.kind, first.min_library) == ("catalog", "additive", (0, 1, 0))
    assert first.checksum == sha256(real_step_one()).digest()


@pytest.mark.parametrize("name", ["1_catalog.sql", "0001_Catalog.sql", "0001-catalog.sql", "catalog.sql", "0001_.sql"])
def test_load_steps_refuses_a_badly_named_sql_file(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text(ADDITIVE_TWO, encoding="utf-8")
    with pytest.raises(LedgerError, match=name):
        load_steps(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "CREATE TABLE ulduar.x (a integer);\n",
        "-- ulduar-kind: additive\nCREATE TABLE ulduar.x (a integer);\n",
        "-- ulduar-min-library: 0.1.0\nCREATE TABLE ulduar.x (a integer);\n",
        "-- ulduar-kind: risky\n-- ulduar-min-library: 0.1.0\nSELECT 1;\n",
        "-- ulduar-kind: additive\n-- ulduar-min-library: 0.1\nSELECT 1;\n",
        "-- ulduar-kind: additive\n-- ulduar-kind: breaking\n-- ulduar-min-library: 0.1.0\nSELECT 1;\n",
        "SELECT 1;\n-- ulduar-kind: additive\n-- ulduar-min-library: 0.1.0\n",
    ],
)
def test_load_steps_refuses_missing_or_bad_headers(tmp_path: Path, text: str) -> None:
    (tmp_path / "0001_x.sql").write_text(text, encoding="utf-8")
    with pytest.raises(LedgerError, match="0001_x.sql"):
        load_steps(tmp_path)


def test_load_steps_refuses_gaps_and_repeats_in_the_numbering(tmp_path: Path) -> None:
    for name in ("0001_a.sql", "0003_c.sql"):
        (tmp_path / name).write_text(ADDITIVE_TWO, encoding="utf-8")
    with pytest.raises(LedgerError, match="without a gap"):
        load_steps(tmp_path)
    (tmp_path / "0003_c.sql").rename(tmp_path / "0001_b.sql")
    with pytest.raises(LedgerError, match="without a gap"):
        load_steps(tmp_path)


def test_load_steps_ignores_files_that_are_not_sql(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text(ADDITIVE_TWO, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a step", encoding="utf-8")
    assert [s.name for s in load_steps(tmp_path)] == ["a"]


# --- install ---


def test_install_applies_every_step_in_order_to_an_empty_database_and_records_them(
    archivum: Archivum, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ulduar.freya.ledger")
    status = install(archivum.config(tmp_path))
    assert status == SchemaStatus(
        applied_step=2, known_step=2, pending=(), min_library_version="0.1.0", compatible=True
    )
    one, two = ledger_rows(archivum)
    assert one[:3] == (1, "catalog", "additive") and one[4] == "0.1.0"
    assert one[3] == sha256(real_step_one()).digest()
    assert two[:3] == (2, "archive", "additive") and two[4] == "0.1.0"
    assert two[3] == sha256((files("ulduar.freya") / "steps" / STEP_TWO_FILE).read_bytes()).digest()
    assert one[5] and two[5]
    assert applied_log_count(caplog) == 2


def test_a_second_install_applies_nothing(archivum: Archivum, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = archivum.config(tmp_path)
    install(config)
    before = ledger_rows(archivum)
    caplog.set_level(logging.INFO, logger="ulduar.freya.ledger")
    status = install(config)
    assert status.pending == ()
    assert ledger_rows(archivum) == before
    assert applied_log_count(caplog) == 0


def test_install_without_a_ledger_variable_is_a_configuration_error(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    config = dataclasses.replace(config, archivum=dataclasses.replace(config.archivum, ledger_dsn_env=None))
    with pytest.raises(ConfigurationError, match="archivum.ledger_dsn_env is not set"):
        install(config)


def test_install_creates_this_month_and_three_ahead_for_every_partitioned_history(
    archivum: Archivum, tmp_path: Path
) -> None:
    install(archivum.config(tmp_path))
    now = datetime.now(UTC)
    months = [(now.year + (now.month - 1 + i) // 12, (now.month - 1 + i) % 12 + 1) for i in range(4)]
    expected = {f"{table}_{year}_{month:02d}" for table in PARTITIONED for year, month in months}
    with archivum.superuser() as su:
        found = {
            r[0]
            for r in su.execute(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'ulduar' AND c.relkind = 'r'"
            )
        }
        assert su.execute("SELECT ulduar.ensure_partitions(now())").fetchone() == (0,)
    assert found == expected


def test_a_pending_breaking_step_is_refused_without_the_flag_and_applied_with_it(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = archivum.config(tmp_path)
    steps = synthetic_steps(tmp_path / "steps", ("0002_extra.sql", BREAKING_TWO))
    _install(config, steps[:1], allow_breaking=False)
    with pytest.raises(LedgerError, match="breaking"):
        _install(config, steps, allow_breaking=False)
    assert [r[0] for r in ledger_rows(archivum)] == [1]
    with archivum.superuser() as su:
        assert su.execute("SELECT to_regclass('ulduar.synthetic_two')").fetchone() == (None,)
    status = _install(config, steps, allow_breaking=True)
    assert (status.applied_step, status.pending) == (2, ())
    assert [r[0] for r in ledger_rows(archivum)] == [1, 2]


def test_a_refused_breaking_step_leaves_a_fresh_database_untouched(archivum: Archivum, tmp_path: Path) -> None:
    steps = synthetic_steps(tmp_path / "steps", ("0002_extra.sql", BREAKING_TWO))
    with pytest.raises(LedgerError, match="nothing was applied"):
        _install(archivum.config(tmp_path), steps, allow_breaking=False)
    with archivum.superuser() as su:
        assert su.execute("SELECT to_regnamespace('ulduar')").fetchone() == (None,)


def test_an_additive_step_is_applied_without_the_flag(archivum: Archivum, tmp_path: Path) -> None:
    steps = synthetic_steps(tmp_path / "steps", ("0002_extra.sql", ADDITIVE_TWO))
    assert _install(archivum.config(tmp_path), steps, allow_breaking=False).applied_step == 2


def test_a_failing_step_rolls_the_whole_install_back(archivum: Archivum, tmp_path: Path) -> None:
    bad = "-- ulduar-kind: additive\n-- ulduar-min-library: 0.1.0\nCREATE TABLE ulduar.half (x integer);\nSELECT 1/0;\n"
    steps = synthetic_steps(tmp_path / "steps", ("0002_bad.sql", bad))
    with pytest.raises(Exception, match="division by zero") as caught:
        _install(archivum.config(tmp_path), steps, allow_breaking=False)
    assert type(caught.value).__module__ == "ulduar.errors"
    with archivum.superuser() as su:
        assert su.execute("SELECT to_regnamespace('ulduar')").fetchone() == (None,)


def test_a_changed_applied_step_is_refused(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    install(config)
    with archivum.superuser() as su:
        su.execute("UPDATE ulduar.schema_ledger SET checksum = decode(repeat('00', 32), 'hex') WHERE step = 1")
    with pytest.raises(LedgerError, match="step 1"):
        install(config)
    with ledger_connection(config) as conn, pytest.raises(LedgerError, match="step 1"):
        check_schema(conn, load_steps())
    join_group(archivum, archivum.recorders[0].role)
    with pytest.raises(LedgerError, match="step 1"):
        schema_status(config)


def test_two_installs_at_once_apply_the_step_once_and_both_succeed(
    archivum: Archivum, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ulduar.freya.ledger")
    config = archivum.config(tmp_path, lock_timeout_s=60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = [f.result() for f in [pool.submit(install, config), pool.submit(install, config)]]
    assert all(s.applied_step == 2 and s.pending == () for s in statuses)
    assert [r[0] for r in ledger_rows(archivum)] == [1, 2]
    assert applied_log_count(caplog) == 2


def test_a_ledger_lock_held_beyond_the_lock_timeout_is_a_ledger_error(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path, lock_timeout_s=1)
    with archivum.superuser() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (LEDGER_LOCK_KEY,))
        with pytest.raises(LedgerError, match="ledger lock"):
            install(config)
    assert install(config).applied_step == 2


# --- check_schema and schema_status ---


def test_check_schema_passes_after_install(installed: Config) -> None:
    with ledger_connection(installed) as conn:
        assert check_schema(conn, load_steps()).compatible is True


def test_check_schema_refuses_a_database_that_is_not_installed(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    with ledger_connection(config) as conn, pytest.raises(SchemaVersionError, match="not installed"):
        check_schema(conn, load_steps())
    with pytest.raises(SchemaVersionError, match="not installed"):
        schema_status(config)


def test_a_recorded_minimum_library_above_this_library_is_refused(archivum: Archivum, installed: Config) -> None:
    with archivum.superuser() as su:
        su.execute("UPDATE ulduar.schema_ledger SET min_library_version = '99.0.0' WHERE step = 1")
    with ledger_connection(installed) as conn, pytest.raises(SchemaVersionError, match="99.0.0"):
        check_schema(conn, load_steps())
    with pytest.raises(SchemaVersionError, match="99.0.0"):
        install(installed)


def test_an_unknown_applied_breaking_step_is_refused_and_an_additive_one_accepted(
    archivum: Archivum, installed: Config
) -> None:
    insert = (
        "INSERT INTO ulduar.schema_ledger (step, name, kind, checksum, min_library_version, applied_by_version)"
        " VALUES (3, 'from_the_future', %s, decode(repeat('01', 32), 'hex'), '0.1.0', '9.9.9')"
    )
    with archivum.superuser() as su:
        su.execute(insert, ("additive",))
    with ledger_connection(installed) as conn:
        status = check_schema(conn, load_steps())
    assert (status.applied_step, status.known_step, status.compatible) == (3, 2, True)
    with archivum.superuser() as su:
        su.execute("UPDATE ulduar.schema_ledger SET kind = 'breaking' WHERE step = 3")
    with ledger_connection(installed) as conn, pytest.raises(SchemaVersionError, match="from_the_future"):
        check_schema(conn, load_steps())
    with pytest.raises(SchemaVersionError, match="from_the_future"):
        install(installed)


def test_known_steps_the_database_lacks_are_refused_at_check_and_listed_by_status(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = archivum.config(tmp_path)
    steps = synthetic_steps(tmp_path / "steps", ("0002_extra.sql", ADDITIVE_TWO))
    _install(config, steps[:1], allow_breaking=False)
    with (
        ledger_connection(config) as conn,
        pytest.raises(SchemaVersionError, match="at step 1.*needs step 2.*freya.install"),
    ):
        check_schema(conn, steps)


def test_schema_status_reads_as_the_recording_role_and_reports_incompatible_instead_of_raising(
    archivum: Archivum, installed: Config
) -> None:
    recorder_role = next(r.role for r in archivum.recorders)
    join_group(archivum, recorder_role)
    assert schema_status(installed).compatible is True
    with archivum.superuser() as su:
        su.execute("UPDATE ulduar.schema_ledger SET min_library_version = '99.0.0' WHERE step = 1")
    status = schema_status(installed)
    assert (status.compatible, status.min_library_version, status.pending) == (False, "99.0.0", ())


# --- what step 1 builds ---


def seed_project(conn: Connection, name: str = "alpha") -> uuid.UUID:
    project_id = uuid7()
    conn.execute("INSERT INTO ulduar.project (project_id, name) VALUES (%s, %s)", (project_id, name))
    conn.execute("INSERT INTO ulduar.scope (project_id, scope) VALUES (%s, 'default')", (project_id,))
    return project_id


def insert_artifact(conn: Connection, project_id: uuid.UUID, unix_ms: int | None = None) -> uuid.UUID:
    artifact_id = uuid7(unix_ms)
    conn.execute(
        "INSERT INTO ulduar.artifact (project_id, scope, artifact_id, event_at, kind, checksum_algorithm, checksum,"
        " size_bytes) VALUES (%s, 'default', %s, %s, 'raw_file', 'sha256', %s, 0)",
        (project_id, artifact_id, uuid7_time(artifact_id), b"\x00" * 32),
    )
    return artifact_id


def insert_edge(conn: Connection, project_id: uuid.UUID, child: uuid.UUID, parent: uuid.UUID) -> None:
    edge_id = uuid7()
    conn.execute(
        "INSERT INTO ulduar.lineage_edge (project_id, edge_id, event_at, child_kind, child_id, parent_kind, parent_id)"
        " VALUES (%s, %s, %s, 'artifact', %s, 'artifact', %s)",
        (project_id, edge_id, uuid7_time(edge_id), child, parent),
    )


def refusal(config: Config, statement: str, params: tuple[object, ...] = ()) -> Exception:
    """The package exception for *statement* run as the ledger role in a transaction that must fail."""
    with ledger_connection(config) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as inside:
            inside.execute(statement, params)
    return _db.translate(caught.value, "Probing the Archivum")


def test_a_lineage_cycle_and_a_foreign_node_are_refused_with_the_library_error_codes(installed: Config) -> None:
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum):
        mine, other = seed_project(conn, "alpha"), seed_project(conn, "beta")
        a, b, foreign = insert_artifact(conn, mine), insert_artifact(conn, mine), insert_artifact(conn, other)
        insert_edge(conn, mine, a, b)
    with ledger_connection(installed) as conn:
        with pytest.raises(psycopg.Error) as cycle, _db.transaction(conn, installed.archivum) as inside:
            insert_edge(inside, mine, b, a)
        with pytest.raises(psycopg.Error) as unknown, _db.transaction(conn, installed.archivum) as inside:
            insert_edge(inside, mine, a, foreign)
    assert cycle.value.sqlstate == "UL001"
    assert unknown.value.sqlstate == "UL002"
    assert isinstance(_db.translate(cycle.value, "Recording"), LineageCycleError)
    assert isinstance(_db.translate(unknown.value, "Recording"), UnknownRecordError)


def test_recorded_history_refuses_update_delete_and_truncate_outside_maintenance(installed: Config) -> None:
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum):
        insert_artifact(conn, seed_project(conn))
    for statement in (
        "UPDATE ulduar.artifact SET origin = 'x'",
        "DELETE FROM ulduar.artifact",
        "TRUNCATE ulduar.artifact",
        "TRUNCATE ulduar.lineage_edge",
        "TRUNCATE ulduar.redaction",
    ):
        assert isinstance(refusal(installed, statement), IsolationError), statement
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        inside.execute("SELECT set_config('ulduar.maintenance', 'redaction', true)")
        assert inside.execute("UPDATE ulduar.artifact SET origin = 'redacted'").rowcount == 1


def test_a_recording_role_sees_and_inserts_only_its_own_project(archivum: Archivum, installed: Config) -> None:
    recorder = archivum.recorders[0]
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum):
        mine, other = seed_project(conn, "alpha"), seed_project(conn, "beta")
        insert_artifact(conn, other)
    join_group(archivum, recorder.role)
    with _db.open_connection(recorder.env, installed.archivum) as conn:
        count = "SELECT count(*) FROM ulduar.artifact"
        with _db.transaction(conn, installed.archivum) as inside:
            assert inside.execute(count).fetchone() == (0,)  # a member of the group, but of no project yet
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        inside.execute(
            "INSERT INTO ulduar.project_member (role_name, project_id) VALUES (%s, %s)", (recorder.role, mine)
        )
    with _db.open_connection(recorder.env, installed.archivum) as conn:
        with _db.transaction(conn, installed.archivum) as inside:
            insert_artifact(inside, mine)
            assert inside.execute(count).fetchone() == (1,)
        for statement in (
            "UPDATE ulduar.artifact SET origin = 'x'",
            "DELETE FROM ulduar.artifact",
            "TRUNCATE ulduar.artifact",
        ):
            with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, installed.archivum) as inside:
                inside.execute(statement)
            assert isinstance(_db.translate(caught.value, "Recording"), IsolationError), statement
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, installed.archivum) as inside:
            insert_artifact(inside, other)
        assert isinstance(_db.translate(caught.value, "Recording"), IsolationError)


def test_drop_empty_partitions_drops_only_empty_ended_months(installed: Config) -> None:
    january = int(datetime(2020, 1, 15, tzinfo=UTC).timestamp() * 1000)
    names = "SELECT relname FROM pg_class WHERE relname LIKE %s ORDER BY relname"
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        assert inside.execute(
            "SELECT ulduar.ensure_partitions(%s, 0)", (datetime(2020, 1, 15, tzinfo=UTC),)
        ).fetchone() == (4,)
        insert_artifact(inside, seed_project(inside), january)
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        before_month_end = datetime(2020, 1, 31, tzinfo=UTC)
        assert inside.execute("SELECT ulduar.drop_empty_partitions(%s)", (before_month_end,)).fetchone() == (0,)
        after_month_end = datetime(2020, 2, 1, tzinfo=UTC)
        assert inside.execute("SELECT ulduar.drop_empty_partitions(%s)", (after_month_end,)).fetchone() == (3,)
        assert inside.execute(names, ("%_2020_01",)).fetchall() == [("artifact_2020_01",)]
        assert inside.execute("SELECT ulduar.drop_empty_partitions(now())").fetchone() == (
            0,
        )  # this month has not ended


def test_only_the_ledger_role_may_run_partition_maintenance(archivum: Archivum, installed: Config) -> None:
    recorder = archivum.recorders[0]
    join_group(archivum, recorder.role)
    with _db.open_connection(recorder.env, installed.archivum) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, installed.archivum) as inside:
            inside.execute("SELECT ulduar.drop_empty_partitions(now())")
        assert isinstance(_db.translate(caught.value, "Maintaining partitions"), IsolationError)
        with _db.transaction(conn, installed.archivum) as inside:
            assert inside.execute("SELECT ulduar.ensure_partitions(now())").fetchone() == (0,)


# --- partitions: concurrency, the time window, session settings, truncate ---


def month_start(months_ahead: int) -> datetime:
    """The first instant (UTC) of the month *months_ahead* months after the current one."""
    now = datetime.now(UTC)
    index = now.year * 12 + now.month - 1 + months_ahead
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


def partition_names(archivum: Archivum, moment: datetime) -> list[str]:
    with archivum.superuser() as su:
        rows = su.execute(
            "SELECT relname FROM pg_class WHERE relname LIKE %s ORDER BY relname", (f"%\\_{moment:%Y\\_%m}",)
        ).fetchall()
    return [r[0] for r in rows]


def ensure(config: Config, moment: datetime) -> int:
    """`ulduar.ensure_partitions(moment, 0)` as the ledger role, in a transaction of its own."""
    with ledger_connection(config) as conn, _db.transaction(conn, config.archivum) as inside:
        row = inside.execute("SELECT ulduar.ensure_partitions(%s, 0)", (moment,)).fetchone()
    assert row is not None
    return int(row[0])


def test_two_callers_for_the_same_new_month_both_succeed_and_one_partition_is_made(
    archivum: Archivum, installed: Config
) -> None:
    month = month_start(12) + timedelta(days=9)
    with archivum.superuser() as holder, ThreadPoolExecutor(max_workers=2) as pool:
        holder.execute("SELECT pg_advisory_lock(%s)", (PARTITION_LOCK_KEY,))
        calls = [pool.submit(ensure, installed, month) for _ in range(2)]
        wait_until_blocked(archivum, 2)  # both are in the function, queued behind the lock
        holder.execute("SELECT pg_advisory_unlock(%s)", (PARTITION_LOCK_KEY,))
        made = sorted(call.result(timeout=30) for call in calls)
    assert made == [0, 4]  # the four partitions of the month, made by one caller only
    assert len(partition_names(archivum, month)) == 4


def test_a_partition_another_session_made_meanwhile_is_not_an_error(archivum: Archivum, installed: Config) -> None:
    month = month_start(14)
    name = f"artifact_{month:%Y_%m}"
    with archivum.superuser() as other, ThreadPoolExecutor(max_workers=1) as pool:
        other.execute("BEGIN")
        other.execute(
            psycopg.sql.SQL("CREATE TABLE ulduar.{} PARTITION OF ulduar.artifact FOR VALUES FROM ({}) TO ({})").format(
                psycopg.sql.Identifier(name), psycopg.sql.Literal(month), psycopg.sql.Literal(month_start(15))
            )
        )
        call = pool.submit(ensure, installed, month)
        wait_until_blocked(archivum, 1)  # the function's own CREATE waits for the table lock, then finds the name taken
        other.execute("COMMIT")
        assert call.result(timeout=30) == 3  # all but the artifact partition are its own
    assert len(partition_names(archivum, month)) == 4


@pytest.mark.parametrize("offset", [timedelta(days=-365 * 16), timedelta(days=365 * 3)])
def test_a_time_outside_the_partition_window_is_refused_clearly(installed: Config, offset: timedelta) -> None:
    error = refusal(installed, "SELECT ulduar.ensure_partitions(%s, 0)", (datetime.now(UTC) + offset,))
    assert type(error) is UlduarError
    assert "outside the window from 15 years ago to 2 years ahead" in str(error)
    assert ensure(installed, month_start(23)) == 4  # inside it, 23 months ahead


def test_partition_bounds_are_utc_month_starts_whatever_the_session_settings(
    archivum: Archivum, installed: Config
) -> None:
    month = month_start(6)
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        inside.execute("SET LOCAL TimeZone = 'Pacific/Kiritimati'")
        inside.execute("SET LOCAL DateStyle = 'German'")
        assert inside.execute("SELECT ulduar.ensure_partitions(%s, 0)", (month + timedelta(days=14),)).fetchone() == (
            4,
        )
    with archivum.superuser() as su:
        su.execute("SET TimeZone = 'UTC'")
        bounds = su.execute(
            "SELECT pg_get_expr(relpartbound, oid) FROM pg_class WHERE relname = %s", (f"artifact_{month:%Y_%m}",)
        ).fetchone()
    assert bounds == (f"FOR VALUES FROM ('{month:%Y-%m-%d} 00:00:00+00') TO ('{month_start(7):%Y-%m-%d} 00:00:00+00')",)


@pytest.mark.parametrize("table", PARTITIONED)
def test_a_partition_cannot_be_truncated_even_by_the_ledger_role(installed: Config, table: str) -> None:
    error = refusal(installed, f"TRUNCATE ulduar.{table}_{datetime.now(UTC):%Y_%m}")
    assert isinstance(error, IsolationError)
    assert "append-only" in str(error)


def test_every_partition_ensure_partitions_makes_carries_the_truncate_trigger(
    archivum: Archivum, installed: Config
) -> None:
    ensure(installed, month_start(20))
    partitions = (
        "SELECT oid FROM pg_class WHERE relispartition AND relkind = 'r' AND relnamespace = 'ulduar'::regnamespace"
    )
    total = count(archivum, f"SELECT count(*) FROM ({partitions}) p")
    guarded = count(
        archivum,
        f"SELECT count(*) FROM pg_trigger WHERE tgname = 'refuse_truncate' AND tgrelid IN ({partitions})",
    )
    assert total == guarded > 8


def test_install_takes_no_steps_argument() -> None:
    assert list(inspect.signature(install).parameters) == ["config", "allow_breaking"]


# --- what step 2 builds ---

ARCHIVE_TABLES = (
    "archive_registration",
    "archive_retention_rule",
    "archive_deletion",
    "archive_check",
    "archive_finding",
)


def insert_registration(
    conn: Connection,
    project_id: uuid.UUID,
    artifact_id: uuid.UUID,
    *,
    scope: str = "default",
    at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO ulduar.archive_registration (project_id, scope, artifact_id, event_at) VALUES (%s, %s, %s, %s)",
        (project_id, scope, artifact_id, at or uuid7_time(artifact_id)),
    )


def insert_history_rule(conn: Connection, project_id: uuid.UUID, history: str) -> None:
    rule_id = uuid7()
    conn.execute(
        "INSERT INTO ulduar.history_retention_rule (project_id, rule_id, event_at, history, keep_days)"
        " VALUES (%s, %s, %s, %s, 30)",
        (project_id, rule_id, uuid7_time(rule_id), history),
    )


def test_step_two_applies_over_step_one_and_the_rows_of_step_one_stay(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    assert _install(config, load_steps()[:1], allow_breaking=False).applied_step == 1
    with ledger_connection(config) as conn, _db.transaction(conn, config.archivum):
        project = seed_project(conn)
        artifact = insert_artifact(conn, project)
    with archivum.superuser() as su:
        assert su.execute("SELECT to_regclass('ulduar.archive_registration')").fetchone() == (None,)
    status = install(config)
    assert (status.applied_step, status.pending) == (2, ())
    with ledger_connection(config) as conn, _db.transaction(conn, config.archivum) as inside:
        assert inside.execute("SELECT artifact_id FROM ulduar.artifact").fetchall() == [(artifact,)]
        insert_registration(inside, project, artifact)
        insert_history_rule(inside, project, "archive_check")
    assert [r[0] for r in ledger_rows(archivum)] == [1, 2]


def test_installing_again_changes_nothing_and_every_archive_table_has_forced_row_level_security(
    archivum: Archivum, installed: Config
) -> None:
    before = ledger_rows(archivum)
    assert install(installed).pending == ()
    assert ledger_rows(archivum) == before
    for table in ARCHIVE_TABLES:
        flags = count(
            archivum,
            "SELECT count(*) FROM pg_class WHERE oid = to_regclass(%s) AND relrowsecurity AND relforcerowsecurity",
            f"ulduar.{table}",
        )
        assert flags == 1, table


def test_the_history_list_of_retention_rules_is_widened_and_nothing_else_is_accepted(installed: Config) -> None:
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        project = seed_project(inside)
        for history in ("artifact", "lineage_edge", "archive_check", "archive_finding"):
            insert_history_rule(inside, project, history)
    with ledger_connection(installed) as conn:
        with pytest.raises(psycopg.Error, match="history_retention_rule_history_check") as caught:
            with _db.transaction(conn, installed.archivum) as inside:
                insert_history_rule(inside, project, "archive_registration")
        assert caught.value.sqlstate == "23514"


def test_ensure_partitions_makes_the_check_histories_and_keeps_its_grants(
    archivum: Archivum, installed: Config
) -> None:
    month = month_start(18)
    assert ensure(installed, month) == 4
    for table in ("archive_check", "archive_finding"):
        name = f"{table}_{month:%Y_%m}"
        assert partition_names(archivum, month).count(name) == 1
        error = refusal(installed, f"TRUNCATE ulduar.{name}")
        assert isinstance(error, IsolationError) and "append-only" in str(error)
    privileges = (
        "SELECT has_function_privilege('ulduar_recorder', 'ulduar.ensure_partitions(timestamptz, integer)', 'EXECUTE'),"
        " has_function_privilege('ulduar_recorder', 'ulduar.drop_empty_partitions(timestamptz)', 'EXECUTE'),"
        " has_function_privilege('public', 'ulduar.ensure_partitions(timestamptz, integer)', 'EXECUTE'),"
        " has_function_privilege('ulduar_recorder', 'ulduar.artifact_is_archived(uuid, uuid)', 'EXECUTE'),"
        " has_function_privilege('public', 'ulduar.artifact_is_archived(uuid, uuid)', 'EXECUTE')"
    )
    with archivum.superuser() as su:
        assert su.execute(privileges).fetchone() == (True, False, False, True, False)


def test_drop_empty_partitions_covers_the_check_histories(installed: Config, archivum: Archivum) -> None:
    january = datetime(2020, 1, 15, tzinfo=UTC)
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        assert inside.execute("SELECT ulduar.ensure_partitions(%s, 0)", (january,)).fetchone() == (4,)
        check_id = uuid7(int(january.timestamp() * 1000))
        inside.execute(
            "INSERT INTO ulduar.archive_check (project_id, check_id, event_at, scope, kind, mode, objects_checked,"
            " findings) VALUES (%s, %s, %s, 'default', 'verification', 'full', 1, 0)",
            (seed_project(inside), check_id, uuid7_time(check_id)),
        )
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        assert inside.execute(
            "SELECT ulduar.drop_empty_partitions(%s)", (datetime(2020, 2, 1, tzinfo=UTC),)
        ).fetchone() == (3,)
    assert partition_names(archivum, january) == ["archive_check_2020_01"]


def test_a_registration_must_name_an_artifact_of_its_project_scope_and_time(installed: Config) -> None:
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum):
        mine, other = seed_project(conn, "alpha"), seed_project(conn, "beta")
        artifact, foreign = insert_artifact(conn, mine), insert_artifact(conn, other)
        conn.execute("INSERT INTO ulduar.scope (project_id, scope) VALUES (%s, 'second')", (mine,))
    insert = (
        "INSERT INTO ulduar.archive_registration (project_id, scope, artifact_id, event_at) VALUES (%s, %s, %s, %s)"
    )
    for scope, artifact_id, at in [
        ("default", uuid7(), datetime.now(UTC)),  # no such artifact
        ("default", foreign, uuid7_time(foreign)),  # another project's
        ("second", artifact, uuid7_time(artifact)),  # another scope
        ("default", artifact, uuid7_time(artifact) + timedelta(milliseconds=1)),  # another time
    ]:
        error = refusal(installed, insert, (mine, scope, artifact_id, at))
        assert isinstance(error, UnknownRecordError), (scope, artifact_id)
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        insert_registration(inside, mine, artifact)


def seed_archive_row(conn: Connection, table: str) -> None:
    """One row of the archive table *table* in a project of its own: the row triggers fire only on a row."""
    project = seed_project(conn, f"p{uuid.uuid4().hex[:8]}")
    record = uuid7()
    at = uuid7_time(record)
    if table == "archive_registration":
        insert_registration(conn, project, insert_artifact(conn, project))
        return
    inserts = {
        "archive_retention_rule": (
            "INSERT INTO ulduar.archive_retention_rule (project_id, rule_id, event_at, retention_class, keep_newest,"
            " shortening_confirmed) VALUES (%s, %s, %s, 'c', 1, false)"
        ),
        "archive_deletion": (
            "INSERT INTO ulduar.archive_deletion (project_id, deletion_id, event_at, scope, checksum, size_bytes,"
            f" stage, cause) VALUES (%s, %s, %s, 'default', '\\x{'00' * 32}', 0, 'intent', 'incomplete_put')"
        ),
        "archive_check": (
            "INSERT INTO ulduar.archive_check (project_id, check_id, event_at, scope, kind, mode, objects_checked,"
            " findings) VALUES (%s, %s, %s, 'default', 'verification', 'full', 1, 0)"
        ),
        "archive_finding": (
            "INSERT INTO ulduar.archive_finding (project_id, finding_id, event_at, check_id, scope, outcome)"
            " VALUES (%s, %s, %s, gen_random_uuid(), 'default', 'passed')"
        ),
    }
    conn.execute(inserts[table], (project, record, at))


def refusal_with_row(config: Config, table: str, statement: str, maintenance: str | None = None) -> Exception:
    """The package exception for *statement* run, with one row of *table* present, as the ledger role."""
    with ledger_connection(config) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as inside:
            seed_archive_row(inside, table)
            if maintenance:
                inside.execute("SELECT set_config('ulduar.maintenance', %s, true)", (maintenance,))
            inside.execute(statement)
    return _db.translate(caught.value, "Probing the Archivum")


@pytest.mark.parametrize("table", ARCHIVE_TABLES)
def test_the_archive_tables_refuse_update_delete_and_truncate_outside_maintenance(
    installed: Config, table: str
) -> None:
    for statement in (
        f"DELETE FROM ulduar.{table}",
        f"UPDATE ulduar.{table} SET recorded_at = now()",
        f"TRUNCATE ulduar.{table}",
    ):
        error = refusal_with_row(installed, table, statement)
        assert isinstance(error, IsolationError) and "append-only" in str(error), statement


@pytest.mark.parametrize("table", ["archive_registration", "archive_check", "archive_finding"])
def test_the_registration_and_the_check_histories_can_age_out_in_retention_mode_only(
    installed: Config, table: str
) -> None:
    with ledger_connection(installed) as conn, _db.transaction(conn, installed.archivum) as inside:
        seed_archive_row(inside, table)
        inside.execute("SELECT set_config('ulduar.maintenance', 'retention', true)")
        assert inside.execute(f"DELETE FROM ulduar.{table}").rowcount == 1
    error = refusal_with_row(installed, table, f"UPDATE ulduar.{table} SET recorded_at = now()", "retention")
    assert "append-only" in str(error)


@pytest.mark.parametrize("table", ["archive_retention_rule", "archive_deletion"])
@pytest.mark.parametrize("mode", ["retention", "redaction"])
def test_a_rule_and_a_deletion_record_can_never_be_removed_even_in_maintenance_mode(
    installed: Config, table: str, mode: str
) -> None:
    error = refusal_with_row(installed, table, f"DELETE FROM ulduar.{table}", mode)
    assert isinstance(error, IsolationError) and "append-only" in str(error)
