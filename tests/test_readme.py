"""The README's own claims, checked against the code rather than trusted."""
from __future__ import annotations

import re
from pathlib import Path

from onetrace_ci.instrument_plan import parse_instrument_plan

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_the_instrument_section_states_the_non_claim(examined):
    examined(1, "the README's instrument section")
    section = README.split("## Instrument from a plan", 1)[1]
    assert ("The patch records what your plan names; it does not find stages you\ndidn't list."
            in section)


def test_the_readme_example_plan_is_a_complete_plan(examined):
    section = README.split("### The plan", 1)[1]
    example = re.search(r"```yaml\n(.*?)```", section, re.S).group(1)
    plan = parse_instrument_plan(example, source="README.md")
    examined(len(plan.stages), "stages in the README's example plan")
    assert [s.name for s in plan.stages] == ["intake", "retrieve", "answer"]
