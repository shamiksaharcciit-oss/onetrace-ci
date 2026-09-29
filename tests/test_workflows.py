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


INTEGRATIONS_LOCK = TESTS_WORKFLOW.parents[2] / "requirements-integrations.lock"
FRAMEWORKS = ("langchain-core", "llama-index-core", "llama-index-instrumentation", "opentelemetry-sdk")


def _pins(lock):
    """{name: [versions]} and every requirement's text, from a hash-pinned lock file."""
    text = lock.read_text(encoding="utf-8").replace("\\\n", " ")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    pins = {}
    for line in lines:
        name, version = re.match(r"([A-Za-z0-9._-]+)==([^\s;]+)", line).groups()
        pins.setdefault(name.lower(), []).append(version)
    return pins, lines


def test_the_framework_integrations_run_in_a_job_of_their_own_that_requires_them(examined):
    jobs = _workflow()["jobs"]
    job = jobs["integrations"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    main_runs = "\n".join(s.get("run", "") for s in jobs["tests"]["steps"])
    examined(2, "the integrations job and the main tests job")
    assert job["strategy"]["matrix"]["python-version"] == ["3.10", "3.11", "3.12"]
    assert "pip install --require-hashes -r requirements-integrations.lock" in runs
    assert "tests/test_discover_integrations.py" in runs
    assert any(s.get("env", {}).get("ONETRACE_CI_REQUIRE_INTEGRATIONS") == "1" for s in job["steps"])
    assert "requirements-integrations.lock" not in main_runs


def test_every_requirement_in_the_integrations_lock_is_pinned_by_hash(examined):
    pins, lines = _pins(INTEGRATIONS_LOCK)
    examined(len(lines), "requirements in the integrations lock")
    assert all(name in pins for name in FRAMEWORKS)
    for line in lines:
        assert "--hash=sha256:" in line, line


def test_the_readme_names_the_framework_versions_the_lock_pins(examined):
    pins, _ = _pins(INTEGRATIONS_LOCK)
    readme = (TESTS_WORKFLOW.parents[2] / "README.md").read_text(encoding="utf-8")
    examined(len(FRAMEWORKS), "frameworks named in the README")
    for name in FRAMEWORKS:
        [version] = pins[name]
        assert f"{name} {version}" in readme, name
