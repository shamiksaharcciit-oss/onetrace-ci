"""The gate and the plan's open questions: a `DECIDE:` left anywhere in the plan, and fields
the run records as `undeclared` (nobody stated them)."""
from __future__ import annotations

import json

import pytest

from onetrace_ci.gate import FAIL, PASS, run_gate
from onetrace_ci.plan import PlanError, parse_plan_text


def _gate(make_run, write_plan, tmp_path, plan_text, **run_kwargs):
    baseline = make_run("baseline")
    run = make_run("candidate", **run_kwargs)
    plan = write_plan("plan.yaml", plan_text)
    return run_gate(run=run, baseline=baseline, plan_path=plan, runner=None,
                    out=tmp_path / "out", review_exit_zero=False)


def test_an_approved_by_still_open_fails_the_gate(make_run, write_plan, tmp_path, examined):
    exit_code, findings = _gate(make_run, write_plan, tmp_path,
                                'approved_by: "DECIDE: who approves this plan?"\n')
    open_ = [f for f in findings if f.check == "plan.approved_by"]
    examined(len(open_), "findings about approved_by")
    assert exit_code == 1
    assert open_[0].verdict == FAIL
    assert "DECIDE:" in open_[0].detail


def test_any_open_question_in_the_plan_fails_the_gate_naming_its_field(make_run, write_plan, tmp_path,
                                                                        examined):
    text = ("approved_by: alice\nstages:\n  - name: retrieve\n"
            '    trust: "DECIDE: operator-authored or externally-sourced?"\n')
    exit_code, findings = _gate(make_run, write_plan, tmp_path, text)
    open_ = [f for f in findings if f.check == "plan.stages[0].trust"]
    examined(len(open_), "findings about the open trust question")
    assert exit_code == 1
    assert open_[0].verdict == FAIL


def test_require_declared_reads_as_a_boolean_and_defaults_to_false(examined):
    examined(3, "require_declared: absent, true, and not a boolean")
    assert parse_plan_text("approved_by: a\n", source="<t>").require_declared is False
    assert parse_plan_text("approved_by: a\nrequire_declared: true\n", source="<t>").require_declared is True
    with pytest.raises(PlanError):
        parse_plan_text("approved_by: a\nrequire_declared: sometimes\n", source="<t>")


def test_a_fully_declared_run_passes_require_declared(make_run, write_plan, tmp_path, examined):
    exit_code, findings = _gate(make_run, write_plan, tmp_path, "approved_by: a\nrequire_declared: true\n")
    [declared] = [f for f in findings if f.check == "declared"]
    examined(1, "the declared finding for a fully declared run")
    assert exit_code == 0
    assert declared.verdict == PASS


def _mark_undeclared(run, stage_file: str):
    """PROVISIONAL, until the SDK can record such a run itself: mark one real receipt's
    re-derivability `undeclared`, the value the SDK's decorators record for a field nobody
    stated."""
    p = run / "receipts" / stage_file
    r = json.loads(p.read_text(encoding="utf-8"))
    r["instrument"]["rederivable"] = "undeclared"
    p.write_text(json.dumps(r, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def test_require_declared_fails_a_run_with_an_undeclared_field_naming_the_stage(
        make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate")
    _mark_undeclared(run, "01-retrieve.json")
    plan = write_plan("plan.yaml", "approved_by: a\nrequire_declared: true\n")
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan, runner=None,
                                   out=tmp_path / "out", review_exit_zero=False)
    [declared] = [f for f in findings if f.check == "declared"]
    examined(1, "the declared finding for a run with an undeclared field")
    assert exit_code == 1
    assert declared.verdict == FAIL
    assert "'retrieve'" in declared.detail and "rederivable" in declared.detail


def test_without_require_declared_the_count_is_an_annotation(make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate")
    _mark_undeclared(run, "01-retrieve.json")
    plan = write_plan("plan.yaml", "approved_by: a\n")
    _, findings = run_gate(run=run, baseline=baseline, plan_path=plan, runner=None,
                           out=tmp_path / "out", review_exit_zero=False)
    [declared] = [f for f in findings if f.check == "declared"]
    examined(1, "the declared annotation")
    assert declared.verdict == PASS
    assert declared.detail.startswith("1 undeclared field")
