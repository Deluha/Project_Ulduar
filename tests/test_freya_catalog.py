"""ulduar.freya.catalog: the catalog session on a real Archivum — registration, lookup, isolation, append-only.

The helpers `onboarded`, `ms_ago` and `partition_exists` are shared by the history and lineage tests.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from tests.conftest import Archivum, Recorder
from ulduar import _db, _ids, freya
from ulduar.config import Config
from ulduar.errors import (
    ArchivumUnavailable,
    IsolationError,
    SchemaVersionError,
    UlduarError,
    UnknownRecordError,
)
from ulduar.freya import ARTIFACT_KINDS, Catalog, Dataset, NodeRef, install, onboard_project, open_catalog

INSUFFICIENT_PRIVILEGE = "42501"
CHECKSUM = hashlib.sha256(b"content").digest()


def digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def ms_ago(days: int) -> int:
    """Unix milliseconds of *days* ago, for a record id in an old month."""
    return int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)


def onboarded(archivum: Archivum, tmp_path: Path, project: str = "alpha") -> Config:
    """A config for *project* with a new recording role, the Archivum installed and the role onboarded."""
    directory = tmp_path / project
    directory.mkdir(exist_ok=True)
    config = archivum.config(directory, project=project)
    install(config)
    onboard_project(config, project, archivum.recorders[-1].role)
    return config


def recorder_of(archivum: Archivum, config: Config) -> Recorder:
    return next(r for r in archivum.recorders if r.env == config.archivum.dsn_env)


def partition_exists(archivum: Archivum, table: str, unix_ms: int) -> bool:
    moment = datetime.fromtimestamp(unix_ms / 1000, UTC)
    with archivum.superuser() as su:
        found = su.execute("SELECT to_regclass(%s)", (f"ulduar.{table}_{moment:%Y_%m}",)).fetchone()
    assert found is not None
    return found[0] is not None


def failure(config: Config, env: str, statement: str, params: tuple[object, ...] = ()) -> psycopg.Error:
    """The driver error of *statement* run in a transaction of its own on the connection named by *env*."""
    with _db.open_connection(env, config.archivum) as conn:
        with pytest.raises(psycopg.Error) as caught, _db.transaction(conn, config.archivum) as inside:
            inside.execute(statement, params)
    return caught.value


# --- opening ---


def test_open_catalog_after_onboarding_holds_the_project_and_scope(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    with open_catalog(config) as catalog:
        assert isinstance(catalog, Catalog)
        assert (catalog.project.name, catalog.scope) == ("alpha", "default")
        assert isinstance(catalog.project.project_id, uuid.UUID)
        assert catalog.project.project_id.version == 7


def test_the_connection_is_closed_when_the_session_ends(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        pass
    with pytest.raises(ArchivumUnavailable):
        catalog.get_dataset("ns", "name")


def test_opening_before_install_is_a_schema_version_error(archivum: Archivum, tmp_path: Path) -> None:
    with pytest.raises(SchemaVersionError, match="not installed"), open_catalog(archivum.config(tmp_path)):
        pass


def test_a_role_of_another_project_is_not_onboarded_for_this_one(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    other = dataclasses.replace(config, project=dataclasses.replace(config.project, name="beta"))
    with pytest.raises(UnknownRecordError, match="Project beta is not onboarded for this role"), open_catalog(other):
        pass


def test_a_role_that_was_never_granted_the_recording_group_is_refused_by_the_database(
    archivum: Archivum, tmp_path: Path
) -> None:
    config = archivum.config(tmp_path)
    install(config)
    with pytest.raises(IsolationError, match="permission denied"), open_catalog(config):
        pass


def test_a_scope_the_project_lacks_is_unknown(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    other = dataclasses.replace(config, project=dataclasses.replace(config.project, scope="elsewhere"))
    with pytest.raises(UnknownRecordError, match="Scope elsewhere"), open_catalog(other):
        pass


def test_the_facade_exports_every_name_it_lists() -> None:
    assert all(hasattr(freya, name) for name in freya.__all__)
    assert len(set(freya.__all__)) == len(freya.__all__)


def test_artifact_kinds_equal_the_rows_of_the_schema(archivum: Archivum, tmp_path: Path) -> None:
    install(archivum.config(tmp_path))
    with archivum.superuser() as su:
        rows = su.execute("SELECT kind FROM ulduar.artifact_kind").fetchall()
    assert sorted(r[0] for r in rows) == sorted(ARTIFACT_KINDS)


# --- datasets ---


def test_registering_a_dataset_twice_returns_the_same_dataset(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        first = catalog.register_dataset("warehouse", "orders")
        again = catalog.register_dataset("warehouse", "orders")
        other_scope = catalog.register_dataset("warehouse", "orders", scope="default")
        assert again == first == other_scope
        assert catalog.get_dataset("warehouse", "orders") == first
        assert first.ref == NodeRef("dataset", first.dataset_id)
        assert first.event_at == _ids.uuid7_time(first.dataset_id)
        assert (first.project_id, first.scope) == (catalog.project.project_id, "default")
        assert catalog.register_dataset("warehouse", "customers") != first


def test_a_missing_dataset_or_scope_is_unknown(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        with pytest.raises(UnknownRecordError, match="warehouse/orders"):
            catalog.get_dataset("warehouse", "orders")
        with pytest.raises(UnknownRecordError):
            catalog.register_dataset("warehouse", "orders", scope="nowhere")
        catalog.register_dataset("warehouse", "orders")  # the session survives a refused call


# --- artifacts ---


def test_an_artifact_round_trips_and_is_found_by_checksum_kind_and_dataset(archivum: Archivum, tmp_path: Path) -> None:
    base = ms_ago(1)
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        dataset = catalog.register_dataset("warehouse", "orders")
        report = catalog.register_artifact(
            "report", CHECKSUM, 10, origin="out/report.pdf", dataset=dataset, record_id=_ids.uuid7(base)
        )
        dump = catalog.register_artifact("database_dump", digest("dump"), 0, record_id=_ids.uuid7(base + 1))
        raw = catalog.register_artifact("raw_file", CHECKSUM, 3, dataset=dataset, record_id=_ids.uuid7(base + 2))
        assert catalog.get_artifact(report.artifact_id) == report
        assert (report.kind, report.checksum, report.size, report.origin) == ("report", CHECKSUM, 10, "out/report.pdf")
        assert (report.dataset_id, report.scope, report.project_id) == (
            dataset.dataset_id,
            "default",
            catalog.project.project_id,
        )
        assert report.ref == NodeRef("artifact", report.artifact_id)
        assert (dump.origin, dump.dataset_id) == (None, None)
        assert catalog.find_artifacts() == [report, dump, raw]
        assert catalog.find_artifacts(checksum=CHECKSUM) == [report, raw]
        assert catalog.find_artifacts(kind="database_dump") == [dump]
        assert catalog.find_artifacts(dataset=dataset) == [report, raw]
        assert catalog.find_artifacts(checksum=CHECKSUM, kind="raw_file", dataset=dataset) == [raw]
        assert catalog.find_artifacts(kind="model") == []
        assert catalog.find_artifacts(scope="nowhere") == []


def test_a_retry_with_the_same_id_and_content_returns_the_recorded_artifact(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        record_id = _ids.uuid7()
        first = catalog.register_artifact("model", CHECKSUM, 5, origin="m.bin", record_id=record_id)
        assert catalog.register_artifact("model", CHECKSUM, 5, origin="m.bin", record_id=record_id) == first
        assert len(catalog.find_artifacts()) == 1


@pytest.mark.parametrize(
    "changed",
    [{"kind": "log"}, {"checksum": digest("other")}, {"size": 6}, {"origin": "n.bin"}, {"origin": None}],
)
def test_a_retry_with_the_same_id_and_other_content_is_refused(
    archivum: Archivum, tmp_path: Path, changed: dict[str, object]
) -> None:
    content: dict[str, object] = {"kind": "model", "checksum": CHECKSUM, "size": 5, "origin": "m.bin"}
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        record_id = _ids.uuid7()
        first = catalog.register_artifact(**content, record_id=record_id)  # type: ignore[arg-type]
        with pytest.raises(UlduarError, match=f"Artifact {record_id} is already recorded with different content"):
            catalog.register_artifact(**{**content, **changed}, record_id=record_id)  # type: ignore[arg-type]
        assert catalog.get_artifact(record_id) == first


def test_a_retry_naming_another_dataset_is_refused(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        record_id = _ids.uuid7()
        catalog.register_artifact("log", CHECKSUM, 1, dataset=catalog.register_dataset("a", "b"), record_id=record_id)
        with pytest.raises(UlduarError, match="different content"):
            catalog.register_artifact("log", CHECKSUM, 1, record_id=record_id)


def test_an_old_month_without_a_partition_is_created_and_the_insert_retried(
    archivum: Archivum, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    long_ago = ms_ago(430)
    config = onboarded(archivum, tmp_path)
    assert not partition_exists(archivum, "artifact", long_ago)
    caplog.set_level("INFO", logger="ulduar.freya.catalog")
    with open_catalog(config) as catalog:
        old = catalog.register_artifact("log", CHECKSUM, 1, record_id=_ids.uuid7(long_ago))
        assert catalog.get_artifact(old.artifact_id) == old
        assert catalog.register_artifact("log", CHECKSUM, 1, record_id=old.artifact_id) == old
    assert partition_exists(archivum, "artifact", long_ago)
    assert sum("created the time partitions" in r.getMessage() for r in caplog.records) == 1


def test_a_driver_error_other_than_a_missing_partition_is_raised_as_the_package_error(
    archivum: Archivum, tmp_path: Path
) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        now = datetime.now(UTC)
        ghost = Dataset(uuid.uuid4(), catalog.project.project_id, "default", "x", "y", now, now)
        with pytest.raises(UnknownRecordError, match="^Registering artifact .* failed"):
            catalog.register_artifact("log", CHECKSUM, 1, dataset=ghost)
        with pytest.raises(UnknownRecordError):
            catalog.register_artifact("log", CHECKSUM, 1, scope="nowhere")


def test_a_missing_artifact_is_unknown(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        missing = _ids.uuid7()
        with pytest.raises(UnknownRecordError, match=str(missing)):
            catalog.get_artifact(missing)


# --- bad arguments, refused before any SQL ---


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"kind": "spreadsheet"}, "'spreadsheet'"),
        ({"checksum": b"\x00" * 31}, "31 bytes"),
        ({"checksum": "0" * 32}, "a str"),
        ({"size": -1}, "-1"),
        ({"size": True}, "True"),
        ({"size": 1.5}, "1.5"),
        ({"origin": ""}, "origin.*0 characters"),
        ({"origin": "x" * 1001}, "origin.*1001 characters"),
        ({"record_id": uuid.UUID(int=0)}, "not a version 7 UUID"),
        ({"record_id": uuid.uuid4()}, "not a version 7 UUID"),
        ({"scope": "Not A Scope"}, "'Not A Scope'"),
        ({"scope": ""}, "''"),
    ],
)
def test_register_artifact_refuses_bad_arguments(
    archivum: Archivum, tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    valid: dict[str, object] = {"kind": "log", "checksum": CHECKSUM, "size": 1}
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        with pytest.raises(ValueError, match=message):
            catalog.register_artifact(**{**valid, **arguments})  # type: ignore[arg-type]
        assert catalog.find_artifacts() == []


@pytest.mark.parametrize(
    ("namespace", "name", "scope", "message"),
    [
        ("", "n", None, "namespace.*0 characters"),
        ("x" * 201, "n", None, "namespace.*201 characters"),
        ("ns", "", None, "name.*0 characters"),
        ("ns", "x" * 501, None, "name.*501 characters"),
        ("ns", "n", "UPPER", "'UPPER'"),
    ],
)
def test_register_dataset_refuses_bad_arguments(
    archivum: Archivum, tmp_path: Path, namespace: str, name: str, scope: str | None, message: str
) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        with pytest.raises(ValueError, match=message):
            catalog.register_dataset(namespace, name, scope=scope)


def test_find_artifacts_refuses_bad_filters(archivum: Archivum, tmp_path: Path) -> None:
    with open_catalog(onboarded(archivum, tmp_path)) as catalog:
        with pytest.raises(ValueError, match="'nothing'"):
            catalog.find_artifacts(kind="nothing")
        with pytest.raises(ValueError, match="checksum"):
            catalog.find_artifacts(checksum=b"short")
        with pytest.raises(ValueError, match="'No'"):
            catalog.find_artifacts(scope="No")


# --- isolation between projects ---


def test_a_project_cannot_see_or_write_another_projects_records(archivum: Archivum, tmp_path: Path) -> None:
    config_a = onboarded(archivum, tmp_path, "alpha")
    config_b = onboarded(archivum, tmp_path, "beta")
    with open_catalog(config_a) as catalog_a:
        dataset = catalog_a.register_dataset("warehouse", "orders")
        artifact = catalog_a.register_artifact("report", CHECKSUM, 1, dataset=dataset)
        project_a = catalog_a.project.project_id
    with open_catalog(config_b) as catalog_b:
        assert catalog_b.project.project_id != project_a
        assert catalog_b.find_artifacts() == []
        assert catalog_b.find_artifacts(checksum=CHECKSUM, kind="report", dataset=dataset) == []
        with pytest.raises(UnknownRecordError):
            catalog_b.get_artifact(artifact.artifact_id)
        with pytest.raises(UnknownRecordError):
            catalog_b.get_dataset("warehouse", "orders")
        with pytest.raises(UnknownRecordError):  # A's dataset id is not a dataset of B
            catalog_b.register_artifact("log", CHECKSUM, 1, dataset=dataset)
        assert catalog_b.register_dataset("warehouse", "orders").dataset_id != dataset.dataset_id
    # B's role names A's project: the database shows it no such project.
    as_a = dataclasses.replace(config_b, project=config_a.project)
    with pytest.raises(UnknownRecordError, match="Project alpha is not onboarded for this role"), open_catalog(as_a):
        pass
    # A raw connection of B's role cannot insert under A's project id either.
    insert = (
        "INSERT INTO ulduar.dataset (project_id, scope, dataset_id, namespace, name, event_at)"
        " VALUES (%s, 'default', %s, 'n', 'n', %s)"
    )
    new_id = _ids.uuid7()
    error = failure(config_b, recorder_of(archivum, config_b).env, insert, (project_a, new_id, _ids.uuid7_time(new_id)))
    assert error.sqlstate == INSUFFICIENT_PRIVILEGE
    assert isinstance(_db.translate(error, "Recording"), IsolationError)


# --- append-only, through the recording role's own connection and the ledger role's ---

REFUSED_STATEMENTS = [
    "UPDATE ulduar.artifact SET origin = 'x'",
    "DELETE FROM ulduar.artifact",
    "TRUNCATE ulduar.artifact",
    "UPDATE ulduar.dataset SET name = 'x'",
    "DELETE FROM ulduar.dataset",
    "TRUNCATE ulduar.dataset",
]


def test_the_recording_role_cannot_update_delete_or_truncate_history(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    with open_catalog(config) as catalog:
        catalog.register_artifact("log", CHECKSUM, 1, origin="a", dataset=catalog.register_dataset("n", "d"))
    env = recorder_of(archivum, config).env
    for statement in REFUSED_STATEMENTS:
        error = failure(config, env, statement)
        assert error.sqlstate == INSUFFICIENT_PRIVILEGE, statement
        assert isinstance(_db.translate(error, "Recording"), IsolationError), statement


def test_the_ledger_role_outside_maintenance_mode_is_refused_by_the_trigger(archivum: Archivum, tmp_path: Path) -> None:
    config = onboarded(archivum, tmp_path)
    with open_catalog(config) as catalog:
        catalog.register_artifact("log", CHECKSUM, 1, origin="a", dataset=catalog.register_dataset("n", "d"))
    assert config.archivum.ledger_dsn_env
    # A truncate of `dataset` is refused earlier still (artifacts reference it), so it is not the trigger's to show.
    for statement in [s for s in REFUSED_STATEMENTS if s != "TRUNCATE ulduar.dataset"] + ["TRUNCATE ulduar.redaction"]:
        error = failure(config, config.archivum.ledger_dsn_env, statement)
        assert error.sqlstate == INSUFFICIENT_PRIVILEGE, statement
        assert "append-only" in (error.diag.message_primary or "")
