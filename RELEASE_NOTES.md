# Release Notes

What shipped, newest first. What is planned: `ROADMAP.md`.

## v0.1.0 — in development

### Phase 0 — Development process

No product code. The development process is ported from Project Blackwing, by way of Caverns of Time,
and adapted to this repository:

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
