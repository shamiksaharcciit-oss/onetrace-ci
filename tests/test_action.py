"""The composite action's own steps, like the generated workflow's, are pinned to commits; and the
commit the generated workflow pins the gate to is one this branch's history holds."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from onetrace_ci.instrument import GATE_ACTION

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "action.yml"


def test_the_gate_the_generated_workflow_pins_is_a_commit_of_this_history(examined):
    """A pin to a commit outside the history that gets pushed would name a commit the remote
    doesn't have, and every workflow `instrument` or `init-ci` writes would fail at the gate."""
    sha = GATE_ACTION.rpartition("@")[2]
    shallow = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-shallow-repository"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    examined(1, "the gate commit the generated workflow pins")
    assert shallow == "false", "this check needs the full history (tests.yml checks out with fetch-depth: 0)"
    ancestor = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", sha, "HEAD"],
                              capture_output=True, timeout=60)
    assert ancestor.returncode == 0, f"the generated workflow pins {sha[:12]}, which is not in this branch's history"


def test_every_action_the_composite_action_uses_is_pinned_to_a_commit(examined):
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    examined(len(uses), "actions the composite action uses")
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a commit"


def test_the_action_uploads_only_the_gate_report_and_for_a_stated_time(examined):
    """The artifact holds only what the gate report holds (stage names, digests, verdicts and
    settings), never the run's stored outputs, which are the pipeline's data; in a public
    repository anyone can read it. It is kept for a stated time."""
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    uploads = [s for s in steps if "upload-artifact" in s.get("uses", "")]
    examined(len(uploads), "upload steps in the composite action")
    for step in uploads:
        paths = [p.strip() for p in str(step["with"]["path"]).splitlines() if p.strip()]
        assert paths == ["${{ inputs.out }}"], paths
        assert 1 <= int(step["with"]["retention-days"]) <= 14


def test_each_gate_step_uploads_its_report_under_its_own_name(examined):
    """Two gate steps in one job (one per entry) upload two reports; an artifact name is unique
    in a workflow run, so each step names its own. The default keeps the one gate's name."""
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    uploads = [s for s in action["runs"]["steps"] if "upload-artifact" in s.get("uses", "")]
    examined(len(uploads), "upload steps in the composite action")
    assert action["inputs"]["report-name"]["default"] == "onetrace-ci-gate-report"
    assert uploads and all(s["with"]["name"] == "${{ inputs.report-name }}" for s in uploads)
