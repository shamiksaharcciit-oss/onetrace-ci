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
