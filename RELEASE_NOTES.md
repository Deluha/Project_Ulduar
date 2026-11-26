# Release Notes

What shipped, newest first. What is planned: `ROADMAP.md`.

## v0.1.0 — in development

### Phase 2 — Hodir

The cold archive (`ulduar/hodir/README.md`):

- Configuration: an optional `[archive]` table — a local directory or an S3-compatible bucket, with a writing
  and a separate deleting credential named by environment variables.
- Storage: content-addressed objects under a scope slot, never overwritten (conditional writes, SHA-256 checked
  by the store on every body and part, multipart above 64 MiB); a write-once format marker; one object manifest
  per registration.
- `hodir.open_archive`: `put_file`, `put_stream` (spooled, stored only on `commit()`), `get` and `get_stream`
  (verified before a file is handed over), `verify` (full or a rotating sample), `reconcile` and `rebuild`.
- Retention: `declare_retention` and `apply_retention` — nothing is deleted unless a class is declared, a dry run
  by default, the newest copies of a dataset kept, a cap per run, a recorded confirmation to shorten a rule.
- Deletion: one recorded protocol (intent, then confirmation once no version is left) for retention, the
  built-in incomplete-put rule (`clean_incomplete`) and `purge`; a put and a deletion of the same content never
  interleave.
- Freya: schema step 2, `Catalog.transaction` and `ledger_transaction` for other components,
  `redact(redaction_id=…)` and `reapply_removals` after a rebuild; an artifact whose object is still archived
  does not age out of history.
- The optional extra `project-ulduar[s3]` (`boto3>=1.36,<2`).

Upgrade notes: `freya.install` applies step 2 (additive) to an Archivum installed by Phase 1. The archive's
bucket needs a writer credential without delete rights, a separate deleter credential, and versioning or a
conditional-write policy (`ulduar/hodir/README.md`).

### Phase 1 — Freya

The catalog and lineage, and the Archivum they live in (`ulduar/README.md`, `ulduar/freya/README.md`):

- Configuration: one TOML file (`ulduar.toml`) read by `ulduar.config`.
- Connection contract: PostgreSQL 18 only, TLS verification for a non-loopback host, time limits on every call.
- Public exceptions for configuration, connection, schema-version and ledger failures.
- Schema ledger: `freya.install` and `schema_status`; step 1 creates roles, tables, functions, partitions,
  row-level security and grants.
- Catalog: `open_catalog`, datasets and artifacts, with idempotent record ids.
- Lineage: edges, ancestors and descendants, with a cycle guard.
- Onboarding, history retention and redaction: `onboard_project`, `declare_history_retention`,
  `apply_history_retention`, `redact`.
- Isolation between projects and append-only history are enforced by the database, not by the library.
- The first runtime dependency: `psycopg>=3.2,<4`, with the `binary` extra.
- Packaging check, Python 3.11–3.15 CI matrix, branch-coverage floor, dev-tool lock, digest-pinned images,
  weekly scan and a threat model.

Upgrade notes: a consumer installs the Archivum with `freya.install` as the ledger role (see
`ulduar/freya/README.md`).

### Phase 0 — Development process

No product code. The development process of this repository:

- `CLAUDE.md`: four operating principles, eleven hard rules, the ask-first list.
- Agents (`implementer`, `reader`, `reviewer`, `scribe`), the session-start and post-edit hooks,
  `.claude/settings.json`, three path rules.
- The development skill: a routing table over six references and three workflows, fourteen guidance
  evals, and the command skills `verify`, `review-diff`, `db-change`, `guidance-change`.
- Package skeleton (`ulduar`, version only), `dev_tools/` with the verify command, the conventions,
  guidance-integrity and docs-sync checkers and the test-services command, and their tests.
- Local test services for every environment — PostgreSQL 18 and a MinIO object store
  (`pgsty/minio`, pinned) — started by one command locally, in cloud sessions and in CI, and as compose
  services in the dev container. The verify command and CI require a passed test of each.
- Pre-commit hooks, gitleaks, and three CI workflows (pre-commit, docs sync, tests), each with one check
  to require on `main`; mypy is strict and gating.
