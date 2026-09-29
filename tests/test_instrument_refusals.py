"""`onetrace-ci instrument` refuses what it cannot handle safely, names the file and line (or
the plan field), and writes no patch at all."""
from __future__ import annotations

import pytest

from onetrace_ci.instrument import Refused, build_patch, main
from tests.instrument_fixtures import MAIN, PLAN, make_repo


def _main_with(body: str) -> str:
    """The fixture's entry module with `run`'s two statements replaced by `body` (indented)."""
    old = "    passages = retrieve(request)\n    return answer(request, passages)\n"
    assert old in MAIN
    return MAIN.replace(old, body)


def _plan_with(old: str, new: str) -> str:
    assert old in PLAN, f"fixture text {old!r} not in the plan"
    return PLAN.replace(old, new, 1)


#: name -> (files to override, substrings the refusal must contain)
CASES = {
    # A missing meaning field, a nested stage, an unresolvable function, a module-level recorder.
    "a missing meaning field": (
        {"onetrace-plan.yaml": _plan_with("    trust: operator-authored\n", "")},
        ["plan field stages[1].trust:"]),
    "a nested stage call": (
        {"pipeline/main.py": _main_with("    return answer(request, retrieve(request))\n")},
        ["pipeline/main.py:8", "nested", "'retrieve'", "'answer'"]),
    "a stage that calls another stage": (
        {"pipeline/retrieval.py": "from pipeline.llm import answer\n\n\ndef retrieve(request):\n"
                                  "    return answer(request, [])\n"},
        ["pipeline/retrieval.py:5", "nested", "'answer'"]),
    "an unresolvable function": (
        {"onetrace-plan.yaml": _plan_with("pipeline.retrieval:retrieve", "pipeline.retrieval:fetch")},
        ["stages[1].function", "pipeline.retrieval:fetch", "pipeline/retrieval.py"]),
    "an unresolvable module": (
        {"onetrace-plan.yaml": _plan_with("pipeline.retrieval:retrieve", "pipeline.nowhere:retrieve")},
        ["stages[1].function", "pipeline.nowhere"]),
    "a module-level recorder already present": (
        {"pipeline/llm.py": "from pathlib import Path\nfrom onetrace.emit import Recorder\n"
                            "REC = Recorder('runs/x', declared_stages=['a'], manifest=Path(__file__), "
                            "policy='fail-closed')\n\n\ndef answer(request, passages):\n    return {}\n"},
        ["pipeline/llm.py:3", "module-level", "Recorder"]),
    "a module-level recorder under another name": (
        {"pipeline/main.py": MAIN.replace("from pipeline.retrieval import retrieve\n",
                                          "from pipeline.retrieval import retrieve\n"
                                          "from onetrace.emit import Recorder as Rec\n"
                                          "REC = Rec('runs/x')\n")},
        ["pipeline/main.py:5", "module-level", "Recorder"]),
    "a recorder inside the entry under another name": (
        {"pipeline/main.py": MAIN.replace("from pipeline.retrieval import retrieve\n",
                                          "from pipeline.retrieval import retrieve\n"
                                          "from onetrace import emit as e\n")
                                 .replace("    passages = retrieve(request)\n",
                                          "    rec = e.Recorder('runs/x')\n    passages = retrieve(request)\n")},
        ["pipeline/main.py:9", "Recorder", "already"]),
    "a module that is not valid Python, named at its line": (
        {"pipeline/llm.py": "def answer(request, passages):\n    x = 1\n    return \"unterminated\n"},
        ["pipeline/llm.py:3", "not valid Python"]),
    # Other constructs the command cannot handle safely.
    "a stage used as a value (dynamic dispatch)": (
        {"pipeline/main.py": _main_with("    fn = retrieve\n    passages = fn(request)\n"
                                        "    return answer(request, passages)\n")},
        ["pipeline/main.py:8", "dynamic dispatch", "'retrieve'"]),
    "a getattr call (dynamic dispatch)": (
        {"pipeline/main.py": _main_with("    passages = getattr(__import__('pipeline.retrieval'), 'x')(request)\n"
                                        "    passages = retrieve(request)\n"
                                        "    return answer(request, passages)\n")},
        ["pipeline/main.py:8", "dynamic dispatch"]),
    "a lambda stage": (
        {"pipeline/retrieval.py": "retrieve = lambda request: [{'id': 'p1', 'text': request}]\n"},
        ["pipeline/retrieval.py:1", "lambda"]),
    "a generator stage": (
        {"pipeline/retrieval.py": "def retrieve(request):\n    yield {'id': 'p1', 'text': request}\n"},
        ["pipeline/retrieval.py:1", "generator"]),
    "an async stage": (
        {"pipeline/retrieval.py": "async def retrieve(request):\n    return []\n"},
        ["pipeline/retrieval.py:1", "async"]),
    "a stage bound to something other than a function": (
        {"pipeline/retrieval.py": "class _R:\n    def retrieve(self, request):\n        return []\n\n\n"
                                  "retrieve = _R().retrieve\n"},
        ["pipeline/retrieval.py:6", "not a function definition"]),
    "a stage called in a loop": (
        {"pipeline/main.py": _main_with("    for _ in range(1):\n        passages = retrieve(request)\n"
                                        "    return answer(request, passages)\n")},
        ["pipeline/main.py:9", "inside a for loop", "'retrieve'"]),
    "a stage called conditionally": (
        {"pipeline/main.py": _main_with("    passages = retrieve(request) if request else []\n"
                                        "    return answer(request, passages)\n")},
        ["pipeline/main.py:8", "conditional", "'retrieve'"]),
    "a stage called inside a lambda": (
        {"pipeline/main.py": _main_with("    get = lambda: retrieve(request)\n"
                                        "    return answer(request, get())\n")},
        ["pipeline/main.py:8", "lambda", "'retrieve'"]),
    "a stage never called": (
        {"pipeline/main.py": _main_with("    return answer(request, [])\n")},
        ["'retrieve'", "never called", "pipeline/main.py"]),
    "a stage called twice": (
        {"pipeline/main.py": _main_with("    passages = retrieve(request)\n    passages = retrieve(request)\n"
                                        "    return answer(request, passages)\n")},
        ["'retrieve'", "pipeline/main.py:8", "pipeline/main.py:9", "more than once"]),
    "stages out of the declared order": (
        {"pipeline/main.py": _main_with("    first = answer(request, [])\n    passages = retrieve(request)\n"
                                        "    return first\n")},
        ["pipeline/main.py:8", "'answer'", "before", "'retrieve'"]),
    "a memory input that is not a parameter": (
        {"onetrace-plan.yaml": _plan_with("memory_inputs: [request]", "memory_inputs: [query]")},
        ["stages[0].memory_inputs", "'query'", "run"]),
    "code already instrumented by hand": (
        {"pipeline/main.py": _main_with("    rec = Recorder('runs/x')\n    passages = retrieve(request)\n"
                                        "    return answer(request, passages)\n")},
        ["pipeline/main.py:8", "Recorder", "already"]),
    "a name the generated code would shadow": (
        {"pipeline/main.py": _main_with("    _onetrace_out = 1\n    passages = retrieve(request)\n"
                                        "    return answer(request, passages)\n")},
        ["pipeline/main.py:8", "_onetrace_"]),
    "a file the plan names that does not exist": (
        {"onetrace-plan.yaml": _plan_with("files: [data/corpus.json]", "files: [data/missing.json]")},
        ["stages[1].files", "data/missing.json"]),
    "an entry function that does not exist": (
        {"onetrace-plan.yaml": _plan_with("entry: pipeline.main:run", "entry: pipeline.main:start")},
        ["entry", "pipeline.main:start", "pipeline/main.py"]),
    "the candidate run landing on the baseline": (
        {"onetrace-plan.yaml": _plan_with("baseline: runs/baseline", "baseline: runs/onetrace-ci-candidate")},
        ["ci.baseline", "run_dir"]),
    # Found by an independent review.
    "a stage inside try/except*": (
        {"pipeline/main.py": _main_with("    try:\n        passages = retrieve(request)\n"
                                        "    except* ValueError:\n        passages = []\n"
                                        "    return answer(request, passages)\n")},
        ["pipeline/main.py:9", "inside a try block", "'retrieve'"]),
    "two stage calls in one statement": (
        {"pipeline/main.py": _main_with("    pair = (retrieve(request), answer(request, []))\n"
                                        "    return pair\n")},
        ["pipeline/main.py:8", "one statement", "'retrieve'", "'answer'"]),
    "a stage in a chained comparison": (
        {"pipeline/main.py": _main_with("    passages = retrieve(request)\n"
                                        "    ok = 0 < len(request) < len(answer(request, passages))\n"
                                        "    return ok\n")},
        ["pipeline/main.py:9", "called conditionally", "'answer'"]),
    "a stage in an assert": (
        {"pipeline/main.py": _main_with("    passages = retrieve(request)\n"
                                        "    assert answer(request, passages)\n    return passages\n")},
        ["pipeline/main.py:9", "assert", "'answer'"]),
    "a stage name shadowed by a parameter": (
        {"pipeline/main.py": MAIN.replace("def run(request):", "def run(request, retrieve=retrieve):")},
        ["pipeline/main.py:8", "'retrieve'", "shadowed"]),
    "a stage name shadowed by a local": (
        {"pipeline/main.py": _main_with("    retrieve = lambda q: []\n    passages = retrieve(request)\n"
                                        "    return answer(request, passages)\n")},
        ["pipeline/main.py:9", "'retrieve'", "shadowed"]),
    "a star import": (
        {"pipeline/main.py": MAIN.replace("from pipeline.retrieval import retrieve\n",
                                          "from pipeline.retrieval import retrieve\nfrom pipeline.llm import *\n")},
        ["pipeline/main.py:4", "star import"]),
    "a module that is both a file and a package": (
        {"pipeline/llm/__init__.py": "def answer(request, passages):\n    return {}\n"},
        ["pipeline/llm.py", "pipeline/llm/__init__.py"]),
    "a stage body calling another stage through a local import": (
        {"pipeline/retrieval.py": "def retrieve(request):\n    from pipeline.llm import answer\n"
                                  "    return answer(request, [])\n"},
        ["pipeline/retrieval.py:3", "nested", "'answer'"]),
    "a file path with backslashes": (
        {"onetrace-plan.yaml": _plan_with("files: [data/corpus.json]", "files: ['..\\..\\outside\\leak.json']")},
        ["stages[1].files", "backslash"]),
    "a run_dir with backslashes": (
        {"onetrace-plan.yaml": _plan_with("run_dir: runs/{run_id}", "run_dir: '..\\..\\outside\\{run_id}'")},
        ["run_dir", "backslash"]),
    "a run_dir without {run_id}": (
        {"onetrace-plan.yaml": _plan_with("run_dir: runs/{run_id}", "run_dir: runs/latest")},
        ["run_dir", "{run_id}"]),
    "a source file that is not UTF-8": (
        {"pipeline/main.py": "# -*- coding: latin-1 -*-\n" + MAIN},
        ["pipeline/main.py:1", "UTF-8"]),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_each_refusal_names_its_target_and_writes_no_patch(case, tmp_path, examined, capsys):
    overrides, expected = CASES[case]
    repo = make_repo(tmp_path / "repo", overrides)
    out = tmp_path / "instrument.patch"
    examined(len(expected), f"substrings the refusal must name: {case}")

    with pytest.raises(Refused) as caught:
        build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    message = str(caught.value).replace("\\", "/")
    for want in expected:
        assert want in message, f"{want!r} not named in:\n{message}"

    #: And through the command itself: exit 1, the same message, and no patch file.
    rc = main(["--style", "wrappers", "--plan", str(repo / "onetrace-plan.yaml"), "--repo", str(repo), "--out", str(out)])
    err = capsys.readouterr().err.replace("\\", "/")
    assert rc == 1
    assert "refused" in err and expected[0] in err
    assert not out.exists()


def test_the_refusal_cases_cover_the_four_required_refusals(examined):
    named = {"a missing meaning field", "a nested stage call", "an unresolvable function",
             "a module-level recorder already present"}
    examined(len(named), "required refusal cases")
    assert named <= set(CASES)
