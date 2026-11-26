# ulduar/freya

## Purpose & scope

Freya is the catalog and lineage component; its database is the Archivum (PostgreSQL 18, its own database,
every object in schema `ulduar`). It holds the schema half (the versioned-SQL ledger, the steps that create
the schema, the checks that refuse a schema this library cannot serve) and the catalog half: a
project's datasets and artifacts registered through a catalog session, and the ledger-role calls that
onboard a project, declare and apply history retention and redact a value; and lineage: "derived from"
edges between artifacts and datasets, with ancestors and descendants queries.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | The facade: re-exports the public surface listed under Interfaces & contracts |
| `ledger.py` | `load_steps`, `install`, `check_schema`, `schema_status`, `library_release`, `open_ledger_connection`, the bootstrap, `LEDGER_LOCK_KEY` |
| `_records.py` | Private, below the others: the records (`Project`, `NodeRef`, `Dataset`, `Artifact`, `LineageEdge`, `LineageNode`), `qualified` (the one home of the schema name), `Connection`, `insert_with_partition_retry` |
| `catalog.py` | `ARTIFACT_KINDS`, `open_catalog` and `Catalog` (with `transaction`, the hook the other components record through); the argument checks `check_kind` and `check_origin`, which Hodir reuses to validate a put before it stores anything |
| `lineage.py` | The edge insert and the ancestor and descendant walks, as functions of `(conn, settings, project, …)` that `Catalog` calls with its own |
| `history.py` | Ledger-role calls: `onboard_project`, `declare_history_retention`, `apply_history_retention`, `redact`; the only code that issues `UPDATE` or `DELETE` against history |
| `steps/0001_catalog.sql` | Step 1: roles, tables, functions, triggers, partitions, row-level security and grants |
| `steps/0002_archive.sql` | Step 2 (additive): the archive's tables, the registration trigger, `artifact_is_archived`, the history list widened, the partition functions replaced |

## How it works

- A step is a file `steps/NNNN_<name>.sql`, numbered 1..N without a gap, never edited once released (a new
  step corrects it; before its first release, step 1 was hardened in place after review, owner-confirmed
  2026-11-26). Its leading comment lines hold two required headers: `-- ulduar-kind: additive|breaking` and
  `-- ulduar-min-library: X.Y.Z` (the lowest library release that still works once the step is applied).
  The ledger records, per applied step: number, name, kind, the SHA-256 of the file, the minimum library
  version, the applying library version and the time (`ulduar.schema_ledger`).
- `install(config, *, allow_breaking=False)` runs as the ledger role, in one transaction with transaction-scoped
  time limits: take the advisory lock `LEDGER_LOCK_KEY` (a lock timeout is a `LedgerError`; a second install
  waits, then finds nothing pending), create schema `ulduar` and the ledger table if absent (the bootstrap, the only DDL
  outside a step), verify the checksum of every applied step, refuse a recorded minimum library version above
  this library's or an unknown applied breaking step, refuse a pending breaking step without
  `allow_breaking=True` (nothing is applied), apply each pending step in order and record it, then
  `ulduar.ensure_partitions(now())`. A failure rolls everything back. The steps come from the package
  (`ledger.load_steps`, internal); the private `ledger._install(config, steps, *, allow_breaking)` is the
  one implementation and lets a test supply synthetic steps.
- `check_schema(conn, steps)` is the check made when a session opens: not installed, schema needs a newer
  library, unknown applied breaking step, or known steps still pending raise `SchemaVersionError`; a changed
  checksum of an applied step raises `LedgerError`; an unknown additive step is accepted.
  `schema_status(config)` reads the same as the recording role and returns `compatible=False` instead of
  raising for a too-old or too-new schema.
- `LEDGER_LOCK_KEY` is the first 8 bytes of SHA-256 of `ulduar.schema_ledger`, as a signed big-endian 64-bit
  integer.
- Roles: the ledger role owns schema `ulduar` and everything in it and is the only role that changes the
  schema. Step 1 creates the NOLOGIN group `ulduar_recorder`; each project's login role joins it (onboarding).
  The group holds `USAGE` on the schema, `SELECT` and `INSERT` on `dataset`, `artifact`,
  `lineage_edge`, `SELECT` on `project`, `project_member`, `scope`, `artifact_kind`,
  `history_retention_rule`, `schema_ledger`, and `EXECUTE` on `member_projects`, `node_exists`,
  `is_ledger_role`, `ensure_partitions`; step 2 adds `SELECT` and `INSERT` on `archive_registration`,
  `archive_deletion`, `archive_check`, `archive_finding`, `SELECT` on `archive_retention_rule` and `EXECUTE` on
  `artifact_is_archived`. No `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES` or `TRIGGER`.
- `ulduar_recorder` is cluster-wide (roles are shared by every database of a server). A second Archivum on
  the same server finds it already there, so its ledger role must hold `ADMIN OPTION` on it (granted once by a
  superuser) to onboard a project. Because the group holds the same grants in each Archivum, a recording role
  of one database can also read `schema_ledger` and call `ensure_partitions` in another one, where row-level
  security shows it no row.

### Schema overview (step 1)

| Object | Notes |
|---|---|
| `project`, `project_member`, `scope` | Written by the ledger role only; a login role belongs to one project; scope matches `NAME_PATTERN` |
| `artifact_kind` | `raw_file`, `database_dump`, `report`, `model`, `log` |
| `dataset` | Identified by project, scope, namespace, name; UUIDv7 id; unique on (project, scope, id) so an artifact can reference it with its scope |
| `artifact` | Kind, SHA-256 checksum, size, origin (a path or id within the source, never an actor); its dataset must be in the artifact's own scope (foreign key on project, scope, dataset id); monthly UTC range partitions on `event_at` |
| `lineage_edge` | Child derived from parent (artifact or dataset), optional `run_id`; monthly partitions; a trigger refuses a node outside the project (SQLSTATE `UL002`), a cycle (`UL001`), a transaction that is not READ COMMITTED (`UL003`) and a self-edge |
| `history_retention_rule`, `history_retention_action`, `redaction` | The retention and redaction records of D40; never age out, never redacted |
| `schema_ledger` | The ledger table (bootstrap) |

### Schema overview (step 2)

| Object | Notes |
|---|---|
| `archive_registration` | An artifact whose object Hodir stored: one row per artifact, with the artifact's own scope and `event_at` and its `retention_class` (NULL: kept for ever). No foreign key to the partitioned `artifact` (making a partition would lock this table); the trigger `check_archive_registration` refuses an artifact that is not in the project with that scope and time (`UL002`, `UnknownRecordError`). Leaves only with its artifact, by retention |
| `archive_retention_rule` | Retention rules per (project, class), appended by the ledger role; the latest wins. The recording role can only read it |
| `archive_deletion` | The record of every deletion from the archive: an intent, then its `confirmed` or `cancelled` outcome (one per intent), the cause (`retention`, `incomplete_put`, `purge`), and for a purge the reason. Never ages out, never redacted. `rule_id` is not a foreign key (a rebuild restores deletion records into an Archivum that may lack the rule) |
| `archive_check`, `archive_finding` | One verification or reconciliation and what it found per object; monthly partitions; age out through history retention |
| `artifact_is_archived(project, artifact)` | True while the artifact has a registration and no confirmed deletion of its content (project, scope, checksum) was recorded after it; both times are the database's `recorded_at`, never a client's |
| `history_retention_rule.history` | The CHECK list is widened to `artifact`, `lineage_edge`, `archive_check`, `archive_finding` (a test holds `HISTORIES` equal to it) |

- Step 2 follows step 1's conventions: UUIDv7 ids whose time is `event_at`, `recorded_at` stamped by the database,
  row-level security forced with `ledger_all` and the member policies (a recording role inserts and selects its own
  project's rows of the four archive tables), append-only triggers (`DELETE:retention` on the registration and the two
  check histories, none on the rule and the deletion record) and `refuse_truncate` on all five tables and every
  partition.

- Every id is a UUIDv7 whose time equals the row's `event_at` (a CHECK). `recorded_at` is the database's time: a
  `BEFORE INSERT` trigger (`stamp_recorded_at`) on every table that has it overwrites whatever the inserting role
  supplies with `clock_timestamp()` (the time of the insert, not the transaction's start). So among declares
  serialised by the retention lock, the one that commits last has the latest `recorded_at` and wins.
- Row-level security is enabled and forced on every keyed table. The ledger role passes the `ledger_all`
  policy; a recording role sees and inserts only rows of the project it belongs to (`project_member`).
- Append-only: triggers refuse `UPDATE`, `DELETE` and `TRUNCATE` on the history tables of step 1 (and step 2's, below) unless the ledger
  role has set `ulduar.maintenance` to a mode that passes that operation on that table: a trigger argument is
  `<operation>:<mode>`, so `DELETE` passes only in `retention` (artifact, lineage edge) and `UPDATE` only in
  `redaction` (artifact, dataset); a mode never allows the other's operation. Truncate is always refused,
  on the parents and, because statement triggers are not inherited, on every partition (`ensure_partitions`
  attaches the same `refuse_truncate` trigger to each partition it makes).
- The lineage cycle guard reads the edges the lock holder committed, which only a READ COMMITTED transaction
  sees: the trigger raises `UL003` (`UlduarError`) in any other isolation level, and the library pins READ
  COMMITTED on every connection (`_db.open_connection`), whatever the role or database defaults to.
- Partitions are created only by `ulduar.ensure_partitions(for_time, months_ahead)` (this month and three
  ahead by default) and dropped, when they ended and are empty, by `ulduar.drop_empty_partitions(before)`,
  which only the ledger role may run. `ensure_partitions` takes an advisory lock on the fixed key
  `-6595867142526573570` (the first 8 bytes of SHA-256 of `ulduar.partitions`, a signed big-endian 64-bit
  integer) and tolerates a partition made meanwhile, so concurrent callers all succeed and the result counts
  only what that call made. `for_time` must lie between 15 years ago and 2 years ahead (else it raises, and
  the library's partition retry surfaces a `UlduarError`), which keeps the year at four digits. Both
  functions pin `TimeZone` to UTC and `DateStyle` to ISO, so the bounds never depend on the caller's session.
  `drop_empty_partitions` first locks the four parent tables (`ACCESS EXCLUSIVE`, which locks every partition
  too), `lineage_edge`, `artifact`, `archive_check`, `archive_finding`, and `ensure_partitions` makes its partitions in the same order
  (`CREATE ... PARTITION OF` locks the parent). That is the order of the only recorder that holds both: an edge
  insert locks `lineage_edge`, then its trigger reads `artifact`. Creating an `artifact` partition also takes
  `SHARE ROW EXCLUSIVE` on `dataset`, `scope` and `artifact_kind`, through the cloned foreign keys. The lock
  order is `lineage_edge`, `artifact`, `archive_check`, `archive_finding`, then {`dataset`, `scope`, `artifact_kind`}: every path takes these tables
  in that order, or touches only one of them in a transaction (every other recorder path; each count of a
  retention dry run, which runs in a transaction of its own, as each delete does), so none deadlocks with
  another. `archive_registration` is in no such path: a registration insert reads `artifact`, and the partition
  functions never lock the registration table. Step 2 replaced both functions (`CREATE OR REPLACE`, which keeps
  their grants) to cover `archive_check` and `archive_finding`.

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
- `Catalog.transaction(what, work, *, event_ats=())` is the hook through which Ulduar's other components (Hodir
  now, Thorim and Algalon later) record on the job's one connection: it runs `work(conn)` in one transaction with
  the session's time limits and returns its result; a driver error is raised as the package's exception, worded
  `<what> failed: ...`, and a package error or any other exception from `work` rolls the transaction back and passes
  through. With *event_ats*, the times of the partitioned rows `work` writes, a missing month is made and `work`
  runs again, so it must then be safe to run twice.
- A record id is a UUIDv7 made by the client; its time is the record's `event_at`. `register_artifact`'s
  `record_id` is the idempotency key: the same id with the same content returns the recorded artifact, with
  other content raises `UlduarError`. A new id is made when none is given.
- An insert into a partitioned table whose month has no partition fails (`23514`, "no partition of relation");
  `insert_with_partition_retry` then calls `ulduar.ensure_partitions(event_at, 0)` in a transaction of its own
  and runs the insert once more (the same id, so it cannot duplicate); a missing partition is told from any other
  check violation by its SQLSTATE and by no constraint being named. Every distinct month of a call's rows is
  made. Partitions are never created any other way (hard rule 7).

### Lineage

- `Catalog.add_lineage(child, parents, *, run_id=None, record_ids=None)` records that `child` was derived from
  each parent, one UUIDv7 per edge, in one transaction (all edges or none) through the partition retry; a node
  is an `Artifact`, a `Dataset` or a `NodeRef`. The trigger of step 1, under a per-project lock, refuses a node
  that is not in the project (`UnknownRecordError`, SQLSTATE `UL002`) and an edge that would close a cycle
  (`LineageCycleError`, `UL001`); a self-edge and an empty parent list are a `ValueError` before any SQL.
- Without `record_ids`, recording the same edge again records another edge (new ids), which the queries fold.
  With `record_ids` (one distinct UUIDv7 per parent, else `ValueError`) the call is idempotent like
  `register_artifact`'s `record_id`: each edge's `event_at` is the time of its id, the insert is
  `ON CONFLICT DO NOTHING`, an edge already recorded under its id with the same child, parent and run is
  returned as stored, and with other content the call raises `UlduarError("Lineage edge <id> is already
  recorded with different content")` and records none.
- `ancestors(node, *, max_depth=None)` and `descendants(...)` are one recursive query each (bounded by
  `max_depth` when given), keeping each node once at its minimum depth; ordered by depth, kind and id; the
  node itself is not in the result; `max_depth` must be at least 1. A node the project does not hold, or that
  has no edges, has an empty result. Row-level security limits a walk to the project. The recursion keeps one
  row per node and depth, so a walk costs nodes times depths, not the number of paths; a cycle cannot be
  recorded, and if one got into the table an unbounded walk would end at the statement timeout
  (`ArchivumUnavailable`).
- An artifact whose object is still archived (`artifact_is_archived`) never ages out: the dry run does not count it
  and the delete skips it, until a confirmed deletion of its content is recorded. The delete is one statement
  that also removes the `archive_registration` rows of the artifacts it deletes, and `rows` counts artifacts.
  `archive_check` and `archive_finding` age out by `event_at`, as lineage edges do.
- An edge ages out by its own `lineage_edge` retention rule, so a walk may name a node whose record has aged
  out of `artifact`.

### History: onboarding, retention, redaction

All of these run as the ledger role (`archivum.ledger_dsn_env`, else `ConfigurationError`), in one
transaction each after `ledger.check_schema`, through `ledger_transaction(config, what, *, purpose=None)`: a context
manager yielding a ledger-role connection in one transaction with the session's time limits, after the schema check;
a driver error is raised as the package's exception, worded `<what> failed: ...`, and `ConfigurationError` without
the ledger variable names *purpose* (default *what*). It is public, the hook through which Ulduar's components run
their own ledger-role calls (Hodir's `declare_retention`), the counterpart of `Catalog.transaction`; `history.py` is the only code that issues `UPDATE` or `DELETE`
against history, each only inside the maintenance setting `ulduar.maintenance` (`set_config(..., true)`, so it
ends with the transaction).

- `onboard_project` reuses the project of that name or creates it (UUIDv7 id), adds the scopes (a sequence of
  names; one bare string is a `ValueError`; `ON CONFLICT DO NOTHING`), records the login role as a member and
  grants it `ulduar_recorder`. A login role belongs to one project: one already in another raises `UlduarError`
  naming role and project and keeps nothing; one that does not exist raises `UnknownRecordError`. Repeating it
  changes nothing.
- `declare_history_retention` appends a rule for a project and history (`artifact`, `lineage_edge`, `archive_check`,
  `archive_finding`); the rule
  recorded last wins; `keep_days=None` keeps for ever (also the state when no rule exists). Nothing is removed.
  It takes the advisory lock of its (project, history) pair (`history.retention_lock_name`) first.
- `apply_history_retention(dry_run=True)` takes the latest rule of every project and history that has a
  `keep_days`; the cutoff is `now - keep_days`. A dry run counts the rows with `event_at` before the cutoff and
  changes nothing. Otherwise, per project and history, one transaction deletes those rows (maintenance
  `retention`) and appends a `history_retention_action` (rows removed, cutoff, rule); each such transaction first
  takes the lock of its pair and re-reads the latest rule: one declared since the rules were read that keeps for
  ever stops that pair (no result), one that keeps longer replaces it, and the result and the action name the rule
  and cutoff actually applied; after all of them
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

- `install(config, *, allow_breaking=False) -> SchemaStatus`: needs `archivum.ledger_dsn_env`.
- `ledger_transaction(config, what, *, purpose=None)`: see History; `history.lookup_project_id(tx, project)` (the id of
  an onboarded project, else `UnknownRecordError`) is the module's helper that Hodir's retention shares.
- `schema_status(config) -> SchemaStatus(applied_step, known_step, pending, min_library_version, compatible)`.
- Not public: `Step` and `load_steps` stay importable from `ulduar.freya.ledger` for internal use only.
- `open_catalog(config) -> context manager yielding Catalog` (recording role). `Catalog.project: Project`,
  `Catalog.scope: str`, and one transaction per call:
  - `register_dataset(namespace, name, *, scope=None) -> Dataset` (again: the existing one);
    `get_dataset(namespace, name, *, scope=None) -> Dataset`.
  - `register_artifact(kind, checksum: bytes, size, *, origin=None, dataset=None, scope=None, record_id=None)
    -> Artifact`: `kind` in `ARTIFACT_KINDS`, `checksum` a 32-byte SHA-256 digest, `size` ≥ 0, `origin` 1 to
    1000 characters (a path or identifier within the source, never an actor), `record_id` a UUIDv7.
  - `transaction(what, work: Callable[[Connection], R], *, event_ats=()) -> R`: see the catalog session.
  - `get_artifact(artifact_id) -> Artifact` (`UnknownRecordError`); `find_artifacts(*, checksum=None,
    kind=None, dataset=None, scope=None) -> list[Artifact]`, ordered by `event_at, artifact_id`.
  - `namespace` 1 to 200 and `name` 1 to 500 characters; `scope` matches `NAME_PATTERN`.
  - `add_lineage(child, parents, *, run_id=None, record_ids=None) -> list[LineageEdge]`;
    `ancestors(node, *, max_depth=None)` and `descendants(node, *, max_depth=None) -> list[LineageNode]`.
- Records (frozen): `LineageEdge(edge_id, child, parent, run_id, event_at, recorded_at)`; `LineageNode(ref,
  depth)`; `Project(project_id, name)`; `NodeRef(kind, id)`; `Dataset(dataset_id, project_id, scope,
  namespace, name, event_at, recorded_at)` and `Artifact(artifact_id, project_id, scope, kind, checksum, size,
  origin, dataset_id, event_at, recorded_at)`, each with a `ref -> NodeRef`.
- Ledger role: `onboard_project(config, project, login_role, *, scopes=("default",)) -> Project`;
  `declare_history_retention(config, project, history, keep_days) -> RetentionRule(rule_id, project, history,
  keep_days, event_at)`; `apply_history_retention(config, *, dry_run=True, now=None) -> list[RetentionResult(
  project, history, rule_id, cutoff, rows, dry_run)]`; `redact(config, project, history, record_id, field,
  reason) -> Redaction(redaction_id, project, history, record_id, field, reason, event_at)`.
- `Connection` (`psycopg.Connection[TupleRow]`) is exported for the callbacks of `Catalog.transaction`.
- Constants: `ARTIFACT_KINDS` (a test holds it equal to the `artifact_kind` rows); in `history.py`: `HISTORIES` (a test holds it equal to the schema's CHECK list),
  `REDACTABLE_FIELDS`, `REDACTION_REASONS`, `MAINTENANCE_SETTING`.
- Exceptions: `ConfigurationError`, `SchemaVersionError`, `LedgerError`, `ArchivumUnavailable` and the other
  members of `ulduar.errors`.
- Library log lines (loggers `ulduar.freya.*`, no handler of their own): `info` per applied step
  (`ledger`), per month in which a call made at least one partition (`_records`, with the count), per
  onboarding, retention action and redaction (`history`); none carries a connection string, a password or a
  recorded value.

## Extending

A schema change is a new step file with the next number, both headers and statements that only add
(`.claude/skills/ulduar-development/references/workflows/changing-own-schema.md`). Never edit a released
step, and never write `UPDATE`, `DELETE` or `TRUNCATE` against a history table in a step. The Archivum
schema is an ask-first item.

## See also

`../README.md` · `../../ROADMAP.md` (Phase 1) · `../../tests/test_freya_ledger.py` · `../../tests/test_freya_catalog.py` · `../../tests/test_freya_history.py` · `../../tests/test_freya_lineage.py`
