"""The plan-file subset."""
from __future__ import annotations

import pytest

from onetrace_ci.plan import PlanError, parse_plan_text


def test_reads_all_five_known_keys(examined):
    text = """\
format: onetrace-ci-plan/0.1
approved_boundaries:
  - retrieve
  - answer
known_limits:
  - embed
reproduce: true
approved_by: alice
"""
    plan = parse_plan_text(text, source="<test>")
    examined(5, "the five known keys")
    assert plan.format == "onetrace-ci-plan/0.1"
    assert plan.approved_boundaries == ("retrieve", "answer")
    assert plan.known_limits == ("embed",)
    assert plan.reproduce is True
    assert plan.approved_by == "alice"


def test_an_unknown_key_is_ignored_but_named(examined):
    text = "approved_by: alice\nsome_future_key: 3\n"
    plan = parse_plan_text(text, source="<test>")
    examined(1, "an unknown top-level key")
    assert plan.ignored_keys == ("some_future_key",)
    assert plan.approved_by == "alice"


def test_missing_approved_by_is_empty_not_an_error(examined):
    """Parsing succeeds either way -- the FAIL verdict for a missing/empty
    `approved_by` is the gate's own job, not the parser's.
    """
    plan = parse_plan_text("format: x\n", source="<test>")
    examined(1, "a plan with no approved_by key at all")
    assert plan.approved_by == ""


def test_reproduce_and_defaults_when_absent(examined):
    plan = parse_plan_text("approved_by: alice\n", source="<test>")
    examined(3, "the three keys with real defaults when absent")
    assert plan.reproduce is False
    assert plan.approved_boundaries == ()
    assert plan.known_limits == ()


@pytest.mark.parametrize("bad", [
    "approved_by:\n  weird_indent: true\n",
    "  leading_indent: true\n",
    "not a key value line at all\n",
    "approved_boundaries: [a, b]\n",
    "approved_by: |\n  a block scalar\n",
    "approved_by: >\n  a folded scalar\n",
    "approved_by: &anchor value\n",
    "approved_by: *alias\n",
    "known_limits:\n  - name: retrieve\n",   # a list item that is itself a mapping
])
def test_outside_the_supported_subset_is_refused(bad, examined):
    examined(1, f"plan text outside the supported subset: {bad!r}")
    with pytest.raises(PlanError):
        parse_plan_text(bad, source="<test>")


def test_the_refusal_names_the_offending_line_number(examined):
    """Fails, never guesses, and says WHERE -- not just that something was
    wrong with the file somewhere in it.
    """
    text = "approved_by: alice\nformat: x\nknown_limits: |\n  a block scalar\n"
    examined(1, "a refusal message, checked for the exact line number")
    with pytest.raises(PlanError, match=r"line 3\b"):
        parse_plan_text(text, source="<test>")


def test_reproduce_must_be_a_bool(examined):
    examined(1, "reproduce set to a non-boolean value")
    with pytest.raises(PlanError):
        parse_plan_text("reproduce: maybe\n", source="<test>")


def test_a_quoted_string_scalar_keeps_its_quotes_out(examined):
    plan = parse_plan_text('approved_by: "Alice Smith"\n', source="<test>")
    examined(1, "a quoted approved_by value")
    assert plan.approved_by == "Alice Smith"


def test_load_plan_refuses_a_missing_file(tmp_path, examined):
    from onetrace_ci.plan import load_plan
    examined(1, "a plan path that does not exist")
    with pytest.raises(PlanError):
        load_plan(tmp_path / "does-not-exist.yaml")
