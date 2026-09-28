"""`onetrace-ci gate`. Real onetrace runs, real `onetrace`/`onetrace-verify`
subprocess calls -- no mocking of either.
"""
from __future__ import annotations

import json

from onetrace_ci.gate import FAIL, PASS, REVIEW, run_gate


def test_an_unchanged_run_against_its_own_baseline_passes(make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate")   # identical content, different run_id
    plan = write_plan("plan.yaml", "approved_by: alice\n")
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan,
                                   runner=None, out=tmp_path / "out", review_exit_zero=False)
    examined(len(findings), "findings for an unchanged run")
    assert exit_code == 0
    assert all(f.verdict == PASS for f in findings)
    verdict = json.loads((tmp_path / "out" / "verdict.json").read_text())
    assert verdict["exit"] == 0


def test_a_changed_stage_is_review_with_localize_attached(make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate", retrieve_text="a genuinely different passage")
    plan = write_plan("plan.yaml", "approved_by: alice\n")
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan,
                                   runner=None, out=tmp_path / "out", review_exit_zero=False)
    checks = {f.check: f for f in findings}
    examined(len(findings), "findings for a changed stage")
    assert exit_code == 2
    assert checks["diff"].verdict == REVIEW
    #: `first_difference` in the real report is itself an object
    #: ({"index": ..., "stage": ...}), not a bare stage name -- this checks
    #: the extracted, clean stage name, not merely that the substring
    #: "retrieve" appears somewhere in a dict repr (which it would even if
    #: the code stringified the whole object by mistake).
    assert checks["diff"].detail == "first difference at stage 'retrieve'"
    assert checks["localize"].verdict == REVIEW
    assert (tmp_path / "out" / "localize" / "localize.json").is_file()


def test_review_exit_zero_makes_a_review_finding_advisory(make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate", retrieve_text="a genuinely different passage")
    plan = write_plan("plan.yaml", "approved_by: alice\n")
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan,
                                   runner=None, out=tmp_path / "out", review_exit_zero=True)
    examined(1, "the overall exit code with --review-exit 0")
    assert exit_code == 0
    assert any(f.verdict == REVIEW for f in findings)   # still reported, just advisory


def test_a_missing_approved_by_fails_regardless_of_the_runs(make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate")
    plan = write_plan("plan.yaml", "format: x\n")   # no approved_by at all
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan,
                                   runner=None, out=tmp_path / "out", review_exit_zero=False)
    checks = {f.check: f for f in findings}
    examined(1, "the approved_by finding")
    assert exit_code == 1
    assert checks["plan.approved_by"].verdict == FAIL


def test_an_unapproved_boundary_fails_coverage(make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate", boundary="retrieve")
    plan = write_plan("plan.yaml", "approved_by: alice\n")   # retrieve NOT approved
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan,
                                   runner=None, out=tmp_path / "out", review_exit_zero=False)
    checks = {f.check: f for f in findings}
    examined(1, "the coverage finding")
    assert exit_code == 1
    assert checks["coverage"].verdict == FAIL
    assert "retrieve" in checks["coverage"].detail


def test_an_approved_boundary_passes_coverage(make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate", boundary="retrieve")
    plan = write_plan("plan.yaml", "approved_by: alice\napproved_boundaries:\n  - retrieve\n")
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan,
                                   runner=None, out=tmp_path / "out", review_exit_zero=False)
    checks = {f.check: f for f in findings}
    examined(1, "the coverage finding, boundary approved")
    assert checks["coverage"].verdict == PASS


def test_an_instrument_annotation_on_a_same_stage_is_review(make_run, write_plan, tmp_path, examined):
    """The table's own separate row: a `same` stage with an instrument or
    config annotation is `review`, even though the diff command's own exit
    code is 0 (identical outputs).
    """
    baseline = make_run("baseline", retrieve_version="1.0.0")
    run = make_run("candidate", retrieve_version="1.0.1")   # same output, different instrument
    plan = write_plan("plan.yaml", "approved_by: alice\n")
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan,
                                   runner=None, out=tmp_path / "out", review_exit_zero=False)
    annotation_findings = [f for f in findings if "annotation" in f.check]
    examined(len(annotation_findings), "instrument-annotation findings")
    assert exit_code == 2
    assert annotation_findings[0].verdict == REVIEW
    assert "retrieve" in annotation_findings[0].check


def test_a_refused_answer_fails_verify(make_run, write_plan, tmp_path, examined):
    """`answer_ok=False` makes the answer stage RAISE inside the recorder
    (fail-open policy: recorded as an error outcome, not silently dropped) --
    `onetrace-verify` itself still passes the RECORD (an error outcome is a
    valid, self-consistent receipt), so this instead exercises the coverage
    path in a run whose own stage genuinely errored, confirming the gate
    reads real, not synthetic, run data end to end.
    """
    baseline = make_run("baseline")
    run = make_run("candidate", answer_ok=False)
    plan = write_plan("plan.yaml", "approved_by: alice\n")
    exit_code, findings = run_gate(run=run, baseline=baseline, plan_path=plan,
                                   runner=None, out=tmp_path / "out", review_exit_zero=False)
    checks = {f.check: f for f in findings}
    examined(1, "the verify finding against a run with an error-outcome stage")
    assert checks["verify"].verdict == PASS   # the RECORD is still self-consistent
    #: But the diff's own outputs necessarily differ (the answer stage never
    #: wrote its artifact), so the overall gate is not a silent pass.
    assert exit_code != 0


def test_unapproved_extra_plan_keys_are_named_in_the_summary(make_run, write_plan, tmp_path, examined):
    baseline = make_run("baseline")
    run = make_run("candidate")
    plan = write_plan("plan.yaml", "approved_by: alice\nsome_future_key: yes\n")
    run_gate(run=run, baseline=baseline, plan_path=plan, runner=None,
            out=tmp_path / "out", review_exit_zero=False)
    summary = (tmp_path / "out" / "summary.md").read_text()
    examined(1, "the summary.md file, searched for the ignored key")
    assert "some_future_key" in summary


def test_sabotage_dropping_the_coverage_check_lets_an_unapproved_boundary_through(
        make_run, write_plan, tmp_path, examined):
    """Never against the real module: a copy of `gate.run_gate` with the
    coverage check simply not appended to `findings` -- the sabotage this
    order's own discipline requires (run for real, not merely argued for).
    """
    import importlib.util
    import inspect
    import sys

    import onetrace_ci.gate as real_gate

    source = inspect.getsource(real_gate)
    needle = "    findings.append(_coverage_finding(run, plan))\n"
    assert needle in source, "the line this sabotage targets has moved or been reworded"
    sabotaged_source = source.replace(needle, "", 1)
    assert sabotaged_source != source

    sabotaged_path = tmp_path / "gate_sabotaged.py"
    sabotaged_path.write_text(sabotaged_source, encoding="utf-8")

    #: A real module, registered in sys.modules -- not a bare `exec()` into a
    #: throwaway namespace, which `@dataclass` cannot fully introspect (it
    #: looks its own defining module up via `sys.modules[cls.__module__]`).
    #: Same technique `onetrace-console`'s own `test_accounts.py` sabotage uses.
    module_name = "onetrace_ci_gate_sabotaged"
    spec = importlib.util.spec_from_file_location(module_name, sabotaged_path)
    sabotaged = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = sabotaged
    try:
        spec.loader.exec_module(sabotaged)
        sabotaged_run_gate = sabotaged.run_gate

        #: The SAME boundary on both sides -- so `diff` itself sees no
        #: asymmetry. A boundary is legitimately COULD-NOT-CHECK to `diff`,
        #: independent of coverage entirely, and PROPAGATES downstream: since
        #: `answer` reads `retrieve`'s own (unverifiable-past-the-boundary)
        #: output, `answer` is correctly COULD-NOT-CHECK too -- found by
        #: running this for real and reading the actual findings, not assumed.
        #: Both are named in `known_limits` here so this test isolates
        #: coverage specifically; `retrieve` is still absent from
        #: `approved_boundaries`, which is the one thing left for the
        #: (real, unsabotaged) coverage check to catch.
        baseline = make_run("baseline", boundary="retrieve")
        run = make_run("candidate", boundary="retrieve")
        plan = write_plan("plan.yaml",
                          "approved_by: alice\nknown_limits:\n  - retrieve\n  - answer\n")

        examined(1, "a run with an unapproved boundary, through the sabotaged gate")
        exit_code, findings = sabotaged_run_gate(
            run=run, baseline=baseline, plan_path=plan, runner=None,
            out=tmp_path / "out", review_exit_zero=False)
        assert "coverage" not in {f.check for f in findings}
        assert exit_code == 0, "the sabotaged gate passed a run with an unapproved boundary"
    finally:
        del sys.modules[module_name]

    #: And the real function still catches it, proving the test above depends
    #: on the real coverage check, not a coincidence of this fixture.
    real_exit_code, real_findings = run_gate(
        run=run, baseline=baseline, plan_path=plan, runner=None,
        out=tmp_path / "out2", review_exit_zero=False)
    assert real_exit_code == 1
    assert {f.check: f.verdict for f in real_findings}["coverage"] == FAIL
