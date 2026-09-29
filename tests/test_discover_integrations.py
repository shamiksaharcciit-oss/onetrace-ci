"""`discover` takes in the frameworks a pipeline uses: LangChain's runs, LlamaIndex's spans and
the OpenTelemetry spans a program emits. The frameworks are test-only dependencies
(requirements-integrations.lock, installed by the integrations job in tests.yml). Where they
aren't installed these tests skip, unless ONETRACE_CI_REQUIRE_INTEGRATIONS=1, where a missing
framework fails the test instead.

The fixture pipelines never reach the network: any download a framework tries goes to a proxy
that isn't there."""
from __future__ import annotations

import importlib
import importlib.metadata as metadata
import json
import os
import subprocess
import sys

import pytest

from onetrace_ci.discover import main, observe
from onetrace_ci.plan import read_document
from tests.discover_fixtures import MARKER, make_discover_repo

REQUEST = "what does the warranty cover"
#: The planted marker as a word: what a name made from a value would look like.
WORD = MARKER.replace("-", "_")
RUN = [sys.executable, "-c", f"from pipeline.main import run; print(run({REQUEST!r}))"]
OFFLINE = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
           "LLM_API_KEY": MARKER}


def need(module):
    if os.environ.get("ONETRACE_CI_REQUIRE_INTEGRATIONS") == "1":
        return importlib.import_module(module)
    return pytest.importorskip(module)


LANGCHAIN = {
    "repo/pipeline/retrieval.py": '''\
import json
import os
from pathlib import Path

from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.vectorstores import InMemoryVectorStore

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


def retrieve(request):
    texts = [d["text"] for d in json.loads(CORPUS.read_text(encoding="utf-8"))]
    texts.append("note: " + os.environ["LLM_API_KEY"])
    store = InMemoryVectorStore.from_texts(texts, DeterministicFakeEmbedding(size=8))
    return [{"text": d.page_content} for d in store.as_retriever(search_kwargs={"k": 1}).invoke(request)]
''',
    "repo/pipeline/llm.py": '''\
import os

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda


def answer(request, passages, mode):
    prompt = ChatPromptTemplate.from_messages([("human", "{question} {context} {key}")])
    chain = prompt | FakeListChatModel(responses=["the warranty covers defects"]) | StrOutputParser()
    text = chain.invoke({"question": request, "context": passages[0]["text"], "key": os.environ["LLM_API_KEY"]})
    tidy = RunnableLambda(lambda s: s.strip()).with_config(run_name="tidy " + os.environ["LLM_API_KEY"])
    return {"answer": str(tidy.invoke(str(text))), "mode": mode}
''',
}

LLAMAINDEX = {
    "repo/pipeline/retrieval.py": '''\
import json
import os
from pathlib import Path

from llama_index.core.indices import VectorStoreIndex
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.schema import TextNode

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


def retrieve(request):
    texts = [d["text"] for d in json.loads(CORPUS.read_text(encoding="utf-8"))]
    texts.append("note: " + os.environ["LLM_API_KEY"])
    #: Every mock embedding is the same vector, so the ids (fixed here) decide the ties.
    nodes = [TextNode(text=t, id_=f"n{i}") for i, t in enumerate(texts)]
    index = VectorStoreIndex(nodes=nodes, embed_model=MockEmbedding(embed_dim=8))
    return [{"text": n.node.get_content()} for n in index.as_retriever(similarity_top_k=1).retrieve(request)]
''',
    "repo/pipeline/llm.py": '''\
import os

from llama_index.core.llms import MockLLM


def answer(request, passages, mode):
    stream = MockLLM().stream_complete(request + " " + passages[0]["text"] + " " + os.environ["LLM_API_KEY"][:0])
    return {"answer": "".join(r.delta or "" for r in stream), "mode": mode}
''',
}

OPENTELEMETRY = {
    "repo/pipeline/telemetry.py": '''\
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

EXPORTER = InMemorySpanExporter()
PROVIDER = TracerProvider()
PROVIDER.add_span_processor(SimpleSpanProcessor(EXPORTER))
TRACER = PROVIDER.get_tracer("pipeline")
''',
    "repo/pipeline/retrieval.py": '''\
import json
import os
from pathlib import Path

from pipeline.telemetry import TRACER

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


def retrieve(request):
    with TRACER.start_as_current_span("retr" + "iever"):
        pass
    with TRACER.start_as_current_span("retrieve") as span:
        span.set_attribute("key", os.environ["LLM_API_KEY"])
        span.add_event("exception", {"exception.type": os.environ["LLM_API_KEY"].replace("-", "_")})
        docs = json.loads(CORPUS.read_text(encoding="utf-8"))
        words = set(request.lower().split())
        return sorted(docs, key=lambda d: -len(words & set(d["text"].lower().split())))[:1]
''',
    "repo/pipeline/llm.py": '''\
import os

from pipeline.telemetry import EXPORTER, TRACER


def answer(request, passages, mode):
    key = os.environ["LLM_API_KEY"]
    try:
        with TRACER.start_as_current_span("answer-" + key):
            raise ValueError("the upstream said " + key)
    except ValueError:
        pass
    spans = EXPORTER.get_finished_spans()
    return {"answer": passages[0]["text"], "mode": mode, "spans": [s.status.status_code.name for s in spans]}
''',
}

FRAMEWORKS = {"langchain": ("langchain_core", LANGCHAIN), "llamaindex": ("llama_index.core.instrumentation", LLAMAINDEX),
              "opentelemetry": ("opentelemetry.sdk.trace", OPENTELEMETRY)}


def discover_with(root, monkeypatch, capsys, files, command=RUN):
    """(rc, draft, report, events, stderr) for a discovery of the fixture pipeline with `files`."""
    repo, _ = make_discover_repo(root, files)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    for k, v in OFFLINE.items():
        monkeypatch.setenv(k, v)
    out = root / "out"
    rc = main(["--entry", "pipeline.main:run", "--repo", str(repo), "--out-dir", str(out), "--", *command])
    texts = [(out / n).read_text(encoding="utf-8") if (out / n).exists() else None
             for n in ("onetrace-plan.draft.yaml", "discovery-report.md", "discovery-events.jsonl")]
    return (rc, *texts, capsys.readouterr().err)


def reasons(report, stage):
    """The reasons the report lists for one stage."""
    block = report.split(f". {stage}: `", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
    return [line[2:] for line in block.splitlines() if line.startswith("- ")]


def test_langchain_runs_are_named_in_the_stage_that_started_them(tmp_path, monkeypatch, capsys, examined):
    need("langchain_core")
    version = metadata.version("langchain-core")
    rc, _, report, _, err = discover_with(tmp_path, monkeypatch, capsys, LANGCHAIN)
    retrieve, answer = reasons(report, "retrieve"), reasons(report, "answer")
    examined(2, "the retrieve and answer stages' reasons")
    assert rc == 0, err
    assert f"runs LangChain's retriever VectorStoreRetriever (langchain-core {version})" in retrieve
    assert not any("installed package langchain_core" in r for r in retrieve), retrieve
    assert (f"runs LangChain's chain RunnableSequence (langchain-core {version}), which ran chain "
            f"ChatPromptTemplate, chain StrOutputParser and chat model FakeListChatModel") in answer
    #: A run the program named with a value: it runs, and its name is not written.
    assert f"runs a LangChain chain whose name is not recorded (langchain-core {version})" in answer


def test_llamaindex_spans_are_named_in_the_stage_that_started_them(tmp_path, monkeypatch, capsys, examined):
    need("llama_index.core.instrumentation")
    version = metadata.version("llama-index-core")
    rc, _, report, _, err = discover_with(tmp_path, monkeypatch, capsys, LLAMAINDEX)
    retrieve, answer = reasons(report, "retrieve"), reasons(report, "answer")
    examined(2, "the retrieve and answer stages' reasons")
    assert rc == 0, err
    assert any(r.startswith(f"runs LlamaIndex's VectorIndexRetriever.retrieve (llama-index-core {version}), which ran ")
               for r in retrieve), retrieve
    assert f"runs LlamaIndex's MockLLM.stream_complete (llama-index-core {version})" in answer


def test_opentelemetry_spans_are_taken_in_by_the_stage_that_emitted_them(tmp_path, monkeypatch, capsys, examined):
    need("opentelemetry.sdk.trace")
    version = metadata.version("opentelemetry-sdk")
    rc, _, report, _, err = discover_with(tmp_path, monkeypatch, capsys, OPENTELEMETRY)
    retrieve, answer = reasons(report, "retrieve"), reasons(report, "answer")
    examined(2, "the retrieve and answer stages' reasons")
    assert rc == 0, err
    assert f"emits the OpenTelemetry span `retrieve` (opentelemetry-sdk {version})" in retrieve
    #: A name the code builds at run time is not written, even one the observer's own code holds.
    assert f"emits an OpenTelemetry span whose name is not written in the code (opentelemetry-sdk {version})" in retrieve
    assert (f"emits an OpenTelemetry span whose name is not written in the code (opentelemetry-sdk {version}); "
            f"it ended with an error (ValueError)") in answer


@pytest.mark.parametrize("framework", sorted(FRAMEWORKS))
def test_no_value_a_framework_handles_reaches_the_outputs(framework, tmp_path, monkeypatch, capsys, examined):
    """The planted marker is in a document, a prompt, a LangChain run's name, a span's attribute
    and a span's name."""
    module, files = FRAMEWORKS[framework]
    need(module)
    rc, draft, report, events, err = discover_with(tmp_path, monkeypatch, capsys, files)
    examined(3, "the draft, the report and the events")
    assert rc == 0, err
    for text in (draft, report, events):
        assert MARKER not in text and WORD not in text


@pytest.mark.parametrize("framework", sorted(FRAMEWORKS))
def test_the_program_prints_the_same_under_observation(framework, tmp_path, examined):
    """What the program prints, and what it writes to stderr, with and without the observer. The
    LlamaIndex pipeline streams its answer, so a stream the observer read would print nothing."""
    module, files = FRAMEWORKS[framework]
    need(module)
    repo, _ = make_discover_repo(tmp_path, files)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(OFFLINE)
    plain = subprocess.run(RUN, cwd=repo, env=env, capture_output=True, text=True, timeout=600)
    observed = observe(RUN, repo=repo, entry="pipeline.main:run", events_path=tmp_path / "e.jsonl", env=env)
    kinds = {e["kind"] for e in observed.events}
    examined(2, "the program's output, plain and observed")
    assert plain.returncode == 0 and observed.returncode == 0, plain.stderr
    assert "framework" in kinds or "span" in kinds, kinds
    assert observed.output == plain.stdout + plain.stderr


def test_a_framework_run_in_the_entry_function_itself_is_among_the_reads_no_stage_explains(
        tmp_path, monkeypatch, capsys, examined):
    need("langchain_core")
    main_file = '''\
import os
from pathlib import Path

from langchain_core.runnables import RunnableLambda

from pipeline.llm import answer
from pipeline.retrieval import retrieve


def run(request):
    request = RunnableLambda(lambda text: text.strip()).invoke(request)
    passages = retrieve(request)
    return answer(request, passages, "normal")
'''
    rc, _, report, _, err = discover_with(tmp_path, monkeypatch, capsys, {**LANGCHAIN, "repo/pipeline/main.py": main_file})
    unexplained = report.split("## Reads no stage explains", 1)[1].split("\n## ", 1)[0]
    examined(1, "the reads no stage explains")
    assert rc == 0, err
    assert (f"LangChain's chain RunnableLambda (langchain-core {metadata.version('langchain-core')}) runs at "
            f"pipeline/main.py:11, in the entry function itself") in unexplained


def test_the_observer_imports_no_framework_the_program_does_not(tmp_path, examined):
    """Its taps wait for the program's own imports."""
    repo, stubs = make_discover_repo(tmp_path)
    frameworks = ("langchain_core", "llama_index", "llama_index_instrumentation", "opentelemetry")
    command = [sys.executable, "-c", f"import sys; from pipeline.main import run; run({REQUEST!r}); "
               f"print(sorted(m for m in {frameworks!r} if m in sys.modules))"]
    env = dict(os.environ, PYTHONPATH=str(stubs), LLM_API_KEY=MARKER)
    observed = observe(command, repo=repo, entry="pipeline.main:run", events_path=tmp_path / "e.jsonl", env=env)
    examined(1, "the modules the program ended with")
    assert observed.returncode == 0, observed.output
    assert observed.output.strip().splitlines()[-1] == "[]"


def test_a_framework_event_records_fingerprints_and_its_library(tmp_path, monkeypatch, capsys, examined):
    need("langchain_core")
    rc, _, _, events, err = discover_with(tmp_path, monkeypatch, capsys, LANGCHAIN)
    runs = [json.loads(line) for line in events.splitlines() if '"kind": "framework"' in line]
    examined(len(runs), "LangChain run events")
    assert rc == 0, err
    assert {r["stage"] for r in runs} == {"pipeline.retrieval:retrieve", "pipeline.llm:answer"}
    for r in runs:
        assert r["library"] == "langchain-core" and r["outcome"] == "ok"
        assert r["inputs"].startswith("fp:") and r["returned"].startswith("fp:")


LANGCHAIN_ASYNC = {"repo/pipeline/llm.py": '''\
import asyncio

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.fake import FakeListLLM


class Seen(AsyncCallbackHandler):
    def __init__(self):
        self.seen = []

    async def on_llm_start(self, serialized, prompts, **kwargs):
        self.seen.append("on_llm_start")

    async def on_llm_end(self, response, **kwargs):
        self.seen.append("on_llm_end")


def answer(request, passages, mode):
    seen = Seen()
    llm = FakeListLLM(responses=["the warranty covers defects"])
    text = asyncio.run(llm.ainvoke(request, config={"callbacks": [seen]}))
    return {"answer": text, "mode": mode, "seen": seen.seen}
'''}


LANGCHAIN_NAMES = {"repo/pipeline/llm.py": '''\
import importlib.util
import sys

from langchain_core.runnables import RunnableLambda

spec = importlib.util.find_spec("this")
spec.loader = importlib.util.LazyLoader(spec.loader)
lazy = importlib.util.module_from_spec(spec)
sys.modules["this"] = lazy
spec.loader.exec_module(lazy)


def answer(request, passages, mode):
    first = RunnableLambda(lambda s: s.strip()).with_config(run_name="NotAClass").invoke(request)
    second = RunnableLambda(lambda s: s.upper()).with_config(run_name="RunnableWithMessageHistory").invoke(first)
    return {"answer": second, "mode": mode, "history imported": "langchain_core.runnables.history" in sys.modules}
'''}


LLAMAINDEX_OWN_FUNCTION = {"repo/pipeline/llm.py": '''\
import os

from llama_index.core.instrumentation import get_dispatcher

dispatcher = get_dispatcher(__name__)


def answer(request, passages, mode):
    def shape(text):
        return text.upper()

    shape.__name__ = shape.__qualname__ = os.environ["LLM_API_KEY"].replace("-", "_")
    return {"answer": dispatcher.span(shape)(passages[0]["text"]), "mode": mode}
'''}


LANGCHAIN_STREAMED = {"repo/pipeline/llm.py": '''\
from langchain_core.runnables import RunnableLambda


def answer(request, passages, mode):
    shout = RunnableLambda(lambda s: s.upper())
    first = "".join(shout.stream(request))
    second = "".join(shout.stream(passages[0]["text"]))
    return {"answer": first + " " + second, "mode": mode}
'''}

LANGCHAIN_EXECUTOR = {"repo/pipeline/llm.py": '''\
import asyncio
import concurrent.futures

from langchain_core.runnables import RunnableLambda


async def shout(text):
    return text.upper()


def answer(request, passages, mode):
    chain = RunnableLambda(shout)

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))

        def bridge():
            future = asyncio.run_coroutine_threadsafe(chain.ainvoke(request), loop)
            try:
                return future.result(timeout=20)
            except Exception as e:
                return "failed: " + type(e).__name__

        return await loop.run_in_executor(None, bridge)

    return {"answer": asyncio.run(main()), "mode": mode}
'''}

def prints_the_same(tmp_path, files, command=RUN, stubs=False):
    """(plain output, observed output, observed events) for the fixture pipeline with `files`."""
    repo, stub_dir = make_discover_repo(tmp_path, files)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(OFFLINE)
    if stubs:
        env["PYTHONPATH"] = str(stub_dir)
    plain = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=600)
    observed = observe(command, repo=repo, entry="pipeline.main:run", events_path=tmp_path / "e.jsonl", env=env)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    return plain.stdout + plain.stderr, observed.output, observed.events


def test_the_program_s_own_async_langchain_handlers_see_every_event(tmp_path, examined):
    """LangChain sends an async run's `on_llm_start` only to handlers that run inline, when any
    does: the observer's handler must not be one of them."""
    need("langchain_core")
    plain, observed, _ = prints_the_same(tmp_path, LANGCHAIN_ASYNC)
    examined(2, "the program's output, plain and observed")
    assert "'on_llm_start'" in plain
    assert observed == plain


def test_naming_a_langchain_run_loads_and_imports_nothing(tmp_path, examined):
    """A lazily loaded module stays unloaded, and a run named like a class LangChain would import
    on first use doesn't import it."""
    need("langchain_core")
    plain, observed, events = prints_the_same(tmp_path, LANGCHAIN_NAMES)
    examined(2, "the program's output, plain and observed")
    assert "'history imported': False" in plain
    assert observed == plain
    assert any(e["kind"] == "framework" for e in events)


def test_a_llamaindex_span_on_the_program_s_own_function_writes_no_name(tmp_path, monkeypatch, capsys, examined):
    """A function's name is the program's to choose, and can be made from a value."""
    need("llama_index.core.instrumentation")
    version = metadata.version("llama-index-core")
    rc, draft, report, events, err = discover_with(tmp_path, monkeypatch, capsys, LLAMAINDEX_OWN_FUNCTION)
    examined(3, "the draft, the report and the events")
    assert rc == 0, err
    for text in (draft, report, events):
        assert WORD not in text
    assert f"runs a LlamaIndex span on code that isn't LlamaIndex's (llama-index-core {version})" in reasons(report, "answer")


def test_the_observer_s_import_hook_stays_in_place_while_it_looks_a_module_up(tmp_path, examined):
    """It never takes itself off `sys.meta_path`: another thread importing then would miss it.
    (It is the `_Finder` of the module `sitecustomize`; another package may have a `_Finder`.)"""
    repo, stubs = make_discover_repo(tmp_path)
    program = ("import sys\n"
               "class Look:\n"
               "    def find_spec(self, name, path=None, target=None):\n"
               "        if name == 'requests':\n"
               "            print('observer hook present:', any(type(f).__module__ == 'sitecustomize' and type(f).__name__ == '_Finder' for f in sys.meta_path))\n"
               "sys.meta_path.insert(0, Look())\n"
               "import requests\n"
               f"from pipeline.main import run; run({REQUEST!r})\n")
    env = dict(os.environ, PYTHONPATH=str(stubs), LLM_API_KEY=MARKER)
    observed = observe([sys.executable, "-c", program], repo=repo, entry="pipeline.main:run",
                       events_path=tmp_path / "e.jsonl", env=env)
    lines = [line for line in observed.output.splitlines() if line.startswith("observer hook present:")]
    examined(len(lines), "lookups of the patched module")
    assert observed.returncode == 0, observed.output
    assert len(lines) >= 2 and all(line.endswith("True") for line in lines), lines


def test_streamed_langchain_runs_are_fingerprinted_by_their_own_inputs(tmp_path, monkeypatch, capsys, examined):
    """A streamed run starts with a placeholder input; its real input comes when it ends."""
    need("langchain_core")
    rc, _, _, events, err = discover_with(tmp_path, monkeypatch, capsys, LANGCHAIN_STREAMED)
    runs = [json.loads(line) for line in events.splitlines() if '"kind": "framework"' in line]
    examined(len(runs), "streamed LangChain runs")
    assert rc == 0, err
    assert len(runs) == 2
    assert runs[0]["inputs"] != runs[1]["inputs"]


def test_an_async_langchain_run_takes_nothing_from_the_program_s_thread_pool(tmp_path, examined):
    """The program's only worker waits on an async run: had the observer's handler been sent to
    that pool, as LangChain sends a handler that is not inline, the run would wait on the worker."""
    need("langchain_core")
    plain, observed, _ = prints_the_same(tmp_path, LANGCHAIN_EXECUTOR)
    examined(2, "the program's output, plain and observed")
    assert "WHAT DOES THE WARRANTY COVER" in plain
    assert observed == plain


WRAPT_RETRIEVAL = {"repo/pipeline/retrieval.py": '''\
import json
from pathlib import Path

import wrapt


class Guarded:
    @property
    def __dict__(self):
        print("Guarded.__dict__ was read")
        return {}


#: A proxy of wrapt's (a C type, whose own __dict__ reads the wrapped object's): reading
#: through it would run Guarded's property.
proxied = wrapt.ObjectProxy(Guarded())

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


@wrapt.decorator
def traced(wrapped, instance, args, kwargs):
    return wrapped(*args, **kwargs)


@traced
def retrieve(request):
    docs = json.loads(CORPUS.read_text(encoding="utf-8"))
    words = set(request.lower().split())
    return sorted(docs, key=lambda d: -len(words & set(d["text"].lower().split())))[:1]
''', "repo/pipeline/llm.py": LANGCHAIN["repo/pipeline/llm.py"]}


def test_python_3_10_names_a_wrapt_decorated_stage_as_newer_pythons_do(tmp_path, monkeypatch, capsys, examined):
    """Python 3.10 has no `co_qualname`; its fallback (forced here on any Python) names what a
    wrapt decorator wraps through wrapt's own C attribute, and reads nothing through a proxy."""
    need("wrapt")
    need("langchain_core")
    names = {}
    for mode in ("co_qualname", "fallback"):
        if mode == "fallback":
            monkeypatch.setenv("ONETRACE_CI_DISCOVER_QUALNAME_FALLBACK", "1")
        rc, draft, _, _, err = discover_with(tmp_path / mode, monkeypatch, capsys, WRAPT_RETRIEVAL)
        assert rc == 0, err
        names[mode] = [s.get("function") for s in read_document(draft, source="draft")["stages"]]
    examined(2, "the stages drafted with each way of naming functions")
    assert names["fallback"] == names["co_qualname"], names
    assert not any("<locals>" in str(n) for n in names["fallback"]), names


def test_python_3_10_naming_reads_nothing_through_a_proxy(tmp_path, monkeypatch, examined):
    need("wrapt")
    need("langchain_core")
    monkeypatch.setenv("ONETRACE_CI_DISCOVER_QUALNAME_FALLBACK", "1")
    plain, observed, _ = prints_the_same(tmp_path, WRAPT_RETRIEVAL)
    examined(2, "the program's output, plain and observed")
    assert "was read" not in plain
    assert observed == plain
