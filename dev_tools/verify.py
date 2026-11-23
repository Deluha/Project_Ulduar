"""The one verification command: every QA step for the current change, one pass/fail table.

Runs the steps from the repo root without stopping at the first failure. The
pytest step REQUIRES the local test services: it exports ULDUAR_REQUIRE_SERVICES=1,
so a service test that cannot connect fails instead of skipping, and defaults each
unset test setting to the local instances of dev_tools/services.py. Exit 0 = every
step passed, 1 = a step failed, 2 = a git failure (never green). --base <ref> sets the docs-sync base (default
origin/main). Steps: the skill's references/qa-and-ci.md#verify.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dev_tools.check_docs_sync import GitError, git  # noqa: E402
from dev_tools.services import REQUIRE_SERVICES_VARIABLE, default_environment  # noqa: E402

DEFAULT_BASE = "origin/main"


class Step(NamedTuple):
    name: str
    command: list[str]


def _tool(script: str, *args: str) -> list[str]:
    return [sys.executable, f"dev_tools/{script}", *args]


def steps(base: str = DEFAULT_BASE) -> list[Step]:
    """Every step, in the order it runs."""
    module = [sys.executable, "-m"]
    return [
        Step("conventions", _tool("check_conventions.py")),
        Step("guidance-integrity", _tool("check_guidance_integrity.py")),
        Step("ruff", [*module, "ruff", "check", "."]),
        Step("mypy", [*module, "mypy"]),
        Step("pytest", [*module, "pytest", "-q"]),
        Step("docs-sync", _tool("check_docs_sync.py", "--base", base)),
    ]


def step_environment(environ: dict[str, str]) -> dict[str, str]:
    """The child environment: the services are required, on the local instances unless others are named."""
    env = {**default_environment(), **environ}
    env[REQUIRE_SERVICES_VARIABLE] = "1"
    env.pop("PYTEST_ADDOPTS", None)  # a caller's `-m "not db"` must not reach the pytest step
    return env


def uncommitted_files() -> set[str]:
    """Staged, unstaged and untracked files."""
    outs = (
        git("diff", "--cached", "--name-only"),
        git("diff", "--name-only"),
        git("ls-files", "--others", "--exclude-standard"),
    )
    return {line.strip() for out in outs for line in out.splitlines() if line.strip()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run every verification step for the current change.")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"docs-sync base ref (default {DEFAULT_BASE})")
    args = parser.parse_args(argv)

    try:
        uncommitted = uncommitted_files()
    except GitError as e:
        print(f"❌ verify: {e}")
        return 2

    env = step_environment(dict(os.environ))
    results: list[tuple[str, int]] = []
    for step in steps(args.base):
        print(f"ℹ️  verify: {step.name} — {' '.join(step.command)}", flush=True)
        results.append((step.name, subprocess.run(step.command, cwd=REPO_ROOT, env=env).returncode))

    print("ℹ️  verify summary")
    for name, code in results:
        print(f"   ✅ {name}: passed" if code == 0 else f"   ❌ {name}: FAILED (exit {code})")
    failed = any(code != 0 for _name, code in results)
    if failed:
        print("⚠️  verify: at least one step failed")
    if uncommitted:
        print(
            "⚠️  verify: docs-sync covers committed changes only — uncommitted files were not checked by it "
            "(the commit-msg hook checks them at commit)"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
