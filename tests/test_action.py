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
