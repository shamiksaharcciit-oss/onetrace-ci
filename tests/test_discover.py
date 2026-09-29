"""`onetrace-ci discover`: from what the fixtures did and what the code says, a draft plan whose
every meaning field is a question for a person, a report, and the events it saw."""
from __future__ import annotations

import os
import re
import sys

import pytest

from onetrace_ci.discover import main
from onetrace_ci.instrument_plan import PlanRefused, parse_instrument_plan
from onetrace_ci.plan import find_open_questions, read_document
from tests.discover_fixtures import MARKER, make_discover_repo


@pytest.fixture(scope="module")
def discovered(tmp_path_factory):
    root = tmp_path_factory.mktemp("discover")
    repo, stubs = make_discover_repo(root)
    out = root / "out"
    env = {"LLM_API_KEY": MARKER, "PYTHONPATH": str(stubs)}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        rc = main(["--entry", "pipeline.main:run", "--repo", str(repo), "--out-dir", str(out), "--",
                   sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_pipeline.py"])
    finally:
        for k, v in saved.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    draft = (out / "onetrace-plan.draft.yaml").read_text(encoding="utf-8")
    report = (out / "discovery-report.md").read_text(encoding="utf-8")
    events = (out / "discovery-events.jsonl").read_text(encoding="utf-8")
    return rc, draft, report, events


def test_it_writes_the_three_files(discovered, examined):
    rc, draft, report, events = discovered
    examined(3, "output files")
    assert rc == 0 and draft and report and events


def test_the_draft_proposes_the_observed_stages_in_order(discovered, examined):
    _, draft, _, _ = discovered
    doc = read_document(draft, source="draft")
    stages = doc["stages"]
    examined(len(stages), "drafted stages")
    assert [s["name"] for s in stages] == ["intake", "retrieve", "answer"]
    intake, retrieve, answer = stages
    assert intake["memory_inputs"] == ["request"]
    assert retrieve["function"] == "pipeline.retrieval:retrieve"
    assert retrieve["files"] == ["data/corpus.json"]
    assert answer["function"] == "pipeline.llm:answer"
    assert answer["inputs"] == ["intake", "retrieve"]          # from the observed data flow
    assert doc["entry"] == "pipeline.main:run"


def test_every_meaning_field_is_a_question_and_nothing_else_is_missing(discovered, examined):
    """The draft is complete except for what a person decides: instrument refuses it only for
    its open questions."""
    _, draft, _, _ = discovered
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(draft, source="draft")
    problems = caught.value.problems
    examined(len(problems), "refusals of the draft")
    fields = {p.split(":")[0].removeprefix("plan field ") for p in problems}
    open_ = {path for path, _ in find_open_questions(read_document(draft, source="draft"))}
    assert fields <= open_, f"refused for something other than an open question: {fields - open_}"
    for must in ("approved_by", "stages[0].trust", "stages[1].trust", "stages[1].rederivable",
                 "stages[2].rederivable", "approved_boundaries", "sign", "anchor", "trust",
                 "require_declared"):
        assert must in open_, f"{must} is not left as a question"


def test_the_report_opens_with_the_number_of_open_questions(discovered, examined):
    _, draft, report, _ = discovered
    count = len(find_open_questions(read_document(draft, source="draft")))
    first = report.splitlines()[2]
    examined(count, "open questions in the draft")
    assert first == f"**{count} `DECIDE:` questions are still open.** Nothing in this draft is a decision."


def test_the_report_states_what_discovery_is_not(discovered, examined):
    _, _, report, _ = discovered
    claims = ["Discovery is not evidence.", "It sees only the code paths the fixtures exercised.",
              "Nothing it drafts is a decision.", "coverage stays incomplete"]
    examined(len(claims), "non-claims")
    for claim in claims:
        assert claim in report


def test_unaccounted_reads_come_first(discovered, examined):
    _, _, report, _ = discovered
    section = report.split("## Reads no stage explains", 1)[1].split("\n## ", 1)[0]
    examined(1, "the unaccounted-reads section")
    assert report.index("## Reads no stage explains") < report.index("## Stages")
    assert "config/settings.txt" in section and "PIPELINE_MODE" in section
    assert "data/corpus.json" not in section and "LLM_API_KEY" not in section


def test_a_branch_the_fixtures_never_took_is_listed(discovered, examined):
    _, _, report, _ = discovered
    section = report.split("## Branches the fixtures never took", 1)[1].split("\n## ", 1)[0]
    examined(1, "the unexercised-branches section")
    assert "pipeline/retrieval.py:10" in section


def test_confidence_is_reasons_never_a_percentage(discovered, examined):
    _, _, report, _ = discovered
    section = report.split("## Stages", 1)[1].split("\n## ", 1)[0]
    examined(1, "the stages section")
    assert "%" not in section
    assert "reads data/corpus.json" in section
    assert "its output is used by answer" in section
    assert "calls llm.example.test over HTTP (requests)" in section


@pytest.mark.parametrize("name", ["draft", "report", "events"])
def test_the_planted_secrets_are_in_none_of_the_outputs(discovered, name, examined):
    """An environment value, a request body and an exception message each held the marker."""
    _, draft, report, events = discovered
    text = {"draft": draft, "report": report, "events": events}[name]
    examined(len(text), f"characters of the {name}")
    assert MARKER not in text
    assert "the upstream said" not in text


def test_a_failing_command_is_refused(tmp_path, capsys, examined):
    repo, stubs = make_discover_repo(tmp_path)
    rc = main(["--entry", "pipeline.main:run", "--repo", str(repo), "--out-dir", str(tmp_path / "o"), "--",
               sys.executable, "-c", "raise SystemExit(3)"])
    examined(1, "a discovery over a failing command")
    assert rc == 1
    err = capsys.readouterr().err
    assert "exited 3" in err and "fix:" in err
    assert not (tmp_path / "o" / "onetrace-plan.draft.yaml").exists()


def test_a_package_the_environment_does_not_have_is_a_question_not_a_proposal(discovered, examined):
    """The generated code reads the package's version from the installed metadata at run time,
    so a package with none (the stub `requests` here) is never proposed as if it were."""
    _, draft, _, _ = discovered
    answer = read_document(draft, source="draft")["stages"][2]
    examined(1, "the answer stage's instrument")
    assert answer["instrument"]["package"].startswith("DECIDE:")
    assert "requests" in answer["instrument"]["package"]


def test_a_call_that_returns_nothing_is_named_as_possibly_not_a_stage(tmp_path, examined):
    """A pre-run check the entry calls is still proposed (discovery never decides what is a
    stage), with the evidence a person needs to drop it."""
    from tests.instrument_fixtures import MAIN, make_repo
    main_py = MAIN.replace("def run(request):\n    \"\"\"Retrieve, then answer.\"\"\"\n",
                           "def check():\n    return None\n\n\ndef run(request):\n    \"\"\"Retrieve, then answer.\"\"\"\n    check()\n")
    assert "check()" in main_py
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": main_py})
    rc = main(["--entry", "pipeline.main:run", "--repo", str(repo), "--out-dir", str(tmp_path / "out"), "--",
               sys.executable, "-c", "from pipeline.main import run; run('warranty')"])
    report = (tmp_path / "out" / "discovery-report.md").read_text(encoding="utf-8")
    section = report.split("### 2. check", 1)[1].split("###", 1)[0]
    examined(1, "the check stage's reasons")
    assert rc == 0
    assert "returned nothing, and no later stage uses its output: it may be a check, not a stage" in section


def test_settings_at_the_stage_call_site_are_drafted_as_a_question(tmp_path, examined):
    """A literal keyword argument where the entry calls a stage is a setting a person may want
    recorded: listed with its value and site, and drafted as a question, never as config."""
    from tests.instrument_fixtures import MAIN, RETRIEVAL, make_repo
    repo = make_repo(tmp_path / "repo", {
        "pipeline/main.py": MAIN.replace("passages = retrieve(request)", "passages = retrieve(request, top_k=1)"),
        "pipeline/retrieval.py": RETRIEVAL.replace("def retrieve(request):", "def retrieve(request, top_k=1):")})
    rc = main(["--entry", "pipeline.main:run", "--repo", str(repo), "--out-dir", str(tmp_path / "out"), "--",
               sys.executable, "-c", "from pipeline.main import run; run('warranty')"])
    report = (tmp_path / "out" / "discovery-report.md").read_text(encoding="utf-8")
    draft = read_document((tmp_path / "out" / "onetrace-plan.draft.yaml").read_text(encoding="utf-8"), source="d")
    retrieve = next(s for s in draft["stages"] if s["name"] == "retrieve")
    examined(1, "the retrieve stage's settings")
    assert rc == 0
    assert "retrieve: top_k=1 (at pipeline/main.py:8)" in report
    assert retrieve["config"].startswith("DECIDE:") and "top_k=1" in retrieve["config"]


def test_a_setting_from_the_environment_is_named_never_valued(tmp_path, examined, monkeypatch):
    from tests.instrument_fixtures import LLM, make_repo
    monkeypatch.setenv("ANSWER_MODEL", MARKER)
    monkeypatch.setenv("ANSWER_SEED", MARKER)
    llm = ("import os\n\n\ndef make(model, temperature, seed):\n    return model\n\n\n" + LLM.replace(
        '    return {"answer"', '    make(model=os.environ["ANSWER_MODEL"], temperature=os.getenv("ANSWER_TEMPERATURE", "0"),\n'
                               '         seed=os.environ.get("ANSWER_SEED"))\n'
                               '    return {"answer"'))
    repo = make_repo(tmp_path / "repo", {"pipeline/llm.py": llm})
    rc = main(["--entry", "pipeline.main:run", "--repo", str(repo), "--out-dir", str(tmp_path / "out"), "--",
               sys.executable, "-c", "from pipeline.main import run; run('warranty')"])
    report = (tmp_path / "out" / "discovery-report.md").read_text(encoding="utf-8")
    draft = (tmp_path / "out" / "onetrace-plan.draft.yaml").read_text(encoding="utf-8")
    examined(2, "outputs checked")
    assert rc == 0
    assert "answer: model from the environment variable ANSWER_MODEL" in report
    assert "answer: temperature from the environment variable ANSWER_TEMPERATURE" in report
    assert "answer: seed from the environment variable ANSWER_SEED" in report
    assert MARKER not in report and MARKER not in draft


def test_a_setting_from_the_environment_at_the_call_site_is_named(tmp_path, examined, monkeypatch):
    from tests.instrument_fixtures import MAIN, RETRIEVAL, make_repo
    monkeypatch.setenv("TOP_K", MARKER)
    main_py = "import os\n" + MAIN.replace("passages = retrieve(request)",
                                          'passages = retrieve(request, top_k=os.environ.get("TOP_K"))')
    repo = make_repo(tmp_path / "repo", {
        "pipeline/main.py": main_py,
        "pipeline/retrieval.py": RETRIEVAL.replace("def retrieve(request):", "def retrieve(request, top_k=1):")})
    rc = main(["--entry", "pipeline.main:run", "--repo", str(repo), "--out-dir", str(tmp_path / "out"), "--",
               sys.executable, "-c", "from pipeline.main import run; run('warranty')"])
    report = (tmp_path / "out" / "discovery-report.md").read_text(encoding="utf-8")
    draft = (tmp_path / "out" / "onetrace-plan.draft.yaml").read_text(encoding="utf-8")
    examined(2, "outputs checked")
    assert rc == 0
    assert "retrieve: top_k from the environment variable TOP_K (at pipeline/main.py:9)" in report
    assert MARKER not in report and MARKER not in draft
