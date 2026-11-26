# ulduar

## Purpose & scope

The importable package. It is generic: nothing here names a consumer or assumes a consumer's schema,
folders or jobs (hard rule 1). Phase 1 adds the configuration file, the connection to the Archivum, the
public exceptions, the record identifiers and the first component, Freya (`freya/README.md`). Phase 2 adds the
`[archive]` configuration section and Hodir, the cold archive (`hodir/README.md`), whose first public calls store a
file or a stream.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | `__version__`, read from the installed distribution's metadata |
| `config.py` | `load_config` and the frozen `Config`, `ProjectSettings`, `ArchivumSettings`, `ArchiveSettings`, `CredentialSettings`; `NAME_PATTERN`, the one definition of a project name or scope |
| `errors.py` | The public exceptions, one module: `UlduarError` and its subclasses |
| `_db.py` | Private: the one connection and transaction helper, the transport guard, the error translation (`translate`, `translated`, `translated_transaction`) |
| `_ids.py` | Private: the only record-id generator, UUIDv7 (`uuid7`, `uuid7_time`) |
| `hodir/` | Hodir's facade (`open_archive`, `Archive`, `PutSink`, `Verification`, `Finding`, `Reconciliation`, `Rebuild`: `put_file`, `put_stream`, `get`, `get_stream`, `verify`, `reconcile`, `rebuild`; `declare_retention`, `apply_retention`, `clean_incomplete`, `purge` and their records) and, private, the object layout, the storage interface with its directory and S3 backends (`hodir/README.md`) |
| `freya/` | Freya's facade, schema ledger, SQL steps (the catalog, then the archive's tables), catalog session with its `transaction` hook, lineage and the ledger-role history calls (`freya/README.md`) |

## How it works

- `load_config` reads one TOML file (default `ulduar.toml`) with `tomllib`. Top-level keys: `version` (the
  integer 1), `[project]` (`name`, optional `scope`, both matching `NAME_PATTERN`) and `[archivum]`
  (`dsn_env`, optional `ledger_dsn_env`, and the timeouts below). An unknown key at any level, a wrong
  type, an out-of-range timeout, a missing or unreadable file or a TOML syntax error raises
  `ConfigurationError` naming the dotted key or the path. The optional `[archive]` table (below) configures
  Hodir; a file without it stays valid.
- Credentials never appear in the file: `dsn_env` and `ledger_dsn_env` name environment variables that hold
  PostgreSQL connection strings. `dsn_env` is the recording role's; `ledger_dsn_env` is the ledger role's,
  used only to install and upgrade the schema, onboard a project and declare, apply and redact history.
- `_db.open_connection` reads the variable, refuses a non-loopback host unless `sslmode` is `verify-full`
  or `verify-ca` (reading what libpq falls back to when the string leaves it out: `PGHOST`, `PGHOSTADDR`,
  `PGSSLMODE`; a connection service, in the string or through `PGSERVICE` / `PGSERVICEFILE`, is refused), opens
  one connection (no pool, no prepared statements) pinned to READ COMMITTED whatever the role or database
  defaults to (the lineage cycle guard relies on it), and refuses a server that is not PostgreSQL 18.
  `_db.transaction` wraps one transaction and sets `statement_timeout`, `lock_timeout` and `transaction_timeout`
  for it alone (`set_config(..., true)`), so the limits survive a connection pooler.
  `_db.translate` turns a driver error into a package exception by SQLSTATE; `_db.translated(what)` does it for
  a block and `_db.translated_transaction(conn, settings, what)` for a transaction.
- Time limits, in seconds (default, range): connect 10 (1–60), statement 30 (1–600), lock 5 (1–60), total
  budget per call 60 (1–3600). A limit that is hit raises `ArchivumUnavailable`.
- `[archive]` (optional; `ArchiveSettings`), keys by backend. Defaults and ranges in parentheses.
  - Both backends: `backend` (required: `"s3"` or `"directory"`); `prefix` ("" or `/`-joined segments each
    matching `NAME_PATTERN`; default `ulduar`); `spool_dir` (an absolute path; default the system temporary
    directory); `spool_limit_gib` (100; 1–100000); `incomplete_put_grace_hours` (168; 24–8760);
    `max_deletions_per_run` (100; 1–1000000). `incomplete_put_grace_hours` is the grace period of `Archive.reconcile` and
    `clean_incomplete`; `max_deletions_per_run` limits `apply_retention`.
  - `directory` only: `path` (required, absolute; it must exist when the store opens). The s3-only keys
    below are refused.
  - `s3` only: `bucket` (required; S3 bucket naming, `^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$`); `region`
    (`^[a-z0-9-]{1,32}$`); `endpoint_url_env` (the variable holding the endpoint URL: `https://`, or `http://`
    to a loopback host); `proxy_env` (the variable holding a proxy URL; absent means no proxy, the environment's
    proxies are never used); `[archive.writer]` and `[archive.deleter]`, each with `access_key_env` and
    `secret_key_env` (required) and `session_token_env` (optional), all names of environment variables. No
    writer means the client's standard credential chain; the deleter is needed only by calls that delete, and
    must name a different `access_key_env` than the writer. `path` is refused.
  - Integers share one bounded reader, so a message ends `between <low> and <high> <unit>` (seconds, hours,
    GiB; none for the deletion count).
- Record ids are UUIDv7 made by the client; a record's `event_at` is the time inside its id.

## Interfaces & contracts

- `ulduar.__version__`: the version from `pyproject.toml`; `0+unknown` in a checkout that was never
  installed.
- `ulduar.config`: `load_config(path="ulduar.toml") -> Config`, `Config(project, archivum, path, archive=None)`,
  `ProjectSettings(name, scope="default")`, `ArchivumSettings(dsn_env, ledger_dsn_env=None, connect_timeout_s=10,
  statement_timeout_s=30, lock_timeout_s=5, call_budget_s=60)`, `ArchiveSettings(backend, path=None, bucket=None,
  prefix="ulduar", region=None, endpoint_url_env=None, proxy_env=None, spool_dir=None, spool_limit_gib=100,
  incomplete_put_grace_hours=168, max_deletions_per_run=100, writer=None, deleter=None)`,
  `CredentialSettings(access_key_env, secret_key_env, session_token_env=None)`, `NAME_PATTERN`, `CONFIG_VERSION`,
  `check_name(value, what) -> str` (the name check; raises `ValueError`).
- `ulduar.errors`: `UlduarError`; `ConfigurationError`; `ArchivumUnavailable` (no connection, a timeout, the
  call budget, a lost connection, a deadlock (40P01) or a serialization failure (40001)); `SchemaVersionError`
  (not installed, schema too new or too old, unsupported server); `LedgerError` (changed step, breaking step without
  its flag, ledger lock); `UnknownRecordError`; `LineageCycleError`; `IsolationError` (refused by row-level security
  or privileges); `ArchiveUnavailable` (the archive store is unreachable, timed out or refused the credential);
  `IntegrityError` (an archived object does not match its checksum or size, or is missing or unreadable);
  `ArchiveFormatError` (the store's format marker is foreign or of an unknown version, or a manifest or a maintenance
  record is not format 1 or cannot be read; raised by `open_archive`, `clean_incomplete` and `rebuild`). A message names the object (project, variable, step, host) and
  never a connection string, a password or a credential's value.
- One runtime dependency: `psycopg>=3.2,<4` (needs a system libpq 16 or newer; the extra
  `project-ulduar[binary]` brings a bundled one). Adding another is an ask-first decision. One optional
  extra beside `binary`: `project-ulduar[s3]` (`boto3>=1.36,<2`), the S3 backend's client; without it
  `open_store` raises `ConfigurationError` for an s3 archive.
- The public Freya surface (`ulduar.freya`: `open_catalog` with its register, find, lineage, ancestors and
  descendants calls and the `transaction` hook, the onboarding, retention, redaction and `reapply_removals` calls, the
  `ledger_transaction` hook, and their records): `freya/README.md`. The public Hodir surface (`ulduar.hodir`:
  `open_archive`, `put_file`, `put_stream`, `get`, `get_stream`, `verify`, `reconcile`, `rebuild`, and on a `Config`
  `declare_retention`, `apply_retention`, `clean_incomplete` and `purge`, with their records): `hodir/README.md`.
- Logging: library code logs through loggers under `ulduar.*`, adds no handler and never touches the root
  logger.

### What the Archivum's host must offer

- PostgreSQL 18, reachable from every job that records, with enough connection slots: a job holds at most
  one connection.
- TLS for a non-loopback host, with certificate verification (`sslmode=verify-full` or `verify-ca`).
- A database of its own, with a ledger login role, which owns the database and so
  the schema `ulduar`, and needs `CREATEROLE` (step 1 creates the group role `ulduar_recorder`), and one
  recording login role per project, created by the consumer. The recording roles hold `SELECT` and
  `INSERT` only (`freya/README.md`).
- The group role `ulduar_recorder` is cluster-wide, shared by every database of the server. For a second
  Archivum on the same server, the ledger role must hold `ADMIN OPTION` on `ulduar_recorder` (granted by a
  superuser, once), or onboarding cannot add its project's login role to the group; a recording role of one
  Archivum can then also read the ledger table and call `ensure_partitions` of another, where row-level
  security shows it no row.

## Extending

Follow the adding-a-component workflow of the development skill
(`.claude/skills/ulduar-development/references/workflows/adding-a-component.md`).

## See also

`freya/README.md` · `ROADMAP.md` · `.claude/skills/ulduar-development/references/architecture.md`
