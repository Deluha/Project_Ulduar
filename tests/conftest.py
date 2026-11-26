"""Shared fixtures: the test services' connections and their guards (tests/README.md).

The settings come from the ULDUAR_TEST_* variables of dev_tools/services.py and must
point at this machine. Without them a service test is skipped with a notice, unless
ULDUAR_REQUIRE_SERVICES=1 (set by dev_tools/verify.py and CI), which turns the skip into
a failure and also fails a whole run in which no database test or no object-store test
passed.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import boto3
import psycopg
import pytest
from botocore.config import Config
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from dev_tools.services import (
    DSN_VARIABLE,
    REQUIRE_SERVICES_VARIABLE,
    S3_ACCESS_KEY_VARIABLE,
    S3_ENDPOINT_VARIABLE,
    S3_SECRET_KEY_VARIABLE,
)
from ulduar.config import Config as UlduarConfig
from ulduar.config import load_config

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Marker → the fixtures that make a test need that service.
SERVICE_FIXTURES = {"db": {"db_dsn", "db_conn", "archivum"}, "s3": {"s3_settings", "s3_client", "s3_bucket"}}
SERVICE_LABELS = {"db": "database", "s3": "object-store"}
# libpq reads these when the connection string leaves them out, or (PGHOSTADDR) even when
# it names a host, so any of them can redirect a local-looking connection string.
REDIRECTING_VARIABLES = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
S3_SCHEMES = ("http", "https")
TEST_BUCKET_PREFIX = "ulduar-test-"
# The shared group role of step 1; the throwaway ledger role must be able to grant it to project roles.
RECORDER_GROUP = "ulduar_recorder"
_PROBLEM_KEY = pytest.StashKey[str]()
_service_test_ids: dict[str, set[str]] = {marker: set() for marker in SERVICE_FIXTURES}
_service_tests_passed: dict[str, int] = {marker: 0 for marker in SERVICE_FIXTURES}


class S3Settings(NamedTuple):
    endpoint: str
    access_key: str
    secret_key: str


def _required(environ: dict[str, str]) -> bool:
    return environ.get(REQUIRE_SERVICES_VARIABLE) == "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that takes a service fixture with that service's marker."""
    for item in items:
        names = set(getattr(item, "fixturenames", ()))
        for marker, fixtures in SERVICE_FIXTURES.items():
            if fixtures & names:
                item.add_marker(getattr(pytest.mark, marker))
                _service_test_ids[marker].add(item.nodeid)


def resolve_dsn(environ: dict[str, str]) -> tuple[str | None, str | None]:
    """(dsn, problem): the test connection string, or why there is none.

    A problem with a dsn of None is a failure when the services are required and a
    skip otherwise; a non-local host is always a failure (the dsn is returned too).
    """
    dsn = environ.get(DSN_VARIABLE)
    if not dsn:
        return None, f"{DSN_VARIABLE} is not set — database tests did not run (tests/README.md)"
    target = next((f"${name}" for name in REDIRECTING_VARIABLES if environ.get(name)), None)
    target = f"wherever {target} says" if target else non_local_target(dsn)
    if target:
        return dsn, f"{DSN_VARIABLE} points at {target} — tests connect to this machine only"
    return dsn, None


def non_local_target(dsn: str) -> str | None:
    """What makes *dsn* reach beyond this machine, as libpq reads it, or None when it is local.

    Every entry of `host` and `hostaddr` must be local (a host list or a query parameter can name
    a second server), a host must be named (else libpq falls back to the environment) and a
    service file entry is refused because its target is not visible here.
    """
    try:
        info = conninfo_to_dict(dsn)
    except psycopg.Error:
        return "an unreadable connection string"
    if info.get("service"):
        return f"service `{info['service']}`"
    hosts = [h for key in ("host", "hostaddr") for h in str(info.get(key) or "").split(",") if h]
    if not info.get("host"):
        return "no named host"
    remote = [h for h in hosts if h not in LOCAL_HOSTS]
    return f"`{remote[0]}`" if remote else None


def resolve_s3(environ: dict[str, str]) -> tuple[S3Settings | None, str | None]:
    """(settings, problem): the object-store test settings, or why there are none.

    Same contract as resolve_dsn: missing settings skip or fail per the requirement, an
    endpoint off this machine always fails.
    """
    names = (S3_ENDPOINT_VARIABLE, S3_ACCESS_KEY_VARIABLE, S3_SECRET_KEY_VARIABLE)
    missing = [name for name in names if not environ.get(name)]
    if missing:
        return None, f"{', '.join(missing)} not set — object-store tests did not run (tests/README.md)"
    settings = S3Settings(*(environ[name] for name in names))
    target = non_local_endpoint(settings.endpoint)
    if target:
        return settings, f"{S3_ENDPOINT_VARIABLE} points at {target} — tests connect to this machine only"
    return settings, None


def non_local_endpoint(endpoint: str) -> str | None:
    """What makes an object-store *endpoint* URL reach beyond this machine, or None when it is local."""
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return "an unreadable endpoint"
    if parts.scheme not in S3_SCHEMES or not host:
        return "an unreadable endpoint"
    return None if host in LOCAL_HOSTS else f"`{host}`"


def missing_service_run_problem(environ: dict[str, str], passed: dict[str, int]) -> str | None:
    """Why a run that requires the services is not acceptable, or None.

    With ULDUAR_REQUIRE_SERVICES=1 at least one database test and one object-store test must have
    passed: a run that deselected them (`-m "not db"`) or lost the files that hold them is not a
    verified run (hard rule 9).
    """
    if not _required(environ):
        return None
    absent = [SERVICE_LABELS[marker] for marker in SERVICE_FIXTURES if passed.get(marker, 0) == 0]
    if absent:
        return f"{REQUIRE_SERVICES_VARIABLE}=1 but no {' or '.join(absent)} test passed — the service tests did not run"
    return None


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" and report.passed:
        for marker, ids in _service_test_ids.items():
            if report.nodeid in ids:
                _service_tests_passed[marker] += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    problem = missing_service_run_problem(dict(os.environ), _service_tests_passed)
    if problem and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if problem:
        session.config.stash[_PROBLEM_KEY] = problem


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    problem = config.stash.get(_PROBLEM_KEY, None)
    if problem:
        terminalreporter.write_line(f"❌ {problem}", red=True)


def _settle(found: bool, problem: str | None) -> None:
    """Fail on a problem with settings present or the services required; skip on missing settings."""
    if problem and (found or _required(dict(os.environ))):
        pytest.fail(problem, pytrace=False)
    if not found:
        pytest.skip(problem or "")


@pytest.fixture(scope="session")
def db_dsn() -> str:
    """The local test connection string; skips or fails per the module docstring."""
    dsn, problem = resolve_dsn(dict(os.environ))
    _settle(dsn is not None, problem)
    assert dsn is not None
    return dsn


@contextmanager
def rolled_back_connection(dsn: str) -> Iterator[psycopg.Connection]:
    """A connection whose uncommitted work is rolled back on exit."""
    with psycopg.connect(dsn) as conn:
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def db_conn(db_dsn: str) -> Iterator[psycopg.Connection]:
    """A connection whose uncommitted work is rolled back when the test ends."""
    with rolled_back_connection(db_dsn) as conn:
        yield conn


class Recorder(NamedTuple):
    """A throwaway recording login role: its name and the environment variable holding its DSN."""

    role: str
    env: str


class Archivum:
    """A throwaway Archivum: an empty database `ulduar_test_<hex>` owned by a throwaway ledger role.

    `config` writes an `ulduar.toml` naming the environment variables that hold the DSNs and returns
    the loaded `Config`; `new_recorder` makes a login role that belongs to no project until onboarded;
    `superuser` opens an autocommit connection to the database for a test that must tamper or inspect.
    """

    def __init__(self, admin_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self._admin_dsn = admin_dsn
        self._monkeypatch = monkeypatch
        token = uuid.uuid4().hex[:12]
        self.database = f"ulduar_test_{token}"
        self.ledger_role = f"ulduar_test_ledger_{token}"
        self.ledger_env = f"ULDUAR_TEST_{token.upper()}_LEDGER_DSN"
        self.recorders: list[Recorder] = []
        self._token = token

    def _dsn(self, role: str, password: str) -> str:
        return make_conninfo(self._admin_dsn, dbname=self.database, user=role, password=password)

    def _create_login_role(self, admin: psycopg.Connection, role: str, env: str, options: str = "") -> None:
        password = secrets.token_hex(16)
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN {} PASSWORD {}").format(
                sql.Identifier(role), sql.SQL(options), sql.Literal(password)
            )
        )
        self._monkeypatch.setenv(env, self._dsn(role, password))

    def create(self) -> None:
        """Create the ledger role and the empty database it owns."""
        with psycopg.connect(self._admin_dsn, autocommit=True) as admin:
            self._create_login_role(admin, self.ledger_role, self.ledger_env, "CREATEROLE")
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(self.database), sql.Identifier(self.ledger_role)
                )
            )
            if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RECORDER_GROUP,)).fetchone():
                admin.execute(
                    sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(
                        sql.Identifier(RECORDER_GROUP), sql.Identifier(self.ledger_role)
                    )
                )

    def new_recorder(self) -> Recorder:
        """A recording login role that is a member of nothing until the library onboards it."""
        n = len(self.recorders) + 1
        recorder = Recorder(f"ulduar_test_rec_{self._token}_{n}", f"ULDUAR_TEST_{self._token.upper()}_REC{n}_DSN")
        with psycopg.connect(self._admin_dsn, autocommit=True) as admin:
            self._create_login_role(admin, recorder.role, recorder.env)
        self.recorders.append(recorder)
        return recorder

    def config(
        self, tmp_path: Path, *, project: str = "test-project", recorder: Recorder | None = None, **timeouts: int
    ) -> UlduarConfig:
        """Write `ulduar.toml` under *tmp_path* (recording role: *recorder* or a new one) and load it."""
        recorder = recorder or self.new_recorder()
        lines = [
            "version = 1",
            "[project]",
            f'name = "{project}"',
            "[archivum]",
            f'dsn_env = "{recorder.env}"',
            f'ledger_dsn_env = "{self.ledger_env}"',
            *(f"{key} = {value}" for key, value in timeouts.items()),
        ]
        path = tmp_path / "ulduar.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return load_config(path)

    @contextmanager
    def superuser(self) -> Iterator[psycopg.Connection]:
        """An autocommit superuser connection to the throwaway database."""
        with psycopg.connect(make_conninfo(self._admin_dsn, dbname=self.database), autocommit=True) as conn:
            yield conn

    def drop(self) -> None:
        """Drop the database (closing its sessions) and then every role the fixture made."""
        with psycopg.connect(self._admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.database)))
            for role in [*(r.role for r in self.recorders), self.ledger_role]:
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
def archivum(db_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Archivum]:
    """A throwaway, empty Archivum database with its own ledger role; dropped with its roles after the test."""
    instance = Archivum(db_dsn, monkeypatch)
    try:
        instance.create()
        yield instance
    finally:
        instance.drop()


@pytest.fixture(scope="session")
def s3_settings() -> S3Settings:
    """The local object-store settings; skips or fails per the module docstring."""
    settings, problem = resolve_s3(dict(os.environ))
    _settle(settings is not None, problem)
    assert settings is not None
    return settings


@pytest.fixture(scope="session")
def s3_client(s3_settings: S3Settings) -> Any:
    """An S3 client for the local object store; it never reads credentials, an endpoint or a proxy from elsewhere.

    `proxies={}` is explicit: left unset, botocore would route through $HTTP(S)_PROXY, sending signed
    requests to another machine whatever the guarded endpoint says.
    """
    return boto3.session.Session().client(
        "s3",
        endpoint_url=s3_settings.endpoint,
        aws_access_key_id=s3_settings.access_key,
        aws_secret_access_key=s3_settings.secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2}, proxies={}),
    )


@pytest.fixture
def s3_bucket(s3_client: Any) -> Iterator[str]:
    """A fresh, empty bucket on the local object store, removed with its objects when the test ends."""
    name = TEST_BUCKET_PREFIX + uuid.uuid4().hex[:12]
    s3_client.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        listed = s3_client.list_objects_v2(Bucket=name).get("Contents", [])
        for item in listed:
            s3_client.delete_object(Bucket=name, Key=item["Key"])
        s3_client.delete_bucket(Bucket=name)
