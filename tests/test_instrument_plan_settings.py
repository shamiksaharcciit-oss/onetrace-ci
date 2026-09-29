"""Per-stage `kind`, `config` and `constants`, and the run's `corpus` link. The settings that
explain a change are written by a person or named for recording at run time; never guessed."""
from __future__ import annotations

import pytest

from onetrace_ci.instrument import Refused, build_patch, main
from onetrace_ci.instrument_plan import PlanRefused, parse_instrument_plan
from tests.instrument_fixtures import PLAN, make_repo

RETRIEVE_INSTRUMENT = "    instrument: {name: word-overlap, package: onetrace-verify, kind: python-package}\n"
SETTINGS = RETRIEVE_INSTRUMENT + "    config: {top_k: 1, metric: word-overlap}\n    constants: [request]\n"
CORPUS = "corpus:\n  from: runs/ingest-latest\n  stages: [retrieve]\n"


def _with(old: str, new: str, text: str = PLAN) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def test_settings_and_corpus_are_read_as_written(examined):
    plan = parse_instrument_plan(_with(RETRIEVE_INSTRUMENT, SETTINGS) + CORPUS, source="<t>")
    retrieve = plan.stages[1]
    examined(3, "settings read from the plan")
    assert retrieve.config == {"top_k": "1", "metric": "word-overlap"}   # as written; never numbers
    assert retrieve.constants == ("request",)
    assert (plan.corpus.source, plan.corpus.stages) == ("runs/ingest-latest", ("retrieve",))
    assert plan.corpus.index_stage is None                              # no default


def test_a_corpus_index_stage_is_read_as_written(examined):
    plan = parse_instrument_plan(PLAN + CORPUS + "  index_stage: split\n", source="<t>")
    examined(1, "the corpus link's index stage")
    assert plan.corpus.index_stage == "split"


REFUSALS = {
    # The three the settings work names.
    "an instrument without a kind": (
        _with("package: onetrace-verify, kind: python-package}", "package: onetrace-verify}"),
        "stages[1].instrument.kind", "'retrieve'"),
    "a corpus stage that is not a planned stage": (
        PLAN + CORPUS.replace("[retrieve]", "[rerank]"), "corpus.stages", "'rerank'"),
    # And the shapes around them.
    "config that is not a mapping": (
        _with(RETRIEVE_INSTRUMENT, RETRIEVE_INSTRUMENT + "    config: [top_k]\n"), "stages[1].config", ""),
    "a boolean in config": (
        _with(RETRIEVE_INSTRUMENT, RETRIEVE_INSTRUMENT + "    config: {lowercase: true}\n"), "stages[1].config.lowercase", "quote"),
    "a constants entry that is not a name": (
        _with(RETRIEVE_INSTRUMENT, RETRIEVE_INSTRUMENT + '    constants: ["top k"]\n'), "stages[1].constants", ""),
    "a corpus without from": (PLAN + "corpus:\n  stages: [retrieve]\n", "corpus.from", ""),
    "an index_stage that is not a name": (PLAN + CORPUS + "  index_stage: [split]\n", "corpus.index_stage", ""),
    "an unknown corpus field": (PLAN + CORPUS + "  index: split\n", "corpus.index", "index_stage"),
    "an index_stage that names a function, not a stage": (
        PLAN + CORPUS + "  index_stage: pipeline.ingest:write_index\n", "corpus.index_stage", "stage"),
    "entries that are not mappings": (PLAN + "entries: [pipeline.main:run, pipeline.ingest:run]\n", "entries[0]",
                                      "must be a mapping"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_each_settings_refusal_names_its_field(case, examined):
    text, field, also = REFUSALS[case]
    examined(1, f"refused settings: {case}")
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(text, source="<t>")
    message = str(caught.value)
    assert f"plan field {field}:" in message, message
    assert also in message


def test_an_unresolvable_constants_name_is_refused_with_file_and_line(tmp_path, examined):
    """`retrieve(request)` has no parameter `top_k`, so there is nothing to record it from."""
    repo = make_repo(tmp_path / "repo", {
        "onetrace-plan.yaml": _with(RETRIEVE_INSTRUMENT, RETRIEVE_INSTRUMENT + "    constants: [top_k]\n")})
    examined(1, "an unresolvable constants name")
    with pytest.raises(Refused) as caught:
        build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    message = str(caught.value).replace("\\", "/")
    assert "pipeline/retrieval.py:7" in message and "'top_k'" in message and "stages[1].constants" in message


def test_a_dotted_constants_name_resolves_from_a_parameter(tmp_path, examined):
    """`request.lower` hangs off the parameter `request`, so it can be read at run time; the
    check gets past name resolution and stops only at the generation that waits for the SDK."""
    repo = make_repo(tmp_path / "repo", {
        "onetrace-plan.yaml": _with(RETRIEVE_INSTRUMENT, RETRIEVE_INSTRUMENT + "    constants: [request.lower]\n")})
    examined(1, "a dotted constants name")
    with pytest.raises(Refused) as caught:
        build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    assert "cannot be resolved" not in str(caught.value)
    assert "plan field stages[1].constants: " in str(caught.value) and "onetrace 0.2.0" in str(caught.value)


def test_stages_that_record_no_settings_are_named(tmp_path, examined, capsys):
    repo = make_repo(tmp_path / "repo")
    main(["--style", "wrappers", "--plan", str(repo / "onetrace-plan.yaml"), "--repo", str(repo), "--out", str(tmp_path / "p")])
    lines = [l for l in capsys.readouterr().out.splitlines() if "no settings" in l]
    examined(len(lines), "lines naming stages without settings")
    assert lines == ["stages that record no settings (no config or constants in the plan): "
                     "'retrieve', 'answer'"]


def test_each_open_corpus_question_is_named_once(examined):
    """A drafted corpus block holds questions, not wrong values: each is refused as open, once."""
    block = ('corpus:\n  from: "DECIDE: which ingest run?"\n  stages: "DECIDE: which stages?"\n'
             '  index_stage: "DECIDE: which ingest stage?"\n')
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(PLAN + block, source="<t>")
    about = [p for p in caught.value.problems if "corpus" in p]
    examined(len(about), "problems about the corpus block")
    assert len(about) == 3 and all("still an open question" in p for p in about), about


def test_an_answered_corpus_field_is_checked_while_another_is_still_open(examined):
    block = 'corpus:\n  from: runs/ingest-latest\n  stages: [nope]\n  index_stage: "DECIDE: which ingest stage?"\n'
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(PLAN + block, source="<t>")
    about = [p for p in caught.value.problems if "corpus" in p]
    examined(len(about), "problems about the corpus block")
    assert any(p.startswith("plan field corpus.stages:") and "'nope'" in p for p in about), about
    assert any(p.startswith("plan field corpus.index_stage:") and "open question" in p for p in about), about


def test_an_open_question_inside_the_corpus_stages_list_is_named_once(examined):
    block = 'corpus:\n  from: runs/ingest-latest\n  stages: ["DECIDE: which stages?"]\n'
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(PLAN + block, source="<t>")
    about = [p for p in caught.value.problems if "corpus" in p]
    examined(len(about), "problems about the corpus block")
    assert len(about) == 1 and "open question" in about[0], about
