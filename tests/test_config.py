"""ulduar.config: a valid file loads with its defaults; every kind of invalid file names what is wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from ulduar.config import (
    CONFIG_VERSION,
    DEFAULT_CONFIG_PATH,
    DEFAULT_SCOPE,
    INCOMPLETE_PUT_GRACE_HOURS_BOUNDS,
    MAX_DELETIONS_BOUNDS,
    SPOOL_LIMIT_GIB_BOUNDS,
    ArchiveSettings,
    ArchivumSettings,
    CredentialSettings,
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


# --- the optional [archive] section ---

S3_ARCHIVE = """
[archive]
backend = "s3"
bucket = "sample-archive"
"""
DIRECTORY_ARCHIVE = """
[archive]
backend = "directory"
path = "/var/archive"
"""


def archive(text: str) -> str:
    return MINIMAL + text


def test_a_file_without_an_archive_section_stays_valid(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, MINIMAL)).archive is None


def test_an_s3_archive_gets_the_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, archive(S3_ARCHIVE)))
    assert config.archive == ArchiveSettings(backend="s3", bucket="sample-archive")
    assert config.archive is not None
    assert (config.archive.prefix, config.archive.spool_limit_gib) == ("ulduar", 100)
    assert (config.archive.incomplete_put_grace_hours, config.archive.max_deletions_per_run) == (168, 100)
    assert config.archive.writer is None and config.archive.deleter is None and config.archive.spool_dir is None


def test_a_directory_archive_gets_the_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, archive(DIRECTORY_ARCHIVE)))
    assert config.archive == ArchiveSettings(backend="directory", path=Path("/var/archive"))


def test_a_full_s3_archive_is_read(tmp_path: Path) -> None:
    text = archive(
        S3_ARCHIVE
        + """
prefix = "one/two.x"
region = "eu-central-1"
endpoint_url_env = "ARCHIVE_ENDPOINT"
proxy_env = "ARCHIVE_PROXY"
spool_dir = "/var/spool/ulduar"
spool_limit_gib = 100000
incomplete_put_grace_hours = 8760
max_deletions_per_run = 1000000
[archive.writer]
access_key_env = "WRITER_KEY"
secret_key_env = "WRITER_SECRET"
session_token_env = "WRITER_TOKEN"
[archive.deleter]
access_key_env = "DELETER_KEY"
secret_key_env = "DELETER_SECRET"
"""
    )
    assert load_config(write(tmp_path, text)).archive == ArchiveSettings(
        backend="s3",
        bucket="sample-archive",
        prefix="one/two.x",
        region="eu-central-1",
        endpoint_url_env="ARCHIVE_ENDPOINT",
        proxy_env="ARCHIVE_PROXY",
        spool_dir=Path("/var/spool/ulduar"),
        spool_limit_gib=100000,
        incomplete_put_grace_hours=8760,
        max_deletions_per_run=1000000,
        writer=CredentialSettings("WRITER_KEY", "WRITER_SECRET", "WRITER_TOKEN"),
        deleter=CredentialSettings("DELETER_KEY", "DELETER_SECRET"),
    )


def test_the_archive_bounds_are_default_low_high() -> None:
    assert SPOOL_LIMIT_GIB_BOUNDS == (100, 1, 100000)
    assert INCOMPLETE_PUT_GRACE_HOURS_BOUNDS == (168, 24, 8760)
    assert MAX_DELETIONS_BOUNDS == (100, 1, 1000000)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (S3_ARCHIVE + "extra = 1\n", "archive.extra"),
        (
            S3_ARCHIVE + '[archive.writer]\naccess_key_env = "A"\nsecret_key_env = "B"\nowner = "x"\n',
            "archive.writer.owner",
        ),
        (
            S3_ARCHIVE + '[archive.deleter]\naccess_key_env = "A"\nsecret_key_env = "B"\nowner = "x"\n',
            "archive.deleter.owner",
        ),
    ],
)
def test_an_unknown_archive_key_is_refused_by_its_dotted_path(tmp_path: Path, text: str, key: str) -> None:
    refused(tmp_path, archive(text), f"`{key}`")


def test_the_archive_must_be_a_table_and_name_its_backend(tmp_path: Path) -> None:
    refused(tmp_path, "archive = 3\n" + MINIMAL, "`archive`", "table")
    refused(tmp_path, archive("[archive]\n"), "`archive.backend`", "required")
    for backend in ('"gcs"', "3", '"S3"'):
        refused(tmp_path, archive(f"[archive]\nbackend = {backend}\n"), "`archive.backend`", "s3, directory")


@pytest.mark.parametrize("key", ["bucket", "region", "endpoint_url_env", "proxy_env"])
def test_an_s3_only_key_is_refused_on_a_directory_archive(tmp_path: Path, key: str) -> None:
    value = {"bucket": "sample-archive", "region": "eu-west-1"}.get(key, "SOME_ENV")
    text = archive(DIRECTORY_ARCHIVE + f'{key} = "{value}"\n')
    refused(tmp_path, text, f"`archive.{key}` applies to the s3 backend only")


@pytest.mark.parametrize("table", ["writer", "deleter"])
def test_a_credential_table_is_refused_on_a_directory_archive(tmp_path: Path, table: str) -> None:
    text = archive(DIRECTORY_ARCHIVE + f'[archive.{table}]\naccess_key_env = "A"\nsecret_key_env = "B"\n')
    refused(tmp_path, text, f"`archive.{table}` applies to the s3 backend only")


def test_a_path_is_refused_on_an_s3_archive(tmp_path: Path) -> None:
    refused(
        tmp_path,
        archive(S3_ARCHIVE + 'path = "/var/archive"\n'),
        "`archive.path` applies to the directory backend only",
    )


def test_a_directory_archive_needs_an_absolute_path(tmp_path: Path) -> None:
    refused(tmp_path, archive('[archive]\nbackend = "directory"\n'), "`archive.path`", "required")
    for path in ('"relative/dir"', '""', "5"):
        refused(tmp_path, archive(f'[archive]\nbackend = "directory"\npath = {path}\n'), "`archive.path`")


def test_an_s3_archive_needs_a_bucket(tmp_path: Path) -> None:
    refused(tmp_path, archive('[archive]\nbackend = "s3"\n'), "`archive.bucket`", "required")


@pytest.mark.parametrize("bucket", ["", "a", "Upper-case", "-lead", "trail-", "has_underscore", "x" * 64, "a/b", "5"])
def test_a_bad_bucket_name_is_refused(tmp_path: Path, bucket: str) -> None:
    refused(tmp_path, archive(f'[archive]\nbackend = "s3"\nbucket = "{bucket}"\n'), "`archive.bucket`")


@pytest.mark.parametrize("bucket", ["abc", "a.b-c", "x" * 63, "1" * 3])
def test_a_valid_bucket_name_is_accepted(tmp_path: Path, bucket: str) -> None:
    assert load_config(write(tmp_path, archive(f'[archive]\nbackend = "s3"\nbucket = "{bucket}"\n'))).archive


@pytest.mark.parametrize("prefix", ["a//b", "/a", "a/", "A", "a/../b", "a b", "a\\\\b", "x" * 64, "-a", 5])
def test_a_bad_prefix_is_refused(tmp_path: Path, prefix: str | int) -> None:
    value = f'"{prefix}"' if isinstance(prefix, str) else prefix
    refused(tmp_path, archive(S3_ARCHIVE + f"prefix = {value}\n"), "`archive.prefix`")


@pytest.mark.parametrize("prefix", ["", "a", "a/b/c", "a.b/c_d/e-f"])
def test_an_empty_or_segmented_prefix_is_accepted(tmp_path: Path, prefix: str) -> None:
    config = load_config(write(tmp_path, archive(S3_ARCHIVE + f'prefix = "{prefix}"\n')))
    assert config.archive is not None and config.archive.prefix == prefix


@pytest.mark.parametrize("region", ["", "US-EAST-1", "us east", "x" * 33, "eu_west"])
def test_a_bad_region_is_refused(tmp_path: Path, region: str) -> None:
    refused(tmp_path, archive(S3_ARCHIVE + f'region = "{region}"\n'), "`archive.region`")


@pytest.mark.parametrize("key", ["endpoint_url_env", "proxy_env"])
@pytest.mark.parametrize("env", ["", "lower", "1ABC", "A-B"])
def test_a_bad_archive_env_name_is_refused(tmp_path: Path, key: str, env: str) -> None:
    refused(tmp_path, archive(S3_ARCHIVE + f'{key} = "{env}"\n'), f"`archive.{key}`")


@pytest.mark.parametrize("key", ["access_key_env", "secret_key_env", "session_token_env"])
def test_a_bad_credential_env_name_is_refused(tmp_path: Path, key: str) -> None:
    keys = {"access_key_env": "A", "secret_key_env": "B", "session_token_env": "C", key: "lower"}
    body = "\n".join(f'{name} = "{value}"' for name, value in keys.items())
    refused(tmp_path, archive(S3_ARCHIVE + f"[archive.writer]\n{body}\n"), f"`archive.writer.{key}`")


@pytest.mark.parametrize("table", ["writer", "deleter"])
@pytest.mark.parametrize("missing", ["access_key_env", "secret_key_env"])
def test_a_credential_names_its_access_and_secret_variables(tmp_path: Path, table: str, missing: str) -> None:
    keys = {"access_key_env": "A", "secret_key_env": "B"}
    del keys[missing]
    body = "\n".join(f'{name} = "{value}"' for name, value in keys.items())
    refused(tmp_path, archive(S3_ARCHIVE + f"[archive.{table}]\n{body}\n"), f"`archive.{table}.{missing}`", "required")


def test_a_credential_is_a_table(tmp_path: Path) -> None:
    refused(
        tmp_path,
        archive('[archive]\nbackend = "s3"\nbucket = "sample-archive"\nwriter = 3\n'),
        "`archive.writer`",
        "table",
    )


def test_the_writer_and_the_deleter_must_be_different_credentials(tmp_path: Path) -> None:
    def credentials(deleter_key: str) -> str:
        return (
            '[archive.writer]\naccess_key_env = "K"\nsecret_key_env = "S1"\n'
            f'[archive.deleter]\naccess_key_env = "{deleter_key}"\nsecret_key_env = "S2"\n'
        )

    message = "`archive.writer` and `archive.deleter` must name different credentials"
    refused(tmp_path, archive(S3_ARCHIVE + credentials("K")), message)
    config = load_config(write(tmp_path, archive(S3_ARCHIVE + credentials("D"))))
    assert config.archive is not None and config.archive.deleter == CredentialSettings("D", "S2")


@pytest.mark.parametrize("key", ["spool_dir"])
@pytest.mark.parametrize("path", ['"relative"', '""', "5"])
def test_a_spool_dir_must_be_an_absolute_path(tmp_path: Path, key: str, path: str) -> None:
    refused(tmp_path, archive(DIRECTORY_ARCHIVE + f"{key} = {path}\n"), f"`archive.{key}`")


@pytest.mark.parametrize(
    ("key", "bad", "unit"),
    [
        ("spool_limit_gib", "0", "between 1 and 100000 GiB"),
        ("spool_limit_gib", "100001", "between 1 and 100000 GiB"),
        ("incomplete_put_grace_hours", "23", "between 24 and 8760 hours"),
        ("incomplete_put_grace_hours", "8761", "between 24 and 8760 hours"),
        ("max_deletions_per_run", "0", "between 1 and 1000000"),
        ("max_deletions_per_run", "1000001", "between 1 and 1000000"),
        ("spool_limit_gib", "true", "integer"),
        ("incomplete_put_grace_hours", "2.5", "integer"),
        ("max_deletions_per_run", '"100"', "integer"),
    ],
)
def test_archive_limits_must_be_integers_within_bounds(tmp_path: Path, key: str, bad: str, unit: str) -> None:
    refused(tmp_path, archive(S3_ARCHIVE + f"{key} = {bad}\n"), f"`archive.{key}`", unit)


def test_the_deletion_count_message_has_no_unit(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_config(write(tmp_path, archive(S3_ARCHIVE + "max_deletions_per_run = 0\n")))
    assert str(caught.value).endswith("between 1 and 1000000")


def test_the_seconds_messages_are_unchanged(tmp_path: Path) -> None:
    refused(tmp_path, MINIMAL + "call_budget_s = 3601\n", "`archivum.call_budget_s` must be between 1 and 3600 seconds")
