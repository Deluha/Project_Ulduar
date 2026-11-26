# Security Policy

## Reporting a vulnerability

Report vulnerabilities **privately** — never in a public issue or PR.

- Use GitHub private vulnerability reporting: *Security → Report a vulnerability* on the repository.
  This is the private channel.

Include the affected file(s), reproduction steps, and impact. You will receive an acknowledgement, and
a fix or mitigation will be coordinated before any public disclosure.

## Supported versions

Nothing is released yet. From the first release on, the current minor line (`pyproject.toml`) is
supported.

## Credentials model

This repository holds no credential and its workflows use no secret.

- **Test services** — the tests run against a throwaway PostgreSQL server and a throwaway MinIO object
  store on the local machine (`dev_tools/services.py`, the same command in CI, the dev container). Their
  credentials are written in the repository on purpose: the instances hold no data and listen on
  localhost only.
- **Consumer systems** — no test, workflow or development session connects to a consumer's database,
  Drive folder or bucket. The test fixtures refuse a connection string or an object-store endpoint that
  can reach another machine (`tests/conftest.py`). A consumer supplies its own credentials at run time;
  Ulduar never stores one in the Archivum, an archived object, a log or its output (hard rule 11).
- **Read-only access** — Ulduar reads a consumer's systems through read-only access and writes only to
  the Archivum and the archive store the consumer gave it (hard rule 6).
- **Enforcement** — convention rule C8 (`dev_tools/check_conventions.py`) fails pre-commit and CI if a
  secret file or build artifact is tracked.
- **Secret scan** — gitleaks (default ruleset, `.gitleaks.toml`) scans the staged changes at commit
  time, and in `ci-pre-commit` the PR's commits and the tree
  (`.claude/skills/ulduar-development/references/checkers.md#checker-secret-scan`). A real finding is
  fixed and the credential rotated, never allow-listed.

## Data

v0.1 archives no personal data (`ROADMAP.md`, scope rule D14). Archiving personal data needs encryption,
retention and erasure rules first.

## SQL safety

Ulduar runs SQL built from names its caller and configuration supply. Identifiers are composed with the
driver's quoting and values are bound as parameters, never formatted into a statement (hard rule 3). A
report of a statement that breaks this rule is a vulnerability report.

## Workflow supply chain

Third-party actions used by the workflows are pinned to a full commit SHA (not a mutable tag) and
reviewed on update; Dependabot's `github-actions` ecosystem proposes those bumps. The gitleaks binary
CI downloads is pinned by version and sha256; the test-service images (PostgreSQL, MinIO) are pinned by tag and digest.

## Threat model

**Assets.** The Archivum's recorded history (catalog, lineage, run ledger); the archive's objects; the
credentials a consumer supplies at run time.

**Trust boundaries.**

- A consumer's jobs to the Archivum, over TLS.
- The recording roles against the ledger role that changes the schema.
- One project against another in a shared Archivum.
- The archive store's writing credential against its retention credential (Phase 2).

| Threat | Mitigation |
|---|---|
| A recording credential rewrites or deletes recorded history | Recording roles hold insert and select only, and triggers refuse an update, a delete or a truncate (only the ledger role, for a declared retention or a recorded redaction, may remove rows); history is corrected by a new record |
| One project reads or writes another's rows | Forced row-level security keyed on project membership |
| A downgrade, or two upgrades racing | The versioned-SQL ledger runs under an advisory lock, checks each step's checksum, and refuses a library older than the schema's minimum version |
| A credential leaks | Configuration names environment variables, never values; no connection string appears in a log or an error |
| Traffic is intercepted | A non-loopback host needs `verify-full` or `verify-ca` TLS |
| A recording role creates partitions without end | `ensure_partitions` accepts only times from 15 years back to 2 years ahead, so a recorder can create at most a bounded set of monthly partitions |
| Two Archivums share one server | The group role `ulduar_recorder` is cluster-wide: a recorder of one Archivum can read the other's schema ledger and call its partition function, while forced row-level security still hides every row; a second Archivum's ledger role needs ADMIN OPTION on the group. Give each Archivum its own server where that matters |
| The supply chain is tampered with | A hashed dev-tool lock, test images pinned by digest, actions pinned by commit SHA, a weekly `pip-audit` and CodeQL run |

Out of scope: a consumer's own systems (Ulduar reads them through read-only access only) and a
compromised superuser of the Archivum's server.
