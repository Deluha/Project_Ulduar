"""Hodir's SQL against the Archivum, in one place: the address lock and the registration insert.

Input: a transaction of the catalog session (`Catalog.transaction`) and the ids and checksums of one put. Output:
the registration row of an archived artifact. Private: Hodir's modules import it, a consumer never does. Every
statement composes identifiers through Freya's `qualified` and binds values as parameters (hard rule 3).
"""

from __future__ import annotations

from uuid import UUID

from psycopg import sql

from ulduar.errors import UlduarError
from ulduar.freya import Artifact, Connection
from ulduar.freya._records import qualified

_REGISTRATION = qualified("archive_registration")
_INSERT_REGISTRATION = sql.SQL(
    "INSERT INTO {} (project_id, scope, artifact_id, event_at, retention_class) VALUES (%s, %s, %s, %s, %s)"
    " ON CONFLICT (project_id, artifact_id) DO NOTHING RETURNING 1"
).format(_REGISTRATION)
_SELECT_CLASS = sql.SQL("SELECT retention_class FROM {} WHERE project_id = %s AND artifact_id = %s").format(
    _REGISTRATION
)


def address_lock_name(project_id: UUID, scope: str, checksum: bytes) -> str:
    """The name of the advisory lock that orders a put and a deletion of one object (project, scope, content)."""
    return f"ulduar.archive:{project_id}:{scope}:{checksum.hex()}"


def lock_address(tx: Connection, project_id: UUID, scope: str, checksum: bytes) -> None:
    """Hold the address lock of the object until the transaction ends."""
    tx.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (address_lock_name(project_id, scope, checksum),)
    )


def insert_registration(
    tx: Connection, project_id: UUID, scope: str, artifact: Artifact, retention_class: str | None
) -> None:
    """Register *artifact*'s object; registering it again with the same retention class changes nothing.

    `UlduarError` when the artifact is already registered with another class: a class is fixed at archive time.
    """
    params = (project_id, scope, artifact.artifact_id, artifact.event_at, retention_class)
    if tx.execute(_INSERT_REGISTRATION, params).fetchone() is not None:
        return
    row = tx.execute(_SELECT_CLASS, (project_id, artifact.artifact_id)).fetchone()
    if row is None or row[0] != retention_class:
        old = None if row is None else row[0]
        raise UlduarError(f"Artifact {artifact.artifact_id} is already archived with retention class {old!r}")
