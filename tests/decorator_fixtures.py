"""A small un-instrumented pipeline whose entry takes no parameters, for `instrument`'s decorator
output. Written into a temporary repository for each test.

`stub_sdk` is a stand-in for onetrace 0.2.0's decorator API, which is not released yet: it
records what each decorator and helper is called with, under the SDK's names, and changes
nothing the decorated functions do. It shows that the generated code imports, and passes the
plan's fields to the arguments the SDK names; it is not the SDK.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

MAIN = '''\
"""The pipeline's one run."""
from pipeline.llm import answer
from pipeline.retrieval import retrieve


def run():
    """Retrieve, then answer."""
    passages = retrieve()
    return answer(passages)
'''

RETRIEVAL = '''\
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def retrieve():
    question = (ROOT / "data" / "question.txt").read_text(encoding="utf-8")
    docs = json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))
    words = set(question.lower().split())
    return sorted(docs, key=lambda d: -len(words & set(d["text"].lower().split())))[:1]
'''

LLM = '''\
def answer(passages):
    """Answer from the best passage."""
    return {"answer": passages[0]["text"]}
'''

CORPUS = [
    {"id": "p1", "text": "The warranty covers manufacturing defects for twelve months."},
    {"id": "p2", "text": "Shipping delays are handled by the logistics partner."},
]

PLAN = '''\
approved_by: alice
entry: pipeline.main:run
run_dir: runs/{run_id}
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
  run: python -c "from pipeline.main import run; print(run())"
  baseline: runs/baseline
'''

STUB_SDK = '''\
"""A stand-in for onetrace 0.2.0's decorator API, for tests: it records each call, under the
SDK spec's names, into the file named by ONETRACE_STUB_LOG, and changes nothing."""
import atexit
import json
import os

_CALLS = []


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, _Pkg):
        return {"pkg": _plain(value.fields)}
    return value


class _Pkg:
    def __init__(self, fields):
        self.fields = fields


def pkg(id, package, *, kind, config=None, rederivable=None, note=None):
    return _Pkg({"id": id, "package": package, "kind": kind, "config": config,
                 "rederivable": rederivable, "note": note})


def run(*, stages=None, run_dir=None, declared_edges=None, **other):
    def decorate(fn):
        _CALLS.append({"run": fn.__qualname__, "stages": stages, "run_dir": run_dir,
                       "declared_edges": declared_edges, "other": _plain(other)})
        return fn
    return decorate


def stage(name, *, instrument=None, files=None, trust=None, rederivable=None, note=None, **other):
    def decorate(fn):
        _CALLS.append({"stage": name, "function": fn.__qualname__, "instrument": _plain(instrument),
                       "files": files, "trust": trust, "rederivable": rederivable, "note": note,
                       "other": _plain(other)})
        return fn
    return decorate


def constant(key, value):
    _CALLS.append({"constant": key})


@atexit.register
def _write():
    path = os.environ.get("ONETRACE_STUB_LOG")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_CALLS, f, sort_keys=True)
'''


def make_repo(root: Path, overrides: dict[str, str | None] | None = None) -> Path:
    """A git repository holding the pipeline and its plan; `overrides` replace (or, as None,
    remove) files by repository path."""
    files = {
        "pipeline/__init__.py": "",
        "pipeline/main.py": MAIN,
        "pipeline/retrieval.py": RETRIEVAL,
        "pipeline/llm.py": LLM,
        "data/question.txt": "What does the warranty cover?",
        "data/corpus.json": json.dumps(CORPUS),
        "onetrace-plan.yaml": PLAN,
    }
    for rel, text in (overrides or {}).items():
        if text is None:
            files.pop(rel, None)
        else:
            files[rel] = text
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((textwrap.dedent(text) if rel.endswith(".py") else text).encode("utf-8"))
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True, timeout=60)
    return root


def stub_sdk(root: Path) -> Path:
    """A directory holding the stand-in `onetrace` package, to put first on PYTHONPATH."""
    (root / "onetrace").mkdir(parents=True, exist_ok=True)
    (root / "onetrace" / "__init__.py").write_bytes(STUB_SDK.encode("utf-8"))
    return root
