"""Shared fixtures: the test services' connections and their guards (tests/README.md).

The settings come from the ULDUAR_TEST_* variables of dev_tools/services.py and must
point at this machine. Without them a service test is skipped with a notice, unless
ULDUAR_REQUIRE_SERVICES=1 (set by dev_tools/verify.py and CI), which turns the skip into
a failure and also fails a whole run in which no database test or no object-store test
passed.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
    S3_DELETER_ACCESS_KEY_VARIABLE,
    S3_DELETER_SECRET_KEY_VARIABLE,
    S3_ENDPOINT_VARIABLE,
    S3_SECRET_KEY_VARIABLE,
    S3_WRITER_ACCESS_KEY_VARIABLE,
    S3_WRITER_SECRET_KEY_VARIABLE,
)
from ulduar import _db, _ids
from ulduar.config import ArchiveSettings, CredentialSettings, load_config
from ulduar.config import Config as UlduarConfig
from ulduar.freya import install, onboard_project, open_catalog
from ulduar.hodir import Archive
from ulduar.hodir import _layout as hodir_layout
from ulduar.hodir._store import open_store

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Marker → the fixtures that make a test need that service.
SERVICE_FIXTURES = {"db": {"db_dsn", "db_conn", "archivum"}, "s3": {"s3_settings", "s3_client", "s3_bucket"}}
SERVICE_LABELS = {"db": "database", "s3": "object-store"}
# libpq reads these when the connection string leaves them out, or (PGHOSTADDR) even when
# it names a host, so any of them can redirect a local-looking connection string.
REDIRECTING_VARIABLES = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
S3_SCHEMES = ("http", "https")
S3_VARIABLES = (
    S3_ENDPOINT_VARIABLE,
    S3_ACCESS_KEY_VARIABLE,
    S3_SECRET_KEY_VARIABLE,
    S3_WRITER_ACCESS_KEY_VARIABLE,
    S3_WRITER_SECRET_KEY_VARIABLE,
    S3_DELETER_ACCESS_KEY_VARIABLE,
    S3_DELETER_SECRET_KEY_VARIABLE,
)
TEST_BUCKET_PREFIX = "ulduar-test-"
# The shared group role of step 1; the throwaway ledger role must be able to grant it to project roles.
RECORDER_GROUP = "ulduar_recorder"
# What the tests record: the SHA-256 of some content, and how long a test waits for another session to block.
CHECKSUM = hashlib.sha256(b"content").digest()
BLOCKED_WAIT_S = 15
# The advisory lock step 1's ensure_partitions takes: the first 8 bytes of SHA-256("ulduar.partitions"), signed.
PARTITION_LOCK_KEY = -6595867142526573570
_PROBLEM_KEY = pytest.StashKey[str]()
_service_test_ids: dict[str, set[str]] = {marker: set() for marker in SERVICE_FIXTURES}
_service_tests_passed: dict[str, int] = {marker: 0 for marker in SERVICE_FIXTURES}


def lineage_lock_name(project_id: uuid.UUID) -> str:
    """The text step 1's lineage trigger hashes into its per-project advisory lock (`hashtextextended(..., 0)`)."""
    return f"ulduar.lineage:{project_id}"


class S3Settings(NamedTuple):
    """The object store's endpoint and the three credentials: the root user's, the archive's writer and deleter."""

    endpoint: str
    access_key: str
    secret_key: str
    writer_access_key: str
    writer_secret_key: str
    deleter_access_key: str
    deleter_secret_key: str


def _required(environ: dict[str, str]) -> bool:
    return environ.get(REQUIRE_SERVICES_VARIABLE) == "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that takes a service fixture with that service's marker."""
    for item in items:
        names = set(getattr(item, "fixturenames", ()))
        for marker, fixtures in SERVICE_FIXTURES.items():
            # An explicit marker counts too: a test over every storage backend marks only its object-store cases.
            if fixtures & names or item.get_closest_marker(marker):
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
    missing = [name for name in S3_VARIABLES if not environ.get(name)]
    if missing:
        return None, f"{', '.join(missing)} not set — object-store tests did not run (tests/README.md)"
    settings = S3Settings(*(environ[name] for name in S3_VARIABLES))
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


# --- shared helpers of the Freya tests (an Archivum is installed, onboarded and inspected the same way in each) ---


def digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def ms_ago(days: int) -> int:
    """Unix milliseconds of *days* ago, for a record id in an old month."""
    return int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)


def onboarded(archivum: Archivum, tmp_path: Path, project: str = "alpha", **timeouts: int) -> UlduarConfig:
    """A config for *project* with a new recording role, the Archivum installed and the role onboarded."""
    directory = tmp_path / project
    directory.mkdir(exist_ok=True)
    config = archivum.config(directory, project=project, **timeouts)
    install(config)
    onboard_project(config, project, archivum.recorders[-1].role)
    return config


def recorder_of(archivum: Archivum, config: UlduarConfig) -> Recorder:
    return next(r for r in archivum.recorders if r.env == config.archivum.dsn_env)


def partition_exists(archivum: Archivum, table: str, unix_ms: int) -> bool:
    moment = datetime.fromtimestamp(unix_ms / 1000, UTC)
    with archivum.superuser() as su:
        found = su.execute("SELECT to_regclass(%s)", (f"ulduar.{table}_{moment:%Y_%m}",)).fetchone()
    assert found is not None
    return found[0] is not None


def failure(config: UlduarConfig, env: str, statement: str, params: tuple[object, ...] = ()) -> psycopg.Error:
    """The driver error of *statement* run in a transaction of its own on the connection named by *env*."""
    with _db.open_connection(env, config.archivum) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as inside:
            inside.execute(statement, params)
    return caught.value


def count(archivum: Archivum, query: str, *params: object) -> int:
    """The one number a superuser's *query* returns."""
    with archivum.superuser() as su:
        row = su.execute(query, params).fetchone()
    assert row is not None
    return int(row[0])


def old_artifacts(config: UlduarConfig, *days: int) -> list[uuid.UUID]:
    """Record one artifact per entry of *days*, that many days old, as the config's recording role."""
    with open_catalog(config) as catalog:
        return [
            catalog.register_artifact("log", CHECKSUM, 1, record_id=_ids.uuid7(ms_ago(d))).artifact_id for d in days
        ]


def wait_until_blocked(archivum: Archivum, sessions: int = 1) -> None:
    """Return once *sessions* sessions of the throwaway database wait for a lock; fail after `BLOCKED_WAIT_S`.

    The deterministic half of a race test: the test holds a lock (or an open transaction), starts the
    competitors, and acts only when they are provably queued behind it.
    """
    query = (
        "SELECT count(DISTINCT l.pid) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
        " WHERE NOT l.granted AND a.datname = current_database()"
    )
    deadline = time.monotonic() + BLOCKED_WAIT_S
    while count(archivum, query) < sessions:
        assert time.monotonic() < deadline, (
            f"fewer than {sessions} session(s) waited for a lock within {BLOCKED_WAIT_S}s"
        )
        time.sleep(0.05)


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


@contextmanager
def temporary_bucket(client: Any, *, versioned: bool = False) -> Iterator[str]:
    """A fresh, empty bucket, removed on exit with every version, delete marker and unfinished upload."""
    name = TEST_BUCKET_PREFIX + uuid.uuid4().hex[:12]
    client.create_bucket(Bucket=name)
    try:
        if versioned:
            client.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
        yield name
    finally:
        for page in client.get_paginator("list_object_versions").paginate(Bucket=name):
            for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
                client.delete_object(Bucket=name, Key=item["Key"], VersionId=item["VersionId"])
        for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=name):
            for upload in page.get("Uploads", []):
                client.abort_multipart_upload(Bucket=name, Key=upload["Key"], UploadId=upload["UploadId"])
        client.delete_bucket(Bucket=name)


@pytest.fixture
def s3_bucket(s3_client: Any) -> Iterator[str]:
    """A fresh, empty bucket on the local object store, removed with its objects when the test ends."""
    with temporary_bucket(s3_client) as name:
        yield name


@pytest.fixture
def archive_bucket(s3_client: Any) -> Iterator[str]:
    """A fresh, empty, unversioned bucket for an archive test."""
    with temporary_bucket(s3_client) as name:
        yield name


@pytest.fixture
def versioned_archive_bucket(s3_client: Any) -> Iterator[str]:
    """A fresh, empty bucket with versioning enabled; teardown removes every version and delete marker."""
    with temporary_bucket(s3_client, versioned=True) as name:
        yield name


def s3_archive_settings(bucket: str, settings: S3Settings, monkeypatch: pytest.MonkeyPatch) -> ArchiveSettings:
    """`ArchiveSettings` for *bucket* on the local object store, naming the test variables and setting them.

    The variables are set to the guarded values, so the library reads them as it would in a consumer's environment.
    """
    values = zip(S3_VARIABLES, settings, strict=True)
    for variable, value in values:
        monkeypatch.setenv(variable, value)
    return ArchiveSettings(
        backend="s3",
        bucket=bucket,
        region="us-east-1",
        endpoint_url_env=S3_ENDPOINT_VARIABLE,
        writer=CredentialSettings(S3_WRITER_ACCESS_KEY_VARIABLE, S3_WRITER_SECRET_KEY_VARIABLE),
        deleter=CredentialSettings(S3_DELETER_ACCESS_KEY_VARIABLE, S3_DELETER_SECRET_KEY_VARIABLE),
    )


@pytest.fixture
def archive_settings_s3(
    archive_bucket: str, s3_settings: S3Settings, monkeypatch: pytest.MonkeyPatch
) -> ArchiveSettings:
    """Settings of an s3 archive in a fresh unversioned bucket, with the writer and deleter test credentials."""
    return s3_archive_settings(archive_bucket, s3_settings, monkeypatch)


@pytest.fixture
def archive_settings_s3_versioned(
    versioned_archive_bucket: str, s3_settings: S3Settings, monkeypatch: pytest.MonkeyPatch
) -> ArchiveSettings:
    """Settings of an s3 archive in a fresh versioned bucket."""
    return s3_archive_settings(versioned_archive_bucket, s3_settings, monkeypatch)


@pytest.fixture
def archive_settings_dir(tmp_path: Path) -> ArchiveSettings:
    """Settings of a directory archive in a fresh directory."""
    root = tmp_path / "archive"
    root.mkdir()
    return ArchiveSettings(backend="directory", path=root)


# --- shared helpers of the Hodir tests (an archive session over each backend, one stored content, fault injection) ---

# The backends a session test runs over; only the bucket case is marked `s3`. The storage-interface tests add a
# versioned bucket and keep their own list.
ARCHIVE_BACKENDS = [pytest.param("directory", id="directory"), pytest.param("s3", id="s3", marks=pytest.mark.s3)]
ARCHIVE_SETTINGS_FIXTURES = {"directory": "archive_settings_dir", "s3": "archive_settings_s3"}
CONTENT = b"the vault keeps this"
CONTENT_CHECKSUM = hashlib.sha256(CONTENT).digest()


class Fault(Exception):
    """What `FaultyStore` raises: no store or driver error looks like it, so a test sees exactly its own failure."""


class FaultyStore:
    """A writing store that forwards every call to a real one and raises `Fault` after the *nth* call of *method*.

    The call itself is made first, so the failure falls between two steps of a put, never inside one. With
    *hide_head* the first that many `head` calls report the object as absent, as a deletion racing the put would.
    """

    def __init__(self, store: Any, method: str = "", nth: int = 1, hide_head: int = 0) -> None:
        self._store = store
        self._method = method
        self._nth = nth
        self._hide_head = hide_head
        self.calls: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._store, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] = self.calls.get(name, 0) + 1
            result = target(*args, **kwargs)
            if name == "head" and self.calls[name] <= self._hide_head:
                return None
            if name == self._method and self.calls[name] == self._nth:
                raise Fault(f"after {name}")
            return result

        return call


@pytest.fixture(params=ARCHIVE_BACKENDS)
def config(request: pytest.FixtureRequest, archivum: Archivum, tmp_path: Path) -> UlduarConfig:
    """An onboarded config (project `alpha`) with the archive of each backend in turn."""
    settings: ArchiveSettings = request.getfixturevalue(ARCHIVE_SETTINGS_FIXTURES[request.param])
    spool = tmp_path / "spool"
    spool.mkdir()
    return replace(onboarded(archivum, tmp_path), archive=replace(settings, spool_dir=spool))


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A file holding `CONTENT`."""
    path = tmp_path / "source.bin"
    path.write_bytes(CONTENT)
    return path


@contextmanager
def faulty_archive(config: UlduarConfig, **fault: Any) -> Iterator[tuple[Archive, FaultyStore]]:
    """An `Archive` over the real store wrapped in a `FaultyStore`, on its own catalog session."""
    assert config.archive is not None
    with open_catalog(config) as catalog:
        store = FaultyStore(open_store(config.archive), **fault)
        yield Archive(catalog, store, config.archive), store  # type: ignore[arg-type]


def object_key(scope: str = "default") -> str:
    """The key of the object `CONTENT` is stored under, for project `alpha`."""
    return hodir_layout.object_key("alpha", scope, CONTENT_CHECKSUM)
