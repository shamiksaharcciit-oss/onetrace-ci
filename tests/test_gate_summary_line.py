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
    assert set(verdict) == {"exit", "findings", "format", "plan", "run", "baseline"}
    assert lines[0] not in summary


# A query run's corpus link (`assertions.corpus_manifest`) is the ingest run it read. onetrace
# names a changed link "corpus link" and never counts it as a setting, so the summary line names it
# the same way, by short digests, and never calls it an instrument or config change. The pinned
# onetrace doesn't annotate the link in `diff` yet, so these build the diff report in the shape
# a release that does writes: `differs` maps the key to [baseline, candidate].
OLD_LINK = "sha256:" + "1" * 64
NEW_LINK = "sha256:" + "2" * 64


def _summary(tmp_path, verdict_of_retrieve, differs, first_difference=None):
    from onetrace_ci.gate import REVIEW, Finding, summary_line
    out = tmp_path / "out"
    (out / "diff").mkdir(parents=True)
    report = {"ladder": [{"stage": "retrieve", "verdict": verdict_of_retrieve}, {"stage": "answer", "verdict": "same"}],
              "annotations": [{"stage": "retrieve", "differs": differs}],
              "first_difference": first_difference}
    (out / "diff" / "diff.json").write_text(json.dumps(report), encoding="utf-8")
    diff = Finding(check="diff", command="onetrace diff", exit_code=0 if first_difference is None else 1,
                   report_path=None, verdict=REVIEW if first_difference else "pass")
    annotation = Finding(check="diff: instrument/config annotation at stage 'retrieve'", command="onetrace diff",
                         exit_code=0, report_path=None, verdict=REVIEW)
    return summary_line([annotation, diff] if first_difference is None else [diff], out)


def test_a_changed_corpus_link_alone_is_named_as_the_corpus_link_not_instrument_or_config(tmp_path, examined):
    line = _summary(tmp_path, "same", {"assertions.corpus_manifest": [OLD_LINK, NEW_LINK]})
    examined(1, "the summary line")
    assert line == 'review: corpus link changed at stage "retrieve" (corpus link 111111111111 -> 222222222222)'
    assert "instrument or config" not in line


def test_a_changed_setting_and_corpus_link_keep_the_link_out_of_instrument_or_config(tmp_path, examined):
    line = _summary(tmp_path, "same", {"assertions.constants.top_k": ["1", "2"],
                                       "assertions.corpus_manifest": [OLD_LINK, NEW_LINK]})
    examined(1, "the summary line")
    assert line == ('review: instrument or config changed at stage "retrieve" (top_k 1 -> 2); '
                    'corpus link changed (111111111111 -> 222222222222)')


def test_a_first_difference_names_a_changed_corpus_link_by_its_short_digests(tmp_path, examined):
    line = _summary(tmp_path, "FIRST DIFFERENCE", {"assertions.corpus_manifest": [OLD_LINK, NEW_LINK]},
                    first_difference={"index": "1", "stage": "retrieve"})
    examined(1, "the summary line")
    assert line == 'review: first difference at stage "retrieve" (corpus link 111111111111 -> 222222222222)'
    assert "assertions.corpus_manifest" not in line
