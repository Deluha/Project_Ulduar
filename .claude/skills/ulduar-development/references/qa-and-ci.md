# QA, CI & operational policy

The hook/CI guard rail, the verify command, the local test services, the GitHub workflows, and the README
and dependency policies. What each checker parses (so you don't break a line shape it greps) is in
`references/checkers.md`. Test inventory: `tests/README.md`.

## Gates

Hooks live in `.pre-commit-config.yaml`; `pre-commit install` wires every stage (the cloud session-start
hook and the dev container do it); bypass one commit with `--no-verify`. All pre-commit-stage hooks also
run on every PR via `ci-pre-commit.yml` (`--all-files`), except `gitleaks`, which CI replaces with a
pinned range-and-tree scan.

| Hook id | Stage | Enforces | Runs locally when |
|---|---|---|---|
| `gitleaks` | pre-commit | Secret scan of the staged changes with `.gitleaks.toml` (`references/checkers.md#checker-secret-scan`) | any commit |
| `project-conventions` | pre-commit | Checker rules C1–C9 (`dev_tools/check_conventions.py`), whole-repo scan | every commit |
| `ruff` | pre-commit | `E`, `F`, `B`, `I`, `UP` (config in `pyproject.toml`); isolated venv | any `.py` |
| `guidance-integrity` | pre-commit | Doc pointers + anchors resolve, `CLAUDE.md` budget, skill routing, single-home tokens (`dev_tools/check_guidance_integrity.py`) | any `.py` / `.md` |
| `docs-sync` | **commit-msg** | Code change in a documented area also touches its doc (`dev_tools/check_docs_sync.py --staged`) | every commit |
| `pytest-fast` | pre-push | `pytest -q -x -m "not db and not s3"` with `ULDUAR_REQUIRE_SERVICES` unset: the tests that need no service | every push |

Claude Code sessions add guards from `.claude/settings.json` (not git hooks — they act while editing):

| Guard | When | Does |
|---|---|---|
| `.claude/hooks/session-start.sh` | session start, cloud sessions only (`ULDUAR_SKIP_DEV_SETUP=1` skips it) | Installs the `dev` extra and the git hooks (skipped while the files that decide the install are unchanged), starts the Docker daemon and the test services, exports their `ULDUAR_TEST_*` settings |
| `.claude/hooks/post_edit_check.py` | after every `.py` edit | `ruff check` on the file + `dev_tools/check_conventions.py`; a failure is fed back to the model |
| `permissions.ask` | before an edit to the guidance machinery and CI: `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`, `.claude/hooks/`, `.github/workflows/`, `.pre-commit-config.yaml`, `.gitleaks.toml` | Forces a confirmation prompt — the mechanical half of the CLAUDE.md "Ask first" list |
| `permissions.deny` | a `Read` of `.env*`, `*.pem`, `*.pfx`, `*.key`; the `Explore` agent | Keeps secret files out of the session context; `Explore` bypasses `reader` |

## verify

Run before committing (every workflow's Verification section points here): `python dev_tools/verify.py`.
It needs the dev install (`pip install -e ".[dev]"`) and the local test services running. It never stops
at the first failure and prints one pass/fail table; `--base <ref>` changes the docs-sync base (default
`origin/main`).

What it runs:

1. `python dev_tools/check_conventions.py`
2. `python dev_tools/check_guidance_integrity.py`
3. `ruff check .`
4. `mypy` — strict, over the package, `dev_tools/` and `.claude/hooks/`
5. `pytest -q` with the services **required**: the step exports `ULDUAR_REQUIRE_SERVICES=1` and drops a
   caller's `PYTEST_ADDOPTS`, so a service test that cannot connect fails instead of skipping, and a run
   in which no database test or no object-store test passed fails as a whole (R9). A changed behaviour
   means updating its test deliberately in the same PR — never by loosening an assertion.
6. `python dev_tools/check_docs_sync.py --base origin/main` — the PR-level docs guard. It sees
   committed changes only; the commit-msg hook covers the commit being made.

A verify run without the test services is a failed verify, not a partial pass. Delete scratch scripts
before the first commit.

## test-services

Two throwaway instances, local to the machine running the tests, holding no data worth keeping: a
PostgreSQL 18 server and an S3-compatible object store (MinIO). Their images, ports and credentials are
stated once, in `dev_tools/services.py`.

| Where | How they start | Settings |
|---|---|---|
| Cloud session | `.claude/hooks/session-start.sh` starts the Docker daemon, then `python dev_tools/services.py start` | exported from `services.py env`; verify falls back to the same local defaults |
| Local clone | `python dev_tools/services.py start` (Docker) | `python dev_tools/services.py env`; verify falls back to the same local defaults |
| CI | the same command, a step of `ci-tests.yml` | `services.py env` appended to `$GITHUB_ENV` |
| Dev container | `db` and `s3` services in `.devcontainer/docker-compose.yml`; `app` and `s3` share the `db` network namespace | set in the compose file |

- CI names no image, port or credential of its own; `tests/test_services.py` fails when it stops calling
  the services command, when the dev container names another image, and when the PostgreSQL server is
  not major version 18.
- There is no fallback to another PostgreSQL version or another object store: if an instance cannot
  start, its tests fail and the work stops there.
- ⚑ Pointing any of this at another server, or changing a version or an image, is ask-first.

## workflows

Four GitHub Actions workflows in `.github/workflows/`. Third-party actions are pinned by commit SHA;
every checkout sets `persist-credentials: false`; a run is cancelled by a newer push only on a pull
request, never on `main`. Header comments in each file are the detailed contract.

| File | Trigger | Runs |
|---|---|---|
| `ci-pre-commit.yml` | PR, push to `main` | `pre-commit run --all-files` (dev extra installed; skips the commit-msg `docs-sync` and pre-push `pytest-fast` hooks and the staged-only `gitleaks` hook); then the pinned, sha256-verified gitleaks scan of the PR's commits and the tree (`references/checkers.md#checker-secret-scan`) |
| `ci-docs-sync.yml` | PR | `check_docs_sync.py --base origin/<PR base> --actor <PR author>` (full history for the per-commit `[skip-docs]` attribution; the actor drives the Dependabot exemption) |
| `ci-tests.yml` | PR, push to `main` | `pytest -q` with branch coverage against the local test services, started by `python dev_tools/services.py start` with their settings from `services.py env`, the services required, on a matrix of Python 3.11 to 3.15 (coverage artifact per version); the 90 % floor on `ulduar/` (`coverage report --include='ulduar/*' --fail-under=90`) is enforced on the 3.11 leg only; gating `mypy`; gating `actionlint` (version pinned in `pyproject.toml`); gating `package` ("Build and import the wheel": builds the wheel, installs it in a fresh venv outside the checkout and checks the import, `py.typed`, the SQL steps and the `License-Expression` metadata); `CI gate` (`needs` all four, `if: always()`, passes only when every need succeeded) stands for the whole workflow in branch protection |
| `ci-scan.yml` | weekly (Monday 05:17 UTC), manual | `pip-audit` (pinned exactly) over `requirements/dev.lock`; CodeQL for Python (`github/codeql-action` pinned by SHA; the one job with `security-events: write`). Not in the `CI gate`, not a required check |

Jobs that install the dev tools (`ci-tests.yml`, `ci-pre-commit.yml`) run
`pip install --require-hashes -r requirements/dev.lock`, then `pip install --no-deps -e .`.

**Required checks.** Branch protection on `main` requires three checks, one per workflow: `CI gate`,
`Run pre-commit hooks` and `Docs in sync with code`. `CI gate` covers only the jobs of `ci-tests.yml`; a
red pre-commit or docs-sync run blocks a merge only when its own check is required too.

No workflow holds a secret, and none connects to anything but its own local test services.

## README policy

- Every code directory keeps a `README.md` with the uniform shape: **Purpose & scope · Files & entry
  points · How it works · Interfaces & contracts · Extending · See also**.
- READMEs carry local detail only — never the hard rules or CI mechanics.
- Root `README.md` = human overview; `RELEASE_NOTES.md` = the single changelog; `ROADMAP.md` = the planned
  releases (scope, phases, status, the phase process); a release's section leaves when it is tagged;
  `tests/README.md` = test inventory.
- A README describes what is built. Planned behaviour belongs in `ROADMAP.md` until its phase ships.

## Dependency policy

`pyproject.toml` is the only place dependencies are declared. Runtime dependencies are ask-first and
arrive in the same PR as their first import. The `dev` extra carries the tooling (pytest, ruff, mypy,
pre-commit, actionlint) and the test suite's clients (a PostgreSQL driver and an S3 client); they are not
a runtime choice, which each component's phase makes at its sign-off. Pin minimally; pin exactly only where
a new release must arrive as a reviewed bump (actionlint, the gitleaks version in the workflow, pip-audit in
`ci-scan.yml`; the test-service images are pinned by digest in `dev_tools/services.py`).

**The dev lock.** `requirements/dev.lock` is the hashed, fully pinned resolution of the `dev` extra; CI installs
it with `--require-hashes`, and the weekly `pip-audit` reads it. It is compiled once, on the oldest supported
Python, and installs on every supported one. Regenerate it after any change to the `dev` extra (pip-tools is
in the extra), commit it with the change, and keep the command exactly:

```
pip-compile --extra dev --generate-hashes --allow-unsafe --strip-extras -o requirements/dev.lock pyproject.toml
```

Dependabot does not maintain it (the lock is compiled from `pyproject.toml`, not a `requirements.in`): a bump
of a pin there is followed by a regeneration in the same PR.
