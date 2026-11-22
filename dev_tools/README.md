# dev_tools

## Purpose & scope

Development tooling: the one verify command, the three checkers and the test-services command. Standard
library only, so each script runs on a bare Python. Not part of the distribution.

## Files & entry points

| File | Command | Does |
|---|---|---|
| `verify.py` | `python dev_tools/verify.py [--base <ref>]` | Runs every QA step and prints one pass/fail table; requires the test services |
| `check_conventions.py` | `python dev_tools/check_conventions.py` | Convention rules C1–C9 |
| `check_guidance_integrity.py` | `python dev_tools/check_guidance_integrity.py` | Guidance pointers, anchors, `CLAUDE.md` budget, skill routing, rule files, command skills |
| `check_docs_sync.py` | `python dev_tools/check_docs_sync.py --base <ref>` or `--staged <commit-msg-file>` | Fails a code change in a documented area without its doc |
| `services.py` | `python dev_tools/services.py start \| stop \| status \| env [postgres] [object-store]` | The local test services: PostgreSQL 18 and MinIO, Docker containers on 127.0.0.1 |

## How it works

- `verify.py` runs conventions, guidance integrity, `ruff check .`, `mypy`, `pytest -q` and docs-sync in
  that order, never stopping at the first failure. For the pytest step it exports
  `ULDUAR_REQUIRE_SERVICES=1`, drops a caller's `PYTEST_ADDOPTS` and fills each unset `ULDUAR_TEST_*`
  setting with the local default from `services.py`.
- The checkers are pure functions over explicit inputs; each `main()` wires the real repository. What
  each one parses: `.claude/skills/ulduar-development/references/checkers.md`.
- `services.py` holds the images, ports, throwaway credentials and the names of the variables the tests
  read, in one place. `start` is idempotent, recreates a container that runs another image than the one
  stated here, and waits until each service answers: PostgreSQL over TCP (`pg_isready`), MinIO on its
  readiness endpoint. `env` prints the settings as `KEY=VALUE` lines.

## Interfaces & contracts

| Script | Exit codes |
|---|---|
| `verify.py` | 0 every step passed · 1 a step failed · 2 git failure |
| `check_conventions.py` | 0 clean · 1 violations · 2 git failure |
| `check_guidance_integrity.py` | 0 clean · 1 violations |
| `check_docs_sync.py` | 0 in sync or exempt · 1 a doc is missing · 2 git failure |
| `services.py` | 0 done · 1 a service could not start or is not ready |

`check_docs_sync.git` and `GitError` are shared by `verify.py` and `check_conventions.py`; the variable
names of `services.py` are shared by `verify.py` and `tests/conftest.py`, and `default_environment()` by
`verify.py` and the tests.

## Extending

A new convention rule is a new `check_*` function in `check_conventions.py`, added to `run_all`, with a
test and a row in the checkers reference. A new documented area is a row in `AREAS` of
`check_docs_sync.py`. A new test service is a row of `SERVICES` in `services.py`, with its fixtures in
`tests/conftest.py`. Changing a checker or a service is a guidance change: follow the changing-guidance
workflow.

## See also

`tests/README.md` · `.claude/skills/ulduar-development/references/qa-and-ci.md`
