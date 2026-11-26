# ulduar/freya

## Purpose & scope

Freya is the catalog and lineage component; its database is the Archivum (PostgreSQL 18, its own database,
every object in schema `ulduar`). So far it holds the schema half: the versioned-SQL ledger, the
step that creates the Phase 1 schema, and the checks that refuse a schema this library cannot serve. The
catalog and lineage calls arrive in the next commits of Phase 1.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | The facade: re-exports `install`, `schema_status`, `SchemaStatus`, `Step`, `load_steps` |
| `ledger.py` | `load_steps`, `install`, `check_schema`, `schema_status`, `library_release`, the bootstrap, `LEDGER_LOCK_KEY` |
| `steps/0001_catalog.sql` | Step 1: roles, tables, functions, triggers, partitions, row-level security and grants |

## How it works

- A step is a file `steps/NNNN_<name>.sql`, numbered 1..N without a gap, never edited once released (a new
  step corrects it). Its leading comment lines hold two required headers: `-- ulduar-kind: additive|breaking`
  and `-- ulduar-min-library: X.Y.Z` (the lowest library release that still works once the step is applied).
  The ledger records, per applied step: number, name, kind, the SHA-256 of the file, the minimum library
  version, the applying library version and the time (`ulduar.schema_ledger`).
- `install(config)` runs as the ledger role, in one transaction with transaction-scoped time limits: take
  the advisory lock `LEDGER_LOCK_KEY` (a lock timeout is a `LedgerError`; a second install waits, then finds
  nothing pending), create schema `ulduar` and the ledger table if absent (the bootstrap, the only DDL
  outside a step), verify the checksum of every applied step, refuse a recorded minimum library version above
  this library's or an unknown applied breaking step, refuse a pending breaking step without
  `allow_breaking=True` (nothing is applied), apply each pending step in order and record it, then
  `ulduar.ensure_partitions(now())`. A failure rolls everything back.
- `check_schema(conn, steps)` is the check made when a session opens: not installed, schema needs a newer
  library, unknown applied breaking step, or known steps still pending raise `SchemaVersionError`; a changed
  checksum of an applied step raises `LedgerError`; an unknown additive step is accepted.
  `schema_status(config)` reads the same as the recording role and returns `compatible=False` instead of
  raising for a too-old or too-new schema.
- `LEDGER_LOCK_KEY` is the first 8 bytes of SHA-256 of `ulduar.schema_ledger`, as a signed big-endian 64-bit
  integer.
- Roles: the ledger role owns schema `ulduar` and everything in it and is the only role that changes the
  schema. Step 1 creates the NOLOGIN group `ulduar_recorder`; each project's login role joins it (onboarding,
  a later commit). The group holds `USAGE` on the schema, `SELECT` and `INSERT` on `dataset`, `artifact`,
  `lineage_edge`, `SELECT` on `project`, `project_member`, `scope`, `artifact_kind`,
  `history_retention_rule`, `schema_ledger`, and `EXECUTE` on `member_projects`, `node_exists`,
  `is_ledger_role`, `ensure_partitions`; no `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES` or `TRIGGER`.

### Schema overview (step 1)

| Object | Notes |
|---|---|
| `project`, `project_member`, `scope` | Written by the ledger role only; a login role belongs to one project; scope matches `NAME_PATTERN` |
| `artifact_kind` | `raw_file`, `database_dump`, `report`, `model`, `log` |
| `dataset` | Identified by project, scope, namespace, name; UUIDv7 id |
| `artifact` | Kind, SHA-256 checksum, size, origin (a path or id within the source, never an actor); monthly UTC range partitions on `event_at` |
| `lineage_edge` | Child derived from parent (artifact or dataset), optional `run_id`; monthly partitions; a trigger refuses a node outside the project (SQLSTATE `UL002`), a cycle (`UL001`) and a self-edge |
| `history_retention_rule`, `history_retention_action`, `redaction` | The retention and redaction records of D40; never age out, never redacted |
| `schema_ledger` | The ledger table (bootstrap) |

- Every id is a UUIDv7 whose time equals the row's `event_at` (a CHECK); `recorded_at` is the database's time.
- Row-level security is enabled and forced on every keyed table. The ledger role passes the `ledger_all`
  policy; a recording role sees and inserts only rows of the project it belongs to (`project_member`).
- Append-only: triggers refuse `UPDATE`, `DELETE` and `TRUNCATE` on the six history tables unless the ledger
  role has set `ulduar.maintenance` to a mode the table declares (`retention` for artifact and lineage edge,
  `redaction` for artifact and dataset); truncate is always refused.
- Partitions are created only by `ulduar.ensure_partitions(for_time, months_ahead)` (this month and three
  ahead by default) and dropped, when they ended and are empty, by `ulduar.drop_empty_partitions(before)`,
  which only the ledger role may run.

## Interfaces & contracts

- `install(config, *, allow_breaking=False, steps=None) -> SchemaStatus`: needs `archivum.ledger_dsn_env`.
  `steps` exists for tests.
- `schema_status(config) -> SchemaStatus(applied_step, known_step, pending, min_library_version, compatible)`.
- `load_steps(directory=None) -> tuple[Step, ...]`; `Step(number, name, kind, min_library, sql, checksum)`.
- Exceptions: `ConfigurationError`, `SchemaVersionError`, `LedgerError`, `ArchivumUnavailable` and the other
  members of `ulduar.errors`.
- Library log lines: one `info` line per applied step, from logger `ulduar.freya.ledger`.

## Extending

A schema change is a new step file with the next number, both headers and statements that only add
(`.claude/skills/ulduar-development/references/workflows/changing-own-schema.md`). Never edit a released
step, and never write `UPDATE`, `DELETE` or `TRUNCATE` against a history table in a step. The Archivum
schema is an ask-first item.

## See also

`../README.md` · `../../ROADMAP.md` (Phase 1) · `../../tests/test_freya_ledger.py`
