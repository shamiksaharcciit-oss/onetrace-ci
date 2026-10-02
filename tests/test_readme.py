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


def test_the_readme_names_the_onetrace_version_the_lock_pins(examined):
    lock = (Path(__file__).resolve().parents[1] / "requirements.lock").read_text(encoding="utf-8")
    pinned = re.search(r"^onetrace==([0-9.]+) ", lock, re.MULTILINE).group(1)
    stated = re.search(r"Everything else here works with `onetrace` ([0-9.]+), the version", README)
    examined(1, "version the README states")
    assert stated is not None
    assert stated.group(1) == pinned


WAYS_IN = "## Four ways in, by effort"
NO_PLAN = "## Add the gate without a plan: `onetrace-ci init-ci`"


def test_the_ways_in_are_ordered_by_effort_before_the_longer_paths(examined):
    assert WAYS_IN in README
    section = README.split(WAYS_IN, 1)[1].split("\n## ", 1)[0]
    steps = [line for line in section.splitlines() if re.match(r"^\d\. \*\*", line)]
    examined(len(steps), "ways in")
    assert [s.split("**")[1].rstrip(":") for s in steps] == ["Decorate by hand", "Add the gate", "Generate from a plan",
                                                  "Discover first"]
    assert README.index(WAYS_IN) < README.index("## Instrument from a plan") < README.index("## Discover the stages first")


def test_the_no_plan_path_is_shown_before_instrument_and_discover(examined):
    examined(1, "the no-plan section")
    assert NO_PLAN in README
    assert README.index(NO_PLAN) < README.index("## Instrument from a plan")
    section = README.split(NO_PLAN, 1)[1].split("\n## ", 1)[0]
    assert "onetrace-ci init-ci" in section and "approved_by" in section and "DECIDE:" in section


def test_the_two_non_claims_are_stated_in_the_spec_s_words(examined):
    examined(2, "non-claims")
    assert "Decorators and patches record only what is decorated." in README
    assert "`undeclared` means nobody stated it, and the gate can require it." in README
