# Project Ulduar

A vault and observatory for data projects. Project Ulduar is a generic Python library and CLI that will
give a data project an immutable archive of what went in and what came out, a catalog of what exists
and where it came from, a record of every run, and a verdict on whether the data is fresh, complete
and recoverable.

**Status: in development, nothing released.** Phase 0 (the development process) is in place; Freya (catalog
and lineage) is built and the next phases follow. `ROADMAP.md` holds the plan and its status.

## Components

Named after the keepers and watchers of Ulduar, the titan vault in Warcraft lore. Each arrives in its
roadmap phase.

| Component | Role | Phase |
|---|---|---|
| **Freya** | Catalog and lineage; its database is the Archivum | 1 |
| **Hodir** | Cold archive, retention, integrity and restore verification | 2 |
| **Mimiron** | The `mimiron` CLI, connectors and the GitHub Action | 3 |
| **Thorim** | Run ledger | 4 |
| **Algalon** | Monitoring: verdicts and alerts | 5 |

The name is a homage. Project Ulduar is not affiliated with, endorsed by or sponsored by Blizzard
Entertainment; Warcraft is a trademark of Blizzard Entertainment, Inc.

## Install

For a consumer:

```bash
pip install "project-ulduar[binary]"
```

The `binary` extra bundles libpq; without it psycopg needs a system libpq 16 or newer. The configuration
file is described in `ulduar/README.md`, installing the Archivum in `ulduar/freya/README.md`.

For development (Python 3.11 or newer, Docker for the local test services):

```bash
git clone https://github.com/Deluha/Project_Ulduar.git
cd Project_Ulduar
pip install -e ".[dev]"
pre-commit install
python dev_tools/services.py start
python dev_tools/verify.py
```

A Codespace or Dev Container sets all of this up itself (`.devcontainer/devcontainer.json`).

## Repository layout

| Path | Holds |
|---|---|
| `ulduar/` | The package (`ulduar/README.md`) |
| `dev_tools/` | The verify command, the checkers, the test-services command (`dev_tools/README.md`) |
| `tests/` | The test suite; service tests run against PostgreSQL 18 and MinIO (`tests/README.md`) |
| `.claude/` | The Claude Code development process: guide, agents, hooks, skills |
| `.github/` | CI workflows |

## Development process

`CLAUDE.md` holds the operating principles, the eleven hard rules and the ask-first list; the development
skill under `.claude/skills/` routes each task to its one authoritative file. `CONTRIBUTING.md` covers
setup, the QA gates and the PR flow.

## Release & versioning

Semantic versioning. During 0.x a minor release may break, with an upgrade note in `RELEASE_NOTES.md`; a
deprecated name warns (`DeprecationWarning`) for at least one minor release before it is removed. Schema
compatibility: an additive step keeps older libraries working, a breaking step raises the schema's minimum
library version (`ulduar/freya/README.md`).

The version is stated once, in `pyproject.toml`. `RELEASE_NOTES.md` records what shipped; `ROADMAP.md`
records what is planned and leaves a release's section when that release is tagged.

## Licence

MIT (`LICENSE`).
