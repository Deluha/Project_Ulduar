# Project Ulduar — Roadmap

The planned scope of the next release. `RELEASE_NOTES.md` (from Phase 0 on) records what shipped; this
file records what is planned. A release's section leaves this file when the release is tagged. Owner
decisions are dated.

Approved by the owner on 2026-11-22: the concept, the form, the process timing and every scope rule
below (D1–D14). What is still marked *proposed* inside a phase is that phase's starting position,
presented as a concrete proposal and confirmed at its sign-off. Nothing is built yet.

## v0.1 — A vault and observatory for data projects

Goal: a generic Python library and CLI that gives a data project a vault and someone watching it — an
immutable archive of what went in and what came out, a catalog of what exists and where it came from,
a record of every run, and a verdict on whether the data is fresh, complete and recoverable. Project
Blackwing is the first consumer, not the design target.

### Why

Findings of the 2026-11-22 survey of the sibling repositories:

| Gap | Today |
|---|---|
| Run history | Project Blackwing records no runs, timings or row counts; workflow logs expire after 30 days. |
| Archive | Its database dumps live 14 days as CI artifacts; Google Drive holds the only copy of the raw files; reports and trained models are not kept. |
| Monitoring | Freshness is printed, never judged; nothing checks volume drift or missed runs; no dump has its restore verified. |
| Catalog and lineage | None in any repository. |
| Blackwing Analytics, DeveloPath | Backups, alerting and audit are planned in each, built in neither. |

### Components

Named after the keepers and watchers of Ulduar, the titan vault.

| Name | Lore | Role |
|---|---|---|
| **Freya** | Keeper of life, tender of the Conservatory | Catalog: registry of projects, datasets and artifacts, and the lineage between them. Its database is **the Archivum**. |
| **Hodir** | Keeper of winter | Cold archive: immutable, content-addressed storage of raw files, database dumps, reports and models; retention; integrity and restore verification. |
| **Thorim** | Keeper of storms, master of the Arena | Run ledger: every run with its status, timings, row counts and check results, linked to what it read and wrote. |
| **Algalon** | The Observer, who judges whether a world still stands | Monitoring: judges freshness, missed runs, volume drift, backup age and archive integrity from what the other three recorded; reports a verdict and raises alerts. |
| **Mimiron** | The tinkerer | The `mimiron` CLI, the connectors (a directory, `pg_dump`, Google Drive) and the GitHub Action a consumer's workflow calls. |

### Scope rules (owner decisions, 2026-11-22)

- **Vault and observatory (D1).** All five components are in v0.1, built in phases: catalog and archive
  first, then tooling, run ledger and monitoring.
- **Library and CLI first (D2).** A Python package and a CLI that run inside each consumer's existing
  GitHub Actions. Nothing to host or keep online in v0.1. A read-only web view and an HTTP API come in
  a later release.
- **Process ported in Phase 0 (D3).** The Project Blackwing development process (`CLAUDE.md`, agents,
  skills, hooks, checkers, CI) is copied and amended in the first development session. This planning
  session wrote this file only.
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
- **Own schema through Caverns of Time (D9).** The Archivum's tables are created and changed through the
  `caverns-of-time` ledger, making Ulduar its second consumer. Depends on that library's Nozdormu phase
  being merged; if it is not when Phase 1 starts, Phase 1 uses plain versioned SQL and says so at its sign-off.
- **Real services in tests (D10).** Database behaviour is tested on a real PostgreSQL 18 and archive
  behaviour on a real S3-compatible server, both throwaway containers. No mocks; a skipped service
  test fails the done gate.
- **Never a live system (D11).** No test, workflow or development session of this repository connects to a
  consumer's database, Drive folder or bucket. Running Ulduar against live data is the consumer
  owner's own run.
- **No secrets (D12).** Credentials arrive from the environment at run time. None is stored in the
  Archivum, written to a log or committed.
- **Not a backup system of record (D13).** Hodir archives logical dumps and files and proves they can be
  restored. Point-in-time recovery (the pgBackRest plans of Blackwing Analytics and DeveloPath) stays
  with each product.
- **No personal data in v0.1 (D14).** The first consumer's data is football statistics. Archiving data about
  academy players or staff (DeveloPath) needs encryption, retention and erasure rules first: see Later.

### Status

| Phase | Status | Pull request |
|---|---|---|
| 0 — Development process | In progress | |
| 1 — Freya | Not started | |
| 2 — Hodir | Not started | |
| 3 — Mimiron | Not started | |
| 4 — Thorim | Not started | |
| 5 — Algalon | Not started | |
| 6 — Blackwing adoption and release | Not started | |

### Running a phase

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
   phase's first commit sets the previous row to "Merged". Each phase adds its entry to one
   "v0.1.0 — in development" section at the top of `RELEASE_NOTES.md`; Phase 6 turns it into the release
   entry and removes the v0.1 section from this file.
6. **Stay in scope.** The scope rules bind every phase. A phase that finds an agreed item infeasible, or
   needs something not listed here, stops and asks the owner; it never narrows or widens scope itself.

### Phase 0 — Development process

Ports the Project Blackwing guidance machinery (`Deluha/Project_Blackwing`). No product code. The
Phase 0 of `Deluha/caverns-of-time` ported the same machinery to a generic library and is the reference
for which parts are generic and which are Blackwing's domain.

| Commit | Content |
|---|---|
| 1 | `CLAUDE.md`: the four operating principles, short-form hard rules with anchors, the ask-first list — rewritten for this repository. |
| 2 | Agents (`implementer`, `reader`, `reviewer`, `scribe`), hooks (session start, post-edit check) and `.claude/settings.json` with the same allow / ask / deny shape. |
| 3 | Development skill as a router with `references/` (architecture and codenames, conventions, style, recipes, QA and CI, checkers, workflows, evals), the path rules, and the generic command skills `verify`, `review-diff`, `db-change`, `guidance-change`. |
| 4 | Package skeleton (`pyproject.toml`, an empty package, `tests/`), the verify command, and the conventions, guidance-integrity and docs-sync checkers with their tests. |
| 5 | Pre-commit, gitleaks, `.editorconfig`, and the CI workflows: pre-commit, docs sync, tests with PostgreSQL 18 and S3-compatible service containers, and the single "CI gate" check. |
| 6 | Local test services: the session-start hook and the dev container start PostgreSQL and the object store and export their connection settings. |
| 7 | `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `RELEASE_NOTES.md`, the pull request template. |

Not ported: Blackwing's domain rules (championships, enrichment, reports, pipeline, raw ingest), its
domain references, its domain skills (`add-championship`, `new-report`, `rename-and-ingest`,
`validate-ingest`, `validate-outputs`), its pipeline and enrichment workflows, and the hard rules that
only make sense there (championship imports, `#%%` markers, the single enrichment path, entry-script
engines, twin reports).

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
- Which Blackwing conventions carry over. *Proposed*: the same choices Caverns of Time made (icon
  status helpers for CLI output only, `logging` in library code, strict gating mypy, actions pinned by
  commit SHA, no `#%%` markers).
- How development sessions get PostgreSQL 18 and an object store. *Proposed*: the official `postgres:18`
  image and a MinIO image, started by the session-start hook; the same images in CI.
- Whether sessions install the `council-of-elrond` plugin, as Blackwing Analytics and DeveloPath do.
- The licence (MIT today).

Sign-off decisions (owner, 2026-11-22: S1–S6 confirmed; S5's object-store image chosen after the
confirmed MinIO image proved unavailable):

| # | Item | Decision |
|---|---|---|
| S1 | Hard rules and ask-first list | Eleven hard rules: generic core; one canonical helper; safe SQL; no unasked files; the vault is immutable; read-only towards consumers; own schema through the ledger; docs move with the code; real services in tests; one module per component with a flow-only `mimiron` entry; no secrets stored or logged. The four Blackwing operating principles unchanged. Ask-first as proposed above plus the licence and the scope rules of this file, as shown at sign-off; the guidance machinery and CI path-gated by `permissions.ask`. Text: `CLAUDE.md`. |
| S2 | Package and layout | Distribution `project-ulduar`, import package `ulduar`, flat at the repository root; one module per component (`freya`, `hodir`, `thorim`, `algalon`); `mimiron` the only CLI entry; dev tooling in `dev_tools/`, not shipped; verify command `python dev_tools/verify.py`; Python 3.11 or newer. |
| S3 | Blackwing conventions | The choices Caverns of Time made: icon status helpers for CLI output only, `logging` in library code, strict gating mypy, actions pinned by commit SHA, no `#%%` markers; kept with them: codenames, the single `CI gate` check, gitleaks, pre-commit, the path-rule and eval mechanisms, the `settings.json` shape and agent tiers. Ported from Caverns of Time's Phase 0, the roadmap's reference for the generic parts. |
| S4 | Licence | MIT, unchanged. |
| S5 | Local test services | PostgreSQL: the official `postgres:18` image. Object store: `pgsty/minio:RELEASE.2026-08-04T00-00-00Z`, a community MinIO build, pinned — the official `minio/minio` image is no longer on Docker Hub and quay.io is unreachable from cloud sessions. Both started by `python dev_tools/services.py start` (from the cloud session-start hook, locally and in CI) and as compose services in the dev container. No fallback: a service that cannot start fails its tests. |
| S6 | `council-of-elrond` plugin | Not installed in v0.1's Phase 0; adding it later is a guidance change. |

### Phase 1 — Freya

| Commit | Content |
|---|---|
| 1 | Configuration and connection: how a consumer names its project and points Ulduar at the Archivum. |
| 2 | Archivum schema and its installation through the ledger. |
| 3 | Catalog model: project, dataset, artifact (kind, checksum, size, origin, time), with register and lookup. |
| 4 | Lineage: "derived from" edges between artifacts and datasets; ancestors and descendants queries. |
| 5 | Docs and release-notes entry. |

Sign-off: the Archivum's tables and columns; whether it is its own database or a schema inside a
consumer's (*proposed*: its own database, so one Archivum serves several projects); the artifact kinds
(*proposed*: raw file, database dump, report, model, log); what identifies a dataset across runs; the
connection contract; Caverns of Time or the fallback.

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
results (such as Blackwing's data-quality checks) are stored as measurements or as their own records.

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

### Phase 6 — Blackwing adoption and release

| Commit | Content |
|---|---|
| 1 | Adoption guide: installing the package, the configuration file, the storage and database a consumer provides. |
| 2 | End-to-end test: a sample consumer project archived, recorded and observed through its configuration alone. |
| 3 | v0.1.0 release: version, release notes, this section removed. |

The Blackwing side is a separate pull request in `Deluha/Project_Blackwing`, under that repository's own
rules: its workflows and anything that reaches its database are ask-first items there. Candidates, in
the order of the gaps above: archive the `pg_dump` taken by its pipeline run and raw ingest instead of
letting it expire; archive each raw Drive file when it is ingested; record its pipeline, ingest and
enrichment runs with their row counts; run `mimiron observe` on a schedule.

Sign-off: how Blackwing installs the package (git dependency or a package index); which bucket and
which database host the Archivum and the archive use, and who pays for them; which of the candidates
above Blackwing adopts first; the read-only database role Ulduar connects with.

### Later (not in v0.1)

- A read-only web view and an HTTP API over the Archivum (D2).
- Adoption by Blackwing Analytics and DeveloPath once their hosting phases exist. DeveloPath holds
  personal data of academy players: required encryption, per-tenant separation, retention and erasure
  rules, with the retention numbers a matter for counsel as in that repository.
- A home for the shared operations modules (Terraform, VPN, alert routing) that DeveloPath's D-42 and
  Blackwing Analytics' D-47 place differently. An owner decision in those repositories; not proposed here.
- Column-level lineage; statistical anomaly detection beyond declared ranges and tolerances.
- More connectors (an HTTP API source, a second database engine); a PostgreSQL version matrix.
