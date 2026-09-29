"""A plan with several entries (`entries:`, one per run type), and the D1a stage
fields `repeats` and `instances`. Every problem is refused, naming its field."""
from __future__ import annotations

import pytest

from onetrace_ci.instrument_plan import PlanRefused, parse_instrument_plan
from onetrace_ci.plan import parse_plan_text

TWO = '''\
approved_by: alice
entries:
  - entry: pipeline.main:run
    run_dir: runs/query/{run_id}
    stages:
      - name: retrieve
        function: pipeline.retrieval:retrieve
        instrument: {name: bm25, package: rank_bm25, kind: retriever}
        rederivable: "true"
    corpus:
      from: runs/ingest-latest
      stages: [retrieve]
      index_stage: split
  - entry: pipeline.ingest:run
    run_dir: runs/ingest/{run_id}
    stages:
      - name: split
        function: pipeline.ingest:split
        instrument: {name: splitter, package: onetrace, kind: chunker}
        rederivable: "true"
approved_boundaries: []
ci:
  install: pip install -e .
  run: python -m pipeline.demo
  baseline: runs/baseline
'''


def _with(old: str, new: str, text: str = TWO) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def test_a_plan_with_two_entries_is_read_entry_by_entry(examined):
    plan = parse_instrument_plan(TWO, source="<t>")
    examined(2, "the plan's entries")
    assert [str(e.entry) for e in plan.entries] == ["pipeline.main:run", "pipeline.ingest:run"]
    assert [e.run_dir for e in plan.entries] == ["runs/query/{run_id}", "runs/ingest/{run_id}"]
    assert [[s.name for s in e.stages] for e in plan.entries] == [["retrieve"], ["split"]]
    query, ingest = plan.entries
    assert (query.corpus.source, query.corpus.stages, query.corpus.index_stage) == \
        ("runs/ingest-latest", ("retrieve",), "split")
    assert ingest.corpus is None


def test_a_plan_with_one_entry_reads_as_one_entry(examined):
    from tests.instrument_fixtures import PLAN
    plan = parse_instrument_plan(PLAN, source="<t>")
    examined(1, "the plan's one entry")
    assert len(plan.entries) == 1 and plan.entries[0].entry == plan.entry
    assert plan.entries[0].stages == plan.stages


REFUSALS = {
    "entries and a top-level entry": (_with("approved_by: alice\n", "approved_by: alice\nentry: pipeline.main:run\n"),
                                       "entry", "entries"),
    "two entries sharing a run_dir": (_with("runs/ingest/{run_id}", "runs/query/{run_id}"),
                                      "entries[1].run_dir", "entries[0]"),
    "two entries sharing a run_dir written two ways": (_with("runs/ingest/{run_id}", "runs/query/{run_id}/"),
                                                       "entries[1].run_dir", "entries[0]"),
    "the same entry twice": (_with("entry: pipeline.ingest:run", "entry: pipeline.main:run"),
                             "entries[1].entry", "entries[0]"),
    "an index_stage of the entry carrying the link": (_with("index_stage: split", "index_stage: retrieve"),
                                                      "entries[0].corpus.index_stage", "'retrieve'"),
    "an index_stage no other entry has": (_with("index_stage: split", "index_stage: embed"),
                                          "entries[0].corpus.index_stage", "'embed'"),
    "a corpus.from that is not a path in the repository": (_with("from: runs/ingest-latest", "from: /srv/runs/ingest"),
                                                          "entries[0].corpus.from", "absolute"),
    "an entry without stages": (_with('''    stages:
      - name: split
        function: pipeline.ingest:split
        instrument: {name: splitter, package: onetrace, kind: chunker}
        rederivable: "true"
''', ""), "entries[1].stages", ""),
    "a stage field missing inside an entry": (_with('''        instrument: {name: splitter, package: onetrace, kind: chunker}
        rederivable: "true"
''', '''        instrument: {name: splitter, package: onetrace, kind: chunker}
'''), "entries[1].stages[0].rederivable", ""),
    "entries that are not a list": ("approved_by: alice\nentries: pipeline.main:run\napproved_boundaries: []\n"
                                    "ci:\n  install: a\n  run: b\n  baseline: runs/baseline\n", "entries", ""),
    "a top-level stages beside entries": (_with("approved_boundaries: []\n", "stages: []\napproved_boundaries: []\n"),
                                          "stages", "entries"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_each_entries_refusal_names_its_field(case, examined):
    text, field, also = REFUSALS[case]
    examined(1, f"refused: {case}")
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(text, source="<t>")
    message = str(caught.value)
    assert f"plan field {field}:" in message, message
    assert also in message, message


def test_a_top_level_corpus_from_is_a_path_in_the_repository_too(examined):
    from tests.instrument_fixtures import PLAN
    examined(1, "a corpus.from outside the repository")
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(PLAN + "corpus:\n  from: ../elsewhere/runs\n  stages: [retrieve]\n", source="<t>")
    assert "plan field corpus.from:" in str(caught.value)


def test_repeats_and_instances_are_read_as_written(examined):
    text = _with('''        rederivable: "true"
    corpus:''', '''        rederivable: "true"
        repeats: true
        instances: [en, fr]
    corpus:''')
    retrieve = parse_instrument_plan(text, source="<t>").entries[0].stages[0]
    examined(2, "repeats and instances")
    assert retrieve.repeats is True
    assert retrieve.instances == ("en", "fr")


@pytest.mark.parametrize("field,value,why", [
    ("repeats", "sometimes", "true or false"),
    ("instances", "[en, en]", "'en'"),
    ("instances", "en", "list"),
])
def test_each_repeats_or_instances_refusal_names_its_field(field, value, why, examined):
    text = _with('''        rederivable: "true"
    corpus:''', f'''        rederivable: "true"
        {field}: {value}
    corpus:''')
    examined(1, f"refused {field}: {value}")
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(text, source="<t>")
    assert f"plan field entries[0].stages[0].{field}:" in str(caught.value) and why in str(caught.value)


def test_the_gate_requires_declared_fields_by_default_in_a_plan_with_entries(examined):
    plan = parse_plan_text(TWO, source="<t>")
    examined(1, "the gate's reading of a plan with entries")
    assert plan.require_declared is True
    assert "entries" not in plan.ignored_keys and "corpus" not in plan.ignored_keys


@pytest.mark.parametrize("case", ["entries", "repeats", "instances"])
def test_the_wrapper_style_refuses_what_only_decorators_generate(case, tmp_path, examined):
    from onetrace_ci.instrument import Refused, build_patch
    from tests.instrument_fixtures import PLAN, make_repo
    plan = {"entries": TWO,
            "repeats": PLAN.replace('    rederivable: "true"\n  - name: answer', '    rederivable: "true"\n    repeats: true\n  - name: answer'),
            "instances": PLAN.replace('    rederivable: "true"\n  - name: answer', '    rederivable: "true"\n    instances: [a, b]\n  - name: answer'),
            }[case]
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": plan})
    examined(1, f"a wrapper-style plan with {case}")
    with pytest.raises(Refused) as caught:
        build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    assert "decorator" in str(caught.value) and case in str(caught.value), str(caught.value)
