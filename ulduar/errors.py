"""The package's public exceptions, in one module, so a consumer catches them without importing a driver.

Every message names the offending object (project, variable name, step, host) and never carries a
connection string, a password or the value of a credential (hard rule 11). Raised with `from e` where a
driver error is the cause.
"""

from __future__ import annotations


class UlduarError(Exception):
    """Base of every error the package raises on purpose."""


class ConfigurationError(UlduarError):
    """The configuration file, or an environment variable it names, is missing or invalid."""


class ArchivumUnavailable(UlduarError):
    """The Archivum could not be used in time: no connection, a timeout, the call budget, a lost connection."""


class SchemaVersionError(UlduarError):
    """The Archivum is not installed, or its schema or server version does not fit this library."""


class LedgerError(UlduarError):
    """The schema ledger refused: a changed step checksum, a breaking step without its flag, or no ledger lock."""


class UnknownRecordError(UlduarError):
    """A record names a project, scope, dataset or artifact the Archivum does not hold."""


class LineageCycleError(UlduarError):
    """A lineage edge would close a cycle."""


class IsolationError(UlduarError):
    """The Archivum refused the call by row-level security or privileges: the role may not do that."""


class ArchiveUnavailable(UlduarError):
    """The archive store could not be used: unreachable, timed out, or refused the credential."""


class IntegrityError(UlduarError):
    """An archived object does not match its recorded checksum or size, or is missing or unreadable."""


class ArchiveFormatError(UlduarError):
    """The archive store's format marker is missing, foreign or of a version this library does not know."""
