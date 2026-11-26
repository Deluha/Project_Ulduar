"""The one connection and transaction helper: reads a DSN from the environment, checks transport and server, opens.

Input: the name of an environment variable and the `ArchivumSettings`. Output: a psycopg connection, a
transaction with transaction-scoped time limits, and the translation of driver errors into the package's
exceptions. Private: Freya and later components import it, a consumer never does. No message built here
carries a DSN, a password or a credential's value (hard rule 11).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow

from ulduar.config import ArchivumSettings
from ulduar.errors import (
    ArchivumUnavailable,
    ConfigurationError,
    IsolationError,
    LineageCycleError,
    SchemaVersionError,
    UlduarError,
    UnknownRecordError,
)

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SECURE_SSLMODES = {"verify-full", "verify-ca"}
SUPPORTED_SERVER_MAJOR = 18
APPLICATION_NAME = "ulduar"
# SQLSTATEs the library raises itself (step 1): a lineage cycle, a node outside the project. UL003 (a lineage
# edge outside READ COMMITTED) has no exception of its own: it surfaces as a plain `UlduarError`.
SQLSTATE_LINEAGE_CYCLE = "UL001"
SQLSTATE_UNKNOWN_NODE = "UL002"
# Timeouts, lock refusals, a deadlock and a serialization failure (the server rolled this transaction back):
# query_canceled, lock_not_available, idle-in-transaction and transaction timeouts, deadlock_detected,
# serialization_failure.
_TRANSIENT_SQLSTATES = {"57014", "55P03", "25P03", "25P04", "40P01", "40001"}
_CONNECTION_CLASS = "08"
_OPERATOR_INTERVENTION_PREFIX = "57P"  # admin shutdown, crash shutdown, cannot connect now
_INSUFFICIENT_PRIVILEGE = "42501"
_FOREIGN_KEY_VIOLATION = "23503"


def read_dsn(variable: str, environ: Mapping[str, str] = os.environ) -> str:
    """The connection string held by environment variable *variable*; its value is never named in an error."""
    value = environ.get(variable)
    if not value:
        raise ConfigurationError(f"Environment variable {variable} is not set — it holds a connection string")
    return value


def _parse(dsn: str, variable: str) -> dict[str, Any]:
    try:
        return dict(conninfo_to_dict(dsn))
    except psycopg.Error:
        # `from None`: libpq's parse message can quote part of the string, password included (hard rule 11).
        raise ConfigurationError(f"The connection string in {variable} is malformed") from None


# DSN key -> the environment variable libpq falls back to when the DSN leaves the key out.
_HOST_FALLBACKS = (("host", "PGHOST"), ("hostaddr", "PGHOSTADDR"))
_SERVICE_VARIABLES = ("PGSERVICE", "PGSERVICEFILE")


def _hosts(info: Mapping[str, Any], environ: Mapping[str, str]) -> list[str]:
    """Every host and hostaddr entry libpq will use (each may be a comma-separated list), env fallback included."""
    return [
        entry
        for key, variable in _HOST_FALLBACKS
        for entry in str(info.get(key) or environ.get(variable) or "").split(",")
        if entry
    ]


def check_transport(dsn: str, variable: str = "the connection string", environ: Mapping[str, str] = os.environ) -> None:
    """Refuse a non-loopback, non-socket host unless TLS verifies the server (sslmode verify-full or verify-ca).

    libpq fills what the DSN leaves out from *environ* (`PGHOST`, `PGHOSTADDR`, `PGSSLMODE`, `PGSERVICE`), so
    the check reads the same fallbacks, and a connection service, whose target is not visible here, is
    refused. A DSN and environment that name no host reach the local Unix socket, which is accepted.
    """
    info = _parse(dsn, variable)
    if info.get("service") or any(environ.get(name) for name in _SERVICE_VARIABLES):
        raise ConfigurationError("A connection service is not supported — name the host in the connection string")
    sslmode = info.get("sslmode") or environ.get("PGSSLMODE")
    for host in _hosts(info, environ):
        local = host in LOOPBACK_HOSTS or host.startswith("/")
        if not local and sslmode not in SECURE_SSLMODES:
            raise ConfigurationError(
                f"Archivum host {host} is not on this machine — "
                "a non-loopback Archivum needs sslmode verify-full or verify-ca"
            )


def _first_host(info: Mapping[str, Any]) -> str:
    return next(iter(_hosts(info, os.environ)), "the default host")


def open_connection(variable: str, settings: ArchivumSettings) -> psycopg.Connection[TupleRow]:
    """Open a connection with the DSN in environment variable *variable*, after the transport and server checks."""
    dsn = read_dsn(variable)
    check_transport(dsn, variable)
    host = _first_host(_parse(dsn, variable))
    try:
        conn: psycopg.Connection[TupleRow] = psycopg.connect(
            dsn,
            connect_timeout=settings.connect_timeout_s,
            prepare_threshold=None,
            autocommit=False,
            application_name=APPLICATION_NAME,
        )
    except psycopg.Error as e:
        raise ArchivumUnavailable(f"Connecting to the Archivum failed: {host}") from e
    # Pinned whatever the role or database defaults to: the lineage cycle guard relies on READ COMMITTED.
    conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
    major = conn.info.server_version // 10000
    if major != SUPPORTED_SERVER_MAJOR:
        conn.close()
        raise SchemaVersionError(
            f"PostgreSQL {major} is not supported — the Archivum needs PostgreSQL {SUPPORTED_SERVER_MAJOR}"
        )
    return conn


@contextmanager
def transaction(
    conn: psycopg.Connection[TupleRow], settings: ArchivumSettings
) -> Iterator[psycopg.Connection[TupleRow]]:
    """One transaction with the statement, lock and total-budget limits set for it alone.

    Only transaction-scoped settings, so the limits survive no pooler and leak into no other session. The
    connection must be idle on entry, or psycopg would open a savepoint instead of a transaction.
    """
    with conn.transaction():
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true),"
            " set_config('transaction_timeout', %s, true)",
            (
                str(settings.statement_timeout_s * 1000),
                str(settings.lock_timeout_s * 1000),
                str(settings.call_budget_s * 1000),
            ),
        )
        yield conn


@contextmanager
def translated(what: str) -> Iterator[None]:
    """Re-raise a driver error raised inside as the package's exception (`translate`); package errors pass through."""
    try:
        yield
    except psycopg.Error as e:
        raise translate(e, what) from e


@contextmanager
def translated_transaction(
    conn: psycopg.Connection[TupleRow], settings: ArchivumSettings, what: str
) -> Iterator[psycopg.Connection[TupleRow]]:
    """`transaction` with `translated`: a driver error is raised as the package's exception, worded `<what> failed`."""
    with translated(what), transaction(conn, settings) as tx:
        yield tx


def translate(error: psycopg.Error, what: str) -> UlduarError:
    """The package exception for a driver error, worded `<what> failed: <server's primary message>`.

    Callers raise it `from` the driver error. The server's primary message never holds the DSN.
    """
    sqlstate = error.sqlstate or ""
    message = f"{what} failed: {error.diag.message_primary or next(iter(str(error).splitlines()), '')}"
    if (
        sqlstate.startswith((_CONNECTION_CLASS, _OPERATOR_INTERVENTION_PREFIX))
        or sqlstate in _TRANSIENT_SQLSTATES
        or isinstance(error, psycopg.OperationalError)
    ):
        return ArchivumUnavailable(message)
    if sqlstate == _INSUFFICIENT_PRIVILEGE:
        return IsolationError(message)
    if sqlstate in (_FOREIGN_KEY_VIOLATION, SQLSTATE_UNKNOWN_NODE):
        return UnknownRecordError(message)
    if sqlstate == SQLSTATE_LINEAGE_CYCLE:
        return LineageCycleError(message)
    return UlduarError(message)
