"""The gate reads a plan with several entries, as `onetrace-ci instrument` writes a workflow for
it: each entry is written as a quoted key (it holds a colon), and an entry whose `ci.baseline`
is null is run but not gated, and named in the gate's report."""
from __future__ import annotations

from onetrace_ci.gate import main
from onetrace_ci.plan import parse_plan_text

INGEST = "pipeline.ingest:run"
TWO = '''\
approved_by: alice
entries:
  - entry: pipeline.ingest:run
    run_dir: runs/ingest/{run_id}
    stages:
      - name: split
        function: pipeline.ingest:split
        instrument: {name: splitter, package: onetrace, kind: chunker}
        rederivable: "true"
  - entry: pipeline.main:run
    run_dir: runs/query/{run_id}
    stages:
      - name: retrieve
        function: pipeline.retrieval:retrieve
        instrument: {name: bm25, package: rank_bm25, kind: retriever}
        rederivable: "true"
approved_boundaries: []
ci:
  install: pip install -e .
  run:
    "pipeline.ingest:run": python -m pipeline.ingest
    "pipeline.main:run": python -m pipeline.demo
  baseline:
    "pipeline.ingest:run": null
    "pipeline.main:run": runs/query-baseline
'''


def test_a_plan_with_several_entries_is_read_and_its_ungated_entries_named(examined):
    plan = parse_plan_text(TWO, source="<t>")
    examined(1, "a plan with two entries")
    assert plan.ungated == (INGEST,)
    assert plan.require_declared is True
    assert plan.ignored_keys == ()


def test_the_gate_s_report_names_every_entry_left_ungated(make_run, write_plan, tmp_path, examined):
    baseline, run = make_run("baseline"), make_run("candidate")
    out = tmp_path / "out"
    rc = main(["--run", str(run), "--baseline", str(baseline), "--plan",
               str(write_plan("plan.yaml", "approved_by: alice\n" + TWO.split("approved_boundaries: []\n")[1])),
               "--out", str(out)])
    summary = (out / "summary.md").read_text(encoding="utf-8")
    examined(1, "the gate's summary")
    assert rc == 0
    assert f"Not gated (ci.baseline is null): {INGEST}" in summary


def test_the_gate_s_report_names_the_run_by_its_folder(tmp_path, write_plan, examined):
    """With one command for every entry, each entry's run carries the same id and is told apart
    by its folder; so the report names the run and the baseline by their folders."""
    import json
    from tests.conftest import _write_run
    plan = write_plan("plan.yaml", "approved_by: alice\n")
    baseline = tmp_path / "baseline"
    _write_run(baseline, "baseline")
    reports = []
    for kind in ("ingest", "query"):
        run = tmp_path / "runs" / kind / "onetrace-ci-candidate"
        _write_run(run, "onetrace-ci-candidate")
        out = tmp_path / f"out-{kind}"
        assert main(["--run", str(run), "--baseline", str(baseline), "--plan", str(plan), "--out", str(out)]) == 0
        reports.append((run, (out / "summary.md").read_text(encoding="utf-8"),
                        json.loads((out / "verdict.json").read_text(encoding="utf-8"))))
    examined(len(reports), "gate reports of two runs with one id")
    for run, summary, verdict in reports:
        assert f"**run:** `{run}`, against **baseline:** `{baseline}`" in summary, summary
        assert (verdict["run"], verdict["baseline"]) == (str(run), str(baseline))
    assert reports[0][1] != reports[1][1]
