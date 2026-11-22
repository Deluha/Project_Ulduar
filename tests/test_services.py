"""The test services themselves: reachable, the agreed versions, isolated per test, guarded to this machine."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from dev_tools import services
from dev_tools.services import (
    DEFAULT_DSN,
    DEFAULT_S3_ENDPOINT,
    DSN_VARIABLE,
    POSTGRES_MAJOR,
    REQUIRE_SERVICES_VARIABLE,
    S3_ACCESS_KEY_VARIABLE,
    S3_ENDPOINT_VARIABLE,
    S3_SECRET_KEY_VARIABLE,
)
from tests.conftest import (
    missing_service_run_problem,
    non_local_endpoint,
    non_local_target,
    resolve_dsn,
    resolve_s3,
    rolled_back_connection,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_server_is_the_agreed_major_version(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    assert int(row[0]) // 10000 == POSTGRES_MAJOR


def test_uncommitted_work_is_rolled_back(db_dsn: str) -> None:
    probe = "SELECT to_regclass('ulduar_isolation_probe')"
    with rolled_back_connection(db_dsn) as conn:
        conn.execute("CREATE TABLE ulduar_isolation_probe (id int)")
        assert conn.execute(probe).fetchone() != (None,)
    with rolled_back_connection(db_dsn) as conn:
        assert conn.execute(probe).fetchone() == (None,)


def test_object_store_round_trips_an_object(s3_client: Any, s3_bucket: str) -> None:
    s3_client.put_object(Bucket=s3_bucket, Key="probe/a.txt", Body=b"vault")
    body = s3_client.get_object(Bucket=s3_bucket, Key="probe/a.txt")["Body"].read()
    assert body == b"vault"
    assert [item["Key"] for item in s3_client.list_objects_v2(Bucket=s3_bucket)["Contents"]] == ["probe/a.txt"]


def test_each_test_gets_a_fresh_bucket(s3_client: Any, s3_bucket: str) -> None:
    assert s3_bucket.startswith("ulduar-test-")
    assert s3_client.list_objects_v2(Bucket=s3_bucket).get("KeyCount") == 0


def test_the_service_containers_listen_on_this_machine_only() -> None:
    for service in services.SERVICES:
        arguments = services.run_arguments(service)
        assert arguments[arguments.index("-p") + 1].startswith(f"{services.HOST}:")
        assert arguments[-1 - len(service.command)] == service.image


def test_default_settings_are_local() -> None:
    environment = services.default_environment()
    assert resolve_dsn(environment) == (DEFAULT_DSN, None)
    settings, problem = resolve_s3(environment)
    assert problem is None and settings is not None and settings.endpoint == DEFAULT_S3_ENDPOINT


def test_missing_settings_are_reported() -> None:
    dsn, problem = resolve_dsn({})
    assert dsn is None and problem is not None and DSN_VARIABLE in problem
    settings, problem = resolve_s3({S3_ENDPOINT_VARIABLE: DEFAULT_S3_ENDPOINT})
    assert settings is None and problem is not None
    assert S3_ACCESS_KEY_VARIABLE in problem and S3_SECRET_KEY_VARIABLE in problem


def test_remote_dsn_is_refused() -> None:
    remote = DEFAULT_DSN.replace("127.0.0.1", "db.example.com")
    dsn, problem = resolve_dsn({DSN_VARIABLE: remote})
    assert dsn == remote and problem is not None and "db.example.com" in problem


@pytest.mark.parametrize(
    ("suffix", "target"),
    [
        ("@localhost:5432,db.example.com:5432/x", "`db.example.com`"),
        ("@localhost/x?host=db.example.com", "`db.example.com`"),
        ("@localhost/x?hostaddr=10.1.2.3", "`10.1.2.3`"),
        ("@localhost/x?service=prod", "service `prod`"),
        ("@/x", "no named host"),
    ],
)
def test_every_way_to_reach_another_database_is_refused(suffix: str, target: str) -> None:
    assert non_local_target("postgresql://u:p" + suffix) == target


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "localhost,127.0.0.1"])
def test_local_database_hosts_are_accepted(host: str) -> None:
    assert non_local_target(f"postgresql://u:p@{host}/x") is None


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
def test_an_environment_redirect_is_refused(variable: str) -> None:
    dsn, problem = resolve_dsn({DSN_VARIABLE: DEFAULT_DSN, variable: "10.1.2.3"})
    assert dsn == DEFAULT_DSN and problem is not None and variable in problem


@pytest.mark.parametrize(
    ("endpoint", "target"),
    [
        ("https://s3.amazonaws.com", "`s3.amazonaws.com`"),
        ("http://bucket.example.com:9000", "`bucket.example.com`"),
        ("http://10.1.2.3:9000", "`10.1.2.3`"),
        ("127.0.0.1:9000", "an unreadable endpoint"),
        ("ftp://127.0.0.1", "an unreadable endpoint"),
        ("http://127.0.0.1:port", "an unreadable endpoint"),
    ],
)
def test_every_way_to_reach_another_object_store_is_refused(endpoint: str, target: str) -> None:
    assert non_local_endpoint(endpoint) == target


@pytest.mark.parametrize("endpoint", ["http://localhost:9000", "http://127.0.0.1:59000", "https://[::1]:9000"])
def test_local_object_store_endpoints_are_accepted(endpoint: str) -> None:
    assert non_local_endpoint(endpoint) is None


def test_a_remote_object_store_is_refused_with_its_settings() -> None:
    environment = {**services.default_environment(), S3_ENDPOINT_VARIABLE: "https://s3.amazonaws.com"}
    settings, problem = resolve_s3(environment)
    assert settings is not None and problem is not None and "s3.amazonaws.com" in problem


def test_a_required_run_needs_a_passed_test_of_each_service() -> None:
    required = {REQUIRE_SERVICES_VARIABLE: "1"}
    assert missing_service_run_problem(required, {"db": 1, "s3": 1}) is None
    problem = missing_service_run_problem(required, {"db": 1, "s3": 0})
    assert problem is not None and "object-store" in problem and "database" not in problem
    assert missing_service_run_problem({}, {"db": 0, "s3": 0}) is None


def test_deselecting_the_service_tests_fails_a_required_run() -> None:
    env = {**os.environ, REQUIRE_SERVICES_VARIABLE: "1"}
    env.pop("PYTEST_ADDOPTS", None)
    command = [sys.executable, "-m", "pytest", "-q", "-m", "not db and not s3", "-p", "no:cacheprovider",
               "tests/test_verify.py"]  # fmt: skip
    run = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert run.returncode == 1 and "no database or object-store test passed" in run.stdout


def test_env_prints_the_default_settings(capsys: pytest.CaptureFixture[str]) -> None:
    assert services.main(["env"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert dict(line.split("=", 1) for line in lines) == services.default_environment()


def test_no_service_named_selects_every_service_and_an_unknown_one_is_refused() -> None:
    assert services.selected([]) == list(services.SERVICES)
    assert [service.name for service in services.selected(["object-store"])] == ["object-store"]
    with pytest.raises(SystemExit) as raised:
        services.main(["status", "redis"])
    assert raised.value.code == 2


def test_ci_starts_the_services_with_the_services_command() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci-tests.yml").read_text(encoding="utf-8")
    assert "python dev_tools/services.py start" in workflow
    assert 'python dev_tools/services.py env >> "$GITHUB_ENV"' in workflow
    assert f'{REQUIRE_SERVICES_VARIABLE}: "1"' in workflow
    assert "image:" not in workflow  # the images are stated once, in dev_tools/services.py


def test_the_dev_container_uses_the_same_images_and_credentials() -> None:
    compose = (REPO_ROOT / ".devcontainer/docker-compose.yml").read_text(encoding="utf-8")
    for service in services.SERVICES:
        assert f"image: {service.image}" in compose, service.name
        for key, value in service.environment.items():
            assert f"{key}: {value}" in compose, key
    for key in (S3_ACCESS_KEY_VARIABLE, S3_SECRET_KEY_VARIABLE):
        assert f"{key}: {services.default_environment()[key]}" in compose, key
