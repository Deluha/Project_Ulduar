# ulduar

## Purpose & scope

The importable package. It is generic: nothing here names a consumer or assumes a consumer's schema,
folders or jobs (hard rule 1). Phase 1 adds the configuration file, the connection to the Archivum, the
public exceptions and the record identifiers; the first component, Freya, follows.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | `__version__`, read from the installed distribution's metadata |
| `config.py` | `load_config` and the frozen `Config`, `ProjectSettings`, `ArchivumSettings`; `NAME_PATTERN`, the one definition of a project name or scope |
| `errors.py` | The public exceptions, one module: `UlduarError` and its subclasses |
| `_db.py` | Private: the one connection and transaction helper, the transport guard, the error translation |
| `_ids.py` | Private: the only record-id generator, UUIDv7 (`uuid7`, `uuid7_time`) |

## How it works

- `load_config` reads one TOML file (default `ulduar.toml`) with `tomllib`. Top-level keys: `version` (the
  integer 1), `[project]` (`name`, optional `scope`, both matching `NAME_PATTERN`) and `[archivum]`
  (`dsn_env`, optional `ledger_dsn_env`, and the timeouts below). An unknown key at any level, a wrong
  type, an out-of-range timeout, a missing or unreadable file or a TOML syntax error raises
  `ConfigurationError` naming the dotted key or the path.
- Credentials never appear in the file: `dsn_env` and `ledger_dsn_env` name environment variables that hold
  PostgreSQL connection strings. `dsn_env` is the recording role's; `ledger_dsn_env` is the ledger role's,
  used only to install and upgrade the schema (and, in later commits, onboarding, retention and redaction).
- `_db.open_connection` reads the variable, refuses a non-loopback host unless `sslmode` is `verify-full`
  or `verify-ca`, opens one connection (no pool, no prepared statements) and refuses a server that is not
  PostgreSQL 18. `_db.transaction` wraps one transaction and sets `statement_timeout`, `lock_timeout` and
  `transaction_timeout` for it alone (`set_config(..., true)`), so the limits survive a connection pooler.
  `_db.translate` turns a driver error into a package exception by SQLSTATE.
- Time limits, in seconds (default, range): connect 10 (1–60), statement 30 (1–600), lock 5 (1–60), total
  budget per call 60 (1–3600). A limit that is hit raises `ArchivumUnavailable`.
- Record ids are UUIDv7 made by the client; a record's `event_at` is the time inside its id.

## Interfaces & contracts

- `ulduar.__version__`: the version from `pyproject.toml`; `0+unknown` in a checkout that was never
  installed.
- `ulduar.config`: `load_config(path="ulduar.toml") -> Config`, `Config(project, archivum, path)`,
  `ProjectSettings(name, scope="default")`, `ArchivumSettings(dsn_env, ledger_dsn_env=None, connect_timeout_s=10,
  statement_timeout_s=30, lock_timeout_s=5, call_budget_s=60)`, `NAME_PATTERN`, `CONFIG_VERSION`.
- `ulduar.errors`: `UlduarError`; `ConfigurationError`; `ArchivumUnavailable` (no connection, a timeout, the
  call budget, a lost connection); `SchemaVersionError` (not installed, schema too new or too old,
  unsupported server); `LedgerError` (changed step, breaking step without its flag, ledger lock);
  `UnknownRecordError`; `LineageCycleError`; `IsolationError` (refused by row-level security or privileges).
  A message names the object (project, variable, step, host) and never a connection string, a password or a
  credential's value.
- One runtime dependency: `psycopg>=3.2,<4` (needs a system libpq 16 or newer; the extra
  `project-ulduar[binary]` brings a bundled one). Adding another is an ask-first decision.
- Logging: library code logs through loggers under `ulduar.*`, adds no handler and never touches the root
  logger.

### What the Archivum's host must offer

- PostgreSQL 18, reachable from every job that records, with enough connection slots: a job holds at most
  one connection.
- TLS for a non-loopback host, with certificate verification (`sslmode=verify-full` or `verify-ca`).
- A database of its own, with a ledger login role, which owns the database and so
  the schema `ulduar`, and needs `CREATEROLE` (step 1 creates the group role `ulduar_recorder`), and one
  recording login role per project, created by the consumer. The recording roles hold `SELECT` and
  `INSERT` only.

## Extending

Follow the adding-a-component workflow of the development skill
(`.claude/skills/ulduar-development/references/workflows/adding-a-component.md`).

## See also

`ROADMAP.md` · `.claude/skills/ulduar-development/references/architecture.md`
