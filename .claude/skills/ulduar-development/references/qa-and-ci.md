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
| `.claude/hooks/session-start.sh` | session start, cloud sessions only (`ULDUAR_SKIP_DEV_SETUP=1` skips it) | Installs the `dev` extra and the git hooks (skipped while the files that decide the install are unchanged) |
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

- There is no fallback to another PostgreSQL version or another object store: if an instance cannot
  start, its tests fail and the work stops there.
- ⚑ Pointing any of this at another server, or changing a version or an image, is ask-first.

## workflows

Three GitHub Actions workflows in `.github/workflows/`. Third-party actions are pinned by commit SHA;
every checkout sets `persist-credentials: false`; a run is cancelled by a newer push only on a pull
request, never on `main`. Header comments in each file are the detailed contract.

None yet: the CI workflows arrive in Phase 0's CI commit.

No workflow holds a secret, and none connects to anything but its own local test services.

## README policy

- Every code directory keeps a `README.md` with the uniform shape: **Purpose & scope · Files & entry
  points · How it works · Interfaces & contracts · Extending · See also**.
- READMEs carry local detail only — never the hard rules or CI mechanics.
- Root `README.md` = human overview; `RELEASE_NOTES.md` = the single changelog; `ROADMAP.md` = the planned
  next release (scope, phases, status, the phase process); its section leaves when the release is tagged;
  `tests/README.md` = test inventory.
- A README describes what is built. Planned behaviour belongs in `ROADMAP.md` until its phase ships.

## Dependency policy

`pyproject.toml` is the only place dependencies are declared. Runtime dependencies are ask-first and
arrive in the same PR as their first import. The `dev` extra carries the tooling (pytest, ruff, mypy,
pre-commit, actionlint) and the test suite's clients (a PostgreSQL driver and an S3 client); they are not
a runtime choice, which each component's phase makes at its sign-off. Pin minimally; pin exactly only where
a new release must arrive as a reviewed bump (actionlint, the gitleaks version in the workflow, the
object-store image).
