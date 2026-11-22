"""The local test services: a PostgreSQL 18 server and an S3-compatible object store (MinIO).

Each is a throwaway Docker container listening on 127.0.0.1 and holding no data worth
keeping. `start [service …]` runs the containers (pulling an image when needed) and waits
until each one answers; `stop` removes them; `status` says whether each is ready; `env`
prints the test settings as KEY=VALUE lines (for $GITHUB_ENV, or `export` in a shell).
Images, ports, credentials and the variable names the tests read are stated here and
nowhere else. The credentials are throwaways for local instances that hold no data.
Exit 0 = done, 1 = a service could not be started or is not ready. Stdlib only.
Details: tests/README.md.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import NamedTuple

HOST = "127.0.0.1"
START_TIMEOUT_SECONDS = 90

POSTGRES_MAJOR = 18
POSTGRES_IMAGE = f"postgres:{POSTGRES_MAJOR}"
POSTGRES_PORT = 55432
POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "ulduar"
POSTGRES_DATABASE = "postgres"
DEFAULT_DSN = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{HOST}:{POSTGRES_PORT}/{POSTGRES_DATABASE}"

# A community MinIO build, pinned: the official image left Docker Hub (ROADMAP.md, Phase 0 sign-off S5).
OBJECT_STORE_IMAGE = "pgsty/minio:RELEASE.2026-08-04T00-00-00Z"
OBJECT_STORE_PORT = 59000
OBJECT_STORE_ACCESS_KEY = "ulduar"
OBJECT_STORE_SECRET_KEY = "ulduar-test-only"
DEFAULT_S3_ENDPOINT = f"http://{HOST}:{OBJECT_STORE_PORT}"
# MinIO answers 200 here once it serves requests.
OBJECT_STORE_READY_PATH = "/minio/health/ready"

# The environment the tests read (tests/conftest.py) and verify, CI and the session hook set.
DSN_VARIABLE = "ULDUAR_TEST_DSN"
S3_ENDPOINT_VARIABLE = "ULDUAR_TEST_S3_ENDPOINT"
S3_ACCESS_KEY_VARIABLE = "ULDUAR_TEST_S3_ACCESS_KEY"
S3_SECRET_KEY_VARIABLE = "ULDUAR_TEST_S3_SECRET_KEY"
REQUIRE_SERVICES_VARIABLE = "ULDUAR_REQUIRE_SERVICES"


def default_environment() -> dict[str, str]:
    """The test settings for the local instances started here."""
    return {
        DSN_VARIABLE: DEFAULT_DSN,
        S3_ENDPOINT_VARIABLE: DEFAULT_S3_ENDPOINT,
        S3_ACCESS_KEY_VARIABLE: OBJECT_STORE_ACCESS_KEY,
        S3_SECRET_KEY_VARIABLE: OBJECT_STORE_SECRET_KEY,
    }


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _postgres_ready(container: str) -> bool:
    """`-h` matters: without it pg_isready tests the unix socket, which also answers during the
    image's first-run bootstrap server, before the real one listens on TCP."""
    out = _docker("exec", container, "pg_isready", "-h", HOST, "-U", POSTGRES_USER, "-d", POSTGRES_DATABASE)
    return out.returncode == 0


def _object_store_ready(_container: str) -> bool:
    # An empty ProxyHandler: the probe goes straight to 127.0.0.1 whatever proxy the shell sets.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(DEFAULT_S3_ENDPOINT + OBJECT_STORE_READY_PATH, timeout=2) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


class Service(NamedTuple):
    name: str
    container: str
    image: str
    port: int
    container_port: int
    environment: dict[str, str]
    command: list[str]
    ready: Callable[[str], bool]


SERVICES = (
    Service(
        "postgres", "ulduar-test-pg", POSTGRES_IMAGE, POSTGRES_PORT, 5432,
        {"POSTGRES_PASSWORD": POSTGRES_PASSWORD}, [], _postgres_ready,
    ),
    Service(
        "object-store", "ulduar-test-s3", OBJECT_STORE_IMAGE, OBJECT_STORE_PORT, 9000,
        {"MINIO_ROOT_USER": OBJECT_STORE_ACCESS_KEY, "MINIO_ROOT_PASSWORD": OBJECT_STORE_SECRET_KEY},
        ["server", "/data"], _object_store_ready,
    ),
)  # fmt: skip
SERVICE_NAMES = tuple(service.name for service in SERVICES)


def run_arguments(service: Service) -> list[str]:
    """The `docker run` arguments that start *service* on this machine only."""
    env = [arg for key, value in service.environment.items() for arg in ("-e", f"{key}={value}")]
    port = ["-p", f"{HOST}:{service.port}:{service.container_port}"]
    return ["run", "-d", "--name", service.container, *env, *port, service.image, *service.command]


def is_running(service: Service) -> bool:
    """Whether the service's container exists, is running and runs the image stated here."""
    out = _docker("inspect", "--format", "{{.State.Running}} {{.Config.Image}}", service.container)
    return out.returncode == 0 and out.stdout.split() == ["true", service.image]


def _wait_until_ready(service: Service) -> bool:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if service.ready(service.container):
            return True
        time.sleep(1)
    return False


def start_one(service: Service) -> int:
    """Run the container if it is not running, then wait until the service answers."""
    if not is_running(service):
        _docker("rm", "-f", service.container)  # a stopped leftover, or one on an older image, blocks the name
        run = _docker(*run_arguments(service))
        if run.returncode != 0:
            print(f"❌ test-services: {service.name}: docker run failed: {run.stderr.strip()}", file=sys.stderr)
            return 1
    if not _wait_until_ready(service):
        print(f"❌ test-services: {service.image} did not become ready in {START_TIMEOUT_SECONDS}s", file=sys.stderr)
        return 1
    print(f"✅ test-services: {service.name} ({service.image}) is ready on {HOST}:{service.port}")
    return 0


def selected(names: Sequence[str]) -> list[Service]:
    """The services named, in their fixed order; all of them when none is named."""
    return [service for service in SERVICES if not names or service.name in names]


def start(names: Sequence[str] = ()) -> int:
    results = [start_one(service) for service in selected(names)]
    return 1 if any(results) else 0


def stop(names: Sequence[str] = ()) -> int:
    """Remove the containers; a missing one is not an error."""
    for service in selected(names):
        _docker("rm", "-f", service.container)
        print(f"✅ test-services: {service.name} stopped")
    return 0


def status(names: Sequence[str] = ()) -> int:
    failed = 0
    for service in selected(names):
        ready = is_running(service) and service.ready(service.container)
        print(f"{'✅' if ready else '❌'} test-services: {service.name} {'ready' if ready else 'not running'}")
        failed |= not ready
    return failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The local test services (Docker): PostgreSQL 18 and MinIO.")
    parser.add_argument("command", choices=("start", "stop", "status", "env"))
    # Checked by hand: Python 3.11's argparse rejects an empty `nargs="*"` list against `choices`.
    parser.add_argument("services", nargs="*", help=f"any of {', '.join(SERVICE_NAMES)}; default: every service")
    args = parser.parse_args(argv)
    unknown = sorted(set(args.services) - set(SERVICE_NAMES))
    if unknown:
        parser.error(f"unknown service: {', '.join(unknown)} (choose from {', '.join(SERVICE_NAMES)})")
    if args.command == "env":
        print("\n".join(f"{key}={value}" for key, value in default_environment().items()))
        return 0
    try:
        return {"start": start, "stop": stop, "status": status}[args.command](args.services)
    except FileNotFoundError:
        print("❌ test-services: docker is not installed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
