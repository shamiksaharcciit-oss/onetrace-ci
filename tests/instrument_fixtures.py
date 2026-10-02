"""A small un-instrumented pipeline, written into a temporary repository for each test.

Its instruments name packages that are installed wherever these tests run (`onetrace` and
`onetrace-verify`), so `importlib.metadata.version(package)` resolves at run time.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

MAIN = '''\
"""The pipeline's one run."""
from pipeline.llm import answer
from pipeline.retrieval import retrieve


def run(request):
    """Retrieve, then answer."""
    passages = retrieve(request)
    return answer(request, passages)
'''

RETRIEVAL = '''\
import json
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


def retrieve(request):
    docs = json.loads(CORPUS.read_text(encoding="utf-8"))
    words = set(request.lower().split())
    return sorted(docs, key=lambda d: -len(words & set(d["text"].lower().split())))[:1]
'''

LLM = '''\
def answer(request, passages):
    return {"answer": passages[0]["text"], "question": request}
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
  - name: intake
    memory_inputs: [request]
    trust: externally-sourced
    rederivable: "true"
  - name: retrieve
    function: pipeline.retrieval:retrieve
    instrument: {name: word-overlap, package: onetrace-verify, kind: python-package}
    inputs: [intake]
    files: [data/corpus.json]
    trust: operator-authored
    rederivable: "true"
  - name: answer
    function: pipeline.llm:answer
    instrument: {name: extractive, package: onetrace, kind: python-package}
    rederivable: "false"
    rederivable_note: "a stand-in for a hosted model"
approved_boundaries: []
ci:
  install: pip install --require-hashes -r requirements.lock
  run: python -c "from pipeline.main import run; run('what does the warranty cover')"
  baseline: runs/baseline
'''


def base_files() -> dict[str, str]:
    return {
        "pipeline/__init__.py": "",
        "pipeline/main.py": MAIN,
        "pipeline/retrieval.py": RETRIEVAL,
        "pipeline/llm.py": LLM,
        "data/corpus.json": json.dumps(CORPUS),
        "onetrace-plan.yaml": PLAN,
    }


def make_repo(root: Path, overrides: dict[str, str | None] | None = None) -> Path:
    """Write the base pipeline under `root`, with `overrides` replacing (or, as None, removing)
    files by relative path. Returns `root`."""
    files = base_files()
    for rel, text in (overrides or {}).items():
        if text is None:
            files.pop(rel, None)
        else:
            files[rel] = text
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(textwrap.dedent(text).encode("utf-8") if rel.endswith(".py") else text.encode("utf-8"))
    #: Its own repository, as a user's would be: `git apply` run inside another repository's
    #: subdirectory silently skips every path outside that subdirectory.
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, timeout=60)
    return root


def real_read_memory() -> str:
    """The instrumented code's intake goes through `ctx.read_memory`, which ships in onetrace 0.1.2,
    the version requirements.lock pins. Only onetrace's own is ever used: an installed onetrace
    without it fails here, loudly, rather than being stood in for."""
    from onetrace.emit import StageContext

    if not hasattr(StageContext, "read_memory"):
        raise AssertionError("the installed onetrace has no ctx.read_memory; install requirements.lock "
                             "(onetrace 0.1.2)")
    return "onetrace's own ctx.read_memory"


def forget_pipeline_modules(prefix: str = "pipeline") -> None:
    """Drop a fixture package from `sys.modules`, so the next import reads the files afresh."""
    for name in [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]:
        del sys.modules[name]
