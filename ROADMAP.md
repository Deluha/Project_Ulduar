# Project Ulduar — Roadmap

The planned scope of the coming releases. `RELEASE_NOTES.md` (from Phase 0 on) records what shipped; this
file records what is planned. A release's section leaves this file when the release is tagged. Owner
decisions are dated.

Approved by the owner on 2026-11-22: the concept, the form, the process timing and every scope rule
below (D1–D14). What is still marked *proposed* inside a phase is that phase's starting position,
presented as a concrete proposal and confirmed at its sign-off. Nothing is built yet.

Decided by the owner on 2026-11-23, in the planning session for Project Molten Core and DeveloPath and in
the review of the pull request that followed: decisions D15–D23 and the personal-data release they add,
whose section follows v0.1's (*proposed* name: v0.2 — Personal data). v0.1 stays as approved. Everything
in that section beyond D15–D23 is *proposed* until the owner confirms it.

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
| Blackwing Analytics, DeveloPath | Backups, alerting and audit are planned in each, built in neither. *2026-11-23:* DeveloPath's are now part of Project Molten Core's plan (D15). |

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
  *Amended 2026-11-23 (D20):* point-in-time recovery now sits with Project Molten Core, not with each
  product; a restore check that needs plaintext runs inside the consumer's realm, never in Ulduar.
- **No personal data in v0.1 (D14).** The first consumer's data is football statistics. Archiving data about
  academy players or staff (DeveloPath) needs encryption, retention and erasure rules first: see Later.
  *Amended 2026-11-23 (D17):* holds for v0.1 as written; lifted only for the personal-data release
  (*proposed* name: v0.2 — Personal data), below.

### Status

| Phase | Status | Pull request |
|---|---|---|
| 0 — Development process | In review | [#2](https://github.com/Deluha/Project_Ulduar/pull/2) |
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
| 3 | v0.1.0 release: version, release notes, this section removed — except the scope rules D1–D14 with their dated notes, which move to a permanent section of this file (owner decision, 2026-11-23, D22). |

The Blackwing side is a separate pull request in `Deluha/Project_Blackwing`, under that repository's own
rules: its workflows and anything that reaches its database are ask-first items there. Candidates, in
the order of the gaps above: archive the `pg_dump` taken by its pipeline run and raw ingest instead of
letting it expire; archive each raw Drive file when it is ingested; record its pipeline, ingest and
enrichment runs with their row counts; run `mimiron observe` on a schedule.

Sign-off: how Blackwing installs the package (git dependency or a package index); which bucket and
which database host the Archivum and the archive use, and who pays for them; which of the candidates
above Blackwing adopts first; the read-only database role Ulduar connects with.

### Later (not in v0.1)

- A read-only web view and an HTTP API over the Archivum (D2). Project Molten Core's console is the
  first consumer that would show Ulduar's verdicts (D16); still later, still an owner decision.
- Adoption by Blackwing Analytics once its hosting phase exists. DeveloPath's data reaches Ulduar through
  Project Molten Core (D15–D17): the required encryption, per-tenant separation, retention and erasure
  rules its personal data needs are the personal-data release below (*proposed* name: v0.2 — Personal
  data), with the retention numbers a matter for the consumer and its counsel.
- The home for DeveloPath's operations modules (Terraform, VPN, alert routing) is Project Molten Core
  (D21). Where Blackwing Analytics' D-47 places its own stays an owner decision in that repository; not
  proposed here. None of it is built in this repository.
- Column-level lineage; statistical anomaly detection beyond declared ranges and tolerances.
- More connectors (an HTTP API source, a second database engine); a PostgreSQL version matrix.

## v0.2 — Personal data

Goal: let a consumer archive, catalog and observe data that holds personal data — first the database
dumps and collected logs of the applications Project Molten Core operates — without Ulduar ever seeing a
dump's plaintext (*proposed*: nor that of any other object declared personal data), with each tenant's
copies separable where the consumer declares a tenant, every copy under a declared retention, and every
deletion recorded. Project Molten Core is the first consumer, not the design target. v0.1 stays as
approved, with no personal data (D14).

The name and number of this release, its phase numbers and everything in this section beyond the owner
decisions D15–D23 are *proposed*: the starting position, to be confirmed by the owner in the review of
the pull request that added this section or at a phase's sign-off. A confirmed item loses its *proposed*
mark and is recorded here, dated; a confirmed scope rule takes a D number. A merge alone confirms none of
them. Nothing is built yet.

### Owner decisions (2026-11-23)

D15–D19 were taken in the planning session for Project Molten Core and DeveloPath; D20–D23 were confirmed
the same day in the review of the pull request that added this section. Sources:
[Project Molten Core #1](https://github.com/Deluha/Project_Molten_Core/pull/1) and
[DeveloPath #15](https://github.com/Deluha/DeveloPath/pull/15).

- **Project Molten Core (D15).** `Deluha/Project_Molten_Core` is the owner's operations platform: it
  deploys the owner's applications, maintains their PostgreSQL servers and backups, and collects logs
  and metrics, on servers at Hetzner. DeveloPath (a multi-tenant platform for football academies,
  holding minors' personal and special-category data) is its first application; DeveloPath's
  infrastructure moved out of DeveloPath into Molten Core.
- **Molten Core uses Ulduar in full (D16).** Thorim records every operations job as a run; Freya
  catalogs dumps and reports; Hodir archives database dumps and collected logs with integrity
  verification; Algalon judges backup age, missed schedules and overdue restore drills, shown in
  Molten Core's console.
- **A personal-data release (D17).** That use includes dumps of applications that hold personal data.
  Before any such data is archived, Ulduar needs required client-side encryption, per-tenant
  separation, declared retention, and recorded deletion and erasure. This release is added to the
  roadmap for them, and D14 is lifted only for it.
- **Not on the critical path (D18).** Molten Core writes dumps, encrypted on the database host, to each
  realm's own object-locked bucket first; the hand-over to Ulduar's archive is switched on per realm
  later and is never a condition for a realm to hold real data. Ulduar blocks no consumer.
- **The archive is per realm (D19).** A realm is one environment of one application (for example
  `developath/uat`). Each realm's archived objects live in that realm's own backup project — its own
  bucket and its own storage credential — so one environment's credential cannot reach another's
  backups. The Archivum (catalog and run ledger) lives in Molten Core's management realm and holds
  metadata only.
- **Point-in-time recovery and plaintext restore checks (D20).** Point-in-time recovery now sits with
  Project Molten Core, not with each product; a restore check that needs plaintext runs inside the
  consumer's realm, never in Ulduar. Recorded as a dated note on D13.
- **Home of DeveloPath's operations modules (D21).** Project Molten Core is the home for DeveloPath's
  operations modules (Terraform, VPN, alert routing). Blackwing Analytics is not decided here.
- **Scope rules outlive v0.1 (D22).** When v0.1 is tagged, the scope rules D1–D14 with their dated notes
  move to a permanent section of this file instead of leaving with the v0.1 section. Phase 6's release
  row is amended accordingly; this decision changes nothing else of v0.1's approved text.
- **The consumers' constraints are decided (D23).** What the two plans require of this release is
  decided, not proposed (Project Molten Core's owner decisions O1–O9, DeveloPath's O37–O48): dumps reach
  Ulduar as ciphertext only, with the private keys on the realm's database host and in an offline escrow,
  never in the management realm and never available to Ulduar; the consumer's post-restore step runs
  after every path that brings back older data, an archive restore verification included, before the data
  is used for anything, and Ulduar never applies erasure or needs plaintext to honour it; retention comes
  from the consumer's manifest and applies to every archived copy; an offboarded tenant's copies age out;
  deleting a key is never reported as erasure, and an erasure or offboarding certificate uses the longest
  retention among backup lock, dumps and archived copies; what Ulduar stores about a consumer carries
  identifiers, states, counts, sizes and checksums only, never names or record contents; legal
  classification and retention numbers stay with the consumer and its counsel. The scope rules below
  restate them.

### Scope rules

They restate the consumers' constraints, which are decided (D23). What a rule adds beyond them is marked
*proposed* and is confirmed by the owner at the phase sign-off that names it. D1–D13 bind this release,
D13 with its note of 2026-11-23 (D20); where the reading of another rule is open (D6 in Phase 7, D2 in
Phase 11), that phase's sign-off says so.

- **Ciphertext only (P1).** A dump is encrypted on the database host before it leaves (in the consumers'
  plans, with one recipient key per database). Ulduar handles ciphertext only for dumps: it receives,
  stores, checksums and verifies it, and never holds, reads or is configured with a private key. The
  private keys stay on the realm's database host and in an offline escrow; they are never in the
  management realm and never available to Ulduar. *Proposed*: Hodir refuses an object of a personal-data
  class that is not declared encrypted; the `pg_dump` connector of v0.1 is not used on a database that
  holds personal data, because Ulduar is handed the dump's ciphertext; how collected logs are encrypted —
  where, and with which key — is the consumer's to declare (Phase 8 sign-off).
- **Integrity on ciphertext, plaintext in the realm (P2).** Hodir's checksum and verification work on
  ciphertext. A restore check that needs plaintext runs inside the consumer's realm, never in Ulduar
  (D20). Molten Core calls the consumer's post-restore step (for DeveloPath: applying its erasure ledger)
  after every path that brings back older data, an archive restore verification included, before the
  restored data is used for anything. Ulduar never applies erasure to a consumer's data and never needs
  to read plaintext to honour it. *Proposed*: the consumer reports the outcome of an in-realm restore
  check, Thorim records it and Algalon judges its age; the restore drill of v0.1's Phase 5 does not run
  on a personal-data class.
- **Per-realm archive, metadata-only Archivum (P3).** A realm's archived objects live in that realm's own
  backup project, under its own storage credential; the Archivum lives in Molten Core's management realm
  and holds metadata only (D19). Credentials arrive from the environment at run time (D12). *Proposed*:
  the storage configuration names one bucket and one credential per realm, and no configuration gives
  one credential reach into two realms.
- **Per-tenant separation (P4).** One tenant's archived copies are kept separable from another's (D17).
  *Proposed*: the object key layout and the catalog carry the scopes the consumer declares (for Molten
  Core: realm, database and, where there is one, tenant), so that a declared tenant's copies can be
  listed, aged out and deleted without touching another's; the content address is scoped inside that
  prefix, so an identical object is never deduplicated across tenants or realms.
- **Declared retention on every copy (P5).** Retention numbers come from the consumer's manifest and
  apply to every archived copy (DeveloPath's: per-database dumps 14 days, logs 30 days). Deletion happens
  only by the declared rule and is itself recorded (D7). The numbers are the consumer's and its
  counsel's; Ulduar invents none. *Proposed*: no default — a personal-data class without a declared
  retention is refused.
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
  names or record contents. *Proposed*: times are stored as well (when a run started and ended, when a
  copy was archived or deleted), since a run ledger and a judgment of age need them; a tenant is an
  opaque identifier issued by the consumer.
- **Boundaries unchanged (P9).** The boundaries of D6, D11 and D13 hold, D13 as amended by its note
  (D20). No test, workflow or development session of this repository touches a realm; tests use synthetic
  ciphertext on the local test services (D10). *Proposed*: the package fixes two roles only — the storage
  boundary (one bucket, one credential) and the unit of separation — and learns from configuration which
  of a consumer's scopes fill them; "realm" and "tenant" in this section are Molten Core's names for
  them, never names in the package (D5).

### Status

| Phase | Status | Pull request |
|---|---|---|
| 7 — Scopes and the catalog | Not started | |
| 8 — Ciphertext and separation | Not started | |
| 9 — Retention, deletion and erasure | Not started | |
| 10 — Operations runs and verdicts | Not started | |
| 11 — Molten Core adoption and release | Not started | |

### Running a phase

As v0.1's Running a phase section, with "v0.2.0 — in development" as the release-notes section; Phase 11
turns it into the release entry and removes this section. *Proposed*: Phase 7 starts only when v0.1.0 is
tagged. The scope rules D1–D14 move to a permanent section then (D22); *proposed*: the Running a phase
steps and the Later list move here at the same time. *Proposed*: when v0.2 is tagged, D15–D23 join D1–D14
in that permanent section, since the notes on D13 and D14 cite them. Phases 7–10 change ask-first items
of `CLAUDE.md` (the Archivum schema, the object layout, the CLI and configuration surface, archive
deletion and retention); a phase's sign-off is where the owner confirms them.

### Phase 7 — Scopes and the catalog

| Commit | Content |
|---|---|
| 1 | Configuration: the consumer's declared scopes; one storage configuration per storage boundary (for Molten Core, a realm), each with its own credential from the environment. |
| 2 | Archivum schema change through the ledger: scope, data class and the declared encryption of an artifact. |
| 3 | Field rule: the catalog and run records of a personal-data project accept identifiers, states, counts, sizes and checksums (*proposed*: and times) only; enforced and tested. |
| 4 | Docs and release-notes entry. |

Sign-off: what the catalog may store (*proposed*: the P8 list and times, plus the declared scopes, an
opaque tenant identifier, the data class, the encryption format and the public recipient-key identifier;
no free text on a personal-data project); whether the scope levels are fixed or declared by the consumer
(*proposed*: declared, so the package fixes no consumer's hierarchy — collected logs have no database
level) and what the package calls them; how a realm maps to a catalog project; how a data class relates
to v0.1's artifact kinds and retention classes; the per-realm storage configuration and how a credential
is named per realm; whether a realm's archive bucket is the bucket the consumer writes first or a second
one, and how D6 reads in either case; where the Archivum runs in the management realm and which roles
reach it; how an opaque tenant identifier is handled if the consumer's counsel treats it as personal
data; whether Ulduar catalogs or ignores the small integrity records an application writes to the export
location a realm offers in its backup project, for example an audit chain head (*proposed*: catalogs them
by identifier, size and the checksum the application supplies, never reading them).

### Phase 8 — Ciphertext and separation

| Commit | Content |
|---|---|
| 1 | Object key layout with the declared scope prefix; the content address scoped inside it. |
| 2 | Required encryption: put refuses an object of a personal-data class that is not declared encrypted; Hodir never decrypts. |
| 3 | Get and verify on ciphertext: the checksum is of the stored bytes; get returns ciphertext only. |
| 4 | Docs and release-notes entry. |

Sign-off: the encryption scheme the consumer uses and whether Ulduar handles public keys — private keys
never, as P1 decides (*proposed*: no public keys either; it records only the format and the recipient-key
identifier the consumer declares; for personal data the consumer encrypts, not Hodir, unlike the optional
encryption v0.1's Phase 2 proposes); how Hodir recognises ciphertext (*proposed*: the declared format
plus a check of the format's header, no decryption); the per-tenant object key layout; how a copy that
holds several tenants' data is recorded (*proposed*: as shared — it cannot be separated and ages out by
its class); whether collected logs are a personal-data class (*proposed*: yes, unless the consumer
declares otherwise) and, if so, who encrypts them, where and with which recipient key — P1's key per
database does not apply to them; what is required of a realm's bucket (versioning, object lock) and how a
lock period or a kept version relates to the declared retention and to a recorded deletion; whether the
`pg_dump` connector is barred from a database that holds personal data (P1).

### Phase 9 — Retention, deletion and erasure

| Commit | Content |
|---|---|
| 1 | Required retention: a personal-data class without a declared retention is refused; the rule applies to every archived copy. |
| 2 | Recorded deletion: the record of what was deleted, by which rule, when and in which run, keeping the identifier, checksum and size and no content. |
| 3 | Offboarding: a consumer's report that a tenant is offboarded, recorded as a new record; that tenant's copies age out by their declared retention; a dry-run listing of the copies and when each ages out. |
| 4 | Erasure evidence: per unit of separation (for Molten Core, a tenant or a database), the copies Ulduar holds or held (those that hold several tenants' data included), their classes, the longest declared retention among them and their deletion records; a key deletion is never counted. |
| 5 | Docs and release-notes entry. |

Sign-off: the retention classes and the recorded-deletion record; how a consumer reports an offboarded
tenant (*proposed*: a `mimiron` command and a library call run by the consumer's own job); who may run a
deletion and with which credential; the layout of the erasure evidence. No retention number and no
certificate text is written here: both stay with the consumer and its counsel.

### Phase 10 — Operations runs and verdicts

| Commit | Content |
|---|---|
| 1 | Operations jobs as runs: Thorim records a job of a realm with the P8 fields (*proposed*: and times) only; command output is not stored. |
| 2 | Reported restore verification: the outcome of a drill run inside the realm, and whether the application's post-restore step was reported complete. |
| 3 | Expectations per realm: backup age, a missed schedule, an overdue restore drill, a copy still present past its retention. |
| 4 | Verdicts for a consumer's console: a machine-readable form of the `mimiron observe` verdict. |
| 5 | Docs and release-notes entry. |

Sign-off: the fields of an operations run; the contract by which a realm reports a restore verification;
the new expectations and their names (*proposed*, as in v0.1's Phase 5: every threshold is declared by
the consumer); how Molten Core's console reads verdicts (*proposed*: the library or the machine-readable
CLI output — the web view and HTTP API stay under Later); the notifier for a realm's failing expectation;
whether the restore drill of v0.1's Phase 5 is switched off for a personal-data class (P2).

### Phase 11 — Molten Core adoption and release

| Commit | Content |
|---|---|
| 1 | Adoption guide for a personal-data consumer: scopes, per-realm storage, declared retention, what the consumer encrypts and reports. |
| 2 | End-to-end test: a sample consumer with two realms and two tenants, synthetic ciphertext, archived, aged out, offboarded and observed through its configuration alone; one realm's credential cannot reach the other's bucket. |
| 3 | v0.2.0 release: version, release notes, this section removed. |

The Molten Core side is a separate pull request in `Deluha/Project_Molten_Core`, under that repository's
own rules. The hand-over to Ulduar's archive is switched on per realm and is never a condition for a
realm to hold real data (D18); until then a realm's dumps stay in its own object-locked bucket.
Candidates: hand over each realm's encrypted dumps and collected logs to Hodir; record its operations
jobs with Thorim; report its in-realm restore drills; show `mimiron observe` verdicts in its console.
Running Ulduar against a realm is the consumer owner's own run (D11).

Sign-off: which realm is switched on first (*proposed*: a non-production realm such as `developath/uat`);
how Molten Core installs the package; the Archivum's host in the management realm; each realm's bucket
and storage credential; which of the candidates above Molten Core adopts first; where Ulduar runs for a
realm (D2 names a consumer's GitHub Actions; Molten Core's jobs run on its own servers); whether CI also
tests Python 3.13, Molten Core's stack (*proposed*: yes — the package requires Python 3.11 or newer,
which does not exclude it).
