# Architecture

Module map, components, the consumer boundary, the two stores, packaging and the tech stack.

## Module map

| Path | Holds |
|---|---|
| `ulduar/` | The package: one module per component, behind an explicit public surface (`ulduar/README.md`) |
| `dev_tools/` | Verify command, checkers, the test-services command; stdlib only, never shipped (`dev_tools/README.md`) |
| `tests/` | The only test location; service fixtures in `tests/conftest.py` (`tests/README.md`) |
| `.claude/` | Guidance machinery: settings, agents, hooks, path rules, the development skill and the command skills |
| `.github/` | CI workflows, Dependabot, the PR template, code owners |
| `.devcontainer/` | Dev container with the same PostgreSQL and object-store images as CI |

## Components

Named after the keepers and watchers of Ulduar, the titan vault. The component names are the codenames;
code, docs and messages use them consistently.

| Codename | Lore | Role | Module | Phase |
|---|---|---|---|---|
| Freya | Keeper of life, tender of the Conservatory | Catalog: registry of projects, datasets and artifacts, and the lineage between them; its database is **the Archivum** | `freya` | 1 |
| Hodir | Keeper of winter | Cold archive: immutable, content-addressed storage; retention; integrity and restore verification | `hodir` | 2 |
| Mimiron | The tinkerer | The `mimiron` CLI (the only command-line entry), the connectors and the GitHub Action | `mimiron` | 3 |
| Thorim | Keeper of storms, master of the Arena | Run ledger: every run with status, timings, measurements, inputs and outputs | `thorim` | 4 |
| Algalon | The Observer | Monitoring: judges freshness, missed runs, volume drift, backup age and integrity; verdicts and alerts | `algalon` | 5 |

A component exists in the package only from its phase on (`ROADMAP.md`); until then nothing imports it
and no doc describes behaviour that is not built.

## Consumer boundary

- A consumer installs the package, writes one configuration file in its own repository and calls the
  public surface or the `mimiron` CLI from its own GitHub Actions; it never reaches into private modules.
- A consumer's own formats (its schema, its folder layout, its run names) are adapted in the consumer's
  repository, through an adapter or the configuration file. The package never learns them (R1).
- Ulduar reads a consumer's database, folders and Drive through read-only access and writes only to its
  own two stores (R6). Running Ulduar against live data is the consumer owner's own run.
- The first consumer is a data-pipeline project; its adoption is a pull request in its own repository under
  its own rules.

## Stores

| Store | Holds | Technology | Contract |
|---|---|---|---|
| The Archivum | Catalog, lineage, runs, measurements, verdicts | PostgreSQL 18; its schema installed through Ulduar's own versioned-SQL ledger (R7) | Append-only history (R5) |
| The archive | Raw files, database dumps, reports, models | An S3-compatible bucket or a local directory behind one storage interface | Immutable, content-addressed objects (R5) |

Both are provided by the consumer; no credential to either is stored in the Archivum or the repository (R11).

## Packaging

- One distribution, `project-ulduar`; one import package, `ulduar`; the version is stated once, in
  `pyproject.toml`, and read at import.
- `dev_tools/` and `tests/` are not packaged.
- Runtime dependencies are ask-first; the `dev` extra carries the tooling and the test suite's clients.

## Tech stack

Python 3.11+ · PostgreSQL 18 · an S3-compatible object store (MinIO in tests) · pytest · ruff · mypy (strict) ·
pre-commit · gitleaks · GitHub Actions · Docker for the local test services.
