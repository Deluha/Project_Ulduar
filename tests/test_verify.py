"""Tests for dev_tools/verify.py: the step list and the service requirement it exports."""

from __future__ import annotations

from dev_tools import verify
from dev_tools.services import DSN_VARIABLE, REQUIRE_SERVICES_VARIABLE, default_environment


def test_steps_are_fixed_and_ordered() -> None:
    names = [step.name for step in verify.steps()]
    assert names == ["conventions", "guidance-integrity", "ruff", "mypy", "pytest", "docs-sync"]


def test_docs_sync_step_gets_the_base() -> None:
    assert verify.steps("origin/dev")[-1].command[-2:] == ["--base", "origin/dev"]


def test_environment_requires_the_services_on_the_local_instances() -> None:
    env = verify.step_environment({})
    assert env[REQUIRE_SERVICES_VARIABLE] == "1"
    assert {key: env[key] for key in default_environment()} == default_environment()


def test_environment_keeps_a_named_setting_and_overrides_a_relaxed_requirement() -> None:
    env = verify.step_environment({DSN_VARIABLE: "named", REQUIRE_SERVICES_VARIABLE: "0"})
    assert env[DSN_VARIABLE] == "named" and env[REQUIRE_SERVICES_VARIABLE] == "1"


def test_environment_drops_pytest_options_from_the_caller() -> None:
    assert "PYTEST_ADDOPTS" not in verify.step_environment({"PYTEST_ADDOPTS": '-m "not db"'})
