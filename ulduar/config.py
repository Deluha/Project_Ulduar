"""The consumer's configuration file: `ulduar.toml`, read with `tomllib`, validated, never defaulted silently.

Input: a TOML path. Output: a frozen `Config`, with an optional `[archive]` section for Hodir. Credentials are
never in the file, only the names of the environment variables that hold them (hard rule 11). Any unknown key,
at any level, is refused by its dotted path. Sits below every component: `_db` and Freya read their settings from here.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ulduar.errors import ConfigurationError

CONFIG_VERSION = 1
DEFAULT_CONFIG_PATH = "ulduar.toml"
# The one definition of a project name or scope; the CHECK constraints of step 1 spell the same pattern.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
DEFAULT_SCOPE = "default"
# The archive's bucket name (S3 naming rules), its key prefix segments (`NAME_PATTERN`) and region.
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
REGION_PATTERN = re.compile(r"^[a-z0-9-]{1,32}$")
DEFAULT_ARCHIVE_PREFIX = "ulduar"
ARCHIVE_BACKENDS = ("s3", "directory")
# (default, lowest, highest) in seconds.
CONNECT_TIMEOUT_BOUNDS = (10, 1, 60)
STATEMENT_TIMEOUT_BOUNDS = (30, 1, 600)
LOCK_TIMEOUT_BOUNDS = (5, 1, 60)
CALL_BUDGET_BOUNDS = (60, 1, 3600)
# (default, lowest, highest) in GiB, hours, and deletions per run.
SPOOL_LIMIT_GIB_BOUNDS = (100, 1, 100000)
INCOMPLETE_PUT_GRACE_HOURS_BOUNDS = (168, 24, 8760)
MAX_DELETIONS_BOUNDS = (100, 1, 1000000)


@dataclass(frozen=True)
class ProjectSettings:
    """The project a consumer records under, and its scope."""

    name: str
    scope: str = DEFAULT_SCOPE


@dataclass(frozen=True)
class ArchivumSettings:
    """How to reach the Archivum: the names of the variables holding its connection strings, and time limits."""

    dsn_env: str
    ledger_dsn_env: str | None = None
    connect_timeout_s: int = CONNECT_TIMEOUT_BOUNDS[0]
    statement_timeout_s: int = STATEMENT_TIMEOUT_BOUNDS[0]
    lock_timeout_s: int = LOCK_TIMEOUT_BOUNDS[0]
    call_budget_s: int = CALL_BUDGET_BOUNDS[0]


@dataclass(frozen=True)
class CredentialSettings:
    """The names of the environment variables holding one storage credential (never its values)."""

    access_key_env: str
    secret_key_env: str
    session_token_env: str | None = None


@dataclass(frozen=True)
class ArchiveSettings:
    """Where Hodir keeps the archive: a backend, its location, and the limits of spooling, grace and deletion.

    `writer` and `deleter` apply to the s3 backend only: no writer means the client's standard credential chain,
    and the deleter is needed only by calls that delete.
    """

    backend: Literal["s3", "directory"]
    path: Path | None = None
    bucket: str | None = None
    prefix: str = DEFAULT_ARCHIVE_PREFIX
    region: str | None = None
    endpoint_url_env: str | None = None
    proxy_env: str | None = None
    spool_dir: Path | None = None
    spool_limit_gib: int = SPOOL_LIMIT_GIB_BOUNDS[0]
    incomplete_put_grace_hours: int = INCOMPLETE_PUT_GRACE_HOURS_BOUNDS[0]
    max_deletions_per_run: int = MAX_DELETIONS_BOUNDS[0]
    writer: CredentialSettings | None = None
    deleter: CredentialSettings | None = None


@dataclass(frozen=True)
class Config:
    """A validated configuration and the file it came from."""

    project: ProjectSettings
    archivum: ArchivumSettings
    path: Path
    archive: ArchiveSettings | None = None


def check_name(value: str, what: str) -> str:
    """*value* when it is a project name or scope (`NAME_PATTERN`); otherwise `ValueError` naming it as the *what*."""
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise ValueError(f"The {what} {value!r} must match {NAME_PATTERN.pattern}")
    return value


def _table(parent: Mapping[str, Any], key: str, prefix: str, *, required: bool) -> Mapping[str, Any]:
    """The sub-table `key` of *parent*, `{}` when optional and absent."""
    dotted = prefix + key
    if key not in parent:
        if required:
            raise ConfigurationError(f"Configuration key `{dotted}` is required")
        return {}
    value = parent[key]
    if not isinstance(value, dict):
        raise ConfigurationError(f"Configuration key `{dotted}` must be a table")
    return value


def _refuse_unknown(table: Mapping[str, Any], allowed: set[str], prefix: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigurationError(f"Unknown configuration key `{prefix}{unknown[0]}`")


def _optional_string(table: Mapping[str, Any], key: str, prefix: str, pattern: re.Pattern[str]) -> str | None:
    """The string at *key* when it is present and matches *pattern*; None when absent."""
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str):
        raise ConfigurationError(f"Configuration key `{prefix}{key}` must be a string")
    if not pattern.fullmatch(value):
        raise ConfigurationError(f"Configuration key `{prefix}{key}` must match {pattern.pattern}")
    return value


def _required_string(table: Mapping[str, Any], key: str, prefix: str, pattern: re.Pattern[str]) -> str:
    value = _optional_string(table, key, prefix, pattern)
    if value is None:
        raise ConfigurationError(f"Configuration key `{prefix}{key}` is required")
    return value


def _bounded_int(table: Mapping[str, Any], key: str, prefix: str, bounds: tuple[int, int, int], unit: str) -> int:
    """The integer at *key* (the default of *bounds* when absent), within its bounds; *unit* ends the message."""
    default, low, high = bounds
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"Configuration key `{prefix}{key}` must be an integer")
    if not low <= value <= high:
        suffix = f" {unit}" if unit else ""
        raise ConfigurationError(f"Configuration key `{prefix}{key}` must be between {low} and {high}{suffix}")
    return value


def _absolute_path(table: Mapping[str, Any], key: str, prefix: str) -> Path | None:
    """The absolute path at *key*; None when absent."""
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str):
        raise ConfigurationError(f"Configuration key `{prefix}{key}` must be a string")
    path = Path(value)
    if not value or not path.is_absolute():
        raise ConfigurationError(f"Configuration key `{prefix}{key}` must be an absolute path")
    return path


def _read_toml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as e:
        raise ConfigurationError(f"Configuration file {path} does not exist") from e
    except OSError as e:
        raise ConfigurationError(f"Configuration file {path} could not be read: {e.strerror}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigurationError(f"Configuration file {path} is not valid TOML: {e}") from e


def load_config(path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH) -> Config:
    """Read and validate the file at *path*; `ConfigurationError` names the key or file at fault."""
    file = Path(path)
    raw = _read_toml(file)
    _refuse_unknown(raw, {"version", "project", "archivum", "archive"}, "")
    if "version" not in raw:
        raise ConfigurationError("Configuration key `version` is required")
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != CONFIG_VERSION:
        raise ConfigurationError(f"Configuration key `version` must be the integer {CONFIG_VERSION}")
    return Config(
        project=_project_settings(_table(raw, "project", "", required=True)),
        archivum=_archivum_settings(_table(raw, "archivum", "", required=True)),
        path=file,
        archive=_archive_settings(_table(raw, "archive", "", required=True)) if "archive" in raw else None,
    )


def _project_settings(table: Mapping[str, Any]) -> ProjectSettings:
    _refuse_unknown(table, {"name", "scope"}, "project.")
    return ProjectSettings(
        name=_required_string(table, "name", "project.", NAME_PATTERN),
        scope=_optional_string(table, "scope", "project.", NAME_PATTERN) or DEFAULT_SCOPE,
    )


def _archivum_settings(table: Mapping[str, Any]) -> ArchivumSettings:
    _refuse_unknown(
        table,
        {"dsn_env", "ledger_dsn_env", "connect_timeout_s", "statement_timeout_s", "lock_timeout_s", "call_budget_s"},
        "archivum.",
    )
    return ArchivumSettings(
        dsn_env=_required_string(table, "dsn_env", "archivum.", ENV_NAME_PATTERN),
        ledger_dsn_env=_optional_string(table, "ledger_dsn_env", "archivum.", ENV_NAME_PATTERN),
        connect_timeout_s=_bounded_int(table, "connect_timeout_s", "archivum.", CONNECT_TIMEOUT_BOUNDS, "seconds"),
        statement_timeout_s=_bounded_int(
            table, "statement_timeout_s", "archivum.", STATEMENT_TIMEOUT_BOUNDS, "seconds"
        ),
        lock_timeout_s=_bounded_int(table, "lock_timeout_s", "archivum.", LOCK_TIMEOUT_BOUNDS, "seconds"),
        call_budget_s=_bounded_int(table, "call_budget_s", "archivum.", CALL_BUDGET_BOUNDS, "seconds"),
    )


_ARCHIVE_KEYS = {
    "backend", "path", "bucket", "prefix", "region", "endpoint_url_env", "proxy_env", "spool_dir",
    "spool_limit_gib", "incomplete_put_grace_hours", "max_deletions_per_run", "writer", "deleter",
}  # fmt: skip
_S3_ONLY_KEYS = ("bucket", "region", "endpoint_url_env", "proxy_env", "writer", "deleter")
_CREDENTIAL_KEYS = {"access_key_env", "secret_key_env", "session_token_env"}


def _archive_settings(table: Mapping[str, Any]) -> ArchiveSettings:
    _refuse_unknown(table, _ARCHIVE_KEYS, "archive.")
    backend = table.get("backend")
    if backend not in ARCHIVE_BACKENDS:
        raise ConfigurationError(
            "Configuration key `archive.backend` is required and must be one of " + ", ".join(ARCHIVE_BACKENDS)
        )
    _refuse_misplaced(table, backend)
    path = _absolute_path(table, "path", "archive.")
    if backend == "directory" and path is None:
        raise ConfigurationError("Configuration key `archive.path` is required")
    writer = _credential_settings(table, "writer")
    deleter = _credential_settings(table, "deleter")
    if writer and deleter and writer.access_key_env == deleter.access_key_env:
        raise ConfigurationError(
            "Configuration keys `archive.writer` and `archive.deleter` must name different credentials"
        )
    return ArchiveSettings(
        backend=backend,
        path=path,
        bucket=_required_string(table, "bucket", "archive.", BUCKET_PATTERN) if backend == "s3" else None,
        prefix=_prefix(table),
        region=_optional_string(table, "region", "archive.", REGION_PATTERN),
        endpoint_url_env=_optional_string(table, "endpoint_url_env", "archive.", ENV_NAME_PATTERN),
        proxy_env=_optional_string(table, "proxy_env", "archive.", ENV_NAME_PATTERN),
        spool_dir=_absolute_path(table, "spool_dir", "archive."),
        spool_limit_gib=_bounded_int(table, "spool_limit_gib", "archive.", SPOOL_LIMIT_GIB_BOUNDS, "GiB"),
        incomplete_put_grace_hours=_bounded_int(
            table, "incomplete_put_grace_hours", "archive.", INCOMPLETE_PUT_GRACE_HOURS_BOUNDS, "hours"
        ),
        max_deletions_per_run=_bounded_int(table, "max_deletions_per_run", "archive.", MAX_DELETIONS_BOUNDS, ""),
        writer=writer,
        deleter=deleter,
    )


def _refuse_misplaced(table: Mapping[str, Any], backend: str) -> None:
    """Refuse the first key that belongs to the other backend."""
    misplaced, owner = (("path",), "directory") if backend == "s3" else (_S3_ONLY_KEYS, "s3")
    for key in misplaced:
        if key in table:
            raise ConfigurationError(f"Configuration key `archive.{key}` applies to the {owner} backend only")


def _prefix(table: Mapping[str, Any]) -> str:
    """The key prefix: empty, or `/`-joined segments that each match `NAME_PATTERN`."""
    value = table.get("prefix", DEFAULT_ARCHIVE_PREFIX)
    if not isinstance(value, str):
        raise ConfigurationError("Configuration key `archive.prefix` must be a string")
    if value and not all(NAME_PATTERN.fullmatch(segment) for segment in value.split("/")):
        raise ConfigurationError(
            "Configuration key `archive.prefix` must be empty or segments joined by `/`, "
            f"each matching {NAME_PATTERN.pattern}"
        )
    return value


def _credential_settings(archive: Mapping[str, Any], key: str) -> CredentialSettings | None:
    if key not in archive:
        return None
    prefix = f"archive.{key}."
    table = _table(archive, key, "archive.", required=True)
    _refuse_unknown(table, _CREDENTIAL_KEYS, prefix)
    return CredentialSettings(
        access_key_env=_required_string(table, "access_key_env", prefix, ENV_NAME_PATTERN),
        secret_key_env=_required_string(table, "secret_key_env", prefix, ENV_NAME_PATTERN),
        session_token_env=_optional_string(table, "session_token_env", prefix, ENV_NAME_PATTERN),
    )
