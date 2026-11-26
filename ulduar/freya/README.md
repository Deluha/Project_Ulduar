# ulduar/freya

## Purpose & scope

Freya is the catalog and lineage component; its database is the Archivum (PostgreSQL 18, its own database,
every object in schema `ulduar`). It holds the schema half (the versioned-SQL ledger, the step that creates
the Phase 1 schema, the checks that refuse a schema this library cannot serve) and the catalog half: a
project's datasets and artifacts registered through a catalog session, and the ledger-role calls that
onboard a project, declare and apply history retention and redact a value; and lineage: "derived from" edges between artifacts and datasets, with ancestors and
descendants queries.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | The facade: re-exports the public surface listed under Interfaces & contracts |
| `ledger.py` | `load_steps`, `install`, `check_schema`, `schema_status`, `library_release`, `open_ledger_connection`, the bootstrap, `LEDGER_LOCK_KEY` |
| `catalog.py` | The records (`Project`, `Dataset`, `Artifact`, `NodeRef`), `ARTIFACT_KINDS`, `open_catalog` and `Catalog`, `insert_with_partition_retry` |
| `lineage.py` | `LineageEdge`, `LineageNode`; the edge insert and the ancestor and descendant walks that `Catalog` delegates to |
| `history.py` | Ledger-role calls: `onboard_project`, `declare_history_retention`, `apply_history_retention`, `redact`; the only code that issues `UPDATE` or `DELETE` against history |
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

### The catalog session

- `open_catalog(config)` opens one connection as the recording role (`archivum.dsn_env`), checks the schema
  (`ledger.check_schema`), finds the project named by `project.name` (row-level security shows the role only
  its own) and checks that `project.scope` exists; the connection closes when the `with` block ends. A role
  that is a member of the recording group but of no project, or of another project, gets `UnknownRecordError`
  ("Project <name> is not onboarded for this role"); a role never granted the group is refused by the database
  (`IsolationError`, permission denied for schema `ulduar`) before it can look.
- Every `Catalog` call is one transaction with transaction-scoped time limits; a driver error is raised as
  the package's exception, worded `<Operation> <object> failed: <the server's message>`, and a package error
  passes through. A bad argument is a `ValueError` naming it, raised before any SQL.
- A record id is a UUIDv7 made by the client; its time is the record's `event_at`. `register_artifact`'s
  `record_id` is the idempotency key: the same id with the same content returns the recorded artifact, with
  other content raises `UlduarError`. A new id is made when none is given.
- An insert into a partitioned table whose month has no partition fails (`23514`, "no partition of relation");
  `insert_with_partition_retry` then calls `ulduar.ensure_partitions(event_at, 0)` in a transaction of its own
  and runs the insert once more (the same id, so it cannot duplicate). Partitions are never created any other
  way (hard rule 7).

### Lineage

- `Catalog.add_lineage(child, parents, *, run_id=None)` records that `child` was derived from each parent, one
  UUIDv7 per edge, in one transaction (all edges or none) through the partition retry; a node is an
  `Artifact`, a `Dataset` or a `NodeRef`. The trigger of step 1, under a per-project lock, refuses a node that
  is not in the project (`UnknownRecordError`, SQLSTATE `UL002`) and an edge that would close a cycle
  (`LineageCycleError`, `UL001`); a self-edge and an empty parent list are a `ValueError` before any SQL. An
  edge is a recorded fact: recording the same one again records another edge, which the queries fold.
- `ancestors(node, *, max_depth=None)` and `descendants(...)` are one recursive query each (`UNION` on kind,
  id and depth, bounded by `max_depth` when given), keeping each node once at its minimum depth; ordered by
  depth, kind and id; the node itself is not in the result; `max_depth` must be at least 1. A node the project
  does not hold, or that has no edges, has an empty result. Row-level security limits a walk to the project.
- An edge ages out by its own `lineage_edge` retention rule, so a walk may name a node whose record has aged
  out of `artifact`.

### History: onboarding, retention, redaction

All of these run as the ledger role (`archivum.ledger_dsn_env`, else `ConfigurationError`), in one
transaction each after `ledger.check_schema`; `history.py` is the only code that issues `UPDATE` or `DELETE`
against history, each only inside the maintenance setting `ulduar.maintenance` (`set_config(..., true)`, so it
ends with the transaction).

- `onboard_project` reuses the project of that name or creates it (UUIDv7 id), adds the scopes
  (`ON CONFLICT DO NOTHING`), records the login role as a member and grants it `ulduar_recorder`. A login role
  belongs to one project: one already in another raises `UlduarError` naming role and project and keeps
  nothing; one that does not exist raises `UnknownRecordError`. Repeating it changes nothing.
- `declare_history_retention` appends a rule for a project and history (`artifact`, `lineage_edge`); the rule
  recorded last wins; `keep_days=None` keeps for ever (also the state when no rule exists). Nothing is removed.
- `apply_history_retention(dry_run=True)` takes the latest rule of every project and history that has a
  `keep_days`; the cutoff is `now - keep_days`. A dry run counts the rows with `event_at` before the cutoff and
  changes nothing. Otherwise, per project and history, one transaction deletes those rows (maintenance
  `retention`) and appends a `history_retention_action` (rows removed, cutoff, rule); after all of them
  `ulduar.drop_empty_partitions` drops the monthly partitions that ended before the earliest cutoff and hold no
  row, in a transaction of its own. A project's rows are never touched by another project's rule. With no
  limited rule nothing happens (one `info` log line). A very large delete is bound by the statement timeout.
- An edge ages out by its own `lineage_edge` rule, not with the artifact or dataset it names: ancestors and
  descendants may therefore name a node whose record has aged out.
- `redact` changes one field of one record and appends a `redaction` record, in one transaction (maintenance
  `redaction`): `artifact.origin` becomes NULL; `dataset.name` or `dataset.namespace` becomes
  `redacted:<redaction id>` (the original identity can then be registered anew as a new dataset). Any other
  pair, or a reason other than `personal_data`, `secret`, `recorded_by_mistake`, is a `ValueError`; a record that
  is not in the project is `UnknownRecordError`. Retention and redaction records are never removed or changed,
  not even by the ledger role in maintenance mode (the triggers of step 1 refuse).

## Interfaces & contracts

- `install(config, *, allow_breaking=False, steps=None) -> SchemaStatus`: needs `archivum.ledger_dsn_env`.
  `steps` exists for tests.
- `schema_status(config) -> SchemaStatus(applied_step, known_step, pending, min_library_version, compatible)`.
- `load_steps(directory=None) -> tuple[Step, ...]`; `Step(number, name, kind, min_library, sql, checksum)`.
- `open_catalog(config) -> context manager yielding Catalog` (recording role). `Catalog.project: Project`,
  `Catalog.scope: str`, and one transaction per call:
  - `register_dataset(namespace, name, *, scope=None) -> Dataset` (again: the existing one);
    `get_dataset(namespace, name, *, scope=None) -> Dataset`.
  - `register_artifact(kind, checksum: bytes, size, *, origin=None, dataset=None, scope=None, record_id=None)
    -> Artifact`: `kind` in `ARTIFACT_KINDS`, `checksum` a 32-byte SHA-256 digest, `size` ≥ 0, `origin` 1 to
    1000 characters (a path or identifier within the source, never an actor), `record_id` a UUIDv7.
  - `get_artifact(artifact_id) -> Artifact` (`UnknownRecordError`); `find_artifacts(*, checksum=None,
    kind=None, dataset=None, scope=None) -> list[Artifact]`, ordered by `event_at, artifact_id`.
  - `namespace` 1 to 200 and `name` 1 to 500 characters; `scope` matches `NAME_PATTERN`.
  - `add_lineage(child, parents, *, run_id=None) -> list[LineageEdge]`; `ancestors(node, *, max_depth=None)` and
    `descendants(node, *, max_depth=None) -> list[LineageNode]`.
- Records (frozen): `LineageEdge(edge_id, child, parent, run_id, event_at, recorded_at)`; `LineageNode(ref,
  depth)`; `Project(project_id, name)`; `NodeRef(kind, id)`; `Dataset(dataset_id, project_id, scope,
  namespace, name, event_at, recorded_at)` and `Artifact(artifact_id, project_id, scope, kind, checksum, size,
  origin, dataset_id, event_at, recorded_at)`, each with a `ref -> NodeRef`.
- Ledger role: `onboard_project(config, project, login_role, *, scopes=("default",)) -> Project`;
  `declare_history_retention(config, project, history, keep_days) -> RetentionRule(rule_id, project, history,
  keep_days, event_at)`; `apply_history_retention(config, *, dry_run=True, now=None) -> list[RetentionResult(
  project, history, rule_id, cutoff, rows, dry_run)]`; `redact(config, project, history, record_id, field,
  reason) -> Redaction(redaction_id, project, history, record_id, field, reason, event_at)`.
- Constants: `ARTIFACT_KINDS` (a test holds it equal to the `artifact_kind` rows); in `history.py`: `HISTORIES`,
  `REDACTABLE_FIELDS`, `REDACTION_REASONS`, `MAINTENANCE_SETTING`.
- Exceptions: `ConfigurationError`, `SchemaVersionError`, `LedgerError`, `ArchivumUnavailable` and the other
  members of `ulduar.errors`.
- Library log lines (loggers `ulduar.freya.*`, no handler of their own): `info` per applied step
  (`ledger`), per created partition month (`catalog`), per onboarding, retention action and redaction
  (`history`); none carries a connection string, a password or a recorded value.

## Extending

A schema change is a new step file with the next number, both headers and statements that only add
(`.claude/skills/ulduar-development/references/workflows/changing-own-schema.md`). Never edit a released
step, and never write `UPDATE`, `DELETE` or `TRUNCATE` against a history table in a step. The Archivum
schema is an ask-first item.

## See also

`../README.md` · `../../ROADMAP.md` (Phase 1) · `../../tests/test_freya_ledger.py` · `../../tests/test_freya_catalog.py` · `../../tests/test_freya_history.py` · `../../tests/test_freya_lineage.py`
