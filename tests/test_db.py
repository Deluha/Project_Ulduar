"""ulduar._db: the transport guard, the DSN variable, the opened connection and its time limits, error translation."""

from __future__ import annotations

import time
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tests.conftest import Archivum
from ulduar import _db
from ulduar.config import ArchivumSettings
from ulduar.errors import (
    ArchivumUnavailable,
    ConfigurationError,
    IsolationError,
    LineageCycleError,
    SchemaVersionError,
    UlduarError,
    UnknownRecordError,
)

# Pure cases: no connection is opened, so these name remote hosts without reaching any.
ALLOWED_DSNS = [
    "host=localhost dbname=a",
    "host=127.0.0.1 dbname=a sslmode=disable",
    "host=::1 dbname=a",
    "host=/var/run/postgresql dbname=a",
    "dbname=a",
    "host=db.example.test dbname=a sslmode=verify-full",
    "host=db.example.test dbname=a sslmode=verify-ca",
    "host=db1.example.test,db2.example.test dbname=a sslmode=verify-full",
    "host=localhost,/var/run/postgresql dbname=a",
]
REFUSED_DSNS = [
    ("host=db.example.test dbname=a", "db.example.test"),
    ("host=db.example.test dbname=a sslmode=require", "db.example.test"),
    ("host=db.example.test dbname=a sslmode=prefer", "db.example.test"),
    ("host=db.example.test dbname=a sslmode=verify-ca,verify-full", "db.example.test"),
    ("host=localhost,db.example.test dbname=a sslmode=disable", "db.example.test"),
    ("host=localhost hostaddr=192.0.2.7 dbname=a", "192.0.2.7"),
    ("host=db.example.test dbname=a sslmode=allow", "db.example.test"),
]


@pytest.mark.parametrize("dsn", ALLOWED_DSNS)
def test_check_transport_accepts_local_and_verified_hosts(dsn: str) -> None:
    _db.check_transport(dsn)


@pytest.mark.parametrize(("dsn", "host"), REFUSED_DSNS)
def test_check_transport_refuses_a_remote_host_without_verified_tls(dsn: str, host: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _db.check_transport(dsn)
    assert host in str(caught.value)
    assert "verify-full or verify-ca" in str(caught.value)


def test_check_transport_refuses_a_malformed_dsn_without_echoing_it() -> None:
    dsn = "host=db.example.test password=hunter2-secret =oops"
    with pytest.raises(ConfigurationError) as caught:
        _db.check_transport(dsn, "ARCHIVUM_DSN")
    assert "hunter2-secret" not in str(caught.value)
    assert "ARCHIVUM_DSN" in str(caught.value)


def test_remote_refusal_never_names_the_password() -> None:
    with pytest.raises(ConfigurationError) as caught:
        _db.check_transport("host=db.example.test password=hunter2-secret sslmode=require")
    assert "hunter2-secret" not in str(caught.value)


def test_read_dsn_returns_the_value_and_names_a_missing_variable() -> None:
    assert _db.read_dsn("X", {"X": "host=localhost"}) == "host=localhost"
    for environ in ({}, {"X": ""}):
        with pytest.raises(ConfigurationError, match="X"):
            _db.read_dsn("X", environ)


def test_open_connection_refuses_a_missing_variable_and_a_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = ArchivumSettings(dsn_env="ULDUAR_TEST_ABSENT_DSN")
    monkeypatch.delenv("ULDUAR_TEST_ABSENT_DSN", raising=False)
    with pytest.raises(ConfigurationError, match="ULDUAR_TEST_ABSENT_DSN"):
        _db.open_connection("ULDUAR_TEST_ABSENT_DSN", settings)
    monkeypatch.setenv("ULDUAR_TEST_ABSENT_DSN", "host=db.example.test password=hunter2-secret sslmode=require")
    with pytest.raises(ConfigurationError, match="db.example.test") as caught:
        _db.open_connection("ULDUAR_TEST_ABSENT_DSN", settings)
    assert "hunter2-secret" not in str(caught.value)


def test_open_connection_reaches_postgresql_18(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    with _db.open_connection(config.archivum.dsn_env, config.archivum) as conn:
        assert conn.info.server_version // 10000 == 18
        assert conn.info.parameter_status("application_name") == "ulduar"
        assert conn.info.dbname == archivum.database
        assert conn.autocommit is False


def test_unreachable_archivum_names_the_host_not_the_dsn(
    archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = archivum.config(tmp_path)
    dsn = make_conninfo(
        make_conninfo("host=127.0.0.1 password=hunter2-secret"),
        port=1,
        connect_timeout=1,
    )
    monkeypatch.setenv(config.archivum.dsn_env, dsn)
    with pytest.raises(ArchivumUnavailable) as caught:
        _db.open_connection(config.archivum.dsn_env, config.archivum)
    assert "127.0.0.1" in str(caught.value)
    assert "hunter2-secret" not in str(caught.value)
    assert isinstance(caught.value.__cause__, psycopg.Error)


def test_unsupported_server_version_is_refused_and_the_connection_closed(
    archivum: Archivum, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = archivum.config(tmp_path)
    monkeypatch.setattr(_db, "SUPPORTED_SERVER_MAJOR", 19)
    with pytest.raises(SchemaVersionError, match="PostgreSQL 18 is not supported.*needs PostgreSQL 19"):
        _db.open_connection(config.archivum.dsn_env, config.archivum)


def test_transaction_sets_only_transaction_scoped_limits(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path, statement_timeout_s=7, lock_timeout_s=3, call_budget_s=90)
    with _db.open_connection(config.archivum.dsn_env, config.archivum) as conn:
        with _db.transaction(conn, config.archivum) as inside:
            row = inside.execute(
                "SELECT current_setting('statement_timeout'), current_setting('lock_timeout'),"
                " current_setting('transaction_timeout')"
            ).fetchone()
        assert row == ("7s", "3s", "90s")
        after = conn.execute("SELECT current_setting('statement_timeout'), current_setting('transaction_timeout')")
        assert after.fetchone() == ("0", "0")


def test_statement_over_the_statement_timeout_is_unavailable(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path, statement_timeout_s=1)
    with _db.open_connection(config.archivum.dsn_env, config.archivum) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as inside:
            inside.execute("SELECT pg_sleep(5)")
        error = _db.translate(caught.value, "Probing the Archivum")
        assert isinstance(error, ArchivumUnavailable)
        assert str(error).startswith("Probing the Archivum failed: ")
        assert conn.execute("SELECT 1").fetchone() == (1,)  # the connection survives a cancelled statement


def test_transaction_over_the_call_budget_is_unavailable(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path, call_budget_s=1)
    started = time.monotonic()
    with _db.open_connection(config.archivum.dsn_env, config.archivum) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as inside:
            for _ in range(10):  # each statement is well inside the statement timeout; the sum is not
                inside.execute("SELECT pg_sleep(0.5)")
        assert isinstance(_db.translate(caught.value, "Probing the Archivum"), ArchivumUnavailable)
        assert conn.closed
    assert time.monotonic() - started < 4


def test_lock_wait_over_the_lock_timeout_is_unavailable(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path, lock_timeout_s=1)
    with archivum.superuser() as holder:
        holder.execute("SELECT pg_advisory_lock(42)")
        with _db.open_connection(config.archivum.dsn_env, config.archivum) as conn:
            with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as inside:
                inside.execute("SELECT pg_advisory_xact_lock(42)")
            assert isinstance(_db.translate(caught.value, "Probing the Archivum"), ArchivumUnavailable)


def test_translate_refusal_by_privileges_is_isolation(archivum: Archivum, tmp_path: Path) -> None:
    config = archivum.config(tmp_path)
    with _db.open_connection(config.archivum.dsn_env, config.archivum) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as inside:
            inside.execute("SELECT * FROM pg_catalog.pg_authid")
    error = _db.translate(caught.value, "Reading the catalog")
    assert isinstance(error, IsolationError)
    assert str(error).startswith("Reading the catalog failed: permission denied")


def raise_in_database(db_conn: psycopg.Connection, sqlstate: str) -> psycopg.Error:
    """The driver error of a server-side RAISE with *sqlstate* (a library code or a standard one)."""
    with pytest.raises(psycopg.Error) as caught:
        db_conn.execute(
            psycopg.sql.SQL("DO $$ BEGIN RAISE EXCEPTION 'probe' USING ERRCODE = {}; END $$").format(
                psycopg.sql.Literal(sqlstate)
            )
        )
    return caught.value


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("UL001", LineageCycleError),
        ("UL002", UnknownRecordError),
        ("23503", UnknownRecordError),
        ("42501", IsolationError),
        ("08006", ArchivumUnavailable),
        ("57014", ArchivumUnavailable),
        ("55P03", ArchivumUnavailable),
        ("57P01", ArchivumUnavailable),
        ("25P03", ArchivumUnavailable),
        ("25P04", ArchivumUnavailable),
        ("23505", UlduarError),
        ("P0001", UlduarError),
    ],
)
def test_translate_maps_by_sqlstate(db_conn: psycopg.Connection, sqlstate: str, expected: type[UlduarError]) -> None:
    error = _db.translate(raise_in_database(db_conn, sqlstate), "Recording")
    assert type(error) is expected
    assert str(error) == "Recording failed: probe"


def test_translate_a_real_foreign_key_violation(db_conn: psycopg.Connection) -> None:
    db_conn.execute("CREATE TEMP TABLE parent (id integer PRIMARY KEY)")
    db_conn.execute("CREATE TEMP TABLE child (parent_id integer REFERENCES parent)")
    with pytest.raises(psycopg.Error) as caught:
        db_conn.execute("INSERT INTO child VALUES (1)")
    assert type(_db.translate(caught.value, "Recording")) is UnknownRecordError
