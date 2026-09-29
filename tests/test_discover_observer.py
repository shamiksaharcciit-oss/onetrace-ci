"""The runtime observer: runs the given command unchanged and records what the pipeline did, as
fingerprints, never values."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from onetrace_ci.discover import observe
from tests.discover_fixtures import MARKER, make_discover_repo


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    root = tmp_path_factory.mktemp("discover")
    repo, stubs = make_discover_repo(root)
    env = dict(os.environ, LLM_API_KEY=MARKER, PYTHONPATH=str(stubs))
    events_path = root / "events.jsonl"
    result = observe([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_pipeline.py"],
                     repo=repo, entry="pipeline.main:run", events_path=events_path, env=env)
    return result, events_path


def _of(result, kind):
    return [e for e in result.events if e["kind"] == kind]


def test_the_command_runs_unchanged_and_its_exit_code_is_kept(observed, examined):
    result, _ = observed
    examined(len(result.events), "events recorded")
    assert result.returncode == 0, result.output


def test_a_file_read_is_attributed_to_the_stage_function_that_made_it(observed, examined):
    result, _ = observed
    reads = [e for e in _of(result, "file-read") if e["name"] == "data/corpus.json"]
    examined(len(reads), "reads of the corpus")
    assert reads and all(e["stage"] == "pipeline.retrieval:retrieve" for e in reads)
    assert reads[0]["site"] == "pipeline/retrieval.py:9"
    assert reads[0]["content"].startswith("fp:")


def test_an_import_time_read_belongs_to_no_stage(observed, examined):
    result, _ = observed
    reads = [e for e in _of(result, "file-read") if e["name"] == "config/settings.txt"]
    examined(len(reads), "reads of the settings file")
    assert reads and reads[0]["stage"] is None and not reads[0]["in_run"]


def test_environment_reads_are_names_only_and_attributed(observed, examined):
    result, _ = observed
    by_name = {e["name"]: e for e in _of(result, "env")}
    examined(len(by_name), "environment names read by the pipeline")
    assert by_name["LLM_API_KEY"]["stage"] == "pipeline.llm:answer"
    assert by_name["RETRIEVE_DEBUG"]["stage"] == "pipeline.retrieval:retrieve"
    assert by_name["PIPELINE_MODE"]["stage"] is None and by_name["PIPELINE_MODE"]["in_run"]
    assert all(set(e) >= {"name", "site"} and "value" not in e for e in _of(result, "env"))


def test_an_http_call_is_recorded_by_library_method_and_host(observed, examined):
    result, _ = observed
    [call] = _of(result, "http")
    examined(1, "http calls")
    assert (call["library"], call["method"], call["host"], call["stage"]) == (
        "requests", "POST", "llm.example.test", "pipeline.llm:answer")
    assert call["request"].startswith("fp:") and call["outcome"] == "200"


def test_stage_calls_are_recorded_in_order_with_their_data_flow(observed, examined):
    result, _ = observed
    calls = _of(result, "call")
    examined(len(calls), "calls the entry function made")
    assert [c["function"] for c in calls] == ["pipeline.retrieval:retrieve", "pipeline.llm:answer"]
    retrieve, answer = calls
    assert retrieve["site"] == "pipeline/main.py:13" and answer["site"] == "pipeline/main.py:14"
    #: Arguments are kept in parameter order: that order is the order a stage's inputs are read.
    assert [name for name, _ in answer["args"]] == ["request", "passages", "mode"]
    args, first = dict(answer["args"]), dict(retrieve["args"])
    assert args["passages"] == retrieve["returned"]      # the output of retrieve, consumed
    assert args["request"] == first["request"]


def test_an_exception_is_recorded_by_type_never_its_message(observed, examined):
    result, _ = observed
    raised = [e for e in _of(result, "exception") if e["stage"] == "pipeline.llm:answer"]
    examined(len(raised), "exceptions inside answer")
    assert raised[0]["type"] == "ValueError"
    assert set(raised[0]) == {"kind", "type", "site", "stage", "in_run"}


def test_executed_lines_are_recorded(observed, examined):
    result, _ = observed
    lines = {(e["file"], n) for e in _of(result, "lines") for n in e["lines"]}
    examined(len(lines), "executed lines")
    assert ("pipeline/retrieval.py", 9) in lines
    assert ("pipeline/retrieval.py", 11) not in lines            # the branch never taken


def test_the_planted_secret_is_nowhere_in_the_events_file(observed, examined):
    _, events_path = observed
    data = events_path.read_bytes()
    examined(len(data), "bytes of the events file")
    assert MARKER.encode() not in data


def test_fingerprints_are_keyed_per_run_so_they_join_only_within_one(tmp_path, examined):
    """The same file, fingerprinted by two discovery runs, gets two unrelated fingerprints: a
    fingerprint cannot be looked up later to recover what it hides."""
    repo, stubs = make_discover_repo(tmp_path)
    env = dict(os.environ, LLM_API_KEY=MARKER, PYTHONPATH=str(stubs))
    cmd = [sys.executable, "-c", "from pipeline.main import run; run('warranty')"]
    fps = []
    for i in range(2):
        r = observe(cmd, repo=repo, entry="pipeline.main:run", events_path=tmp_path / f"e{i}.jsonl", env=env)
        fps.append({e["content"] for e in r.events if e["kind"] == "file-read" and e["name"] == "data/corpus.json"})
    examined(2, "discovery runs")
    assert fps[0] and fps[1] and fps[0].isdisjoint(fps[1])


def test_the_observer_is_never_imported_by_the_package(examined):
    """Importing the observer installs its hooks in the importing process. The package only ever
    copies it as a file; nothing may import it."""
    import re
    from pathlib import Path

    import onetrace_ci
    sources = sorted(Path(onetrace_ci.__file__).parent.glob("*.py"))
    examined(len(sources), "package modules checked")
    for src in sources:
        if src.name == "_observer.py":
            continue
        text = src.read_text(encoding="utf-8")
        assert not re.search(r"import\s+_observer|from\s+onetrace_ci\s+import\s+.*_observer|"
                             r"onetrace_ci\._observer", text), f"{src.name} imports the observer"


def test_file_names_keep_their_case(tmp_path, examined):
    """On a case-insensitive filesystem the observer still records the name as it is spelled:
    the draft and the generated code must name the file that exists everywhere."""
    from tests.instrument_fixtures import make_repo
    repo = make_repo(tmp_path / "repo", {
        "data/Corpus.JSON": "[]",
        "pipeline/retrieval.py": "from pathlib import Path\n\n\ndef retrieve(request):\n"
                                 "    return (Path(__file__).resolve().parents[1] / 'data' / 'Corpus.JSON').read_text()\n"})
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, timeout=60)  # named once tracked
    r = observe([sys.executable, "-c", "from pipeline.main import run\ntry:\n    run('q')\nexcept Exception:\n    pass"],
                repo=repo, entry="pipeline.main:run", events_path=tmp_path / "e.jsonl")
    names = [e["name"] for e in r.events if e["kind"] == "file-read"]
    examined(len(names), "file reads")
    assert "data/Corpus.JSON" in names
