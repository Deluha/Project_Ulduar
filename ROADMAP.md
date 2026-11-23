# Project Ulduar — Roadmap

The planned scope of the coming releases. `RELEASE_NOTES.md` (from Phase 0 on) records what shipped; this
file records what is planned. A release's section leaves this file when the release is tagged. Owner
decisions are dated.

Approved by the owner on 2026-11-22: the concept, the form, the process timing and every scope rule
below (D1–D14). What is still marked *proposed* inside a phase is that phase's starting position,
presented as a concrete proposal and confirmed at its sign-off. Nothing is built yet.

Decided by the owner on 2026-11-23, in the planning session for the first personal-data consumers and in
the review of the pull request that followed: decisions D15–D26 and the personal-data release they add,
whose section follows v0.1's (v0.2 — Personal data). v0.1 stays as approved. Everything in that section
is *proposed* until the owner confirms it, unless it restates one of those decisions.

## Why

The gaps of a typical data project, as surveyed on 2026-11-22:

| Gap | Today |
|---|---|
| Run history | No runs, timings or row counts are recorded; workflow logs expire after 30 days. |
| Archive | Database dumps live 14 days as CI artifacts; a shared drive holds the only copy of the raw files; reports and trained models are not kept. |
| Monitoring | Freshness is printed, never judged; nothing checks volume drift or missed runs; no dump has its restore verified. |
| Catalog and lineage | None. |
| Other planned consumers | Backups, alerting and audit are planned, not built. |

## Components

Named after the keepers and watchers of Ulduar, the titan vault.

| Name | Lore | Role |
|---|---|---|
| **Freya** | Keeper of life, tender of the Conservatory | Catalog: registry of projects, datasets and artifacts, and the lineage between them. Its database is **the Archivum**. |
| **Hodir** | Keeper of winter | Cold archive: immutable, content-addressed storage of raw files, database dumps, reports and models; retention; integrity and restore verification. |
| **Thorim** | Keeper of storms, master of the Arena | Run ledger: every run with its status, timings, row counts and check results, linked to what it read and wrote. |
| **Algalon** | The Observer, who judges whether a world still stands | Monitoring: judges freshness, missed runs, volume drift, backup age and archive integrity from what the other three recorded; reports a verdict and raises alerts. |
| **Mimiron** | The tinkerer | The `mimiron` CLI, the connectors (a directory, `pg_dump`, Google Drive) and the GitHub Action a consumer's workflow calls. |

## Owner decisions and scope rules

The permanent section (D22): decisions stay here, in number order, when a release's section leaves
this file. A dated note amends a decision; its original text is kept — except where D26 required
rewording.

### Scope rules (owner decisions, 2026-11-22)

- **Vault and observatory (D1).** All five components are in v0.1, built in phases: catalog and archive
  first, then tooling, run ledger and monitoring.
- **Library and CLI first (D2).** A Python package and a CLI that run inside each consumer's existing
  GitHub Actions. Nothing to host or keep online in v0.1. A read-only web view and an HTTP API come in
  a later release.
- **Process set up in Phase 0 (D3).** The development process (`CLAUDE.md`, agents, skills, hooks,
  checkers, CI) is set up in the first development session. This planning session wrote this file only.
- **Delivery (D4).** One pull request per phase, several commits each. Each phase lists what the owner
  confirms before its code is written.

- **Generic core (D5).** No consumer's name, schema, table or convention enters the package. A consumer
  integrates through an adapter and a configuration file in its own repository.
- **Read-only towards consumers (D6).** Ulduar never writes to a consumer's database or source folder. It
  reads through a role that can only read, and it writes only to the Archivum and the archive store.
- **The vault is immutable (D7).** An archived object is never overwritten. Deletion happens only by a
  declared retention rule and is itself recorded. Recorded history (catalog entries, runs, verdicts)
  is corrected by a new record, never by an edit.
- **Stores (D8).** PostgreSQL 18 for the Archivum; an S3-compatible bucket or a local directory for the
  archive, behind one storage interface.
- **Own schema through its own ledger (D9).** The Archivum's tables are created and changed through
  versioned SQL steps that Ulduar applies itself and records in a ledger table of the Archivum. No
  external migration library.
- **Real services in tests (D10).** Database behaviour is tested on a real PostgreSQL 18 and archive
  behaviour on a real S3-compatible server, both throwaway containers. No mocks; a skipped service
  test fails the done gate.
- **Never a live system (D11).** No test, workflow or development session of this repository connects to a
  consumer's database, Drive folder or bucket. Running Ulduar against live data is the consumer
  owner's own run.
- **No secrets (D12).** Credentials arrive from the environment at run time. None is stored in the
  Archivum, written to a log or committed.
- **Not a backup system of record (D13).** Hodir archives logical dumps and files and proves they can be
  restored. Point-in-time recovery stays outside Ulduar, with whoever operates the consumer's database
  servers.
  *Amended 2026-11-23 (D20):* a restore check that needs plaintext runs inside the consumer's realm,
  never in Ulduar.
- **No personal data in v0.1 (D14).** The first consumer's data holds no personal data. Archiving
  personal data needs encryption, retention and erasure rules first.
  *Amended 2026-11-23 (D17):* holds for v0.1 as written; lifted only for the personal-data release
  (v0.2 — Personal data).

### Owner decisions (2026-11-23)

D15–D19 were taken in the planning session for the first personal-data consumers; D20–D26 were
confirmed the same day in the review of the pull request that added the personal-data release.

- **An operations platform as consumer (D15).** A consumer may be an operations platform: it deploys
  applications, maintains their PostgreSQL servers and backups, and collects logs and metrics. Its
  applications may hold personal data.
- **Such a consumer uses Ulduar in full (D16).** Thorim records every operations job as a run; Freya
  catalogs dumps and reports; Hodir archives database dumps and collected logs with integrity
  verification; Algalon judges backup age, missed schedules and overdue restore drills, shown in the
  consumer's own console.
- **A personal-data release (D17).** That use includes dumps of applications that hold personal data.
  Before any such data is archived, Ulduar needs required client-side encryption, per-tenant
  separation, declared retention, and recorded deletion and erasure. This release is added to the
  roadmap for them, and D14 is lifted only for it.
- **Not on the critical path (D18).** The consumer writes dumps, encrypted on the database host, to each
  realm's own object-locked bucket first; the hand-over to Ulduar's archive is switched on per realm
  later and is never a condition for a realm to hold real data. Ulduar blocks no consumer.
- **The archive is per realm (D19).** A realm is one environment of one application (for example
  `app/uat`). Each realm's archived objects live in that realm's own backup project — its own bucket and
  its own storage credential — so one environment's credential cannot reach another's backups. The
  Archivum (catalog and run ledger) lives in the consumer's management realm and holds metadata only.
- **Point-in-time recovery and plaintext restore checks (D20).** Point-in-time recovery sits with the
  consumer's operations platform, never with Ulduar; a restore check that needs plaintext runs inside
  the consumer's realm, never in Ulduar. Recorded as a dated note on D13.
- **Operations modules are not Ulduar's (D21).** Infrastructure modules (Terraform, VPN, alert routing)
  have no home in this repository; they belong to the consumer's operations platform.
- **Scope rules outlive v0.1 (D22).** The scope rules D1–D14 with their dated notes live in a permanent
  section of this file and do not leave with the v0.1 section. Phase 6's release row is amended
  accordingly. The roadmap was restructured on 2026-11-23 so that this section is that permanent one.
- **The consumers' constraints are decided (D23).** What the first personal-data consumers require of
  this release is decided, not proposed: dumps reach Ulduar as ciphertext only, with the private keys on
  the realm's database host and in an offline escrow, never in the management realm and never available
  to Ulduar; the consumer's post-restore step runs after every path that brings back older data, an
  archive restore verification included, before the data is used for anything, and Ulduar never applies
  erasure or needs plaintext to honour it; retention comes from the consumer's manifest and applies to
  every archived copy; an offboarded tenant's copies age out; deleting a key is never reported as
  erasure, and an erasure or offboarding certificate uses the longest retention among backup lock, dumps
  and archived copies; what Ulduar stores about a consumer carries identifiers, states, counts, sizes
  and checksums only, never names or record contents; legal classification and retention numbers stay
  with the consumer and its counsel. The scope rules P1–P9 restate them.
  *Amended 2026-11-23 (D24):* times of Ulduar's own events may be stored as well.
- **Times are metadata (D24).** What Ulduar stores about a consumer may carry times of its own events:
  when a run started and ended, when a copy was archived, verified or deleted. It never carries a time
  taken from a record's contents.
- **The personal-data release is v0.2 (D25).** Its name is "v0.2 — Personal data" and its phases are
  7–11, so other plans can cite them. What the release contains beyond the owner decisions stays
  *proposed* until its phases' sign-offs.
- **Standalone (D26).** Ulduar is a standalone library: it depends on none of the owner's other projects
  and names none of them anywhere in this repository. Consumers appear by role only. D3, D9, D13–D16,
  D18–D21 and D23 were reworded in place on 2026-11-23 to that end, and the schema ledger is Ulduar's
  own; a checker enforces the rule.

## Status

| Phase | Release | Status | Pull request |
|---|---|---|---|
| 0 — Development process | v0.1 | Merged | [#2](https://github.com/Deluha/Project_Ulduar/pull/2) |
| 1 — Freya | v0.1 | Not started | |
| 2 — Hodir | v0.1 | Not started | |
| 3 — Mimiron | v0.1 | Not started | |
| 4 — Thorim | v0.1 | Not started | |
| 5 — Algalon | v0.1 | Not started | |
| 6 — First adoption and release | v0.1 | Not started | |
| 7 — Scopes and the catalog | v0.2 | Not started | |
| 8 — Ciphertext and separation | v0.2 | Not started | |
| 9 — Retention, deletion and erasure | v0.2 | Not started | |
| 10 — Operations runs and verdicts | v0.2 | Not started | |
| 11 — Personal-data adoption and release | v0.2 | Not started | |

## Running a phase

One development session per phase, in order. A phase starts only when the previous phase's pull request
is merged. Kick-off prompt: "Implement Phase N of `ROADMAP.md` following its Running a phase section."

1. **Read.** This file, then `CLAUDE.md` and the development skill (both exist from Phase 0 on).
2. **Sign-off first.** Present the phase's sign-off items to the owner as concrete proposals (table
   layouts, signatures, CLI output samples, configuration samples) and wait for confirmation before
   writing code. Record each confirmed decision under the phase, dated, in the phase's first commit.
3. **Implement.** One commit per table row, in order, each passing the verify command.
4. **Review.** The `reviewer` agent reviews the whole phase diff before the pull request opens; its
   findings are fixed in further commits and reviewed again.
5. **Deliver.** One pull request for the phase; the phase's Status row names it ("In review"). The next
   phase's first commit sets the previous row to "Merged". Each phase adds its entry to the release's "in
   development" section at the top of `RELEASE_NOTES.md` ("v0.1.0 — in development", then "v0.2.0 — in
   development"); the release's last phase (Phase 6, Phase 11) turns it into the release entry and
   removes that release's section from this file.
6. **Stay in scope.** The scope rules bind every phase. A phase that finds an agreed item infeasible, or
   needs something not listed here, stops and asks the owner; it never narrows or widens scope itself.

*Proposed*: Phase 7 starts only when v0.1.0 is tagged. Phases 7–11 change ask-first items of `CLAUDE.md`
(the Archivum schema, the object layout, the CLI and configuration surface, archive deletion and
retention); a phase's sign-off is where the owner confirms them.

## v0.1 — A vault and observatory for data projects

Goal: a generic Python library and CLI that gives a data project a vault and someone watching it — an
immutable archive of what went in and what came out, a catalog of what exists and where it came from,
a record of every run, and a verdict on whether the data is fresh, complete and recoverable. A
data-pipeline project is the first consumer, not the design target.

### Phase 0 — Development process

Sets up the development process: guidance, agents, hooks, checkers and CI. No product code.

| Commit | Content |
|---|---|
| 1 | `CLAUDE.md`: the four operating principles, short-form hard rules with anchors, the ask-first list — written for this repository. |
| 2 | Agents (`implementer`, `reader`, `reviewer`, `scribe`), hooks (session start, post-edit check) and `.claude/settings.json` with an allow / ask / deny shape. |
| 3 | Development skill as a router with `references/` (architecture and codenames, conventions, style, recipes, QA and CI, checkers, workflows, evals), the path rules, and the generic command skills `verify`, `review-diff`, `db-change`, `guidance-change`. |
| 4 | Package skeleton (`pyproject.toml`, an empty package, `tests/`), the verify command, and the conventions, guidance-integrity and docs-sync checkers with their tests. |
| 5 | Pre-commit, gitleaks, `.editorconfig`, and the CI workflows: pre-commit, docs sync, tests with PostgreSQL 18 and S3-compatible service containers, and the single "CI gate" check. |
| 6 | Local test services: the session-start hook and the dev container start PostgreSQL and the object store and export their connection settings. |
| 7 | `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `RELEASE_NOTES.md`, the pull request template. |

Sign-off:

- The hard rules: their final wording in `CLAUDE.md`. They restate scope rules D5–D12. *Proposed*: generic core; one canonical helper; safe SQL; no unasked files; the vault
  is immutable; read-only towards consumers; own schema through the ledger; docs move with the code;
  real services in tests; one module per component with a flow-only CLI entry; no secrets stored or logged.
- The ask-first list. *Proposed*: the Archivum schema; the archive's object layout and checksum
  definition; public API and CLI surface; any deletion from the archive and every retention default;
  Algalon's default thresholds and any relaxation of a check; runtime dependencies; supported
  PostgreSQL versions; connecting anything to a system other than the local test services; the
  guidance machinery and CI.
- Package name and layout. *Proposed*: distribution `project-ulduar`, import package `ulduar`, one
  module per component (`freya`, `hodir`, `thorim`, `algalon`), `mimiron` the only CLI entry, dev
  tooling in `dev_tools/`; Python 3.11 or newer.
- Which conventions apply. *Proposed*: icon status helpers for CLI output only, `logging` in library
  code, strict gating mypy, actions pinned by commit SHA, no `#%%` markers.
- How development sessions get PostgreSQL 18 and an object store. *Proposed*: the official `postgres:18`
  image and a MinIO image, started by the session-start hook; the same images in CI.
- Whether sessions install a review plugin.
- The licence (MIT today).

Sign-off decisions (owner, 2026-11-22: S1–S6 confirmed; S5's object-store image chosen after the
confirmed MinIO image proved unavailable):

| # | Item | Decision |
|---|---|---|
| S1 | Hard rules and ask-first list | Eleven hard rules: generic core; one canonical helper; safe SQL; no unasked files; the vault is immutable; read-only towards consumers; own schema through the ledger; docs move with the code; real services in tests; one module per component with a flow-only `mimiron` entry; no secrets stored or logged. The four operating principles. Ask-first as proposed above plus the licence and the scope rules of this file, as shown at sign-off; the guidance machinery and CI path-gated by `permissions.ask`. Text: `CLAUDE.md`. |
| S2 | Package and layout | Distribution `project-ulduar`, import package `ulduar`, flat at the repository root; one module per component (`freya`, `hodir`, `thorim`, `algalon`); `mimiron` the only CLI entry; dev tooling in `dev_tools/`, not shipped; verify command `python dev_tools/verify.py`; Python 3.11 or newer. |
| S3 | Conventions | Icon status helpers for CLI output only, `logging` in library code, strict gating mypy, actions pinned by commit SHA, no `#%%` markers; with them: codenames, the single `CI gate` check, gitleaks, pre-commit, the path-rule and eval mechanisms, the `settings.json` shape and agent tiers. |
| S4 | Licence | MIT, unchanged. |
| S5 | Local test services | PostgreSQL: the official `postgres:18` image. Object store: `pgsty/minio:RELEASE.2026-08-04T00-00-00Z`, a community MinIO build, pinned — the official `minio/minio` image is no longer on Docker Hub and quay.io is unreachable from cloud sessions. Both started by `python dev_tools/services.py start` (from the cloud session-start hook, locally and in CI) and as compose services in the dev container. No fallback: a service that cannot start fails its tests. |
| S6 | Session plugin | None installed in v0.1's Phase 0; adding one later is a guidance change. |

### Phase 1 — Freya

| Commit | Content |
|---|---|
| 1 | Configuration and connection: how a consumer names its project and points Ulduar at the Archivum. |
| 2 | The schema ledger (versioned SQL steps, applied once and recorded) and the Archivum schema installed through it. |
| 3 | Catalog model: project, dataset, artifact (kind, checksum, size, origin, time), with register and lookup. |
| 4 | Lineage: "derived from" edges between artifacts and datasets; ancestors and descendants queries. |
| 5 | Docs and release-notes entry. |

Sign-off: the Archivum's tables and columns; whether it is its own database or a schema inside a
consumer's (*proposed*: its own database, so one Archivum serves several projects); the artifact kinds
(*proposed*: raw file, database dump, report, model, log); what identifies a dataset across runs; the
connection contract; the ledger's table and step format.

### Phase 2 — Hodir

| Commit | Content |
|---|---|
| 1 | Storage interface with two backends: a local directory and an S3-compatible bucket. |
| 2 | Put: stream an object in, checksum it, store it under its content address, register it with Freya; an identical object is stored once. |
| 3 | Get and restore: fetch by catalog entry, verify the checksum on the way out. |
| 4 | Verify: re-read stored objects and compare checksums, in full or by sample; record the result. |
| 5 | Retention: declared classes, a dry-run listing of what a rule would remove, recorded deletion. |
| 6 | Docs and release-notes entry. |

Sign-off: the object key layout; the checksum (*proposed*: SHA-256); retention classes and their
defaults (*proposed*: nothing is deleted unless a consumer declares a rule); client-side encryption
before upload (*proposed*: optional in v0.1, required before any personal data); whether bucket
versioning or object lock is required of a consumer's bucket.

### Phase 3 — Mimiron

| Commit | Content |
|---|---|
| 1 | `mimiron` CLI skeleton, configuration loading, console helpers, exit codes. |
| 2 | `mimiron archive put / get / list / verify` and `mimiron catalog show / lineage`. |
| 3 | Connector: a directory of files (reports, exports, models). |
| 4 | Connector: `pg_dump` of a database, read-only, streamed into the archive. |
| 5 | Connector: a Google Drive folder, archiving files that are new or changed since the last run. |
| 6 | A composite GitHub Action that installs Ulduar and runs one `mimiron` command. |
| 7 | Docs and release-notes entry. |

Sign-off: command names, flags and output layout; exit codes for use in CI; the configuration file
format (*proposed*: one TOML file in the consumer's repository); how the Drive connector authenticates
(*proposed*: the consumer's own service account, read-only scope); where the GitHub Action lives.

### Phase 4 — Thorim

| Commit | Content |
|---|---|
| 1 | Run model: a run of a named job, with start, end, status, and where it ran (workflow, commit). |
| 2 | Python API: a context manager that opens and closes a run and records a failure before re-raising. |
| 3 | Measurements: named numbers per run (row counts, durations, file counts). |
| 4 | Inputs and outputs: a run links to the artifacts it read and wrote, feeding Freya's lineage. |
| 5 | `mimiron run -- <command>`: wrap any command as a recorded run; `mimiron runs list / show`. |
| 6 | Docs and release-notes entry. |

Sign-off: the run tables; how a run that never reported its end is represented; the measurement
naming scheme; what Thorim reads from the GitHub Actions environment; whether a consumer's own check
results (such as data-quality checks) are stored as measurements or as their own records.

### Phase 5 — Algalon

| Commit | Content |
|---|---|
| 1 | Expectation model: declared per project in configuration, evaluated only from recorded history. |
| 2 | Freshness and schedule: a dataset or job older than its allowed age; a scheduled run that never came. |
| 3 | Volume: a measurement outside a declared range, or outside a tolerance of its own recent history. |
| 4 | Vault health: the newest dump older than allowed; an integrity verification overdue or failed. |
| 5 | Restore drill: restore the newest dump into a scratch database and record whether it loads and what it holds. |
| 6 | `mimiron observe`: one verdict table, exit codes, and the verdict recorded as history. |
| 7 | Notifiers: a GitHub issue per failing expectation, opened once and closed on recovery; a generic webhook. |
| 8 | Docs and release-notes entry. |

Sign-off: the expectation vocabulary and its configuration layout; default thresholds (*proposed*:
none — every threshold is declared by the consumer); the verdict levels and their names; where the
restore drill may run (*proposed*: only a throwaway local instance, never a consumer's server); the
issue format and labels.

### Phase 6 — First adoption and release

| Commit | Content |
|---|---|
| 1 | Adoption guide: installing the package, the configuration file, the storage and database a consumer provides. |
| 2 | End-to-end test: a sample consumer project archived, recorded and observed through its configuration alone. |
| 3 | v0.1.0 release: version, release notes, this section removed; the scope rules D1–D14 with their dated notes stay in the permanent section (owner decision, 2026-11-23, D22). |

The consumer's side is a separate pull request in its own repository, under that repository's own
rules: its workflows and anything that reaches its database are ask-first items there. Candidates, in
the order of the gaps above: archive the `pg_dump` its pipeline takes instead of letting it expire;
archive each raw file when it is ingested; record its pipeline runs with their row counts; run
`mimiron observe` on a schedule.

Sign-off: how the first consumer installs the package (git dependency or a package index); which bucket
and which database host the Archivum and the archive use, and who pays for them; which of the
candidates above it adopts first; the read-only database role Ulduar connects with.

## v0.2 — Personal data

Goal: let a consumer archive, catalog and observe data that holds personal data — first the database
dumps and collected logs of the applications an operations platform runs — without Ulduar ever seeing a
dump's plaintext (*proposed*: nor that of any other object declared personal data), with each tenant's
copies separable where the consumer declares a tenant, every copy under a declared retention, and every
deletion recorded. An operations platform is the first such consumer, not the design target. v0.1 stays
as approved, with no personal data (D14).

Everything in this section that does not restate an owner decision (D15–D26) is *proposed*: the starting
position, to be confirmed by the owner in the review of the pull request that added this section or at a
phase's sign-off. A confirmed item loses its *proposed* mark and is recorded, dated; a confirmed scope
rule takes a D number and moves to the permanent section. A merge alone confirms none of them. Nothing is
built yet.

### Scope rules for personal data (P1–P9)

They restate the consumers' constraints, which are decided (D23). What a rule adds beyond them is marked
*proposed* and is confirmed by the owner at the phase sign-off that names it. D1–D13 bind this release,
D13 with its note of 2026-11-23 (D20); where the reading of another rule is open (D6 in Phase 7, D2 in
Phase 11), that phase's sign-off says so.

- **Ciphertext only (P1).** A dump is encrypted on the database host before it leaves (in the consumers'
  plans, with one recipient key per database). Ulduar handles ciphertext only for dumps: it receives,
  stores, checksums and verifies it, and never holds, reads or is configured with a private key. The
  private keys stay on the realm's database host and in an offline escrow; they are never in the
  management realm and never available to Ulduar. *Proposed*: Hodir refuses an object of a personal-data
  class that is not declared encrypted; the `pg_dump` connector is not used on a database that holds
  personal data, because Ulduar is handed the dump's ciphertext; how collected logs are encrypted —
  where, and with which key — is the consumer's to declare (Phase 8 sign-off).
- **Integrity on ciphertext, plaintext in the realm (P2).** Hodir's checksum and verification work on
  ciphertext. A restore check that needs plaintext runs inside the consumer's realm, never in Ulduar
  (D20). The operations platform calls the application's post-restore step (for example applying an
  erasure ledger) after every path that brings back older data, an archive restore verification
  included, before the restored data is used for anything. Ulduar never applies erasure to a consumer's
  data and never needs to read plaintext to honour it. *Proposed*: the consumer reports the outcome of
  an in-realm restore check, Thorim records it and Algalon judges its age; Algalon's restore drill
  (restoring the newest dump into a scratch database) does not run on a personal-data class.
- **Per-realm archive, metadata-only Archivum (P3).** A realm's archived objects live in that realm's own
  backup project, under its own storage credential; the Archivum lives in the consumer's management realm
  and holds metadata only (D19). Credentials arrive from the environment at run time (D12). *Proposed*:
  the storage configuration names one bucket and one credential per realm, and no configuration gives
  one credential reach into two realms.
- **Per-tenant separation (P4).** One tenant's archived copies are kept separable from another's (D17).
  *Proposed*: the object key layout and the catalog carry the scopes the consumer declares (for example
  realm, database and, where there is one, tenant), so that a declared tenant's copies can be
  listed, aged out and deleted without touching another's; the content address is scoped inside that
  prefix, so an identical object is never deduplicated across tenants or realms.
- **Declared retention on every copy (P5).** Retention numbers come from the consumer's manifest and
  apply to every archived copy. Deletion happens only by the declared rule and is itself recorded (D7).
  The numbers are the consumer's and its counsel's; Ulduar invents none. *Proposed*: no default — a
  personal-data class without a declared retention is refused.
- **Deleting a key is never erasure (P6).** The deletion of an encryption key is never reported as
  erasure: Ulduar reports a copy as gone only when it aged out by retention or was deleted by a declared
  rule, itself recorded. An erasure or offboarding certificate is issued by the consumer and uses the
  longest retention among backup lock, dumps and archived copies. How the legal documents classify these
  tools is with the consumer's counsel; nothing here classifies them, and Ulduar writes no certificate
  and no legal text. *Proposed*: Ulduar supplies recorded evidence for the archived copies it holds,
  those that hold several tenants' data included; the backup lock and copies held elsewhere are the
  consumer's to count.
- **Offboarding (P7).** When a consumer reports a tenant as offboarded, that tenant's archived copies age
  out by their declared retention, without touching other tenants (P4).
- **No personal data in the catalog or the run ledger (P8).** Catalog entries, run records and anything
  else Ulduar stores about a consumer carry identifiers, states, counts, sizes and checksums only — never
  names or record contents. Times of Ulduar's own events may be stored, never a time taken from a
  record's contents (D24). *Proposed*: a tenant is an opaque identifier issued by the consumer.
- **Boundaries unchanged (P9).** The boundaries of D6, D11 and D13 hold, D13 as amended by its note
  (D20). No test, workflow or development session of this repository touches a realm; tests use synthetic
  ciphertext on the local test services (D10). *Proposed*: the package fixes two roles only — the storage
  boundary (one bucket, one credential) and the unit of separation — and learns from configuration which
  of a consumer's scopes fill them; "realm" and "tenant" in this section are a consumer's names for
  them, never names in the package (D5).

### Phase 7 — Scopes and the catalog

| Commit | Content |
|---|---|
| 1 | Configuration: the consumer's declared scopes; one storage configuration per storage boundary (for example a realm), each with its own credential from the environment. |
| 2 | Archivum schema change through the ledger: scope, data class and the declared encryption of an artifact. |
| 3 | Field rule: the catalog and run records of a personal-data project accept identifiers, states, counts, sizes, checksums and times of Ulduar's own events (D24) only; enforced and tested. |
| 4 | Docs and release-notes entry. |

Sign-off: what the catalog may store (*proposed*: the P8 list, plus the declared scopes, an opaque tenant
identifier, the data class, the encryption format and the public recipient-key identifier; no free text
on a personal-data project); whether the scope levels are fixed or declared by the consumer (*proposed*:
declared, so the package fixes no consumer's hierarchy — collected logs have no database level) and what
the package calls them; how a realm maps to a catalog project; how a data class relates to v0.1's
artifact kinds and retention classes; the per-realm storage configuration and how a credential is named
per realm; whether a realm's archive bucket is the bucket the consumer writes first or a second one, and
how D6 reads in either case; where the Archivum runs in the management realm and which roles reach it;
how an opaque tenant identifier is handled if the consumer's counsel treats it as personal data; whether
Ulduar catalogs or ignores the small integrity records an application writes to the export location a
realm offers in its backup project, for example an audit chain head (*proposed*: catalogs them by
identifier, size and the checksum the application supplies, never reading them).

### Phase 8 — Ciphertext and separation

| Commit | Content |
|---|---|
| 1 | Object key layout with the declared scope prefix; the content address scoped inside it. |
| 2 | Required encryption: put refuses an object of a personal-data class that is not declared encrypted; Hodir never decrypts. |
| 3 | Get and verify on ciphertext: the checksum is of the stored bytes; get returns ciphertext only. |
| 4 | Docs and release-notes entry. |

Sign-off: the encryption scheme the consumer uses and whether Ulduar handles public keys — private keys
never, as P1 decides (*proposed*: no public keys either; it records only the format and the recipient-key
identifier the consumer declares; for personal data the consumer encrypts, not Hodir, unlike Hodir's
optional client-side encryption); how Hodir recognises ciphertext (*proposed*: the declared format plus a
check of the format's header, no decryption); the per-tenant object key layout; how a copy that holds
several tenants' data is recorded (*proposed*: as shared — it cannot be separated and ages out by its
class); whether collected logs are a personal-data class (*proposed*: yes, unless the consumer declares
otherwise) and, if so, who encrypts them, where and with which recipient key — P1's key per database does
not apply to them; what is required of a realm's bucket (versioning, object lock) and how a lock period
or a kept version relates to the declared retention and to a recorded deletion; whether the `pg_dump`
connector is barred from a database that holds personal data (P1).

### Phase 9 — Retention, deletion and erasure

| Commit | Content |
|---|---|
| 1 | Required retention: a personal-data class without a declared retention is refused; the rule applies to every archived copy. |
| 2 | Recorded deletion: the record of what was deleted, by which rule, when and in which run, keeping the identifier, checksum and size and no content. |
| 3 | Offboarding: a consumer's report that a tenant is offboarded, recorded as a new record; that tenant's copies age out by their declared retention; a dry-run listing of the copies and when each ages out. |
| 4 | Erasure evidence: per unit of separation (for example a tenant or a database), the copies Ulduar holds or held (those that hold several tenants' data included), their classes, the longest declared retention among them and their deletion records; a key deletion is never counted. |
| 5 | Docs and release-notes entry. |

Sign-off: the retention classes and the recorded-deletion record; how a consumer reports an offboarded
tenant (*proposed*: a `mimiron` command and a library call run by the consumer's own job); who may run a
deletion and with which credential; the layout of the erasure evidence. No retention number and no
certificate text is written here: both stay with the consumer and its counsel.

### Phase 10 — Operations runs and verdicts

| Commit | Content |
|---|---|
| 1 | Operations jobs as runs: Thorim records a job of a realm with the P8 fields only; command output is not stored. |
| 2 | Reported restore verification: the outcome of a drill run inside the realm, and whether the application's post-restore step was reported complete. |
| 3 | Expectations per realm: backup age, a missed schedule, an overdue restore drill, a copy still present past its retention. |
| 4 | Verdicts for a consumer's console: a machine-readable form of the `mimiron observe` verdict. |
| 5 | Docs and release-notes entry. |

Sign-off: the fields of an operations run; the contract by which a realm reports a restore verification;
the new expectations and their names (*proposed*: every threshold is declared by the consumer); how
the consumer's console reads verdicts (*proposed*: the library or the machine-readable CLI output — the
web view and HTTP API stay under Later); the notifier for a realm's failing expectation; whether
Algalon's restore drill is switched off for a personal-data class (P2).

### Phase 11 — Personal-data adoption and release

| Commit | Content |
|---|---|
| 1 | Adoption guide for a personal-data consumer: scopes, per-realm storage, declared retention, what the consumer encrypts and reports. |
| 2 | End-to-end test: a sample consumer with two realms and two tenants, synthetic ciphertext, archived, aged out, offboarded and observed through its configuration alone; one realm's credential cannot reach the other's bucket. |
| 3 | v0.2.0 release: version, release notes, this section removed. |

The consumer's side is a separate pull request in its own repository, under that repository's own
rules. The hand-over to Ulduar's archive is switched on per realm and is never a condition for a
realm to hold real data (D18); until then a realm's dumps stay in its own object-locked bucket.
Candidates: hand over each realm's encrypted dumps and collected logs to Hodir; record its operations
jobs with Thorim; report its in-realm restore drills; show `mimiron observe` verdicts in its console.
Running Ulduar against a realm is the consumer owner's own run (D11).

Sign-off: which realm is switched on first (*proposed*: a non-production realm); how the consumer
installs the package; the Archivum's host in the management realm; each realm's bucket and storage
credential; which of the candidates above it adopts first; where Ulduar runs for a realm (D2 names a
consumer's GitHub Actions; an operations platform's jobs run on its own servers); whether CI also tests
Python 3.13, the first personal-data consumer's stack (*proposed*: yes — the package requires Python
3.11 or newer, which does not exclude it).

## Later

In no planned release; each is an owner decision when its time comes.

- A read-only web view and an HTTP API over the Archivum (D2): still later, still an owner decision.
  A consumer's console is the first to show Ulduar's verdicts (D16); in v0.2 it reads them without
  either (Phase 10).
- Adoption by further consumers once their hosting exists.
- Column-level lineage; statistical anomaly detection beyond declared ranges and tolerances.
- More connectors (an HTTP API source, a second database engine); a PostgreSQL version matrix.
