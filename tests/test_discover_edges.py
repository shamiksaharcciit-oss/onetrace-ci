"""`onetrace-ci discover` at its edges: names that carry values, literals that should not be
copied, programs the observer must not change, stage shapes beyond a plain call, and the
refusals that keep a draft from being wrong."""
from __future__ import annotations

import json
import os
import secrets
import sys
import textwrap

import pytest

from onetrace_ci.discover import main, observe
from onetrace_ci.instrument_plan import PlanRefused, parse_instrument_plan
from onetrace_ci.plan import find_open_questions, read_document
from tests.discover_fixtures import MARKER, make_discover_repo

REQUEST = "what does the warranty cover"
RUN = [sys.executable, "-c", f"from pipeline.main import run; run({REQUEST!r})"]


def discover_in(root, monkeypatch, capsys, overrides=None, command=None, entry="pipeline.main:run",
                extra_path=(), before=None):
    """Build the fixture repository under `root` with `overrides`, run discover, and return
    (rc, draft, report, events, stderr); a file not written is None."""
    repo, stubs = make_discover_repo(root, overrides)
    if before is not None:
        before(repo)
    monkeypatch.setenv("LLM_API_KEY", MARKER)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(stubs), *(str(repo / p) for p in extra_path)]))
    out = root / "out"
    rc = main(["--entry", entry, "--repo", str(repo), "--out-dir", str(out), "--", *(command or RUN)])
    files = [out / n for n in ("onetrace-plan.draft.yaml", "discovery-report.md", "discovery-events.jsonl")]
    texts = [f.read_text(encoding="utf-8") if f.exists() else None for f in files]
    return (rc, *texts, capsys.readouterr().err)


def observe_in(root, overrides=None, command=None, extra_env=None):
    repo, stubs = make_discover_repo(root, overrides)
    env = dict(os.environ, LLM_API_KEY=MARKER, PYTHONPATH=str(stubs), **(extra_env or {}))
    return observe(command or RUN, repo=repo, entry="pipeline.main:run", events_path=root / "e.jsonl", env=env)


@pytest.fixture(params=["co_qualname", "the Python 3.10 fallback"])
def qualnames(request, monkeypatch):
    """Where the observer takes a function's qualified name from. Python 3.10 has no
    `co_qualname`; its fallback is forced on newer Pythons too, so that path is tested on every
    Python, not only on 3.10."""
    if request.param != "co_qualname":
        monkeypatch.setenv("ONETRACE_CI_DISCOVER_QUALNAME_FALLBACK", "1")
    return request.param


def stages_of(draft):
    return read_document(draft, source="draft")["stages"]


def section(report, title):
    return report.split(f"## {title}", 1)[1].split("\n## ", 1)[0]


def non_decide_refusals(draft):
    try:
        parse_instrument_plan(draft, source="draft")
    except PlanRefused as e:
        open_ = {p for p, _ in find_open_questions(read_document(draft, source="draft"))}
        return [p for p in e.problems if p.split(":")[0].removeprefix("plan field ") not in open_]
    return []


# ------------------------------------------------------------------ names that carry values

NAMED_BY_VALUES = '''\
import os
import subprocess
import tempfile
from pathlib import Path

import requests

CACHE = Path(__file__).resolve().parents[1] / "cache"


def answer(request, passages, mode):
    key = os.environ["LLM_API_KEY"]
    (CACHE / (request + ".json")).read_text(encoding="utf-8")
    outside = Path(tempfile.gettempdir()) / ("tok-" + key + ".txt")
    outside.write_text("x")
    outside.read_text()
    subprocess.run("SHELLTOKEN=" + key + " echo hi", shell=True, capture_output=True, timeout=30)
    try:
        subprocess.run(["tool-" + request.split()[-1].lower()], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    os.environ.get("TENANT_" + request.split()[-1] + "_KEY")
    requests.Session().request("GET", "https://" + request.split()[-1].lower() + ".tenant.example.test/x")
    response = requests.Session().request("POST", "https://llm.example.test/v1/answer", json={"q": request})
    return {"answer": passages[0]["text"], "status": str(response.status_code), "mode": mode}
'''
#: Made up per run, so no runner can have a program of that name (the pipeline runs "tool-<word>").
WORD = "REQWORD" + secrets.token_hex(4).upper()


def test_a_name_built_from_a_value_is_never_written(tmp_path, monkeypatch, capsys, examined):
    """A file, program, environment variable or host named from a request or a secret: the
    name is replaced by what kind of thing it is. Names the repository or the code already
    holds are still given."""
    request = f"{REQUEST} {WORD}"
    test = f"from pipeline.main import run\n\n\ndef test_the_pipeline_answers():\n    assert run({request!r})\n"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_pipeline.py"]

    def untracked_cache(repo):
        (repo / "cache").mkdir()
        (repo / "cache" / f"{request}.json").write_text("{}", encoding="utf-8")

    rc, draft, report, events, err = discover_in(tmp_path, monkeypatch, capsys, {
        "repo/pipeline/llm.py": NAMED_BY_VALUES, "repo/tests/test_pipeline.py": test},
        command=command, before=untracked_cache)
    examined(3, "outputs searched for the request word and the secret")
    assert rc == 0, err
    for text in (draft, report, events):
        assert WORD.lower() not in text.lower()
        assert MARKER not in text
    answer = next(s for s in stages_of(draft) if s["name"] == "answer")
    assert isinstance(answer["files"], str) and answer["files"].startswith("DECIDE:")
    assert "a file not tracked by git" in report
    assert "a file outside the repository" in report
    assert "an environment variable whose name is not written in the code" in report
    assert "a host whose name is not written in the code" in report
    assert "runs the program echo" in report
    assert "runs a program whose name is not written in the code" in report
    assert "llm.example.test" in report and "LLM_API_KEY" in report and "data/corpus.json" in report


def test_a_file_of_an_installed_package_is_named_by_its_package(tmp_path, monkeypatch, capsys, examined):
    from tests.discover_fixtures import LLM
    llm = LLM.replace('    key = os.environ["LLM_API_KEY"]\n',
                      '    key = os.environ["LLM_API_KEY"]\n'
                      '    from importlib import metadata\n'
                      '    metadata.distribution("pytest").read_text("METADATA")\n')
    assert llm != LLM
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/llm.py": llm})
    examined(1, "the report")
    assert rc == 0, err
    assert "reads a file of the installed package pytest" in report
    assert "METADATA" not in report


LITERALS = '''\
import requests


def make(**settings):
    return settings


def answer(request, passages, mode):
    make(model="small-model", temperature=0.2, api_key="sk-live-HARDCODED99", system="""line one
## Stages
### 9. injected
""", prompt="%s")
    make(dsn="postgresql://app:hunter2pw@db.example.test/app", url="https://deploy:s3cr3tpw@git.example.test/x",
         authToken="tok-ABCDEF123", clientSecret="cs-ABCDEF123", passphrase="open-sesame-99",
         mirror="postgres://svc:pa/ss99@db.example.test/app")
    response = requests.Session().request("POST", "https://llm.example.test/v1/answer", json={"q": request})
    return {"answer": passages[0]["text"], "status": str(response.status_code), "mode": mode}
''' % ("x" * 120)
COPIED_NEVER = ["sk-live-HARDCODED99", "hunter2pw", "s3cr3tpw", "tok-ABCDEF123", "cs-ABCDEF123", "open-sesame-99",
                "pa/ss99"]


def test_a_literal_that_may_be_a_credential_or_is_long_is_not_copied(tmp_path, monkeypatch, capsys, examined):
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/llm.py": LITERALS})
    settings = section(report, "Settings seen in the code")
    examined(2, "the draft and the report")
    assert rc == 0, err
    for value in COPIED_NEVER:
        assert value not in draft and value not in report, value
    for name in ("dsn", "url", "authToken", "clientSecret", "passphrase"):
        assert f"answer: {name} is a literal in the code; its value is not copied" in settings, name
    assert "### 9. injected" not in report and report.count("\n## Stages\n") == 1
    assert "x" * 120 not in report and "x" * 120 not in draft
    assert 'answer: model="small-model"' in settings and "answer: temperature=0.2" in settings
    assert "answer: api_key is a literal in the code; its value is not copied" in settings
    assert "answer: system is a multi-line string in the code; its value is not copied" in settings
    assert "answer: prompt is a string of 120 characters in the code; its value is not copied" in settings


# ------------------------------------------------------------------ refusals

def test_an_entry_that_is_not_a_function_of_the_module_is_refused(tmp_path, monkeypatch, capsys, examined):
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys, entry="pipeline.main:runn")
    examined(1, "a discovery with a misspelled entry")
    assert rc == 1 and draft is None
    assert "has no top-level function runn" in err and "fix:" in err


def test_an_entry_the_command_never_called_is_refused(tmp_path, monkeypatch, capsys, examined):
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys,
                                       command=[sys.executable, "-c", "import pipeline.main"])
    examined(1, "a discovery whose command never calls the entry")
    assert rc == 1 and draft is None
    assert "never called pipeline.main:run" in err and "fix:" in err


def test_a_command_that_never_loads_the_observer_is_refused(tmp_path, monkeypatch, capsys, examined):
    script = f"import sys; sys.path.insert(0, {str(tmp_path / 'stubs')!r}); {RUN[2]}"
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys, command=[sys.executable, "-E", "-c", script])
    examined(1, "a discovery whose interpreter ignores PYTHONPATH")
    assert rc == 1 and draft is None
    assert "the observer never loaded" in err and "fix:" in err


def test_discover_never_overwrites_its_outputs(tmp_path, monkeypatch, capsys, examined):
    rc, draft, _, _, _ = discover_in(tmp_path, monkeypatch, capsys)
    assert rc == 0
    answered = draft.replace('approved_by: "DECIDE: who approves this plan?"', "approved_by: alice")
    assert answered != draft
    (tmp_path / "out" / "onetrace-plan.draft.yaml").write_text(answered, encoding="utf-8")
    rc2 = main(["--entry", "pipeline.main:run", "--repo", str(tmp_path / "repo"), "--out-dir",
                str(tmp_path / "out"), "--", *RUN])
    err = capsys.readouterr().err
    examined(1, "a second discovery into the same folder")
    assert rc2 == 1 and "already exists" in err and "fix:" in err
    assert (tmp_path / "out" / "onetrace-plan.draft.yaml").read_text(encoding="utf-8") == answered


def test_every_stage_asks_for_its_rederivable_note(tmp_path, monkeypatch, capsys, examined):
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys)
    stages = stages_of(draft)
    examined(len(stages), "drafted stages")
    assert rc == 0, err
    for s in stages:
        assert str(s.get("rederivable_note", "")).startswith("DECIDE:"), s["name"]


def test_a_brace_in_a_file_name_is_drafted_so_it_reads_back(tmp_path, monkeypatch, capsys, examined):
    from tests.discover_fixtures import CORPUS, RETRIEVAL
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys, {
        "repo/data/corpus.json": None, "repo/data/corpus.{v2}.json": json.dumps(CORPUS),
        "repo/pipeline/retrieval.py": RETRIEVAL.replace('"corpus.json"', '"corpus.{v2}.json"')})
    examined(1, "the drafted files")
    assert rc == 0, err
    assert next(s for s in stages_of(draft) if s["name"] == "retrieve")["files"] == ["data/corpus.{v2}.json"]


# ------------------------------------------------------------------ the observed program is unchanged

def test_open_stored_on_a_class_still_works(tmp_path, examined):
    llm = textwrap.dedent('''\
        from pathlib import Path


        class Reader:
            read = open


        def answer(request, passages, mode):
            with Reader().read(Path(__file__)) as f:
                f.read()
            return {"answer": passages[0]["text"]}
        ''')
    r = observe_in(tmp_path, {"repo/pipeline/llm.py": llm})
    examined(1, "the observed command")
    assert r.returncode == 0, r.output


def test_a_value_too_deep_to_fingerprint_does_not_stop_the_program(tmp_path, examined):
    main_py = textwrap.dedent('''\
        def stage(tree):
            return 1


        def run(request):
            t = []
            for _ in range(100000):
                t = [t]
            return stage(t)
        ''')
    r = observe_in(tmp_path, {"repo/pipeline/main.py": main_py})
    examined(1, "the observed command")
    assert r.returncode == 0, r.output[-500:]
    call = next(e for e in r.events if e["kind"] == "call")
    assert call["args"][0][1].startswith("unrecorded:")


HTTPX_STUB = '''\
"""A stand-in for httpx: a streamed request's body cannot be read before it is sent."""
__version__ = "0.0-stub"


class RequestNotRead(Exception):
    pass


class URL:
    def __init__(self, url):
        self.host = url.split("/")[2]
        self._url = url

    def __str__(self):
        return self._url


class Request:
    def __init__(self, method, url, stream=None):
        self.method, self.url = method, URL(url)

    @property
    def content(self):
        raise RequestNotRead("streamed")


class Response:
    status_code = 200


class Client:
    def send(self, request, **kw):
        return Response()


class AsyncClient:
    async def send(self, request, **kw):
        return Response()
'''


def test_a_streamed_request_body_is_not_read(tmp_path, examined):
    llm = textwrap.dedent('''\
        import httpx


        def answer(request, passages, mode):
            httpx.Client().send(httpx.Request("POST", "https://llm.example.test/v1", stream=iter([b"x"])))
            return {"answer": passages[0]["text"]}
        ''')
    r = observe_in(tmp_path, {"repo/pipeline/llm.py": llm, "stubs/httpx/__init__.py": HTTPX_STUB})
    examined(1, "the observed command")
    assert r.returncode == 0, r.output[-500:]
    http = [e for e in r.events if e["kind"] == "http"]
    assert http and http[0]["request"] == "unrecorded:stream"


def test_tracing_lines_does_not_resolve_paths_line_by_line(tmp_path, examined):
    """The observer resolves a file's path once, not on every line it traces: before this, a
    tight loop ran a hundred times slower."""
    main_py = textwrap.dedent('''\
        def spin(n):
            total = 0
            for i in range(n):
                total += i
            return total


        def run(request):
            return spin(5000)
        ''')
    script = ("import os\nfrom pipeline.main import run\nreal = os.path.abspath\ncount = [0]\n"
              "def counting(p):\n    count[0] += 1\n    return real(p)\n"
              "os.path.abspath = counting\nrun('q')\nprint('ABSPATH', count[0])\n")
    r = observe_in(tmp_path, {"repo/pipeline/main.py": main_py}, command=[sys.executable, "-c", script])
    counted = int(r.output.split("ABSPATH", 1)[1].split()[0])
    examined(1, "the count of path resolutions")
    assert r.returncode == 0, r.output
    assert counted < 100, counted


def test_each_stage_keeps_its_own_exception(tmp_path, examined):
    main_py = textwrap.dedent('''\
        def parse(request):
            try:
                {}["missing"]
            except KeyError:
                pass
            return request


        def score(request):
            try:
                int("x")
            except ValueError:
                pass
            return 1


        def run(request):
            return score(parse(request))
        ''')
    r = observe_in(tmp_path, {"repo/pipeline/main.py": main_py})
    raised = {(e["stage"], e["type"]) for e in r.events if e["kind"] == "exception"}
    examined(len(raised), "exceptions recorded")
    assert ("pipeline.main:parse", "KeyError") in raised
    assert ("pipeline.main:score", "ValueError") in raised


# ------------------------------------------------------------------ what counts as a stage

def test_lambdas_and_generator_expressions_in_the_entry_are_not_stages(tmp_path, monkeypatch, capsys, examined, qualnames):
    main_py = textwrap.dedent('''\
        from pipeline.llm import answer
        from pipeline.retrieval import retrieve


        def run(request):
            passages = (lambda r: retrieve(r))(request)
            passages = sorted(passages, key=lambda p: p["id"])
            n = sum(1 for p in passages)
            return answer(request, passages, "normal")
        ''')
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py})
    functions = [s.get("function") for s in stages_of(draft)]
    examined(len(functions), "drafted stages")
    assert rc == 0, err
    assert functions == [None, "pipeline.retrieval:retrieve", "pipeline.llm:answer"]


def test_a_shared_decorator_does_not_merge_stages(tmp_path, monkeypatch, capsys, examined, qualnames):
    from tests.discover_fixtures import LLM, RETRIEVAL
    util = textwrap.dedent('''\
        import functools


        def traced(fn):
            @functools.wraps(fn)
            def wrapper(*a, **kw):
                return fn(*a, **kw)
            return wrapper
        ''')
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {
        "repo/pipeline/util.py": util,
        "repo/pipeline/retrieval.py": RETRIEVAL.replace("def retrieve", "from pipeline.util import traced\n\n\n@traced\ndef retrieve"),
        "repo/pipeline/llm.py": LLM.replace("def answer", "from pipeline.util import traced\n\n\n@traced\ndef answer")})
    functions = [s.get("function") for s in stages_of(draft)]
    examined(len(functions), "drafted stages")
    assert rc == 0, err
    assert functions == [None, "pipeline.retrieval:retrieve", "pipeline.llm:answer"]
    assert "wrapper" not in section(report, "Stages")
    assert ("pipeline.util:traced.<locals>.wrapper was called by run at pipeline/main.py:13 and pipeline/main.py:14"
            in section(report, "Called by the entry, but not proposed"))


def test_a_method_named_like_the_entry_is_not_the_entry(tmp_path, monkeypatch, capsys, examined, qualnames):
    main_py = textwrap.dedent('''\
        from pipeline.llm import answer
        from pipeline.retrieval import retrieve


        class Job:
            def run(self, request):
                return retrieve(request)


        def run(request):
            passages = Job().run(request)
            return answer(request, passages, "normal")
        ''')
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py})
    functions = [s.get("function") for s in stages_of(draft)]
    examined(len(functions), "drafted stages")
    assert rc == 0, err
    assert functions == [None, "pipeline.main:Job.run", "pipeline.llm:answer"]
    assert "it is a method, and instrument wraps only module-level functions" in report


def test_async_stages_are_counted_once_with_their_data_flow(tmp_path, monkeypatch, capsys, examined):
    main_py = textwrap.dedent('''\
        import asyncio


        async def retrieve(request):
            await asyncio.sleep(0)
            return ["p1"]


        async def answer(request, passages):
            await asyncio.sleep(0)
            return passages[0]


        async def run(request):
            passages = await retrieve(request)
            return await answer(request, passages)
        ''')
    command = [sys.executable, "-c", f"import asyncio; from pipeline.main import run; asyncio.run(run({REQUEST!r}))"]
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py},
                                            command=command)
    stages = section(report, "Stages")
    examined(1, "the stages section")
    assert rc == 0, err
    assert "times" not in stages and "StopIteration" not in stages and "may be a check" not in stages
    assert next(s for s in stages_of(draft) if s["name"] == "answer")["inputs"] == ["intake", "retrieve"]
    assert stages.count("it is async, and instrument does not wrap async functions yet") == 2


def test_a_stage_run_in_another_thread_is_named_not_hidden(tmp_path, monkeypatch, capsys, examined):
    main_py = textwrap.dedent('''\
        from concurrent.futures import ThreadPoolExecutor

        from pipeline.llm import answer
        from pipeline.retrieval import retrieve


        def run(request):
            with ThreadPoolExecutor(2) as pool:
                passages = pool.submit(retrieve, request).result()
            return answer(request, passages, "normal")
        ''')
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py})
    unaccounted = section(report, "Reads no stage explains")
    examined(1, "the unaccounted section")
    assert rc == 0, err
    assert "pipeline.retrieval:retrieve ran in another thread or task while the entry was running" in unaccounted
    assert "data/corpus.json` is read at pipeline/retrieval.py:9, in another thread or task" in unaccounted
    assert "outside any run" not in unaccounted


def test_stage_names_never_collide(tmp_path, monkeypatch, capsys, examined):
    overrides = {
        "repo/pipeline/a.py": "def load(x):\n    return [x]\n",
        "repo/pipeline/b.py": "def load(x):\n    return x + ['b']\n",
        "repo/pipeline/steps.py": "def intake(request):\n    return request.upper()\n",
        "repo/pipeline/main.py": textwrap.dedent('''\
            from pipeline import a, b
            from pipeline.steps import intake


            def run(request):
                x = intake(request)
                y = a.load(x)
                return b.load(y)
            '''),
    }
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys, overrides)
    names = [s["name"] for s in stages_of(draft)]
    examined(len(names), "drafted stage names")
    assert rc == 0, err
    assert len(names) == len(set(names)) == 4, names
    assert non_decide_refusals(draft) == []


def test_none_is_never_a_data_flow(tmp_path, monkeypatch, capsys, examined):
    main_py = textwrap.dedent('''\
        from pipeline.llm import answer
        from pipeline.retrieval import retrieve


        def validate(request):
            assert request


        def run(request):
            validate(request)
            passages = retrieve(request)
            return answer(request, passages, "normal")
        ''')
    llm = textwrap.dedent('''\
        def answer(request, passages, mode, cache=None):
            return {"answer": passages[0]["text"], "mode": mode}
        ''')
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys,
                                            {"repo/pipeline/main.py": main_py, "repo/pipeline/llm.py": llm})
    answer = next(s for s in stages_of(draft) if s["name"] == "answer")
    examined(1, "the answer stage")
    assert rc == 0, err
    assert answer["inputs"] == ["intake", "retrieve"]
    assert "may be a check" in report.split("validate: `pipeline.main:validate`", 1)[1].split("###", 1)[0]


@pytest.mark.parametrize("site, marker", [(".venv/tools", "repo/.venv/pyvenv.cfg"),
                                          ("vendor/site-packages", None)],
                         ids=["a virtual environment", "a site-packages folder"])
def test_installed_code_inside_the_repository_is_not_user_code(tmp_path, monkeypatch, capsys, examined, site, marker):
    lib = textwrap.dedent('''\
        import os


        def f():
            os.environ.get("FAKELIB_MODE")
            try:
                raise KeyError("x")
            except KeyError:
                pass
            return 1
        ''')
    llm = textwrap.dedent('''\
        import fakelib


        def answer(request, passages, mode):
            fakelib.f()
            return {"answer": passages[0]["text"]}
        ''')
    overrides = {f"repo/{site}/fakelib/__init__.py": lib, "repo/pipeline/llm.py": llm}
    if marker:
        overrides[marker] = "home = /usr/bin\n"
    rc, _, report, events, err = discover_in(tmp_path, monkeypatch, capsys, overrides, extra_path=[site])
    parsed = [json.loads(line) for line in events.splitlines()]
    top = site.split("/")[0]
    examined(len(parsed), "events")
    assert rc == 0, err
    assert not [e for e in parsed if str(e.get("site", "")).startswith(top) or str(e.get("file", "")).startswith(top)]
    #: The command line is the person's own, and names the interpreter it ran.
    assert [line for line in report.splitlines() if f"{top}/" in line and not line.startswith("- Command:")] == []
    assert "reads the environment variable FAKELIB_MODE" in section(report, "Stages")


def test_reading_source_for_a_traceback_is_not_a_stage_read(tmp_path, monkeypatch, capsys, examined):
    llm = textwrap.dedent('''\
        import traceback


        def answer(request, passages, mode):
            try:
                raise ValueError("x")
            except ValueError:
                traceback.format_exc()
            return {"answer": passages[0]["text"]}
        ''')
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/llm.py": llm})
    answer = next(s for s in stages_of(draft) if s["name"] == "answer")
    examined(1, "the answer stage")
    assert rc == 0, err
    assert "files" not in answer
    assert "reads pipeline/llm.py" not in report


def test_a_branch_body_on_the_same_line_is_listed_as_not_known(tmp_path, monkeypatch, capsys, examined):
    retrieval = textwrap.dedent('''\
        import json
        from pathlib import Path

        CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


        def retrieve(request):
            docs = json.loads(CORPUS.read_text(encoding="utf-8"))
            if not docs: return []
            return docs[:1]
        ''')
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/retrieval.py": retrieval})
    examined(1, "the unexercised section")
    assert rc == 0, err
    assert ("pipeline/retrieval.py:9: the `if` body is on the `if` line, so whether it ran is not known"
            in section(report, "Branches the fixtures never took"))


OPENAI_STUB = '''\
__version__ = "9.9.9"

import requests


def create(q):
    return requests.Session().request("POST", "https://api.openai.example.test/v1/chat", json={"q": q})
'''


def test_the_library_label_comes_from_the_calling_package(tmp_path, examined):
    llm = textwrap.dedent('''\
        import openai
        import requests


        def answer(request, passages, mode):
            openai.create(request)
            requests.Session().request("POST", "https://llm.example.test/v1/answer", json={"q": request})
            return {"answer": passages[0]["text"]}
        ''')
    r = observe_in(tmp_path / "anthropic", {"repo/pipeline/llm.py": llm, "stubs/openai/__init__.py": OPENAI_STUB})
    http = [(e["library"], e["version"], e["host"]) for e in r.events if e["kind"] == "http"]
    examined(len(http), "HTTP events")
    assert r.returncode == 0, r.output
    assert http == [("openai", "9.9.9", "api.openai.example.test"), ("requests", "0.0-stub", "llm.example.test")]


# ------------------------------------------------------------------ the second review

STREAMING_REQUESTS_STUB = '''\
"""A stand-in for requests whose streamed body can be read once, as the real one's can."""
import io

__version__ = "0.0-stub"


class Response:
    def __init__(self, status_code, body, stream):
        self.status_code = status_code
        self.raw = io.BytesIO(body)
        self._content = False if stream else body

    @property
    def content(self):
        if self._content is False:
            self._content = self.raw.read()
        return self._content


class Session:
    def request(self, method, url, stream=False, **kwargs):
        return Response(200, b"event: one\\nevent: two\\n", stream)
'''


def test_a_streamed_response_is_left_for_the_program(tmp_path, examined):
    llm = textwrap.dedent('''\
        import requests


        def answer(request, passages, mode):
            r = requests.Session().request("GET", "https://llm.example.test/stream", stream=True)
            body = r.raw.read()
            assert body == b"event: one\\nevent: two\\n", body
            return {"answer": passages[0]["text"]}
        ''')
    r = observe_in(tmp_path, {"repo/pipeline/llm.py": llm, "stubs/requests/__init__.py": STREAMING_REQUESTS_STUB})
    examined(1, "the observed command")
    assert r.returncode == 0, r.output[-500:]
    http = [e for e in r.events if e["kind"] == "http"]
    assert http and http[0]["response"] == "unrecorded:stream"


def test_a_name_is_taken_only_from_the_code_that_used_it(tmp_path, monkeypatch, capsys, examined):
    """A value that happens to be written in a test file or the test runner's code, or that
    matches a mere identifier in the pipeline's code, is not written there as a name."""
    #: Both are made up per run, so no runner can have a program of either name.
    program = "parcel" + secrets.token_hex(4)          # written only in the test file
    identifier = "passages_" + secrets.token_hex(4)    # an identifier in the pipeline, never a string
    llm = textwrap.dedent('''\
        import os
        import subprocess

        import requests


        def answer(request, passages, mode):
            IDENTIFIER = passages
            requests.Session().request("GET", "https://" + os.environ["SVC_HOST"] + "/x")
            requests.Session().request("GET", "https://" + os.environ["SVC_HOST_2"] + "/x")
            requests.Session().request("POST", "https://llm.example.test/v1/answer")
            for program in (request.split()[-1], os.environ["TOOL"]):
                try:
                    subprocess.run([program], capture_output=True, timeout=30)
                except (OSError, subprocess.SubprocessError):
                    pass
            return {"answer": IDENTIFIER[0]["text"]}
        ''').replace("IDENTIFIER", identifier)
    test = f"from pipeline.main import run\n\n\ndef test_the_pipeline_answers():\n    assert run('where is my {program}')\n"
    monkeypatch.setenv("SVC_HOST", "hookimpl")             # a word pluggy's own source is full of
    monkeypatch.setenv("SVC_HOST_2", "example")            # inside a URL in the pipeline, but not as a host
    monkeypatch.setenv("TOOL", identifier)
    rc, draft, report, events, err = discover_in(
        tmp_path, monkeypatch, capsys, {"repo/pipeline/llm.py": llm, "repo/tests/test_pipeline.py": test},
        command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_pipeline.py"])
    examined(3, "outputs")
    assert rc == 0, err
    for text in (draft, report, events):
        assert "calls hookimpl over" not in text and '"host": "hookimpl"' not in text
        assert "calls example over" not in text and '"host": "example"' not in text
        assert program not in text and f"runs the program {identifier}" not in text and f'"program": "{identifier}"' not in text
    assert "a host whose name is not written in the code" in report
    assert "calls llm.example.test over HTTP" in report
    assert "runs a program whose name is not written in the code" in report


def test_an_async_entry_cancelled_at_an_await_is_over(tmp_path, monkeypatch, capsys, examined):
    """A coroutine closed while suspended leaves at its await: that is the end of the run, so a
    read after it is outside any run, not in another thread or task."""
    main_py = textwrap.dedent('''\
        import asyncio
        from pathlib import Path

        from pipeline.retrieval import retrieve


        async def run(request):
            retrieve(request)
            await asyncio.sleep(10)


        def after():
            return (Path(__file__).resolve().parents[1] / "config" / "settings.txt").read_text()
        ''')
    command = [sys.executable, "-c",
               "import asyncio\nfrom pipeline.main import run, after\n"
               "try:\n    asyncio.run(asyncio.wait_for(run('q'), 0.2))\nexcept asyncio.TimeoutError:\n    pass\n"
               "after()\n"]
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py},
                                        command=command)
    unaccounted = section(report, "Reads no stage explains")
    examined(1, "the unaccounted section")
    assert rc == 0, err
    assert "the file `config/settings.txt` is read at pipeline/main.py:13, outside any run" in unaccounted


def test_when_git_cannot_list_the_tracked_files_discovery_is_refused(tmp_path, monkeypatch, capsys, examined):
    """Inside a git work tree, only git can say which names are safe to write; when it can't
    (not installed, or refusing the folder), no name is guessed from what happens to exist."""
    def untracked_cache(repo):
        (repo / "cache").mkdir()
        (repo / "cache" / "CUSTOMERACME42.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("PATH", "")            # git is not found; the command is run by absolute path

    retrieval = textwrap.dedent('''\
        import json
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[1]


        def retrieve(request):
            (ROOT / "cache" / ("CUSTOMER" + "ACME42.json")).read_text()
            return json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))[:1]
        ''')
    rc, draft, report, events, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/retrieval.py": retrieval},
                                                 before=untracked_cache)
    examined(1, "a discovery where git can't be run")
    assert rc == 1 and draft is None and report is None and events is None
    assert "git could not list the files it tracks" in err and "fix:" in err


def test_the_observers_own_metadata_lookup_is_not_a_read(tmp_path, monkeypatch, capsys, examined):
    rc, _, report, events, err = discover_in(tmp_path, monkeypatch, capsys, {
        "stubs/requests-0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: requests\nVersion: 0.0\n"})
    answer = report.split("answer: `pipeline.llm:answer`", 1)[1].split("###", 1)[0]
    http = [json.loads(line) for line in events.splitlines() if '"http"' in line]
    examined(len(http), "HTTP events")
    assert rc == 0, err
    assert http and http[0]["version"] == "0.0"          # the metadata was read, by the observer
    assert "reads a file" not in answer
    assert "reads a file" not in section(report, "Proposed boundaries")


def test_a_generator_stage_is_not_said_to_return_nothing(tmp_path, monkeypatch, capsys, examined):
    main_py = textwrap.dedent('''\
        def stream(request):
            yield from request.split()


        def answer(words):
            return len(words)


        def run(request):
            words = list(stream(request))
            return answer(words)
        ''')
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py})
    stages = section(report, "Stages")
    examined(1, "the stages section")
    assert rc == 0, err
    assert "may be a check" not in stages
    assert "it is a generator, and instrument does not wrap generators" in stages
    assert "stream's output could not be fingerprinted" in stages


def test_a_generator_closed_early_leaves_no_call_behind(tmp_path, monkeypatch, capsys, examined):
    main_py = textwrap.dedent('''\
        def stream(request):
            for word in request.split():
                yield word


        def answer(words):
            return len(words)


        def run(request):
            first = next(stream(request))
            loud = (lambda s, t=None: s.upper())(first)
            return answer(loud)
        ''')
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py})
    examined(1, "the report")
    assert rc == 0, err
    assert "is the output of stream" not in report


def test_a_class_based_decorator_does_not_merge_stages(tmp_path, monkeypatch, capsys, examined, qualnames):
    from tests.discover_fixtures import LLM, RETRIEVAL
    util = textwrap.dedent('''\
        import functools


        class traced:
            def __init__(self, fn):
                self.fn = fn
                functools.update_wrapper(self, fn)

            def __call__(self, *a, **kw):
                return self.fn(*a, **kw)
        ''')
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys, {
        "repo/pipeline/util.py": util,
        "repo/pipeline/retrieval.py": RETRIEVAL.replace("def retrieve", "from pipeline.util import traced\n\n\n@traced\ndef retrieve"),
        "repo/pipeline/llm.py": LLM.replace("def answer", "from pipeline.util import traced\n\n\n@traced\ndef answer")})
    functions = [s.get("function") for s in stages_of(draft)]
    examined(len(functions), "drafted stages")
    assert rc == 0, err
    assert functions == [None, "pipeline.retrieval:retrieve", "pipeline.llm:answer"]


def test_a_nested_function_the_entry_calls_is_named_as_passed_through(tmp_path, monkeypatch, capsys, examined, qualnames):
    retrieval = textwrap.dedent('''\
        import json
        from pathlib import Path

        CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


        def make_retriever(path):
            def retrieve(request):
                return json.loads(path.read_text(encoding="utf-8"))[:1]
            return retrieve


        retrieve = make_retriever(CORPUS)
        ''')
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/retrieval.py": retrieval})
    passed = section(report, "Called by the entry, but not proposed")
    examined(1, "the passed-through section")
    assert rc == 0, err
    assert "pipeline.retrieval:make_retriever.<locals>.retrieve was called by run at pipeline/main.py:13" in passed
    assert "inside pipeline.retrieval:make_retriever.<locals>.retrieve" in section(report, "Reads no stage explains")


def test_one_exception_raised_in_two_stages_is_kept_in_both(tmp_path, examined):
    main_py = textwrap.dedent('''\
        SHARED = ValueError("x")


        def parse(request):
            try:
                raise SHARED
            except ValueError:
                pass
            return request


        def score(request):
            try:
                raise SHARED
            except ValueError:
                pass
            return 1


        def run(request):
            return score(parse(request))
        ''')
    r = observe_in(tmp_path, {"repo/pipeline/main.py": main_py})
    raised = {(e["stage"], e["type"]) for e in r.events if e["kind"] == "exception"}
    examined(len(raised), "exceptions recorded")
    assert raised == {("pipeline.main:parse", "ValueError"), ("pipeline.main:score", "ValueError")}


def test_the_library_label_is_the_callers_not_any_frames(tmp_path, examined):
    anthropic = '__version__ = "9.9.9"\n\n\ndef run_tool(fn, q):\n    return fn(q)\n'
    llm = textwrap.dedent('''\
        import anthropic
        import requests


        def fetch(q):
            return requests.Session().request("GET", "https://llm.example.test/tool", json={"q": q})


        def answer(request, passages, mode):
            anthropic.run_tool(fetch, request)
            return {"answer": passages[0]["text"]}
        ''')
    r = observe_in(tmp_path, {"repo/pipeline/llm.py": llm, "stubs/anthropic/__init__.py": anthropic})
    http = [(e["library"], e["version"]) for e in r.events if e["kind"] == "http"]
    examined(len(http), "HTTP events")
    assert r.returncode == 0, r.output[-500:]
    assert http == [("requests", "0.0-stub")]


def test_an_entry_module_that_is_not_valid_python_is_refused(tmp_path, monkeypatch, capsys, examined):
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": "def run(request:\n"})
    examined(1, "a discovery over a broken entry module")
    assert rc == 1 and draft is None
    assert "not valid Python" in err and "fix:" in err


def test_an_entry_that_calls_no_function_says_no_stage_was_proposed(tmp_path, monkeypatch, capsys, examined):
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys,
                                            {"repo/pipeline/main.py": "def run(request):\n    return request.upper()\n"})
    examined(1, "the report")
    assert rc == 0, err
    assert "**No stage was proposed.**" in report


def test_every_python_process_the_command_starts_keeps_its_events(tmp_path, examined):
    """Each process writes events of its own, so none is lost when they end together. The loss
    itself comes and goes with timing; the count of processes observed does not."""
    script = ("import subprocess, sys\nfrom pipeline.main import run\nrun('q')\n"
              "children = [subprocess.Popen([sys.executable, '-c', 'pass']) for _ in range(24)]\n"
              "assert all(c.wait(timeout=120) == 0 for c in children)\n")
    r = observe_in(tmp_path, command=[sys.executable, "-c", script])
    examined(1, "the observed command")
    assert r.returncode == 0, r.output[-500:]
    assert r.processes == 25
    assert sum(1 for e in r.events if e["kind"] == "packages") == 25


def test_the_report_says_how_many_python_processes_were_observed(tmp_path, monkeypatch, capsys, examined):
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys)
    examined(1, "the report")
    assert rc == 0, err
    assert "- Python processes observed: 1" in report


def test_a_generator_entry_closed_early_is_over(tmp_path, monkeypatch, capsys, examined):
    """A generator closed at a yield leaves there: that is the end of the run, so a read after
    it is outside any run. (instrument refuses a generator entry, but discovery still reports
    what happened truthfully.)"""
    main_py = textwrap.dedent('''\
        from pathlib import Path

        from pipeline.retrieval import retrieve


        def run(request):
            yield retrieve(request)
            yield "more"


        def after():
            return (Path(__file__).resolve().parents[1] / "config" / "settings.txt").read_text()
        ''')
    command = [sys.executable, "-c",
               "from pipeline.main import run, after\ng = run('q')\nnext(g)\ng.close()\nafter()\n"]
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py},
                                        command=command)
    unaccounted = section(report, "Reads no stage explains")
    examined(1, "the unaccounted section")
    assert rc == 0, err
    assert "the file `config/settings.txt` is read at pipeline/main.py:12, outside any run" in unaccounted


def test_a_code_file_made_during_the_run_is_not_named(tmp_path, monkeypatch, capsys, examined):
    llm = textwrap.dedent('''\
        import importlib
        from pathlib import Path


        def answer(request, passages, mode):
            name = "gen_" + request.split()[-1]
            (Path(__file__).parent / (name + ".py")).write_text("def f():\\n    return 1\\n")
            importlib.invalidate_caches()
            importlib.import_module("pipeline." + name).f()
            return {"answer": passages[0]["text"]}
        ''')
    command = [sys.executable, "-c", "from pipeline.main import run; run('what about tenantacme77')"]
    rc, draft, report, events, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/llm.py": llm},
                                                 command=command)
    examined(3, "outputs")
    assert rc == 0, err
    for text in (draft, report, events):
        body = "\n".join(line for line in text.splitlines() if not line.startswith(("- Command:", '  run: "DECIDE')))
        assert "tenantacme77" not in body


def test_open_still_looks_like_the_builtin(tmp_path, examined):
    llm = textwrap.dedent('''\
        import copy
        import inspect
        import pickle


        def answer(request, passages, mode):
            assert pickle.loads(pickle.dumps(open)) is open
            assert copy.deepcopy(open) is open and copy.copy(open) is open
            assert str(inspect.signature(open)).startswith("(file, mode='r', buffering=-1")
            assert open.__text_signature__ and open.__self__ is not None
            return {"answer": passages[0]["text"]}
        ''')
    r = observe_in(tmp_path, {"repo/pipeline/llm.py": llm})
    examined(1, "the observed command")
    assert r.returncode == 0, r.output[-500:]


# ------------------------------------------------------------------ the third review

def test_packages_come_from_the_process_that_ran_the_entry(tmp_path, monkeypatch, capsys, examined):
    """Under a launcher (tox, nox, `poetry run`, a test controller), the fixtures run in a child
    process; the versions are the ones that process imported."""
    from tests.discover_fixtures import RETRIEVAL
    retrieval = RETRIEVAL.replace("import json\n", "import json\n\nimport yaml\n").replace(
        "    docs = json.loads(", "    yaml.safe_load('[1]')\n    docs = json.loads(")
    launch = ("import subprocess\nimport sys\n\nsys.exit(subprocess.run([sys.executable, '-c', "
              f"\"from pipeline.main import run; run({REQUEST!r})\"], timeout=300).returncode)\n")
    rc, draft, _, _, err = discover_in(tmp_path, monkeypatch, capsys,
                                       {"repo/pipeline/retrieval.py": retrieval, "repo/launch.py": launch},
                                       command=[sys.executable, "launch.py"])
    retrieve = next(s for s in stages_of(draft) if s["name"] == "retrieve")
    examined(1, "the retrieve stage's package")
    assert rc == 0, err
    assert retrieve["instrument"]["package"] == "PyYAML"


def test_functions_in_code_made_during_the_run_stay_apart(tmp_path, monkeypatch, capsys, examined):
    main_py = textwrap.dedent('''\
        import importlib
        import sys
        from pathlib import Path

        GEN = Path(__file__).resolve().parents[1] / "gen"
        GEN.mkdir(exist_ok=True)
        (GEN / "alpha.py").write_text("def score(x):\\n    return [x]\\n")
        (GEN / "beta.py").write_text("def rank(y):\\n    return y + ['r']\\n")
        importlib.invalidate_caches()
        from gen.alpha import score  # noqa: E402
        from gen.beta import rank  # noqa: E402


        def run(request):
            s = score(request)
            return rank(s)
        ''')
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py})
    stages = [s for s in stages_of(draft) if "function" in s]
    examined(len(stages), "drafted function stages")
    assert rc == 0, err
    assert len(stages) == 2 and stages[0]["name"] != stages[1]["name"]
    assert all(str(s["function"]).startswith("DECIDE:") for s in stages)
    assert stages[0]["name"] in stages[1]["inputs"]
    assert "times" not in section(report, "Stages")


def test_hosts_written_in_the_code_in_any_usual_shape_are_named(tmp_path, monkeypatch, capsys, examined):
    llm = textwrap.dedent('''\
        import requests

        API = "api.example.com/v1"
        MIRROR = "https://svc@mirror.example.com/v1"
        LEGACY = "legacy.example.com:8443"
        CDN = "//cdn.example.com/x"


        def answer(request, passages, mode):
            for url in ("https://" + API, MIRROR, "http://" + LEGACY + "/x", "https:" + CDN):
                requests.Session().request("GET", url)
            return {"answer": passages[0]["text"]}
        ''')
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/llm.py": llm})
    examined(4, "hosts")
    assert rc == 0, err
    for host in ("api.example.com", "mirror.example.com", "legacy.example.com", "cdn.example.com"):
        assert f"calls {host} over HTTP" in report, host
    assert "a host whose name is not written in the code" not in report


def test_an_exception_thrown_into_a_context_manager_stage_is_not_its_own(tmp_path, monkeypatch, capsys, examined):
    main_py = textwrap.dedent('''\
        import contextlib


        @contextlib.contextmanager
        def session(request):
            yield request


        def retrieve(s):
            raise LookupError("no such passage")


        def run(request):
            try:
                with session(request) as s:
                    retrieve(s)
            except LookupError:
                pass
            return None
        ''')
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys, {"repo/pipeline/main.py": main_py})
    session_reasons = report.split("session: `pipeline.main:session`", 1)[1].split("###", 1)[0]
    retrieve_reasons = report.split("retrieve: `pipeline.main:retrieve`", 1)[1].split("###", 1)[0]
    examined(2, "stages' reasons")
    assert rc == 0, err
    assert "raised LookupError" in retrieve_reasons
    assert "raised" not in session_reasons


def test_no_stage_is_said_plainly_even_when_something_was_passed_through(tmp_path, monkeypatch, capsys, examined, qualnames):
    tools = "class Loader:\n    def __call__(self, request):\n        return request.upper()\n"
    main_py = "from pipeline.tools import Loader\n\n\ndef run(request):\n    return Loader()(request)\n"
    rc, _, report, _, err = discover_in(tmp_path, monkeypatch, capsys,
                                        {"repo/pipeline/tools.py": tools, "repo/pipeline/main.py": main_py})
    examined(1, "the report")
    assert rc == 0, err
    assert "**No stage was proposed.** The entry called no module-level function of the repository directly" in report
    assert "pipeline.tools:Loader.__call__ was called by run" in section(report, "Called by the entry, but not proposed")


def test_observe_keeps_its_events_apart_from_files_it_did_not_write(tmp_path, examined):
    repo, stubs = make_discover_repo(tmp_path)
    events_path = tmp_path / "run[1].jsonl"
    unrelated = tmp_path / "run[1].jsonl.old-backup.part"
    unrelated.write_text("someone else's\n", encoding="utf-8")
    env = dict(os.environ, LLM_API_KEY=MARKER, PYTHONPATH=str(stubs))
    r = observe(RUN, repo=repo, entry="pipeline.main:run", events_path=events_path, env=env)
    examined(1, "the observed command")
    assert r.returncode == 0, r.output[-500:]
    assert any(e["kind"] == "entry" for e in r.events)
    assert unrelated.read_text(encoding="utf-8") == "someone else's\n"


# ------------------------------------------------------------------ pathlib imported first (Python 3.10)

ACCESSOR_RETRIEVAL = '''\
import json
import pathlib
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


def retrieve(request):
    with pathlib._NormalAccessor.open(CORPUS, "r", -1, "utf-8") as f:     # how 3.10's Path.open reads
        docs = json.load(f)
    words = set(request.lower().split())
    return sorted(docs, key=lambda d: -len(words & set(d["text"].lower().split())))[:1]
'''

PATHLIB_FIRST = (
    "import io\n"
    "import pathlib\n"
    "if not hasattr(pathlib, '_NormalAccessor'):\n"
    "    class _NormalAccessor:              # 3.11+: stand in for 3.10's, which holds the real open\n"
    "        open = io.open\n"
    "    pathlib._NormalAccessor = _NormalAccessor\n"
    "import site\n"
    "site.main()                             # the observer loads only now, after pathlib\n"
    "from pipeline.main import run\n"
    f"run({REQUEST!r})\n")


def test_reads_through_a_pathlib_imported_before_the_observer_are_seen(tmp_path, monkeypatch, capsys, examined):
    """An editable install's .pth can import pathlib before the observer loads. On Python 3.10,
    pathlib then holds the real open, and every Path read would go unseen."""
    rc, draft, report, _, err = discover_in(tmp_path, monkeypatch, capsys,
                                            {"repo/pipeline/retrieval.py": ACCESSOR_RETRIEVAL},
                                            command=[sys.executable, "-S", "-c", PATHLIB_FIRST])
    retrieve = next(s for s in stages_of(draft) if s["name"] == "retrieve")
    examined(1, "the retrieve stage's files")
    assert rc == 0, err
    assert retrieve.get("files") == ["data/corpus.json"]
