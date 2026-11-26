"""The Archivum's schema ledger: versioned SQL steps, applied once, in order, under a lock, and recorded.

Steps are package data (`steps/NNNN_<name>.sql`) with two header comments, `-- ulduar-kind:` (additive or
breaking) and `-- ulduar-min-library:` (the lowest library release that still works against the schema once
the step is applied). `install` applies the pending steps in one transaction under an advisory lock;
`check_schema` and `schema_status` read the ledger at open time and refuse a schema this library cannot serve.
The bootstrap below is the only DDL outside a step (hard rule 7).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from importlib.resources.abc import Traversable

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

import ulduar
from ulduar import _db
from ulduar.config import Config
from ulduar.errors import ConfigurationError, LedgerError, SchemaVersionError, UlduarError

logger = logging.getLogger(__name__)

STEPS_PACKAGE = "ulduar.freya"
STEPS_DIRECTORY = "steps"
STEP_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
KIND_HEADER = re.compile(r"^--\s*ulduar-kind:\s*(additive|breaking)\s*$")
MIN_LIBRARY_HEADER = re.compile(r"^--\s*ulduar-min-library:\s*(\d+\.\d+\.\d+)\s*$")
RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
ADDITIVE = "additive"
BREAKING = "breaking"
# The first 8 bytes of SHA-256("ulduar.schema_ledger"), read as a big-endian signed 64-bit integer.
LEDGER_LOCK_KEY = -7893859028443541850
SCHEMA = "ulduar"
LEDGER_TABLE = "schema_ledger"
_LEDGER = sql.Identifier(SCHEMA, LEDGER_TABLE)

Release = tuple[int, int, int]


@dataclass(frozen=True)
class Step:
    """One schema step: its number and name (from the file name), headers, text and SHA-256 of the file."""

    number: int
    name: str
    kind: str
    min_library: Release
    sql: str
    checksum: bytes


@dataclass(frozen=True)
class SchemaStatus:
    """Where the Archivum's schema stands against this library's steps."""

    applied_step: int
    known_step: int
    pending: tuple[int, ...]
    min_library_version: str | None
    compatible: bool


@dataclass(frozen=True)
class _Applied:
    """A row of the ledger table."""

    number: int
    name: str
    kind: str
    checksum: bytes
    min_library_version: str


def library_release(version: str = ulduar.__version__) -> Release:
    """The leading `X.Y.Z` of *version* (`0.1.0.dev0` is (0, 1, 0)); (0, 0, 0) when there is none (`0+unknown`)."""
    match = RELEASE.match(version)
    if match is None:
        return (0, 0, 0)
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def _header(lines: list[str], pattern: re.Pattern[str], what: str, file: str) -> str:
    found = [m.group(1) for line in lines if (m := pattern.match(line))]
    if len(found) != 1:
        raise LedgerError(f"Schema step file {file} needs exactly one `{what}` header in its leading comment lines")
    return found[0]


def _parse_step(file: str, content: bytes, number: int, name: str) -> Step:
    text = content.decode("utf-8")
    leading: list[str] = []
    for line in text.splitlines():
        if not line.startswith("--"):
            break
        leading.append(line.strip())
    kind = _header(leading, KIND_HEADER, "-- ulduar-kind: additive|breaking", file)
    min_library = library_release(_header(leading, MIN_LIBRARY_HEADER, "-- ulduar-min-library: X.Y.Z", file))
    return Step(number, name, kind, min_library, text, sha256(content).digest())


def load_steps(directory: Traversable | None = None) -> tuple[Step, ...]:
    """The schema steps of *directory* (default: the package's own), numbered exactly 1..N.

    A `.sql` file whose name is not `NNNN_<name>.sql`, a missing or repeated header, or a gap or a
    repeat in the numbering raises `LedgerError`. *directory* exists so a test can supply synthetic steps.
    """
    if directory is None:
        directory = files(STEPS_PACKAGE) / STEPS_DIRECTORY
    steps: list[Step] = []
    for entry in sorted(directory.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".sql"):
            continue
        match = STEP_FILE.match(entry.name)
        if match is None:
            raise LedgerError(f"Schema step file {entry.name} is not named NNNN_<name>.sql")
        steps.append(_parse_step(entry.name, entry.read_bytes(), int(match.group(1)), match.group(2)))
    if [s.number for s in steps] != list(range(1, len(steps) + 1)):
        found = [s.number for s in steps]
        raise LedgerError(f"Schema steps must be numbered 1..{len(steps)} without a gap; found {found}")
    return tuple(steps)


def _bootstrap(conn: psycopg.Connection[TupleRow]) -> None:
    """Create the schema and the ledger table if absent: the one DDL outside a step."""
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} ("
            "step integer PRIMARY KEY CHECK (step > 0), "
            "name text NOT NULL, "
            "kind text NOT NULL CHECK (kind IN ('additive', 'breaking')), "
            "checksum bytea NOT NULL CHECK (octet_length(checksum) = 32), "
            "min_library_version text NOT NULL, "
            "applied_by_version text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        ).format(_LEDGER)
    )


def _ledger_installed(conn: psycopg.Connection[TupleRow]) -> bool:
    """Whether the ledger table exists, read from the catalog so a role without schema access gets no error."""
    row = conn.execute(
        "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname = %s",
        (SCHEMA, LEDGER_TABLE),
    ).fetchone()
    return row is not None


def _read_applied(conn: psycopg.Connection[TupleRow]) -> dict[int, _Applied]:
    rows = conn.execute(
        sql.SQL("SELECT step, name, kind, checksum, min_library_version FROM {} ORDER BY step").format(_LEDGER)
    ).fetchall()
    return {r[0]: _Applied(r[0], r[1], r[2], bytes(r[3]), r[4]) for r in rows}


@dataclass(frozen=True)
class _Assessment:
    """The ledger read against this library's steps; every field is a finding, none is an error yet."""

    pending: tuple[Step, ...]
    too_new_for_library: str | None  # the highest recorded minimum library version, when above this library
    unknown_breaking: _Applied | None
    highest_min_library: str | None

    def schema_problem(self) -> str | None:
        """Why this library cannot serve the schema as it is (not counting pending steps), or None."""
        if self.too_new_for_library:
            return (
                f"The Archivum needs ulduar {self.too_new_for_library} or newer; this is {ulduar.__version__} "
                "— upgrade the library"
            )
        if self.unknown_breaking:
            step = self.unknown_breaking
            return (
                f"The Archivum has breaking step {step.number} ({step.name}), unknown to this library "
                "— upgrade the library"
            )
        return None


def _assess(applied: dict[int, _Applied], steps: tuple[Step, ...]) -> _Assessment:
    """Compare the ledger with *steps*; a changed checksum of a known applied step raises `LedgerError`."""
    known = {s.number: s for s in steps}
    for number, row in applied.items():
        if number in known and row.checksum != known[number].checksum:
            raise LedgerError(
                f"Applied schema step {number} ({row.name}) was changed after it was recorded — its checksum differs"
            )
    highest = max((r.min_library_version for r in applied.values()), key=library_release, default=None)
    too_new = highest if highest is not None and library_release(highest) > library_release() else None
    unknown_breaking = next((r for n, r in sorted(applied.items()) if n not in known and r.kind == BREAKING), None)
    pending = tuple(s for s in steps if s.number not in applied)
    return _Assessment(pending, too_new, unknown_breaking, highest)


def _status(applied: dict[int, _Applied], steps: tuple[Step, ...], assessment: _Assessment) -> SchemaStatus:
    return SchemaStatus(
        applied_step=max(applied, default=0),
        known_step=len(steps),
        pending=tuple(s.number for s in assessment.pending),
        min_library_version=assessment.highest_min_library,
        compatible=assessment.schema_problem() is None and not assessment.pending,
    )


def _pending_message(applied: dict[int, _Applied], steps: tuple[Step, ...]) -> str:
    at = max(applied, default=0)
    return f"The Archivum is at step {at}; this library needs step {len(steps)} — run freya.install"


def check_schema(conn: psycopg.Connection[TupleRow], steps: tuple[Step, ...]) -> SchemaStatus:
    """Refuse a schema this library cannot serve; otherwise its status.

    Raises `SchemaVersionError` when the Archivum is not installed, needs a newer library, holds a breaking
    step this library does not know, or is older than this library's steps; `LedgerError` when a known
    applied step's checksum differs. An unknown additive step is accepted.
    """
    if not _ledger_installed(conn):
        raise SchemaVersionError("The Archivum is not installed — run freya.install")
    applied = _read_applied(conn)
    assessment = _assess(applied, steps)
    problem = assessment.schema_problem()
    if problem:
        raise SchemaVersionError(problem)
    if assessment.pending:
        raise SchemaVersionError(_pending_message(applied, steps))
    return _status(applied, steps, assessment)


def _take_lock(conn: psycopg.Connection[TupleRow]) -> None:
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LEDGER_LOCK_KEY,))
    except psycopg.errors.LockNotAvailable as e:
        raise LedgerError("Another install holds the ledger lock — try again when it has finished") from e


def _apply(conn: psycopg.Connection[TupleRow], step: Step) -> None:
    # The step text is package data, never built from input, and may hold many statements and `%` signs,
    # so it runs as is, without parameters.
    conn.execute(step.sql)
    conn.execute(
        sql.SQL(
            "INSERT INTO {} (step, name, kind, checksum, min_library_version, applied_by_version)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
        ).format(_LEDGER),
        (step.number, step.name, step.kind, step.checksum, ".".join(map(str, step.min_library)), ulduar.__version__),
    )
    logger.info("applied Archivum schema step %d (%s, %s)", step.number, step.name, step.kind)


def _install_in_transaction(conn: psycopg.Connection[TupleRow], steps: tuple[Step, ...], allow_breaking: bool) -> None:
    _take_lock(conn)
    _bootstrap(conn)
    applied = _read_applied(conn)
    assessment = _assess(applied, steps)
    problem = assessment.schema_problem()
    if problem:
        raise SchemaVersionError(problem)
    breaking = [s for s in assessment.pending if s.kind == BREAKING]
    if breaking and not allow_breaking:
        raise LedgerError(
            f"Schema step {breaking[0].number} ({breaking[0].name}) is breaking — nothing was applied; "
            "install again with allow_breaking=True once every client is ready"
        )
    for step in assessment.pending:
        _apply(conn, step)
    conn.execute("SELECT ulduar.ensure_partitions(now())")


def _read_status(conn: psycopg.Connection[TupleRow], steps: tuple[Step, ...]) -> SchemaStatus:
    applied = _read_applied(conn)
    return _status(applied, steps, _assess(applied, steps))


def install(config: Config, *, allow_breaking: bool = False, steps: tuple[Step, ...] | None = None) -> SchemaStatus:
    """Install or upgrade the Archivum's schema to this library's steps, as the ledger role.

    One transaction under an advisory lock: another install waits, up to the lock timeout, then sees the
    steps this one applied. A pending breaking step needs *allow_breaking*; nothing is applied without it.
    *steps* exists so a test can supply synthetic steps. Returns the status after the install.
    """
    settings = config.archivum
    if settings.ledger_dsn_env is None:
        raise ConfigurationError("archivum.ledger_dsn_env is not set — install needs the ledger role")
    steps = load_steps() if steps is None else steps
    with _db.open_connection(settings.ledger_dsn_env, settings) as conn:
        try:
            with _db.transaction(conn, settings):
                _install_in_transaction(conn, steps, allow_breaking)
                return _read_status(conn, steps)
        except UlduarError:
            raise
        except psycopg.Error as e:
            raise _db.translate(e, "Installing the Archivum") from e


def schema_status(config: Config) -> SchemaStatus:
    """The Archivum's schema status as the recording role reads it, read-only.

    A schema that is too old or too new for this library gives `compatible=False` instead of an error;
    one that is not installed raises `SchemaVersionError`, and a changed applied step `LedgerError`.
    """
    settings = config.archivum
    steps = load_steps()
    with _db.open_connection(settings.dsn_env, settings) as conn:
        try:
            with _db.transaction(conn, settings):
                if not _ledger_installed(conn):
                    raise SchemaVersionError("The Archivum is not installed — run freya.install")
                return _read_status(conn, steps)
        except UlduarError:
            raise
        except psycopg.Error as e:
            raise _db.translate(e, "Reading the Archivum's schema status") from e
