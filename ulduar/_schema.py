"""What Freya and Hodir share about the Archivum's schema: its names, vocabularies, validators and locks.

Private, below every component beside `_db` and `_ids`: a component imports it, never another component's private
module for these. `qualified` is the one home of the schema name; every composed statement reaches a table or
function of the Archivum through it. The vocabularies (artifact kinds, histories, redactable fields, reasons) are
the sets the schema's CHECK constraints and the `artifact_kind` rows of step 1 and step 2 spell, held equal by tests.
"""

from __future__ import annotations

from typing import Literal, get_args
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from ulduar.errors import UnknownRecordError

SCHEMA = "ulduar"

Connection = psycopg.Connection[TupleRow]

RetentionHistory = Literal["artifact", "lineage_edge", "archive_check", "archive_finding"]
RedactionHistory = Literal["artifact", "dataset"]
RedactionReason = Literal["personal_data", "secret", "recorded_by_mistake"]
# The histories that age out, the (history, field) pairs that may be redacted, and why: the schema's
# CHECK constraints spell the same sets.
HISTORIES: tuple[str, ...] = get_args(RetentionHistory)
REDACTABLE_FIELDS: dict[str, tuple[str, ...]] = {"artifact": ("origin",), "dataset": ("name", "namespace")}
REDACTION_REASONS: tuple[str, ...] = get_args(RedactionReason)

# The rows of `ulduar.artifact_kind` (step 1 is the schema's home; a test holds the two equal).
ARTIFACT_KINDS = ("raw_file", "database_dump", "report", "model", "log")
CHECKSUM_ALGORITHM = "sha256"
CHECKSUM_BYTES = 32
NAMESPACE_MAX = 200
NAME_MAX = 500
ORIGIN_MAX = 1000


def qualified(name: str) -> sql.Identifier:
    """The table or function *name* of schema `ulduar`, quoted."""
    return sql.Identifier(SCHEMA, name)


ARCHIVE_REGISTRATION = qualified("archive_registration")
ARTIFACT_IS_ARCHIVED = qualified("artifact_is_archived")
_SELECT_PROJECT_ID = sql.SQL("SELECT project_id FROM {} WHERE name = %s").format(qualified("project"))


def advisory_xact_lock(tx: Connection, name: str) -> None:
    """Hold the advisory lock called *name* until the transaction ends (the lock key is the text's 64-bit hash)."""
    tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))


def lookup_project_id(tx: Connection, project: str) -> UUID:
    """The id of the onboarded *project*; `UnknownRecordError` when there is none."""
    row = tx.execute(_SELECT_PROJECT_ID, (project,)).fetchone()
    if row is None:
        raise UnknownRecordError(f"Project {project} is not onboarded")
    project_id: UUID = row[0]
    return project_id


def check_text(argument: str, value: str | None, limit: int, *, required: bool = True) -> None:
    """*value* is a string of 1 to *limit* characters (or None when not *required*); the message names the length."""
    if value is None and not required:
        return
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        found = f"{len(value)} characters" if isinstance(value, str) else f"a {type(value).__name__}"
        raise ValueError(f"The {argument} must be a string of 1 to {limit} characters, got {found}")


def check_kind(kind: str) -> None:
    """`ValueError` unless *kind* is one of `ARTIFACT_KINDS`."""
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"Artifact kind {kind!r} is not one of {', '.join(ARTIFACT_KINDS)}")


def check_checksum(checksum: bytes) -> None:
    """`ValueError` unless *checksum* is a SHA-256 digest of `CHECKSUM_BYTES` bytes."""
    if not isinstance(checksum, bytes) or len(checksum) != CHECKSUM_BYTES:
        found = f"{len(checksum)} bytes" if isinstance(checksum, bytes) else f"a {type(checksum).__name__}"
        raise ValueError(f"The checksum must be a SHA-256 digest of {CHECKSUM_BYTES} bytes, got {found}")


def check_origin(origin: str | None) -> None:
    """`ValueError` unless *origin* is None or a string of 1 to `ORIGIN_MAX` characters."""
    check_text("origin", origin, ORIGIN_MAX, required=False)
