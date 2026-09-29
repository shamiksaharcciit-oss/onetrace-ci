"""The gate's summary line comes first, in human mode: what happened, in one line, before the
findings. The exit codes, verdict.json and summary.md are unchanged."""
from __future__ import annotations

import json

from onetrace_ci.gate import main


def _gate(make_run, write_plan, tmp_path, capsys, plan="approved_by: alice\n", **candidate):
    baseline = make_run("baseline")
    run = make_run("candidate", **candidate)
    plan_path = write_plan("plan.yaml", plan)
    out = tmp_path / "out"
    rc = main(["--run", str(run), "--baseline", str(baseline), "--plan", str(plan_path), "--out", str(out)])
    lines = capsys.readouterr().out.splitlines()
    return rc, lines, out


def test_a_pass_says_how_many_stages_are_the_same(make_run, write_plan, tmp_path, capsys, examined):
    rc, lines, _ = _gate(make_run, write_plan, tmp_path, capsys)
    examined(len(lines), "lines the gate printed")
    assert rc == 0
    assert lines[0] == "pass: 2 stages same as baseline"


def test_a_review_names_the_first_differing_stage_and_the_setting(make_run, write_plan, tmp_path, capsys,
                                                                   examined):
    rc, lines, _ = _gate(make_run, write_plan, tmp_path, capsys, retrieve_text="another passage", top_k="2")
    examined(len(lines), "lines the gate printed")
    assert rc == 2
    assert lines[0] == 'review: first difference at stage "retrieve" (top_k 1 -> 2)'


def test_a_review_without_a_changed_setting_names_the_stage_alone(make_run, write_plan, tmp_path, capsys,
                                                                   examined):
    rc, lines, _ = _gate(make_run, write_plan, tmp_path, capsys, retrieve_text="another passage")
    examined(1, "the summary line")
    assert rc == 2
    assert lines[0] == 'review: first difference at stage "retrieve"'


def test_an_annotation_only_review_says_what_changed(make_run, write_plan, tmp_path, capsys, examined):
    rc, lines, _ = _gate(make_run, write_plan, tmp_path, capsys, retrieve_version="1.0.1")
    examined(1, "the summary line")
    assert rc == 2
    assert lines[0] == 'review: instrument or config changed at stage "retrieve" (instrument.version 1.0.0 -> 1.0.1)'


def test_a_fail_names_the_first_failing_check(make_run, write_plan, tmp_path, capsys, examined):
    rc, lines, _ = _gate(make_run, write_plan, tmp_path, capsys, plan="format: x\n")
    examined(1, "the summary line")
    assert rc == 1
    assert lines[0].startswith("fail: plan.approved_by: missing or empty")


def test_the_reports_do_not_carry_the_summary_line(make_run, write_plan, tmp_path, capsys, examined):
    _, lines, out = _gate(make_run, write_plan, tmp_path, capsys)
    verdict = json.loads((out / "verdict.json").read_text(encoding="utf-8"))
    summary = (out / "summary.md").read_text(encoding="utf-8")
    examined(2, "report files")
    assert set(verdict) == {"exit", "findings", "format", "plan"}
    assert lines[0] not in summary
