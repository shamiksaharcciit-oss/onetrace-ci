"""`onetrace-ci instrument --style decorators`: the plan as onetrace 0.2.0's decorators.

The patch is applied with `git apply`, as a person would, and the files are read back. The
generated pipeline is run against `stub_sdk`, a stand-in that records what each decorator is
called with under the SDK's names. Two forms wait for onetrace 0.2.0's candidate; their tests
run now, as expected failures that say why.
"""
from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

from onetrace_ci.errors import explain
from onetrace_ci.instrument import Refused, build_patch, main, workflow_text
from onetrace_ci.instrument_plan import load_instrument_plan
from tests.decorator_fixtures import LLM, MAIN, PLAN, make_repo, stub_sdk

RUN = [sys.executable, "-c", "from pipeline.main import run; print(run())"]
IMPORT = "import onetrace as ot\n"


def _patch(repo, style="decorators"):
    return build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style=style)


def _apply(repo, patch, tmp_path):
    p = tmp_path / "instrument.patch"
    p.write_bytes(patch.encode("utf-8"))
    subprocess.run(["git", "-C", str(repo), "apply", str(p)], check=True, capture_output=True, timeout=60)


def _read(repo, rel):
    return (repo / rel).read_text(encoding="utf-8")


def _with(old, new, text=PLAN):
    assert text.count(old) == 1, old
    return text.replace(old, new)


def test_the_entry_gets_ot_run_naming_its_stages_its_run_dir_and_its_edges(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    _apply(repo, _patch(repo).patch, tmp_path)
    main_py = _read(repo, "pipeline/main.py")
    examined(1, "the entry module")
    assert '''@ot.run(
    stages=["retrieve", "answer"],
    run_dir="runs/{run_id}",
    declared_edges=[{"from": "retrieve", "to": "answer"}],
)
def run():''' in main_py
    assert main_py.count(IMPORT) == 1


def test_each_stage_gets_ot_stage_with_the_plan_s_meaning_fields(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    _apply(repo, _patch(repo).patch, tmp_path)
    retrieval, llm = _read(repo, "pipeline/retrieval.py"), _read(repo, "pipeline/llm.py")
    examined(2, "the stage modules")
    assert '''@ot.stage(
    "retrieve",
    instrument=ot.pkg("word-overlap", "onetrace-verify", kind="retriever"),
    files=["data/question.txt", "data/corpus.json"],
    trust="operator-authored",
    rederivable=True,
)
def retrieve():''' in retrieval
    assert '''@ot.stage(
    "answer",
    instrument=ot.pkg("extractive", "onetrace", kind="answerer"),
    rederivable=False,
    note="a stand-in for a hosted model",
)
def answer(passages):''' in llm
    for text in (retrieval, llm):
        assert text.count(IMPORT) == 1


def test_settings_and_constants_are_generated_as_written(tmp_path, examined):
    plan = _with("kind: retriever}\n", 'kind: retriever}\n    config: {top_k: "1"}\n')
    plan = _with('    rederivable: "false"\n', '    rederivable: "false"\n    constants: [passages]\n', plan)
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": plan})
    _apply(repo, _patch(repo).patch, tmp_path)
    retrieval, llm = _read(repo, "pipeline/retrieval.py"), _read(repo, "pipeline/llm.py")
    examined(2, "the stage modules")
    assert 'instrument=ot.pkg("word-overlap", "onetrace-verify", kind="retriever", config={"top_k": "1"}),' in retrieval
    assert 'def answer(passages):\n    """Answer from the best passage."""\n    ot.constant("passages", passages)\n' in llm
    assert _patch(repo).files == []


def test_a_changed_constants_list_replaces_the_calls_at_the_top_of_the_body(tmp_path, examined):
    plan = _with('    rederivable: "false"\n', '    rederivable: "false"\n    constants: [passages]\n')
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": plan})
    _apply(repo, _patch(repo).patch, tmp_path)
    (repo / "onetrace-plan.yaml").write_text(PLAN, encoding="utf-8")
    _apply(repo, _patch(repo).patch, tmp_path)
    llm = _read(repo, "pipeline/llm.py")
    examined(1, "the stage module after the constants were dropped")
    assert "ot.constant(" not in llm
    assert 'def answer(passages):\n    """Answer from the best passage."""\n    return' in llm


def test_constants_go_after_a_docstring_written_on_one_line_with_the_code(tmp_path, examined):
    llm = 'def answer(passages): """Answer from the best passage."""; return {"answer": passages[0]["text"]}\n'
    plan = _with('    rederivable: "false"\n', '    rederivable: "false"\n    constants: [passages]\n')
    repo = make_repo(tmp_path / "repo", {"pipeline/llm.py": llm, "onetrace-plan.yaml": plan})
    _apply(repo, _patch(repo).patch, tmp_path)
    text = _read(repo, "pipeline/llm.py")
    examined(1, "the stage module")
    assert ('def answer(passages):\n    """Answer from the best passage."""\n    ot.constant("passages", passages)\n'
            '    return {"answer": passages[0]["text"]}\n') in text
    assert _patch(repo).files == []


def test_an_async_stage_retried_with_a_timeout_in_a_loop_is_decorated(tmp_path, examined):
    """`asyncio.wait_for` awaits the one call it is given before it returns: calls made one after
    another overlap nothing."""
    llm = LLM.replace("def answer", "async def answer")
    main_py = MAIN.replace("from pipeline.llm import answer\n", "import asyncio\n\nfrom pipeline.llm import answer\n")
    main_py = main_py.replace("def run():", "async def run():").replace(
        "    return answer(passages)\n",
        "    for _ in range(2):\n        out = await asyncio.wait_for(answer(passages), 5)\n    return out\n")
    plan = _with('    rederivable: "false"\n', '    rederivable: "false"\n    repeats: true\n')
    repo = make_repo(tmp_path / "repo", {"pipeline/llm.py": llm, "pipeline/main.py": main_py, "onetrace-plan.yaml": plan})
    result = _patch(repo)
    examined(1, "the patch for a retry loop")
    assert "pipeline/llm.py" in result.files


def test_a_stage_s_output_that_is_only_read_needs_no_trust(tmp_path, examined):
    """An `if` test, `not`, a comparison and `len` or `print` read a value and change nothing."""
    main_py = MAIN.replace("    passages = retrieve()\n",
                           "    passages = retrieve()\n    if not passages:\n        return None\n"
                           "    if passages:\n        print(len(passages))\n    if passages == []:\n        return None\n")
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": main_py})
    result = _patch(repo)
    examined(1, "the patch for an entry that reads a stage's output")
    assert "pipeline/llm.py" in result.files


def test_another_run_s_stage_in_a_module_a_stage_reaches_is_left_alone(tmp_path, examined):
    """Only the functions a stage reaches are checked for an @ot.stage: a shared module may hold
    another run type's hand-decorated stages."""
    fmt = TIDY.replace('@ot.stage("tidy", rederivable=True)\n', "") + (
        '\n\n@ot.stage("split", rederivable=True)\ndef split(x):\n    return x\n')
    llm = "from pipeline.fmt import tidy\n\n\n" + LLM.replace('passages[0]["text"]', 'tidy(passages)[0]["text"]')
    repo = make_repo(tmp_path / "repo", {"pipeline/fmt.py": fmt, "pipeline/llm.py": llm})
    result = _patch(repo)
    examined(1, "the patch for a stage that reaches a shared module")
    assert "pipeline/llm.py" in result.files and "pipeline/fmt.py" not in result.files


def test_a_local_named_like_a_helper_is_not_the_helper(tmp_path, examined):
    """`fetch` in the entry is its local, not the module's `fetch` that runs a stage."""
    main_py = MAIN.replace('    """Retrieve, then answer."""\n',
                           '    """Retrieve, then answer."""\n    fetch = len\n    fetch("x")\n') + (
        "\n\ndef fetch():\n    return retrieve()\n")
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": main_py})
    result = _patch(repo)
    examined(1, "the patch for an entry with a local named like a helper")
    assert "pipeline/main.py" in result.files


def test_a_person_s_own_ot_constant_call_is_kept(tmp_path, examined):
    llm = 'import onetrace as ot\n\n\n' + LLM.replace(
        '    """Answer from the best passage."""\n',
        '    """Answer from the best passage."""\n    ot.constant("model", "m-1")\n')
    repo = make_repo(tmp_path / "repo", {"pipeline/llm.py": llm})
    _apply(repo, _patch(repo).patch, tmp_path)
    examined(1, "the stage module")
    assert '    """Answer from the best passage."""\n    ot.constant("model", "m-1")\n' in _read(repo, "pipeline/llm.py")


def test_a_decorator_laid_out_differently_but_equal_to_the_plan_is_left_alone(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    _apply(repo, _patch(repo).patch, tmp_path)
    llm = _read(repo, "pipeline/llm.py")
    start, end = llm.index("@ot.stage("), llm.index("def answer")
    one_line = ('@ot.stage("answer", instrument=ot.pkg("extractive", "onetrace", kind="answerer"), '
                'rederivable=False, note="a stand-in for a hosted model")\n')
    (repo / "pipeline/llm.py").write_text(llm[:start] + one_line + llm[end:], encoding="utf-8")
    examined(1, "the patch after the decorator was reformatted")
    assert _patch(repo).files == []


def test_a_file_with_crlf_line_endings_keeps_them(tmp_path, examined):
    repo = make_repo(tmp_path / "repo", {"pipeline/llm.py": LLM.replace("\n", "\r\n")})
    result = _patch(repo)
    added = [line for line in result.patch.split("diff --git a/pipeline/llm.py")[1].split("diff --git")[0]
             .split("\n") if line.startswith("+") and not line.startswith("+++")]
    examined(len(added), "lines added to the CRLF module")
    assert added and all(line.endswith("\r") for line in added), added


def test_wrappers_are_the_default_style_until_onetrace_0_2_0_is_pinned(tmp_path, capsys, examined):
    """Decorator output imports onetrace 0.2.0's @ot.run and @ot.stage, which the pinned onetrace
    lacks; until onetrace-ci pins 0.2.0, the command and the function generate wrappers."""
    repo = make_repo(tmp_path / "repo")
    out = tmp_path / "p.patch"
    rc = main(["--plan", str(repo / "onetrace-plan.yaml"), "--repo", str(repo), "--out", str(out)])
    api = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo)
    examined(2, "the patches from the command and the function, with no style named")
    assert rc == 0, capsys.readouterr().err
    for patch in (out.read_text(encoding="utf-8"), api.patch):
        assert "Recorder(" in patch and "@ot.run(" not in patch


def test_a_sign_block_is_refused_in_decorator_style_too(tmp_path, examined):
    plan = PLAN + ("sign:\n  key_env: ONETRACE_KEY\n  when_key_missing: unsigned\n"
                   "trust:\n  file: keys.json\n  untrusted_signature: review\n")
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": plan, "keys.json": "{}"})
    examined(1, "a plan with a sign block")
    with pytest.raises(Refused) as caught:
        _patch(repo)
    assert caught.value.problems == [p for p in caught.value.problems if p.startswith("plan field sign: signing")]
    assert explain(caught.value.problems[0])[0] == "needs-sdk"


def test_the_generated_code_calls_the_sdk_with_the_plan_s_fields_and_prints_the_same(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    before = subprocess.run(RUN, cwd=repo, capture_output=True, text=True, timeout=120)
    _apply(repo, _patch(repo).patch, tmp_path)
    log = tmp_path / "calls.json"
    env = dict(os.environ, PYTHONPATH=str(stub_sdk(tmp_path / "sdk")), ONETRACE_STUB_LOG=str(log))
    after = subprocess.run(RUN, cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    calls = json.loads(log.read_text(encoding="utf-8"))
    examined(len(calls), "decorator calls the generated code made")
    assert before.returncode == 0 and after.returncode == 0, after.stderr
    assert after.stdout == before.stdout
    by = {c.get("stage") or "run": c for c in calls}
    assert by["run"]["stages"] == ["retrieve", "answer"] and by["run"]["run_dir"] == "runs/{run_id}"
    assert by["run"]["declared_edges"] == [{"from": "retrieve", "to": "answer"}]
    assert by["retrieve"]["instrument"] == {"pkg": {"id": "word-overlap", "package": "onetrace-verify",
                                                    "kind": "retriever", "config": None, "rederivable": None,
                                                    "note": None}}
    assert (by["retrieve"]["trust"], by["retrieve"]["rederivable"]) == ("operator-authored", True)
    assert (by["answer"]["rederivable"], by["answer"]["note"]) == (False, "a stand-in for a hosted model")
    assert all(c["other"] == {} for c in calls)


def test_code_already_decorated_as_the_plan_says_gives_an_empty_patch(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    _apply(repo, _patch(repo).patch, tmp_path)
    again = _patch(repo)
    examined(1, "the second patch")
    assert again.patch == "" and again.files == []


def test_a_changed_meaning_field_replaces_the_decorator_and_shows_old_and_new(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    _apply(repo, _patch(repo).patch, tmp_path)
    plan = _with('    rederivable: "false"\n    rederivable_note: "a stand-in for a hosted model"\n',
                 '    rederivable: "true"\n')
    (repo / "onetrace-plan.yaml").write_text(plan, encoding="utf-8")
    result = _patch(repo)
    examined(1, "the patch after the plan changed")
    assert result.files == ["pipeline/llm.py"]
    assert "-    rederivable=False,\n" in result.patch and "+    rederivable=True,\n" in result.patch
    assert '-    note="a stand-in for a hosted model",\n' in result.patch


def test_both_styles_write_the_same_workflow(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    result = _patch(repo)
    plan = load_instrument_plan(repo / "onetrace-plan.yaml")
    examined(1, "the workflow in the decorator patch")
    assert ".github/workflows/onetrace.yml" in result.files
    _apply(repo, result.patch, tmp_path)
    assert _read(repo, ".github/workflows/onetrace.yml") == workflow_text(plan, "onetrace-plan.yaml")


def test_style_decorators_refuses_until_onetrace_0_2_0_is_pinned(tmp_path, capsys, examined):
    """`--style decorators` refuses, with a fix line, and writes no patch: the code it would
    generate can't run on the onetrace requirements.lock pins. The generator itself stays tested
    through build_patch(style="decorators") and stub_sdk."""
    repo = make_repo(tmp_path / "repo")
    out = tmp_path / "p.patch"
    rc = main(["--plan", str(repo / "onetrace-plan.yaml"), "--repo", str(repo), "--out", str(out),
               "--style", "decorators"])
    err = capsys.readouterr().err
    examined(1, "the refusal")
    assert rc == 1
    assert not out.exists()
    assert "--style decorators" in err and "needs onetrace 0.2.0" in err
    assert "    fix: " in err and "decorators-need-sdk" in err


def test_the_readme_states_the_default_style_rule_in_one_place(examined):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    rule = "`wrappers` is the default style until onetrace-ci pins onetrace 0.2.0"
    examined(1, "README")
    assert readme.count(rule) == 1
    assert "`--style decorators` refuses" in readme


def test_a_stage_that_repeats_may_be_called_in_a_loop(tmp_path, examined):
    main_py = MAIN.replace("    passages = retrieve()\n", "    for _ in range(2):\n        passages = retrieve()\n")
    plan = _with('    rederivable: "true"\n  - name: answer', '    rederivable: "true"\n    repeats: true\n  - name: answer')
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": main_py, "onetrace-plan.yaml": plan})
    result = _patch(repo)
    examined(1, "the patch for a repeating stage")
    assert "pipeline/retrieval.py" in result.files


def test_an_async_stage_is_decorated(tmp_path, examined):
    llm = LLM.replace("def answer(passages):", "async def answer(passages):")
    main_py = MAIN.replace("from pipeline.llm import answer\n", "import asyncio\n\nfrom pipeline.llm import answer\n")
    main_py = main_py.replace("    return answer(passages)\n", "    return asyncio.run(answer(passages))\n")
    repo = make_repo(tmp_path / "repo", {"pipeline/llm.py": llm, "pipeline/main.py": main_py})
    _apply(repo, _patch(repo).patch, tmp_path)
    examined(1, "the async stage module")
    assert ")\nasync def answer(passages):" in _read(repo, "pipeline/llm.py")


def test_a_stage_s_output_passed_straight_to_the_next_stage_is_not_a_nested_call(tmp_path, examined):
    """`retrieve` returns before `answer` starts, so the SDK records two stages and one edge."""
    main_py = MAIN.replace("    passages = retrieve()\n    return answer(passages)\n", "    return answer(retrieve())\n")
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": main_py})
    result = _patch(repo)
    examined(1, "the patch for a call inside a call")
    assert "pipeline/llm.py" in result.files and "pipeline/retrieval.py" in result.files


SUBMIT_ONCE = MAIN.replace(
    "    passages = retrieve()\n",
    "    import concurrent.futures\n    with concurrent.futures.ThreadPoolExecutor() as pool:\n"
    "        passages = pool.submit(retrieve).result()\n")


@pytest.mark.xfail(strict=True, raises=Refused, reason="a stage run in another thread waits for the SDK to "
                                                       "show, by a test against onetrace 0.2.0's candidate, "
                                                       "that the call is recorded in the run; generated once "
                                                       "it does")
def test_a_stage_handed_once_to_a_pool_is_decorated_when_the_plan_states_what_it_trusts(tmp_path, examined):
    """One call cannot overlap itself. Its result reaches `answer` through a future, not as
    `retrieve`'s return value, so `answer` needs its trust stated."""
    plan = _with('    rederivable: "false"\n', '    trust: operator-authored\n    rederivable: "false"\n')
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": SUBMIT_ONCE, "onetrace-plan.yaml": plan})
    result = _patch(repo)
    examined(1, "the patch for a stage handed once to a pool")
    assert "pipeline/retrieval.py" in result.files


def test_an_async_stage_run_as_one_task_is_decorated(tmp_path, examined):
    """An asyncio task inherits the caller's context, so the SDK finds the run in it."""
    llm = LLM.replace("def answer", "async def answer")
    main_py = MAIN.replace("from pipeline.llm import answer\n", "import asyncio\n\nfrom pipeline.llm import answer\n")
    main_py = main_py.replace("def run():", "async def run():").replace(
        "    return answer(passages)\n", "    task = asyncio.create_task(answer(passages))\n    return await task\n")
    repo = make_repo(tmp_path / "repo", {"pipeline/llm.py": llm, "pipeline/main.py": main_py})
    result = _patch(repo)
    examined(1, "the patch for an async stage run as one task")
    assert "pipeline/llm.py" in result.files


REFUSED = {
    "a function stage named intake": (
        {"onetrace-plan.yaml": PLAN.replace("  - name: answer\n", "  - name: intake\n")}, ["'intake'", "reserved"]),
    "a corpus link": (
        {"onetrace-plan.yaml": PLAN + "corpus:\n  from: runs/ingest-latest\n  stages: [retrieve]\n"},
        ["corpus", "waits"]),
    "named instances": (
        {"onetrace-plan.yaml": PLAN.replace('    rederivable: "true"\n  - name: answer',
                                            '    rederivable: "true"\n    instances: [en, fr]\n  - name: answer')},
        ["instances", "waits"]),
    "a lambda stage": ({"pipeline/llm.py": "answer = lambda passages: {'answer': passages[0]['text']}\n"}, ["lambda"]),
    "a generator stage": ({"pipeline/llm.py": LLM.replace('    return {"answer"', '    yield {"answer"')}, ["generator"]),
    "an async generator stage": ({"pipeline/llm.py": LLM.replace("def answer", "async def answer").replace(
        '    return {"answer"', '    yield {"answer"')}, ["generator"]),
    "a stage that calls another stage in its body": ({"pipeline/llm.py": "from pipeline.retrieval import retrieve\n\n\n"
                                                      + LLM.replace("passages[0]", "(passages or retrieve())[0]")},
                                                     ["'answer'", "'retrieve'", "nested"]),
    "a stage the entry never calls": ({"pipeline/main.py": MAIN.replace(
        "    return answer(passages)\n", "    return passages\n")}, ["'answer'", "never called"]),
    "a stage name shadowed in the entry": ({"pipeline/main.py": MAIN.replace(
        '    """Retrieve, then answer."""\n', '    """Retrieve, then answer."""\n    answer = print\n')},
        ["'answer'", "shadowed"]),
    "code already instrumented in wrapper style": ({"pipeline/main.py": MAIN.replace(
        "from pipeline.llm import answer\n",
        "from pipeline.llm import answer\n# onetrace-ci instrument: generated from the plan (sha256:0123456789abcdef).\n")},
        ["wrapper style"]),
    "a Recorder already created in the entry": ({"pipeline/main.py": MAIN.replace(
        "from pipeline.llm import answer\n", "from onetrace.emit import Recorder\nfrom pipeline.llm import answer\n").replace(
        "    passages = retrieve()\n", "    Recorder('runs/x')\n    passages = retrieve()\n")}, ["already creates a Recorder"]),
    "an unresolvable function": ({"onetrace-plan.yaml": PLAN.replace("pipeline.llm:answer", "pipeline.llm:respond")},
                                 ["respond"]),
    "a function already decorated as another stage": (
        {"pipeline/llm.py": 'import onetrace as ot\n\n\n@ot.stage("reply", rederivable=False)\n' + LLM},
        ["'reply'", "already"]),
    "calls of one stage that may overlap": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n",
        "    import concurrent.futures\n    with concurrent.futures.ThreadPoolExecutor() as pool:\n"
        "        pool.submit(retrieve)\n        pool.submit(retrieve)\n    passages = retrieve()\n")},
        ["'retrieve'", "pipeline/main.py:10", "pipeline/main.py:11", "pipeline/main.py:12", "overlap", "waits"]),
    "a stage mapped over a pool": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n",
        "    import concurrent.futures\n    with concurrent.futures.ThreadPoolExecutor() as pool:\n"
        "        passages = list(pool.map(retrieve, []))\n"),
        "onetrace-plan.yaml": PLAN.replace('    rederivable: "false"\n', '    trust: operator-authored\n    rederivable: "false"\n')},
        ["'retrieve'", "pipeline/main.py:10", "(map)", "overlap", "waits"]),
    "a decorated function the plan does not name": (
        {"pipeline/llm.py": 'import onetrace as ot\n\n\n@ot.stage("summary", rederivable=False)\n'
                            'def summarise(text):\n    return text\n\n\n' + LLM},
        ["'summary'", "summarise", "already", "the plan names no stage for it"]),
    "a stage called twice without repeats": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n", "    passages = retrieve()\n    passages = retrieve()\n")}, ["repeats: true"]),
    "a stage given a value no stage made, without trust": ({"pipeline/main.py": MAIN.replace(
        "    return answer(passages)\n", "    return answer(passages + [])\n")}, ["stages[1].trust", "answer"]),
    "the name ot taken in a stage module": ({"pipeline/llm.py": "ot = 1\n\n\n" + LLM}, ["'ot'", "reserves"]),
    "a stage with parameters handed to a pool, without trust": ({"pipeline/main.py": MAIN.replace(
        "    return answer(passages)\n",
        "    import concurrent.futures\n    with concurrent.futures.ThreadPoolExecutor() as pool:\n"
        "        return pool.submit(answer, passages).result()\n")}, ["stages[1].trust", "handed over"]),
    "a stage's output changed before the next stage, without trust": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n", "    passages = retrieve()\n    passages = passages[:1]\n")},
        ["stages[1].trust", "'passages'"]),
    "an entry with parameters, each one listed": (
        {"pipeline/main.py": MAIN.replace("def run():", "def run(request):").replace(
            "    passages = retrieve()\n", "    passages = retrieve()\n    print(request)\n"),
         "onetrace-plan.yaml": PLAN.replace("stages:\n", "stages:\n  - name: intake\n    memory_inputs: [request]\n"
                                           "    trust: externally-sourced\n    rederivable: \"true\"\n")},
        ["takes parameters", "rederivable=", "waits"]),
}

#: Two run types: an ingest, listed first so it runs first, and the query. The ingest's stage
#: function sits in a module the query's patch also touches.
TWO_ENTRIES = '''\
approved_by: alice
entries:
  - entry: pipeline.ingest:run
    run_dir: runs/ingest-{run_id}
    stages:
      - name: index
        function: pipeline.retrieval:build
        instrument: {name: word-index, package: onetrace-verify, kind: indexer}
        files: [data/corpus.json]
        trust: operator-authored
        rederivable: "true"
  - entry: pipeline.main:run
    run_dir: runs/query-{run_id}
    stages:
      - name: retrieve
        function: pipeline.retrieval:retrieve
        instrument: {name: word-overlap, package: onetrace-verify, kind: retriever}
        files: [data/question.txt, data/corpus.json]
        trust: operator-authored
        rederivable: "true"
      - name: answer
        function: pipeline.llm:answer
        instrument: {name: extractive, package: onetrace, kind: answerer}
        rederivable: "false"
        rederivable_note: "a stand-in for a hosted model"
approved_boundaries: []
ci:
  install: pip install --require-hashes -r requirements.lock
  run:
    "pipeline.ingest:run": python -c "from pipeline.ingest import run; print(run())"
    "pipeline.main:run": python -c "from pipeline.main import run; print(run())"
  baseline:
    "pipeline.ingest:run": runs/ingest-baseline
    "pipeline.main:run": runs/query-baseline
'''
INGEST_MODULE = 'from pipeline.retrieval import build\n\n\ndef run():\n    """Build the index."""\n    return build()\n'
BUILD = '\n\ndef build():\n    return json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))\n'


def _two_entries(root, plan=TWO_ENTRIES, ingest=INGEST_MODULE, **others):
    from tests.decorator_fixtures import RETRIEVAL
    return make_repo(root, {"pipeline/ingest.py": ingest, "pipeline/retrieval.py": RETRIEVAL + BUILD,
                            "onetrace-plan.yaml": plan, **others})


def test_an_entry_that_calls_another_entry_s_stage_is_refused(tmp_path, examined):
    """`build` is the ingest's stage `index`; the query's @ot.run does not declare it, so the SDK
    would refuse it at the call."""
    main_py = MAIN.replace("from pipeline.llm import answer\n",
                           "from pipeline.llm import answer\nfrom pipeline.retrieval import build\n").replace(
        "    passages = retrieve()\n", "    build()\n    passages = retrieve()\n")
    repo = _two_entries(tmp_path / "repo", **{"pipeline/main.py": main_py})
    examined(1, "a query that calls the ingest's stage")
    with pytest.raises(Refused) as caught:
        _patch(repo)
    message = str(caught.value)
    assert "'index'" in message and "a stage of another entry" in message, message
    assert "other-entry-stage" in [explain(p)[0] for p in caught.value.problems]


def test_an_entry_that_reaches_another_entry_s_stage_through_a_helper_gets_that_fix(tmp_path, examined):
    """Calling it in the entry's own body is refused too, so the fix is the other entry's: plan
    it here as well, alike, or leave it to its own entry."""
    main_py = MAIN.replace("from pipeline.llm import answer\n",
                           "from pipeline.llm import answer\nfrom pipeline.util import prep\n").replace(
        "    passages = retrieve()\n", "    prep()\n    passages = retrieve()\n")
    util = "from pipeline.retrieval import build\n\n\ndef prep():\n    return build()\n"
    repo = _two_entries(tmp_path / "repo", **{"pipeline/main.py": main_py, "pipeline/util.py": util})
    examined(1, "a query that reaches the ingest's stage through a helper")
    with pytest.raises(Refused) as caught:
        _patch(repo)
    message = str(caught.value)
    assert "'index'" in message and "pipeline.util:prep" in message, message
    assert [explain(p)[0] for p in caught.value.problems] == ["other-entry-stage"], caught.value.problems


def test_a_stage_that_calls_another_entry_s_stage_is_refused(tmp_path, examined):
    llm = "from pipeline.retrieval import build\n\n\n" + LLM.replace("passages[0]", "(passages or build())[0]")
    repo = _two_entries(tmp_path / "repo", **{"pipeline/llm.py": llm})
    examined(1, "a query stage that calls the ingest's stage")
    with pytest.raises(Refused) as caught:
        _patch(repo)
    message = str(caught.value)
    assert "'answer'" in message and "'index'" in message and "nested" in message, message


def test_each_entry_gets_its_own_ot_run_and_the_second_patch_is_empty(tmp_path, examined):
    repo = _two_entries(tmp_path / "repo")
    result = _patch(repo)
    _apply(repo, result.patch, tmp_path)
    ingest, main_py, retrieval = (_read(repo, f"pipeline/{m}.py") for m in ("ingest", "main", "retrieval"))
    examined(3, "the modules the two entries touch")
    assert '@ot.run(\n    stages=["index"],\n    run_dir="runs/ingest-{run_id}",\n)\ndef run():' in ingest
    assert ('@ot.run(\n    stages=["retrieve", "answer"],\n    run_dir="runs/query-{run_id}",\n'
            '    declared_edges=[{"from": "retrieve", "to": "answer"}],\n)\ndef run():') in main_py
    assert '@ot.stage(\n    "index",\n' in retrieval and '@ot.stage(\n    "retrieve",\n' in retrieval
    assert _read(repo, ".github/workflows/onetrace.yml") == workflow_text(
        load_instrument_plan(repo / "onetrace-plan.yaml"), "onetrace-plan.yaml")
    #: The ingest's @ot.stage sits in a module the query touches; it is another entry's, not a
    #: stage the plan names nowhere.
    again = _patch(repo)
    assert again.patch == "" and again.files == []


def test_a_function_two_entries_plan_differently_is_refused(tmp_path, examined):
    plan = TWO_ENTRIES.replace("        function: pipeline.retrieval:build\n",
                               "        function: pipeline.retrieval:retrieve\n")
    repo = _two_entries(tmp_path / "repo", plan, INGEST_MODULE.replace("build", "retrieve"))
    examined(1, "a plan whose two entries plan one function differently")
    with pytest.raises(Refused) as caught:
        _patch(repo)
    message = str(caught.value)
    assert "entries[1].stages[0]" in message and "planned differently" in message, message
    assert explain(caught.value.problems[0])[0] == "invalid-value"


def test_a_problem_in_the_second_entry_names_its_field(tmp_path, examined):
    plan = TWO_ENTRIES.replace("pipeline.llm:answer", "pipeline.llm:respond")
    repo = _two_entries(tmp_path / "repo", plan)
    examined(1, "a plan whose second entry names a missing function")
    with pytest.raises(Refused) as caught:
        _patch(repo)
    assert "plan field entries[1].stages[1].function" in str(caught.value), str(caught.value)

ANSWER_TRUSTED = PLAN.replace('    rederivable: "false"\n', '    trust: operator-authored\n    rederivable: "false"\n')
RETRIEVE_REPEATS = ANSWER_TRUSTED.replace('    rederivable: "true"\n  - name: answer',
                                          '    rederivable: "true"\n    repeats: true\n  - name: answer')
POOL = "    import concurrent.futures\n    with concurrent.futures.ThreadPoolExecutor() as pool:\n"
AGAIN = "from pipeline.retrieval import retrieve\n\n\nclass Again:\n    def go(self):\n        return retrieve()\n"
TIDY = 'import onetrace as ot\n\n\n@ot.stage("tidy", rederivable=True)\ndef tidy(x):\n    return x\n'
ANSWER_BLOCK = ('  - name: answer\n    function: pipeline.llm:answer\n'
                '    instrument: {name: extractive, package: onetrace, kind: answerer}\n'
                '    rederivable: "false"\n    rederivable_note: "a stand-in for a hosted model"\n')

#: Where a stage's calls cannot be seen from the entry function, or its argument may not be a
#: stage's return value unchanged, or another stage runs inside it.
REFUSED.update({
    "a stage called in a nested function that a pool maps": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n",
        "    def work(_):\n        return retrieve()\n" + POOL + "        passages = list(pool.map(work, [1, 2]))[0]\n"),
        "onetrace-plan.yaml": RETRIEVE_REPEATS}, ["'retrieve'", "nested function or lambda", "dynamic dispatch"]),
    "a stage called in a lambda that a pool maps": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n",
        "    work = lambda _: retrieve()\n" + POOL + "        passages = list(pool.map(work, [1, 2]))[0]\n"),
        "onetrace-plan.yaml": RETRIEVE_REPEATS}, ["'retrieve'", "nested function or lambda", "dynamic dispatch"]),
    "a stage bound to another name": ({"pipeline/main.py": MAIN.replace(
        "    return answer(passages)\n", "    work = answer\n" + POOL + "        return list(pool.map(work, [passages]))\n"),
        "onetrace-plan.yaml": ANSWER_TRUSTED}, ["'answer'", "used as a value", "dynamic dispatch"]),
    "a stage handed to a helper": ({"pipeline/main.py": MAIN.replace(
        "    return answer(passages)\n", "    return twice(answer, passages)\n") + (
        "\n\ndef twice(f, x):\n    return [f(x), f(x)]\n"),
        "onetrace-plan.yaml": ANSWER_TRUSTED}, ["'answer'", "used as a value", "dynamic dispatch"]),
    "a stage handed to the builtin map": ({"pipeline/main.py": MAIN.replace(
        "    return answer(passages)\n", "    return list(map(answer, [passages]))\n"),
        "onetrace-plan.yaml": ANSWER_TRUSTED}, ["'answer'", "used as a value"]),
    "a stage handed to a stage": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n    return answer(passages)\n", "    return answer(retrieve)\n")},
        ["'retrieve'", "used as a value"]),
    "an async stage's calls gathered from a list": (
        {"pipeline/llm.py": LLM.replace("def answer", "async def answer"),
         "pipeline/main.py": MAIN.replace("from pipeline.llm import answer\n", "import asyncio\n\nfrom pipeline.llm import answer\n")
         .replace("    return answer(passages)\n",
                  "    calls = [answer(passages), answer(passages)]\n    return asyncio.run(asyncio.gather(*calls))\n"),
         "onetrace-plan.yaml": ANSWER_TRUSTED.replace('    rederivable: "false"\n', '    rederivable: "false"\n    repeats: true\n')},
        ["'answer'", "is async", "not awaited"]),
    "a stage started twice in a task group": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n",
        "    tg = None\n    tg.start_soon(retrieve)\n    tg.start_soon(retrieve)\n    passages = retrieve()\n")},
        ["'retrieve'", "(start_soon)", "overlap", "waits"]),
    "the entry reaches a stage through a helper": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n", "    passages = fetch()\n") + "\n\ndef fetch():\n    return retrieve()\n",
        "onetrace-plan.yaml": ANSWER_TRUSTED}, ["reaches stage 'retrieve'", "pipeline.main:fetch", "dynamic dispatch"]),
    "a stage that calls another stage through a helper": (
        {"pipeline/helpers.py": "from pipeline.retrieval import retrieve\n\n\ndef more():\n    return retrieve()\n",
         "pipeline/llm.py": "from pipeline.helpers import more\n\n\n" + LLM.replace("passages[0]", "(passages or more())[0]")},
        ["'answer'", "'retrieve'", "through pipeline.helpers:more", "nested"]),
    "a stage's output changed in place, without trust": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n", "    passages = retrieve()\n    passages.reverse()\n")},
        ["stages[1].trust", "'passages'", "unchanged"]),
    "a stage's output changed by index, without trust": ({"pipeline/main.py": MAIN.replace(
        "    passages = retrieve()\n", "    passages = retrieve()\n    passages[0] = passages[0]\n")},
        ["stages[1].trust", "'passages'", "unchanged"]),
    "a stage called without a defaulted parameter, without trust": (
        {"pipeline/llm.py": LLM.replace("def answer(passages):", 'def answer(passages, style="short"):')},
        ["stages[1].trust", "'style'", "default"]),
    "an @ot.stage left on a function the entry calls": (
        {"onetrace-plan.yaml": PLAN.replace(ANSWER_BLOCK, ""),
         "pipeline/llm.py": 'import onetrace as ot\n\n\n@ot.stage("answer", rederivable=False)\n' + LLM},
        ["'answer'", "the plan names no stage for it"]),
    "an async stage's calls wrapped in wait_for, then gathered": (
        {"pipeline/llm.py": LLM.replace("def answer", "async def answer"),
         "pipeline/main.py": MAIN.replace("from pipeline.llm import answer\n", "import asyncio\n\nfrom pipeline.llm import answer\n")
         .replace("def run():", "async def run():")
         .replace("    return answer(passages)\n",
                  "    a = asyncio.wait_for(answer(passages), 5)\n    b = asyncio.wait_for(answer(passages), 5)\n"
                  "    return await asyncio.gather(a, b)\n"),
         "onetrace-plan.yaml": PLAN.replace('    rederivable: "false"\n', '    rederivable: "false"\n    repeats: true\n')},
        ["'answer'", "is async", "not awaited"]),
    "the entry reaches a stage through an inherited method": (
        {"pipeline/again.py": AGAIN.replace("class Again:", "class Base:") + "\n\nclass Again(Base):\n    pass\n",
         "pipeline/main.py": MAIN.replace("from pipeline.llm import answer\n",
                                          "from pipeline.again import Again\nfrom pipeline.llm import answer\n")
         .replace("    passages = retrieve()\n", "    passages = retrieve()\n    Again().go()\n")},
        ["reaches stage 'retrieve'", "pipeline.again:Base.go", "dynamic dispatch"]),
    "a stage handed once to a thread pool": (
        {"pipeline/main.py": SUBMIT_ONCE, "onetrace-plan.yaml": ANSWER_TRUSTED},
        ["'retrieve'", "(submit)", "a stage run in another thread waits for the SDK to show it is recorded"]),
    "a stage run in a thread of its own": (
        {"pipeline/main.py": MAIN.replace(
            "    passages = retrieve()\n",
            "    import threading\n    worker = threading.Thread(target=retrieve)\n    worker.start()\n"
            "    worker.join()\n    passages = []\n"), "onetrace-plan.yaml": ANSWER_TRUSTED},
        ["'retrieve'", "(Thread)", "a stage run in another thread waits for the SDK to show it is recorded"]),
    "a stage spawned as a greenlet": (
        {"pipeline/main.py": MAIN.replace(
            "    passages = retrieve()\n", "    worker = gevent.spawn(retrieve)\n    worker.join()\n    passages = []\n"),
         "onetrace-plan.yaml": ANSWER_TRUSTED},
        ["'retrieve'", "(spawn)", "a stage run in another thread waits for the SDK to show it is recorded"]),
    "an async stage run on another loop's thread": (
        {"pipeline/llm.py": LLM.replace("def answer", "async def answer"),
         "pipeline/main.py": MAIN.replace("from pipeline.llm import answer\n", "import asyncio\n\nfrom pipeline.llm import answer\n")
         .replace("    return answer(passages)\n",
                  "    return asyncio.run_coroutine_threadsafe(answer(passages), LOOP).result()\n")},
        ["'answer'", "(run_coroutine_threadsafe)", "a stage run in another thread waits for the SDK to show it is recorded"]),
    "a stage handed once to a thread pool, without trust for what comes back": (
        {"pipeline/main.py": SUBMIT_ONCE}, ["another thread", "stages[1].trust", "'answer'"]),
    "a constants name that is not a parameter": (
        {"onetrace-plan.yaml": PLAN.replace('    rederivable: "false"\n', '    rederivable: "false"\n    constants: [pasages]\n')},
        ["'pasages'", "constants"]),
    "the entry reaches a stage through a class": (
        {"pipeline/again.py": AGAIN,
         "pipeline/main.py": MAIN.replace("from pipeline.llm import answer\n",
                                          "from pipeline.again import Again\nfrom pipeline.llm import answer\n")
         .replace("    passages = retrieve()\n", "    passages = retrieve()\n    Again().go()\n")},
        ["reaches stage 'retrieve'", "pipeline.again:Again.go", "dynamic dispatch"]),
    "a stage that calls another stage through a class": (
        {"pipeline/again.py": AGAIN,
         "pipeline/llm.py": "from pipeline.again import Again\n\n\n" + LLM.replace("passages[0]", "(passages or Again().go())[0]")},
        ["'answer'", "'retrieve'", "Again.go", "nested"]),
    "an @ot.stage left in a module a stage reaches": (
        {"pipeline/fmt.py": TIDY, "pipeline/llm.py": "from pipeline.fmt import tidy\n\n\n" + LLM.replace(
            'passages[0]["text"]', 'tidy(passages)[0]["text"]')},
        ["'tidy'", "the plan names no stage for it"]),
    "an @ot.stage left in a module the entry's helper reaches": (
        {"pipeline/fmt.py": TIDY,
         "pipeline/util.py": "from pipeline.fmt import tidy\n\n\ndef prep(x):\n    return tidy(x)\n",
         "pipeline/main.py": MAIN.replace("from pipeline.llm import answer\n",
                                          "from pipeline.llm import answer\nfrom pipeline.util import prep\n")
         .replace("    passages = retrieve()\n", '    prep("x")\n    passages = retrieve()\n')},
        ["'tidy'", "the plan names no stage for it"]),
})


#: The error code a refusal must map to, where decorator style added the code or its pattern.
CODE = {"a corpus link": "decorator-waits", "named instances": "decorator-waits",
        "an entry with parameters, each one listed": "decorator-waits",
        "calls of one stage that may overlap": "decorator-waits", "a stage mapped over a pool": "decorator-waits",
        "a stage called twice without repeats": "repeats",
        "a function already decorated as another stage": "already-decorated",
        "a decorated function the plan does not name": "already-decorated",
        "a function stage named intake": "reserved-name", "the name ot taken in a stage module": "reserved-name",
        "code already instrumented in wrapper style": "instrumented-other-plan",
        "a stage name shadowed in the entry": "shadowed",
        "a stage given a value no stage made, without trust": "missing-field",
        "a stage called in a nested function that a pool maps": "dynamic-dispatch",
        "a stage called in a lambda that a pool maps": "dynamic-dispatch",
        "a stage bound to another name": "dynamic-dispatch", "a stage handed to a helper": "dynamic-dispatch",
        "a stage handed to the builtin map": "dynamic-dispatch", "a stage handed to a stage": "dynamic-dispatch",
        "an async stage's calls gathered from a list": "dynamic-dispatch",
        "the entry reaches a stage through a helper": "dynamic-dispatch",
        "a stage started twice in a task group": "decorator-waits",
        "a stage that calls another stage through a helper": "nested-stage",
        "a stage's output changed in place, without trust": "missing-field",
        "a stage's output changed by index, without trust": "missing-field",
        "a stage called without a defaulted parameter, without trust": "missing-field",
        "an @ot.stage left on a function the entry calls": "already-decorated",
        "an async stage's calls wrapped in wait_for, then gathered": "dynamic-dispatch",
        "the entry reaches a stage through an inherited method": "dynamic-dispatch",
        "a constants name that is not a parameter": "invalid-value",
        "a stage handed once to a thread pool": "decorator-waits",
        "a stage run in a thread of its own": "decorator-waits",
        "a stage spawned as a greenlet": "decorator-waits",
        "an async stage run on another loop's thread": "decorator-waits",
        "the entry reaches a stage through a class": "dynamic-dispatch",
        "a stage that calls another stage through a class": "nested-stage",
        "an @ot.stage left in a module a stage reaches": "already-decorated",
        "an @ot.stage left in a module the entry's helper reaches": "already-decorated"}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_each_decorator_refusal_names_its_target(case, tmp_path, examined):
    overrides, words = REFUSED[case]
    repo = make_repo(tmp_path / "repo", overrides)
    examined(1, f"refused: {case}")
    with pytest.raises(Refused) as caught:
        _patch(repo)
    message = str(caught.value)
    for word in words:
        assert word in message, message
    codes = [explain(p) for p in caught.value.problems]
    assert all(codes), caught.value.problems
    if case in CODE:
        assert CODE[case] in [c for c, _ in codes], (codes, caught.value.problems)


PARAMETERS_MAIN = MAIN.replace("def run():", "def run(request, mode):").replace(
    "    passages = retrieve()\n", "    passages = retrieve()\n    print(request, mode)\n")
INTAKE = '''  - name: intake
    memory_inputs: [request, mode]
    trust: externally-sourced
    rederivable: "true"
    rederivable_note: "the request as the caller sent it"
'''


def test_every_parameter_of_the_entry_is_listed_or_the_plan_is_refused(tmp_path, examined):
    """The SDK records every parameter of the entry, so the plan lists each."""
    plan = _with("stages:\n", "stages:\n" + INTAKE.replace("[request, mode]", "[request]"))
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": PARAMETERS_MAIN, "onetrace-plan.yaml": plan})
    examined(1, "a plan that leaves a parameter out")
    with pytest.raises(Refused) as caught:
        _patch(repo)
    assert "'mode'" in str(caught.value) and "memory_inputs" in str(caught.value)
    assert "memory-input" in [explain(p)[0] for p in caught.value.problems]


@pytest.mark.xfail(strict=True, raises=Refused, reason="an entry with parameters records its intake through "
                                       "@ot.run(trust=, rederivable=, note=), which onetrace 0.2.0's candidate "
                                       "does not carry yet; generated once it does")
def test_an_entry_with_parameters_gets_its_intake_on_ot_run(tmp_path, examined):
    plan = _with("stages:\n", "stages:\n" + INTAKE)
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": PARAMETERS_MAIN, "onetrace-plan.yaml": plan})
    _apply(repo, _patch(repo).patch, tmp_path)
    examined(1, "the entry module")
    assert '''@ot.run(
    stages=["intake", "retrieve", "answer"],
    run_dir="runs/{run_id}",
    declared_edges=[{"from": "retrieve", "to": "answer"}],
    trust="externally-sourced",
    rederivable=True,
    note="the request as the caller sent it",
)
def run(request, mode):''' in _read(repo, "pipeline/main.py")


@pytest.mark.xfail(strict=True, raises=Refused, reason="a corpus link is written as ot.corpus_from(<ingest run>, "
                                                       "stages=[...], index_stage=...), which onetrace 0.2.0's "
                                                       "candidate does not carry yet; generated once it does")
def test_a_corpus_link_names_its_stages_on_ot_run(tmp_path, examined):
    plan = PLAN + "corpus:\n  from: runs/ingest-latest\n  stages: [retrieve]\n  index_stage: split\n"
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": plan})
    _apply(repo, _patch(repo).patch, tmp_path)
    examined(1, "the entry module")
    assert '''@ot.run(
    stages=["retrieve", "answer"],
    run_dir="runs/{run_id}",
    declared_edges=[{"from": "retrieve", "to": "answer"}],
    corpus=ot.corpus_from("runs/ingest-latest", stages=["retrieve"], index_stage="split"),
)
def run():''' in _read(repo, "pipeline/main.py")


@pytest.mark.xfail(strict=True, raises=Refused, reason="a named instance is called as stage.instance(name)(...), which "
                                       "onetrace 0.2.0's candidate does not carry yet; generated once it does")
def test_named_instances_are_called_through_instance(tmp_path, examined):
    main_py = MAIN.replace("    passages = retrieve()\n",
                           "    import concurrent.futures\n    with concurrent.futures.ThreadPoolExecutor() as pool:\n"
                           "        en = pool.submit(retrieve)\n        fr = pool.submit(retrieve)\n"
                           "        passages = en.result() + fr.result()\n")
    plan = _with('    rederivable: "true"\n  - name: answer', '    rederivable: "true"\n    instances: [en, fr]\n  - name: answer')
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": main_py, "onetrace-plan.yaml": plan})
    _apply(repo, _patch(repo).patch, tmp_path)
    examined(1, "the entry module")
    text = _read(repo, "pipeline/main.py")
    assert 'pool.submit(retrieve.instance("en"))' in text and 'pool.submit(retrieve.instance("fr"))' in text
