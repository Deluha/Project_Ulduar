"""The consumer's configuration file: `ulduar.toml`, read with `tomllib`, validated, never defaulted silently.

Input: a TOML path. Output: a frozen `Config`. Credentials are never in the file, only the names of the
environment variables that hold them (hard rule 11). Any unknown key, at any level, is refused by its
dotted path. Sits below every component: `_db` and Freya read their settings from here.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ulduar.errors import ConfigurationError

CONFIG_VERSION = 1
DEFAULT_CONFIG_PATH = "ulduar.toml"
# The one definition of a project name or scope; the CHECK constraints of step 1 spell the same pattern.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
DEFAULT_SCOPE = "default"
# (default, lowest, highest) in seconds.
CONNECT_TIMEOUT_BOUNDS = (10, 1, 60)
STATEMENT_TIMEOUT_BOUNDS = (30, 1, 600)
LOCK_TIMEOUT_BOUNDS = (5, 1, 60)
CALL_BUDGET_BOUNDS = (60, 1, 3600)


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
class Config:
    """A validated configuration and the file it came from."""

    project: ProjectSettings
    archivum: ArchivumSettings
    path: Path


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


def _seconds(table: Mapping[str, Any], key: str, prefix: str, bounds: tuple[int, int, int]) -> int:
    default, low, high = bounds
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"Configuration key `{prefix}{key}` must be an integer")
    if not low <= value <= high:
        raise ConfigurationError(f"Configuration key `{prefix}{key}` must be between {low} and {high} seconds")
    return value


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
    _refuse_unknown(raw, {"version", "project", "archivum"}, "")
    if "version" not in raw:
        raise ConfigurationError("Configuration key `version` is required")
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != CONFIG_VERSION:
        raise ConfigurationError(f"Configuration key `version` must be the integer {CONFIG_VERSION}")
    return Config(
        project=_project_settings(_table(raw, "project", "", required=True)),
        archivum=_archivum_settings(_table(raw, "archivum", "", required=True)),
        path=file,
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
        connect_timeout_s=_seconds(table, "connect_timeout_s", "archivum.", CONNECT_TIMEOUT_BOUNDS),
        statement_timeout_s=_seconds(table, "statement_timeout_s", "archivum.", STATEMENT_TIMEOUT_BOUNDS),
        lock_timeout_s=_seconds(table, "lock_timeout_s", "archivum.", LOCK_TIMEOUT_BOUNDS),
        call_budget_s=_seconds(table, "call_budget_s", "archivum.", CALL_BUDGET_BOUNDS),
    )
