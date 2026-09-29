"""The plan `onetrace-ci instrument` reads. Every field that carries meaning is written by a
person; a missing one is refused, by name, and never filled in by guessing."""
from __future__ import annotations

import pytest

from onetrace_ci.instrument_plan import PlanRefused, parse_instrument_plan

COMPLETE = """\
approved_by: alice
entry: pipeline.main:run
run_dir: runs/{run_id}
stages:
  - name: intake
    memory_inputs: [request]
    trust: externally-sourced
    rederivable: "true"
  - name: retrieve
    function: pipeline.retrieval:retrieve
    instrument: {name: bm25, package: rank_bm25, kind: retriever}
    inputs: [intake]
    files: [data/corpus.json]
    trust: operator-authored
    rederivable: "true"
  - name: answer
    function: pipeline.llm:answer
    instrument: {name: model-call, package: openai, kind: model}
    rederivable: "false"
    rederivable_note: "hosted model; sampling not reproducible"
approved_boundaries: []
ci:
  install: pip install --require-hashes -r requirements.lock
  run: python -m pipeline.demo
  baseline: runs/baseline
"""


def test_a_complete_plan_reads_every_field(examined):
    plan = parse_instrument_plan(COMPLETE, source="<t>")
    examined(len(plan.stages), "stages in the complete plan")
    assert plan.approved_by == "alice"
    assert (plan.entry.module, plan.entry.function) == ("pipeline.main", "run")
    assert plan.run_dir == "runs/{run_id}"
    assert [s.name for s in plan.stages] == ["intake", "retrieve", "answer"]
    intake, retrieve, answer = plan.stages
    assert intake.function is None
    assert intake.memory_inputs == ("request",)
    assert intake.trust == "externally-sourced"
    assert retrieve.function.module == "pipeline.retrieval"
    assert retrieve.function.function == "retrieve"
    assert (retrieve.instrument.name, retrieve.instrument.package) == ("bm25", "rank_bm25")
    assert retrieve.files == ("data/corpus.json",)
    assert retrieve.inputs == ("intake",)
    assert answer.rederivable == "false"
    assert answer.rederivable_note == "hosted model; sampling not reproducible"
    assert plan.approved_boundaries == ()
    assert (plan.ci.install, plan.ci.run, plan.ci.baseline) == (
        "pip install --require-hashes -r requirements.lock", "python -m pipeline.demo",
        "runs/baseline")


def test_inputs_default_to_the_previous_stage_one_after_another(examined):
    """Stages run one after another and pass their artifacts along; `inputs` overrides that."""
    plan = parse_instrument_plan(COMPLETE, source="<t>")
    examined(3, "the resolved inputs of each stage")
    assert plan.stages[0].inputs == ()
    assert plan.stages[1].inputs == ("intake",)          # written
    assert plan.stages[2].inputs == ("retrieve",)        # the default: the previous stage


def test_rederivable_may_be_written_as_a_boolean(examined):
    plan = parse_instrument_plan(COMPLETE.replace('rederivable: "false"', "rederivable: false"),
                                 source="<t>")
    examined(1, "an unquoted boolean rederivable")
    assert plan.stages[2].rederivable == "false"


def _without(text: str, line: str) -> str:
    assert line in text, f"fixture line {line!r} not found"
    return text.replace(line, "", 1)


def _swap(text: str, old: str, new: str) -> str:
    assert old in text, f"fixture text {old!r} not found"
    return text.replace(old, new, 1)


#: (plan text, the field the refusal must name)
REFUSALS = {
    "approved_by missing": (_without(COMPLETE, "approved_by: alice\n"), "approved_by"),
    "approved_by empty": (_swap(COMPLETE, "approved_by: alice", 'approved_by: ""'), "approved_by"),
    "approved_by None": (_swap(COMPLETE, "approved_by: alice", "approved_by: None"), "approved_by"),
    "entry missing": (_without(COMPLETE, "entry: pipeline.main:run\n"), "entry"),
    "entry without a function": (_swap(COMPLETE, "entry: pipeline.main:run", "entry: pipeline.main"), "entry"),
    "run_dir missing": (_without(COMPLETE, "run_dir: runs/{run_id}\n"), "run_dir"),
    "run_dir absolute": (_swap(COMPLETE, "run_dir: runs/{run_id}", "run_dir: /tmp/{run_id}"), "run_dir"),
    "run_dir unknown placeholder": (_swap(COMPLETE, "run_dir: runs/{run_id}", "run_dir: runs/{when}"), "run_dir"),
    "approved_boundaries missing": (_without(COMPLETE, "approved_boundaries: []\n"), "approved_boundaries"),
    "stages missing": (COMPLETE.split("stages:")[0] + "approved_boundaries: []\nci:\n  run: x\n  baseline: b\n  install: i\n", "stages"),
    "a stage name missing": (_swap(COMPLETE, "  - name: answer\n    function:", "  - function:"), "stages[2].name"),
    "a duplicate stage name": (_swap(COMPLETE, "  - name: answer", "  - name: retrieve"), "stages[2].name"),
    "rederivable missing": (_without(COMPLETE, '    rederivable: "false"\n'), "stages[2].rederivable"),
    "rederivable not a boolean": (_swap(COMPLETE, 'rederivable: "false"', "rederivable: maybe"), "stages[2].rederivable"),
    "trust missing, files named": (_without(COMPLETE, "    trust: operator-authored\n"), "stages[1].trust"),
    "trust missing, memory inputs named": (_without(COMPLETE, "    trust: externally-sourced\n"), "stages[0].trust"),
    "trust not a trust class": (_swap(COMPLETE, "trust: operator-authored", "trust: trusted"), "stages[1].trust"),
    "trust secret": (_swap(COMPLETE, "trust: operator-authored", "trust: secret"), "stages[1].trust"),
    "a function stage with no instrument": (_without(COMPLETE, "    instrument: {name: model-call, package: openai, kind: model}\n"), "stages[2].instrument"),
    "an instrument with no package": (_swap(COMPLETE, "{name: bm25, package: rank_bm25, kind: retriever}", "{name: bm25}"), "stages[1].instrument.package"),
    "an instrument with no name": (_swap(COMPLETE, "{name: bm25, package: rank_bm25, kind: retriever}", "{package: rank_bm25}"), "stages[1].instrument.name"),
    "an instrument with an unknown key": (_swap(COMPLETE, "{name: bm25, package: rank_bm25, kind: retriever}", "{name: bm25, package: rank_bm25, version: x}"), "stages[1].instrument.version"),
    "a stage with neither a function nor memory inputs": (_without(COMPLETE, "    memory_inputs: [request]\n"), "stages[0].function"),
    "memory inputs on a function stage": (_swap(COMPLETE, "    inputs: [intake]\n", "    inputs: [intake]\n    memory_inputs: [q]\n"), "stages[1].memory_inputs"),
    "a class method": (_swap(COMPLETE, "pipeline.llm:answer", "pipeline.llm:Model.answer"), "stages[2].function"),
    "inputs naming a later stage": (_swap(COMPLETE, "inputs: [intake]", "inputs: [answer]"), "stages[1].inputs"),
    "inputs naming no stage": (_swap(COMPLETE, "inputs: [intake]", "inputs: [nowhere]"), "stages[1].inputs"),
    "a file outside the repo": (_swap(COMPLETE, "files: [data/corpus.json]", "files: [../corpus.json]"), "stages[1].files"),
    "an absolute file": (_swap(COMPLETE, "files: [data/corpus.json]", "files: [/etc/corpus.json]"), "stages[1].files"),
    "a glob": (_swap(COMPLETE, "files: [data/corpus.json]", "files: [data/*.json]"), "stages[1].files"),
    "an unknown stage key": (_swap(COMPLETE, "    inputs: [intake]\n", "    inputs: [intake]\n    trsut: x\n"), "stages[1].trsut"),
    "a DECIDE: left open": (_swap(COMPLETE, "trust: operator-authored", 'trust: "DECIDE: is corpus.json operator-authored or externally-sourced?"'), "stages[1].trust"),
    "a DECIDE: in approved_by": (_swap(COMPLETE, "approved_by: alice", 'approved_by: "DECIDE: who approves this plan?"'), "approved_by"),
    "ci missing": (COMPLETE.split("ci:")[0], "ci"),
    "ci.run missing": (_without(COMPLETE, "  run: python -m pipeline.demo\n"), "ci.run"),
    "ci.baseline missing": (_without(COMPLETE, "  baseline: runs/baseline\n"), "ci.baseline"),
    "ci.install missing": (_without(COMPLETE, "  install: pip install --require-hashes -r requirements.lock\n"), "ci.install"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_each_refusal_names_its_field(case, examined):
    text, field = REFUSALS[case]
    examined(1, f"refused plan: {case}")
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(text, source="<t>")
    assert f"plan field {field}:" in str(caught.value), str(caught.value)


def test_every_missing_field_is_named_at_once(examined):
    """A person fixing a plan sees every field that is still missing, not one per run."""
    text = _without(_without(COMPLETE, "    trust: operator-authored\n"), '    rederivable: "false"\n')
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(text, source="<t>")
    examined(len(caught.value.problems), "problems named in one refusal")
    fields = [p.split(":")[0] for p in caught.value.problems]
    assert "plan field stages[1].trust" in fields
    assert "plan field stages[2].rederivable" in fields


def test_a_plan_that_leaves_meaning_unstated_is_refused_naming_each_field(examined):
    """This plan leaves `retrieve`'s trust and two stages' re-derivability unstated, and
    says nothing about CI. The generator would have to invent all of it, so it refuses and
    names every field."""
    example = """\
approved_by: alice
entry: pipeline.main:run            # the function that is one run
run_dir: runs/{run_id}
stages:
  - name: intake
    memory_inputs: [request]        # -> ctx.read_memory, trust class stated below
    trust: externally-sourced
  - name: retrieve
    function: pipeline.retrieval:retrieve
    instrument: {name: bm25, package: rank_bm25, kind: retriever}   # version read at run time
    inputs: [intake]
    files: [data/corpus.json]       # -> ctx.read_external
  - name: answer
    function: pipeline.llm:answer
    instrument: {name: model-call, package: openai, kind: model}
    rederivable: "false"
    rederivable_note: "hosted model; sampling not reproducible"
approved_boundaries: []
"""
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(example, source="<t>")
    named = {p.split(":")[0].removeprefix("plan field ") for p in caught.value.problems}
    examined(len(named), "fields the example leaves unstated")
    assert {"stages[0].rederivable", "stages[1].rederivable", "stages[1].trust", "ci"} <= named
    assert "stages[2].rederivable" not in named   # stated, so not named
