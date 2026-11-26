"""Hodir's SQL against the Archivum, in one place: the address lock, the registration insert, the archived check
and the verification reads and writes.

Input: a transaction of the catalog session (`Catalog.transaction`) and the ids and checksums of one put, get or
verification. Output: the registration row of an archived artifact, whether an artifact's object is still archived,
the live addresses of a scope (all, or the sample a verification takes next) and the rows of a verification.
Private: Hodir's modules import it, a consumer never does. Every statement composes identifiers through
Freya's `qualified` and binds values as parameters (hard rule 3).
"""

from __future__ import annotations

from uuid import UUID

from psycopg import sql

from ulduar import _ids
from ulduar.errors import UlduarError
from ulduar.freya import Artifact, Connection
from ulduar.freya._records import qualified
from ulduar.hodir._records import Verification

_REGISTRATION = qualified("archive_registration")
_INSERT_REGISTRATION = sql.SQL(
    "INSERT INTO {} (project_id, scope, artifact_id, event_at, retention_class) VALUES (%s, %s, %s, %s, %s)"
    " ON CONFLICT (project_id, artifact_id) DO NOTHING RETURNING 1"
).format(_REGISTRATION)
_SELECT_CLASS = sql.SQL("SELECT retention_class FROM {} WHERE project_id = %s AND artifact_id = %s").format(
    _REGISTRATION
)


_IS_ARCHIVED = sql.SQL("SELECT {}(%s, %s)").format(qualified("artifact_is_archived"))
_LIVE = sql.SQL(
    "SELECT DISTINCT x.checksum, x.size_bytes FROM {registration} r"
    " JOIN {artifact} x ON x.project_id = r.project_id AND x.artifact_id = r.artifact_id"
    " WHERE r.project_id = %(project_id)s AND r.scope = %(scope)s AND {archived}(r.project_id, r.artifact_id)"
).format(registration=_REGISTRATION, artifact=qualified("artifact"), archived=qualified("artifact_is_archived"))
_SELECT_LIVE = sql.SQL("{} ORDER BY checksum").format(_LIVE)
# The sample: the live addresses whose latest passed finding (by the database's clock) is oldest, never-passed first.
_SELECT_SAMPLE = sql.SQL(
    "WITH live AS ({live}) SELECT l.checksum, l.size_bytes FROM live l"
    " LEFT JOIN LATERAL (SELECT max(f.recorded_at) AS last_passed FROM {finding} f"
    " WHERE f.project_id = %(project_id)s AND f.scope = %(scope)s AND f.checksum = l.checksum"
    " AND f.outcome = 'passed') p ON true"
    " ORDER BY p.last_passed NULLS FIRST, l.checksum LIMIT %(n)s"
).format(live=_LIVE, finding=qualified("archive_finding"))
_INSERT_CHECK = sql.SQL(
    "INSERT INTO {} (project_id, check_id, event_at, scope, kind, mode, objects_checked, findings)"
    " VALUES (%s, %s, %s, %s, 'verification', %s, %s, %s)"
).format(qualified("archive_check"))
_INSERT_FINDING = sql.SQL(
    "INSERT INTO {} (project_id, finding_id, event_at, check_id, scope, checksum, size_bytes, outcome)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
).format(qualified("archive_finding"))


def address_lock_name(project_id: UUID, scope: str, checksum: bytes) -> str:
    """The name of the advisory lock that orders a put and a deletion of one object (project, scope, content)."""
    return f"ulduar.archive:{project_id}:{scope}:{checksum.hex()}"


def lock_address(tx: Connection, project_id: UUID, scope: str, checksum: bytes) -> None:
    """Hold the address lock of the object until the transaction ends."""
    tx.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (address_lock_name(project_id, scope, checksum),)
    )


def is_archived(tx: Connection, project_id: UUID, artifact_id: UUID) -> bool:
    """True while the artifact is registered and no confirmed deletion of its content was recorded after that."""
    row = tx.execute(_IS_ARCHIVED, (project_id, artifact_id)).fetchone()
    return row is not None and bool(row[0])


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


def live_addresses(tx: Connection, project_id: UUID, scope: str) -> list[tuple[bytes, int]]:
    """The distinct (checksum, size) of the scope's registrations whose artifact is archived, by checksum."""
    rows = tx.execute(_SELECT_LIVE, {"project_id": project_id, "scope": scope}).fetchall()
    return [(row[0], row[1]) for row in rows]


def sample_addresses(tx: Connection, project_id: UUID, scope: str, n: int) -> list[tuple[bytes, int]]:
    """The *n* live addresses whose latest passed finding is oldest, those never passed first."""
    rows = tx.execute(_SELECT_SAMPLE, {"project_id": project_id, "scope": scope, "n": n}).fetchall()
    return [(row[0], row[1]) for row in rows]


def insert_check(tx: Connection, project_id: UUID, check: Verification) -> None:
    """Record *check*: one `archive_check` row and one `archive_finding` row per finding."""
    tx.execute(
        _INSERT_CHECK,
        (project_id, check.check_id, check.event_at, check.scope, check.mode, check.objects_checked, len(check.failed)),
    )
    with tx.cursor() as cursor:
        cursor.executemany(
            _INSERT_FINDING,
            [
                (
                    project_id,
                    f.finding_id,
                    _ids.uuid7_time(f.finding_id),
                    check.check_id,
                    f.scope,
                    f.checksum,
                    f.size,
                    f.outcome,
                )
                for f in check.findings
            ],
        )
