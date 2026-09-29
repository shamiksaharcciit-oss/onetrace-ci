"""The composite action's own steps, like the generated workflow's, are pinned to commits."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ACTION = Path(__file__).resolve().parents[1] / "action.yml"


def test_every_action_the_composite_action_uses_is_pinned_to_a_commit(examined):
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    examined(len(uses), "actions the composite action uses")
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a commit"
