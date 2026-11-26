"""ulduar.config: a valid file loads with its defaults; every kind of invalid file names what is wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from ulduar.config import (
    CONFIG_VERSION,
    DEFAULT_CONFIG_PATH,
    DEFAULT_SCOPE,
    ArchivumSettings,
    load_config,
)
from ulduar.errors import ConfigurationError

MINIMAL = """
version = 1
[project]
name = "sample"
[archivum]
dsn_env = "SAMPLE_DSN"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ulduar.toml"
    path.write_text(text, encoding="utf-8")
    return path


def refused(tmp_path: Path, text: str, *needles: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_config(write(tmp_path, text))
    for needle in needles:
        assert needle in str(caught.value)


def test_minimal_file_gets_the_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL))
    assert config.project.name == "sample"
    assert config.project.scope == DEFAULT_SCOPE
    assert config.archivum == ArchivumSettings(dsn_env="SAMPLE_DSN")
    assert (config.archivum.connect_timeout_s, config.archivum.statement_timeout_s) == (10, 30)
    assert (config.archivum.lock_timeout_s, config.archivum.call_budget_s) == (5, 60)
    assert config.archivum.ledger_dsn_env is None
    assert config.path == tmp_path / "ulduar.toml"


def test_full_file_is_read(tmp_path: Path) -> None:
    text = MINIMAL.replace('dsn_env = "SAMPLE_DSN"', 'dsn_env = "SAMPLE_DSN"\nledger_dsn_env = "SAMPLE_LEDGER_DSN"')
    text = text.replace('name = "sample"', 'name = "sample"\nscope = "weekly.a-b_c"')
    text += "connect_timeout_s = 1\nstatement_timeout_s = 600\nlock_timeout_s = 60\ncall_budget_s = 3600\n"
    config = load_config(write(tmp_path, text))
    assert config.project.scope == "weekly.a-b_c"
    assert config.archivum.ledger_dsn_env == "SAMPLE_LEDGER_DSN"
    assert config.archivum.call_budget_s == 3600


def test_default_path_is_relative_to_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, MINIMAL)
    monkeypatch.chdir(tmp_path)
    assert load_config().path == Path(DEFAULT_CONFIG_PATH)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("extra = 1\n" + MINIMAL, "extra"),
        (MINIMAL.replace('name = "sample"', 'name = "sample"\nowner = "x"'), "project.owner"),
        (MINIMAL.replace('dsn_env = "SAMPLE_DSN"', 'dsn_env = "SAMPLE_DSN"\nwait_s = 3'), "archivum.wait_s"),
        (MINIMAL.replace('dsn_env = "SAMPLE_DSN"', 'dsn = "x"'), "archivum.dsn"),
    ],
)
def test_unknown_key_is_refused_by_its_dotted_path(tmp_path: Path, text: str, key: str) -> None:
    refused(tmp_path, text, f"`{key}`")


@pytest.mark.parametrize("version", ["", "version = 2", "version = true", 'version = "1"', "version = 1.0"])
def test_version_must_be_the_integer_one(tmp_path: Path, version: str) -> None:
    refused(tmp_path, MINIMAL.replace("version = 1", version), "`version`")
    assert CONFIG_VERSION == 1


def test_required_table_is_named(tmp_path: Path) -> None:
    refused(tmp_path, 'version = 1\n[archivum]\ndsn_env = "X"\n', "`project`", "required")
    refused(tmp_path, 'version = 1\n[project]\nname = "x"\n', "`archivum`", "required")


@pytest.mark.parametrize("name", ["", "Upper", "-lead", "a b", "a/b", "x" * 64])
def test_bad_project_name_or_scope_is_refused(tmp_path: Path, name: str) -> None:
    refused(tmp_path, MINIMAL.replace("sample", name, 1), "`project.name`")
    refused(tmp_path, MINIMAL.replace('name = "sample"', f'name = "sample"\nscope = "{name}"'), "`project.scope`")


def test_name_longest_allowed_is_accepted(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, MINIMAL.replace("sample", "x" * 63, 1))).project.name == "x" * 63


@pytest.mark.parametrize("env", ["", "lower", "1ABC", "A-B", "A B"])
def test_bad_env_name_is_refused(tmp_path: Path, env: str) -> None:
    refused(tmp_path, MINIMAL.replace("SAMPLE_DSN", env), "`archivum.dsn_env`")
    text = MINIMAL.replace('dsn_env = "SAMPLE_DSN"', f'dsn_env = "SAMPLE_DSN"\nledger_dsn_env = "{env}"')
    refused(tmp_path, text, "`archivum.ledger_dsn_env`")


def test_missing_and_mistyped_keys_are_named(tmp_path: Path) -> None:
    refused(tmp_path, MINIMAL.replace('dsn_env = "SAMPLE_DSN"', ""), "`archivum.dsn_env`", "required")
    refused(tmp_path, MINIMAL.replace('name = "sample"', ""), "`project.name`", "required")
    refused(tmp_path, MINIMAL.replace('"SAMPLE_DSN"', "5"), "`archivum.dsn_env`", "string")
    refused(tmp_path, 'version = 1\nproject = 3\n[archivum]\ndsn_env = "X"\n', "`project`", "table")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("connect_timeout_s", "0"),
        ("connect_timeout_s", "61"),
        ("statement_timeout_s", "0"),
        ("statement_timeout_s", "601"),
        ("lock_timeout_s", "0"),
        ("lock_timeout_s", "61"),
        ("call_budget_s", "0"),
        ("call_budget_s", "3601"),
        ("call_budget_s", "true"),
        ("lock_timeout_s", "false"),
        ("statement_timeout_s", "2.5"),
        ("connect_timeout_s", '"10"'),
    ],
)
def test_timeouts_must_be_integers_within_bounds(tmp_path: Path, key: str, bad: str) -> None:
    refused(tmp_path, MINIMAL + f"{key} = {bad}\n", f"`archivum.{key}`")


def test_missing_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist") as caught:
        load_config(tmp_path / "absent.toml")
    assert "absent.toml" in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_unreadable_path_is_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="could not be read") as caught:
        load_config(tmp_path)  # a directory
    assert str(tmp_path) in str(caught.value)


def test_toml_syntax_error_is_named(tmp_path: Path) -> None:
    path = write(tmp_path, "version = \n")
    with pytest.raises(ConfigurationError, match="not valid TOML") as caught:
        load_config(path)
    assert str(path) in str(caught.value)
