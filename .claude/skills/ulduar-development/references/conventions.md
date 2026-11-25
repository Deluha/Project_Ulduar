# Conventions — the rulebook

Full forms of the hard rules R1–R11 (short forms: `CLAUDE.md`), the simplicity audit, error handling, and
SQL and archive patterns. Each rule names how it is enforced: a checker rule C1–C9
(`references/checkers.md#checker-project-conventions`), a test, or review.

## R1 generic core, standalone

The package serves any data project. Nothing inside it names a consumer, a consumer's schema, table, folder
or job, or copies a consumer's convention. A consumer integrates through an adapter and a configuration
file that live in the consumer's repository; a sample consumer in `tests/` is a test fixture, not an
exception. Project, dataset, job, schema, table, bucket and path names always arrive as arguments or
configuration, never as constants. Ulduar is also standalone: no other project is named anywhere in the
repository and none is a dependency.

Enforced: C1 (a deny-list of hashed project names, matched inside every letter-run of every text file and
path of the repository), review for the rest.

## R2 one canonical helper

A helper, a constant and a SQL statement are each defined once and imported. Before writing a helper,
grep for an existing one (`references/recipes.md#grep`). Library code reports through the standard
`logging` module so a consumer controls the output; it never prints. Command-line output goes through
the package's console helpers (one module, the icon vocabulary of `references/style.md#messages`).
The dev tools under `dev_tools/` are stdlib-only command-line scripts and print their own status lines.

Enforced: C2 (`print` in package code), review for duplication.

## R3 safe SQL

Ulduar receives names from its caller and its configuration and runs SQL built from them, so every
statement is composed safely:

- An identifier is composed with the driver's identifier quoting.
- A value is bound as a parameter.
- A statement is never built with an f-string, `%`, `+` or `.format()`.

Enforced: C3 (formatted string passed straight to `execute`), review for indirect cases.

## R4 no files the user didn't ask for

`tests/` is the only test location. No planning, progress, notes or decision markdown: the permanent
docs are the root `README.md`, `ROADMAP.md`, `RELEASE_NOTES.md`, `CONTRIBUTING.md`, `SECURITY.md`,
`CLAUDE.md`, one `README.md` per code directory and the files under `.claude/`. Scratch scripts and
notes go to the session scratchpad and are never committed.

Enforced: C4.

## R5 the vault is immutable

- An archived object is written once under its content address and never overwritten; storing an
  identical object again is a no-op, and a different object never reuses a key.
- An object leaves the archive only by a recorded deletion: a retention rule the consumer declared, the
  built-in rule for what an interrupted put left behind, or a purge (`ROADMAP.md` D38, D39). The record
  is written to the Archivum before the object goes, the deletion runs under the retention credential,
  and the credential that writes cannot delete (D35). No other code path deletes from the archive.
- Recorded history — catalog entries, lineage edges, runs, measurements, verifications, verdicts — is
  append-only: a correction, a failure, a re-run and a retention action are new records. No code path
  issues `UPDATE`, `DELETE` or `TRUNCATE` against a history table except the retention the consumer
  declared and the recorded redaction, which the ledger role alone runs (D40); the recording roles cannot
  (D30). No migration of the Archivum rewrites existing rows.

Enforced: review until the stores exist; Phases 1 and 2 add tests that the Archivum and the storage
interface refuse both.

## R6 read-only towards consumers

Ulduar never writes to a consumer's database, source folder or Drive. It reads a database through a
role that can only read and in a read-only transaction, a folder and a Drive through read-only access,
and it writes only to the Archivum and the archive store the consumer gave it. A connector that would
need write access to its source is out of scope and goes to the owner.

Enforced: review; the connectors' phase adds a test that a write through a connector's session fails.

## R7 own schema through the ledger

The Archivum's tables, views and functions are created and changed by steps recorded in Ulduar's own
versioned-SQL ledger. No ad-hoc DDL in application code, no schema created as a side effect of an
unrelated call. The ledger's own table is created by the ledger's bootstrap, the one statement outside a
step.
Workflow: `references/workflows/changing-own-schema.md`.

Enforced: review, plus the workflow's test checklist.

## R8 docs move with the code

A change in a documented area updates that area's doc in the same PR: the area's `README.md`, or
`CLAUDE.md`, or the skill's `SKILL.md` / references. `[skip-docs]` on a line of its own in a commit
message exempts that commit's files; it is an escape hatch, not the default. Area map and mechanics:
`references/checkers.md#checker-docs-sync`.

Enforced: the docs-sync guard (commit-msg hook, verify, and CI where its check is required:
`references/qa-and-ci.md#workflows`).

## R9 real services in tests

- Behaviour that touches the database is tested against PostgreSQL 18, and behaviour that touches the
  archive against an S3-compatible server (and a real directory for the local backend) — never a mock,
  a stub client or another engine.
- A third-party service with no local real server (Google Drive, the GitHub interface) is the one exception: it is
  tested against a local simulated server built from recorded real responses and reached through a
  fixture like any service (`ROADMAP.md` D42). The exception never covers the database or the archive.
- Tests reach the services only through the fixtures of `tests/conftest.py`, or a conftest helper fed a
  fixture's guarded address (`rolled_back_connection(db_dsn)`); a test never opens its own connection or
  client and never names a connection string.
- Every service address must point at this machine. No test, workflow or session connects to a
  consumer's database, Drive folder or bucket, or to any shared service.
- A test never skips itself. Without the test services a service test skips with a notice under plain
  `pytest`, and fails under the verify command and in CI, so the done gate cannot pass untested.

Details: `tests/README.md`, `references/qa-and-ci.md#test-services`.

Enforced: C6, C9, the fixtures' local-only guards, and the required-services switch, under which a run
without a passed database test and a passed object-store test fails.

## R10 one module per component

Each component (`references/architecture.md#components`) is one module, or one sub-package behind a
facade that only re-exports, with an explicit public surface. Components call each other through that
surface. The `mimiron` entry module parses the command line and holds command flow only; every
behaviour it offers is a call into a component, so the library and the CLI can never diverge.

Enforced: C7 (command-line parsing outside the entry module), review for the rest.

## R11 no secrets stored or logged

- Credentials (database passwords, storage keys, service-account files, tokens) arrive from the
  environment or a file the consumer points at, at run time, and are held only in memory.
- None is written to the Archivum, an archived object's metadata, a log line, an exception message,
  CLI output or a verdict. A connection string is logged with its password removed, or not at all.
- None is committed: tracked secret files fail C8 and the secret scan
  (`references/checkers.md#checker-secret-scan`). The test services' throwaway credentials are the one
  exception (`SECURITY.md`).

Enforced: C8, gitleaks, review; each phase that handles a credential adds a test that it does not
appear in the log output or the recorded rows.

## Simplicity audit

Every PR leaves touched files clearer, more concise and more modular.

| Property | Rule |
|---|---|
| Clarity | Obvious names; small focused functions; no cleverness |
| Conciseness | No dead code or copy-paste duplication; a function > ~60 lines is split into helpers |
| Modularity | Shared logic is lifted to one home; never copy a helper between modules |
| Single source of truth | Table names, SQL statements, key layouts, version numbers and limits each live in exactly one place — duplication is a bug |

**Audit (mandatory per PR):** before finalizing, audit every touched file plus files directly referenced
by the changes — Python, SQL and Markdown alike; fix each finding or explicitly justify leaving it:
1 dead imports · 2 unused variables/constants · 3 commented-out code (delete; git history keeps it) ·
4 duplicated logic → shared helper · 5 orphaned helpers · 6 over-long functions → named helpers ·
7 over-engineering (speculative parameters, unused hierarchies, premature abstraction) · 8 magic numbers
→ named constants · 9 patterns inconsistent with the codebase → align unless documented · 10 redundant
comments restating code → *why* comments or delete.

| Tier | Action | Examples |
|---|---|---|
| **High** | Just do it | Remove a dead import; delete a commented-out block; name a magic number |
| **Medium** | Do it, note it in the PR description | Rename for clarity; split an oversized function |
| **Low** | **Never unilaterally — ⚑ flag and ask first** | A public signature change; anything on the CLAUDE.md "Ask first" list |

Low-tier findings are never silently bundled into a PR. Surgical over sweeping: no multi-file refactor
when a targeted change suffices, no touching unrelated code in a PR; surface nearby smells separately.

## Error handling

- **Stores:** catch the driver's or client's specific exceptions, add context and **re-raise** — never
  swallow. A failed write leaves no catalog entry for an object that was not stored, and no stored
  object without its catalog entry being retried or reported. Re-wrap with `raise … from e`.
  The one exception is a run's recording path: a failure to record is reported and never raised into the
  consumer's job, while a command whose purpose is the archive or the verdict still fails with its own
  exit code (`ROADMAP.md` D33).
- **Integrity:** a checksum mismatch is never retried away or downgraded to a warning; it is recorded
  and raised.
- **Inputs:** validate early (an unknown project, an empty path, a missing configuration key) and fail
  loudly with a specific exception naming the offending value; never silently default.
- **Public errors:** the package raises its own exception types from one module, so a consumer can
  catch them without importing a driver or a storage client.
- **Type hints** on every function; `mypy --strict` gates the package and the dev tools.

## SQL and archive patterns

- One transaction per recorded event; a record and the state it describes commit together or not at all.
- Read the system catalogs (`pg_catalog`, `information_schema`) rather than parsing DDL text.
- Statements are idempotent where the engine allows it (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`).
- The Archivum's objects live in their own schema, never in a consumer's.
- An object is streamed, never read whole into memory; its checksum is computed while it streams and
  verified again on the way out.
- An object key or a file path built from a caller's name stays inside its prefix or root: a `..`
  segment, an absolute path or a separator inside a name is refused, never resolved.
