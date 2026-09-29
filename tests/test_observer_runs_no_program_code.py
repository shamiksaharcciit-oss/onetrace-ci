"""The observer reads what the program does and never runs any of the program's code to do so:
naming a function (Python 3.10's fallback walks a module's globals), fingerprinting a value, and
naming a program read no attribute through the program's own code. No `__getattr__` runs, no
lazily imported module loads, no `__repr__` or `__iter__` of the program's own is called. The
program prints the same with the observer as without it."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from onetrace_ci.discover import observe
from tests.discover_fixtures import MARKER, make_discover_repo

REQUEST = "what does the warranty cover"
RUN = [sys.executable, "-c", f"from pipeline.main import run; print(run({REQUEST!r}))"]

RETRIEVAL = '''\
import importlib.util
import json
import sys
from pathlib import Path

#: A module imported lazily: it loads (and prints the Zen of Python) the first time any of its
#: attributes is read.
spec = importlib.util.find_spec("this")
spec.loader = importlib.util.LazyLoader(spec.loader)
zen = importlib.util.module_from_spec(spec)
sys.modules["this"] = zen
spec.loader.exec_module(zen)

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"
LOOKED_UP = []


class Tracked:
    """Records every attribute lookup its class doesn't answer, and every repr."""

    def __getattr__(self, name):
        LOOKED_UP.append(name)
        raise AttributeError(name)

    def __repr__(self):
        LOOKED_UP.append("repr")
        return "Tracked()"


class Marks(set):
    """A set of the program's own kind, which records being iterated."""

    def __iter__(self):
        LOOKED_UP.append("iter")
        return set.__iter__(self)


def retrieve(request, tracked, marks, more):
    docs = json.loads(CORPUS.read_text(encoding="utf-8"))
    return docs[:1]
'''

MAIN = '''\
import subprocess

from pipeline.llm import answer
from pipeline.retrieval import LOOKED_UP, Marks, Tracked, retrieve


def run(request):
    passages = retrieve(request, Tracked(), {Tracked()}, Marks({"a", "b"}))
    try:
        subprocess.run([Tracked(), "x"], timeout=60)
    except TypeError:
        pass
    return answer(request, passages, "normal")["answer"], sorted(set(LOOKED_UP))
'''


@pytest.fixture(params=["co_qualname", "the Python 3.10 fallback"])
def qualnames(request, monkeypatch):
    """Python 3.10 has no `co_qualname`; its fallback is forced on newer Pythons too."""
    if request.param != "co_qualname":
        monkeypatch.setenv("ONETRACE_CI_DISCOVER_QUALNAME_FALLBACK", "1")
    return request.param


def test_the_observer_runs_none_of_the_program_s_code(tmp_path, qualnames, examined):
    repo, stubs = make_discover_repo(tmp_path, {"repo/pipeline/retrieval.py": RETRIEVAL,
                                                "repo/pipeline/main.py": MAIN})
    env = dict(os.environ, PYTHONPATH=str(stubs), LLM_API_KEY=MARKER)
    plain = subprocess.run(RUN, cwd=repo, env=env, capture_output=True, text=True, timeout=300)
    observed = observe(RUN, repo=repo, entry="pipeline.main:run", events_path=tmp_path / "e.jsonl", env=env)
    examined(2, "the program's output, plain and observed")
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert plain.stdout.strip().endswith("[])"), plain.stdout
    assert observed.output == plain.stdout + plain.stderr
    assert any(e["kind"] == "call" for e in observed.events)
