"""A plan with several entries is run entry by entry and gated entry by entry.

`ci.run` is one command that runs every entry, or a mapping from each entry to its command.
`ci.baseline` is one path when there is one entry, or a mapping from each entry to its baseline
path, or to null (not gated). The workflow runs the entries in the plan's order and gates each
entry that has a baseline against that baseline only: a query run is never compared with an
ingest baseline. The gate's report names every entry left ungated.
"""
from __future__ import annotations

import pytest

from onetrace_ci.errors import explain
from onetrace_ci.gate import main as gate_main
from onetrace_ci.instrument import CANDIDATE_RUN_ID, workflow_text
from onetrace_ci.instrument_plan import PlanRefused, parse_instrument_plan
from onetrace_ci.plan import parse_plan_text

INGEST, QUERY = "pipeline.ingest:run", "pipeline.main:run"

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
    "pipeline.ingest:run": runs/ingest-baseline
    "pipeline.main:run": runs/query-baseline
'''
ONE_COMMAND = TWO.replace('  run:\n    "pipeline.ingest:run": python -m pipeline.ingest\n'
                          '    "pipeline.main:run": python -m pipeline.demo\n', "  run: python -m pipeline.all\n")
INGEST_UNGATED = TWO.replace('    "pipeline.ingest:run": runs/ingest-baseline\n', '    "pipeline.ingest:run": null\n')


def _with(old, new, text=TWO):
    assert text.count(old) == 1, old
    return text.replace(old, new)


def _refused(text):
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(text, source="<t>")
    #: Every refusal names its fix: each maps to an error code.
    assert all(explain(p) for p in caught.value.problems), caught.value.problems
    return "\n".join(caught.value.problems)


# ------------------------------------------------------------------ the plan

def test_each_entry_s_command_and_baseline_are_read_from_the_mappings(examined):
    ci = parse_instrument_plan(TWO, source="<t>").ci
    examined(2, "the entries' commands and baselines")
    assert (ci.run, ci.baseline) == (None, None)
    assert ci.run_of(INGEST) == "python -m pipeline.ingest" and ci.run_of(QUERY) == "python -m pipeline.demo"
    assert ci.baseline_of(INGEST) == "runs/ingest-baseline" and ci.baseline_of(QUERY) == "runs/query-baseline"


def test_one_command_may_run_every_entry(examined):
    ci = parse_instrument_plan(ONE_COMMAND, source="<t>").ci
    examined(2, "the entries' commands")
    assert ci.run == "python -m pipeline.all" and ci.run_of(INGEST) == ci.run_of(QUERY) == "python -m pipeline.all"


def test_null_leaves_an_entry_ungated(examined):
    ci = parse_instrument_plan(INGEST_UNGATED, source="<t>").ci
    examined(2, "the entries' baselines")
    assert ci.baseline_of(INGEST) is None and ci.baseline_of(QUERY) == "runs/query-baseline"


def test_a_mapping_for_a_plan_with_one_entry_reads_as_its_one_value(examined):
    from tests.instrument_fixtures import PLAN
    plan = parse_instrument_plan(PLAN.replace("  baseline: runs/baseline\n",
                                              '  baseline:\n    "pipeline.main:run": runs/baseline\n'), source="<t>")
    examined(1, "the one entry's baseline")
    assert plan.ci.baseline == "runs/baseline"


REFUSALS = {
    "one baseline path for two entries": (
        _with('  baseline:\n    "pipeline.ingest:run": runs/ingest-baseline\n    "pipeline.main:run": runs/query-baseline\n',
              "  baseline: runs/baseline\n"), ["ci.baseline", "one baseline path for 2 entries"]),
    "a baseline mapping that leaves an entry out": (
        _with('    "pipeline.main:run": runs/query-baseline\n', ""), ["ci.baseline", "missing", "'pipeline.main:run'"]),
    "a baseline mapping naming an entry the plan does not have": (
        _with('    "pipeline.main:run": runs/query-baseline\n',
              '    "pipeline.main:run": runs/query-baseline\n    "pipeline.other:run": runs/other\n'),
        ["ci.baseline", "'pipeline.other:run'", "is not one of"]),
    "a run mapping that leaves an entry out": (
        _with('    "pipeline.main:run": python -m pipeline.demo\n', ""), ["ci.run", "missing", "'pipeline.main:run'"]),
    "a run mapping naming an entry the plan does not have": (
        _with('    "pipeline.main:run": python -m pipeline.demo\n',
              '    "pipeline.main:run": python -m pipeline.demo\n    "pipeline.other:run": python -m other\n'),
        ["ci.run", "'pipeline.other:run'", "is not one of"]),
    "two entries mapped to one baseline": (
        _with("runs/query-baseline", "runs/ingest-baseline/"), ["ci.baseline", "'runs/ingest-baseline", "both"]),
    "a baseline that is not a path in the repository": (
        _with("runs/query-baseline", "/abs/query-baseline"), ["ci.baseline", "absolute"]),
    "a baseline that is neither a path nor null": (
        _with("    \"pipeline.main:run\": runs/query-baseline\n", "    \"pipeline.main:run\": [a, b]\n"),
        ["ci.baseline", "must be a path, or null"]),
    "a command that is not text": (
        _with("    \"pipeline.main:run\": python -m pipeline.demo\n", "    \"pipeline.main:run\": [a, b]\n"),
        ["ci.run", "must be text"]),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_each_ci_refusal_names_its_field(case, examined):
    text, words = REFUSALS[case]
    message = _refused(text)
    examined(1, f"refused: {case}")
    for word in words:
        assert word in message, message


# ------------------------------------------------------------------ the workflow

def _steps(text):
    """The workflow's steps, as (name, the lines under it)."""
    steps, name, body = [], None, []
    for line in text.splitlines():
        if line.startswith("      - "):
            if name is not None:
                steps.append((name, body))
            name, body = line[8:], []
        elif name is not None:
            body.append(line.strip())
    steps.append((name, body))
    return steps


def test_two_entries_are_run_in_the_plan_s_order_and_each_gated_against_its_own_baseline(examined):
    steps = _steps(workflow_text(parse_instrument_plan(TWO, source="<t>"), "onetrace-plan.yaml"))
    names = [n for n, _ in steps]
    examined(len(steps), "the workflow's steps")
    ingest_id, query_id = f"{CANDIDATE_RUN_ID}-pipeline-ingest-run", f"{CANDIDATE_RUN_ID}-pipeline-main-run"
    run_ingest, run_query = names.index(f"name: Run {INGEST}"), names.index(f"name: Run {QUERY}")
    gate_ingest, gate_query = names.index(f"name: onetrace-ci gate, {INGEST}"), names.index(f"name: onetrace-ci gate, {QUERY}")
    assert run_ingest < run_query < gate_ingest < gate_query
    assert f"ONETRACE_RUN_ID: {ingest_id}" in steps[run_ingest][1] and "python -m pipeline.ingest" in steps[run_ingest][1]
    assert f"ONETRACE_RUN_ID: {query_id}" in steps[run_query][1] and "python -m pipeline.demo" in steps[run_query][1]
    assert f'run: "runs/ingest/{ingest_id}"' in steps[gate_ingest][1]
    assert 'baseline: "runs/ingest-baseline"' in steps[gate_ingest][1]
    assert f'run: "runs/query/{query_id}"' in steps[gate_query][1]
    assert 'baseline: "runs/query-baseline"' in steps[gate_query][1]
    #: Each gate writes its own report, uploaded under its own name.
    assert 'out: "onetrace-ci-out-pipeline-ingest-run"' in steps[gate_ingest][1]
    assert 'report-name: "onetrace-ci-gate-report-pipeline-main-run"' in steps[gate_query][1]


def test_the_run_order_is_the_plan_s_order(examined):
    swapped = TWO.replace("  - entry: pipeline.ingest:run\n", "  - entry: FIRST\n").replace(
        "  - entry: pipeline.main:run\n", "  - entry: pipeline.ingest:run\n").replace(
        "  - entry: FIRST\n", "  - entry: pipeline.main:run\n")
    swapped = swapped.replace("runs/ingest/{run_id}", "RUNDIR").replace("runs/query/{run_id}", "runs/ingest/{run_id}").replace(
        "RUNDIR", "runs/query/{run_id}")
    names = [n for n, _ in _steps(workflow_text(parse_instrument_plan(swapped, source="<t>"), "onetrace-plan.yaml"))]
    examined(len(names), "the workflow's steps")
    assert names.index(f"name: Run {QUERY}") < names.index(f"name: Run {INGEST}")


def test_an_ungated_entry_has_no_gate_step(examined):
    names = [n for n, _ in _steps(workflow_text(parse_instrument_plan(INGEST_UNGATED, source="<t>"),
                                                "onetrace-plan.yaml"))]
    examined(len(names), "the workflow's steps")
    assert f"name: onetrace-ci gate, {QUERY}" in names
    assert not any(n.startswith("name: onetrace-ci gate") and INGEST in n for n in names)
    assert f"name: Run {INGEST}" in names


def test_one_command_runs_once_and_each_entry_s_run_is_found_in_its_own_run_dir(examined):
    steps = _steps(workflow_text(parse_instrument_plan(ONE_COMMAND, source="<t>"), "onetrace-plan.yaml"))
    names = [n for n, _ in steps]
    examined(len(steps), "the workflow's steps")
    assert names.count("name: Run the pipeline") == 1
    run = steps[names.index("name: Run the pipeline")][1]
    assert f"ONETRACE_RUN_ID: {CANDIDATE_RUN_ID}" in run
    assert f'run: "runs/ingest/{CANDIDATE_RUN_ID}"' in steps[names.index(f"name: onetrace-ci gate, {INGEST}")][1]
    assert f'run: "runs/query/{CANDIDATE_RUN_ID}"' in steps[names.index(f"name: onetrace-ci gate, {QUERY}")][1]


def test_a_plan_with_one_entry_keeps_the_one_run_and_one_gate(examined):
    from tests.instrument_fixtures import PLAN
    names = [n for n, _ in _steps(workflow_text(parse_instrument_plan(PLAN, source="<t>"), "onetrace-plan.yaml"))]
    examined(len(names), "the workflow's steps")
    assert "name: Run the pipeline" in names and "name: onetrace-ci gate" in names


# ------------------------------------------------------------------ the gate's report

def test_the_gate_s_report_names_every_entry_left_ungated(make_run, write_plan, tmp_path, capsys, examined):
    plan = parse_plan_text(INGEST_UNGATED, source="<t>")
    baseline, run = make_run("baseline"), make_run("candidate")
    out = tmp_path / "out"
    rc = gate_main(["--run", str(run), "--baseline", str(baseline), "--plan",
                    str(write_plan("plan.yaml", "approved_by: alice\n" + INGEST_UNGATED.split("approved_boundaries: []\n")[1])),
                    "--out", str(out)])
    summary = (out / "summary.md").read_text(encoding="utf-8")
    examined(1, "the gate's summary")
    assert plan.ungated == (INGEST,)
    assert rc == 0
    assert f"Not gated (ci.baseline is null): {INGEST}" in summary
