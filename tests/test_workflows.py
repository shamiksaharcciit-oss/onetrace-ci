"""onetrace-ci's own test workflow: every Python it supports, actions pinned to commits, read-only,
and every install pinned by hash."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

TESTS_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"


def _workflow():
    return yaml.safe_load(TESTS_WORKFLOW.read_text(encoding="utf-8"))


def test_the_tests_run_on_every_python_onetrace_ci_supports(examined):
    jobs = _workflow()["jobs"]
    versions = jobs["tests"]["strategy"]["matrix"]["python-version"]
    examined(len(versions), "Python versions in the matrix")
    assert versions == ["3.10", "3.11", "3.12"]


def test_every_action_the_tests_workflow_uses_is_pinned_to_a_commit(examined):
    uses = [s["uses"] for job in _workflow()["jobs"].values() for s in job["steps"] if "uses" in s]
    examined(len(uses), "actions the tests workflow uses")
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a commit"


def test_the_tests_workflow_can_only_read_and_installs_only_what_is_pinned_by_hash(examined):
    workflow = _workflow()
    installs = [line.strip() for job in workflow["jobs"].values() for s in job["steps"]
                for line in s.get("run", "").splitlines() if "pip install" in line]
    examined(len(installs), "pip installs in the tests workflow")
    assert workflow["permissions"] == {"contents": "read"}
    for line in installs:
        assert "--require-hashes -r " in line or line == "pip install --no-deps -e .", line
