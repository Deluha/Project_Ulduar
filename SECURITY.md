# Security Policy

## Reporting a vulnerability

Report vulnerabilities **privately** — never in a public issue or PR.

- Preferred: open a private report via GitHub Security Advisories
  (*Security → Report a vulnerability* on the repository).
- Alternatively: contact the repository owner directly through a private channel.

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
CI downloads is pinned by version and sha256; the MinIO test image is pinned by its release tag.
