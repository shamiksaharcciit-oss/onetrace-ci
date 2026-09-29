"""A small pipeline for `onetrace-ci discover`, with what the discovery gate plants in it:

- a file read and an environment read that no stage explains (made outside any stage);
- secrets: a marker in an environment value, in a request body, and in an exception message;
- a branch the fixtures never take.

`requests` here is a stub package on a separate path (never the network), standing in for the
real library, which is not a dependency of onetrace-ci.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

MARKER = "PLANTED-SECRET-7f3a9c"

MAIN = '''\
"""The pipeline's one run."""
import os
from pathlib import Path

from pipeline.llm import answer
from pipeline.retrieval import retrieve

SETTINGS = (Path(__file__).resolve().parents[1] / "config" / "settings.txt").read_text()


def run(request):
    mode = os.environ.get("PIPELINE_MODE", "normal")
    passages = retrieve(request)
    return answer(request, passages, mode)
'''

RETRIEVAL = '''\
import json
import os
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus.json"


def retrieve(request):
    docs = json.loads(CORPUS.read_text(encoding="utf-8"))
    if os.environ.get("RETRIEVE_DEBUG"):
        print("never taken in the fixtures")
    words = set(request.lower().split())
    return sorted(docs, key=lambda d: -len(words & set(d["text"].lower().split())))[:1]
'''

LLM = '''\
import os

import requests


def answer(request, passages, mode):
    key = os.environ["LLM_API_KEY"]
    try:
        raise ValueError("the upstream said " + key)
    except ValueError:
        pass
    response = requests.Session().request("POST", "https://llm.example.test/v1/answer",
                                          json={"question": request, "key": key})
    return {"answer": passages[0]["text"], "status": str(response.status_code), "mode": mode}
'''

TEST = '''\
from pipeline.main import run


def test_the_pipeline_answers():
    assert run("what does the warranty cover")["answer"]
'''

REQUESTS_STUB = '''\
"""A stand-in for the requests library: no network, a fixed response."""
__version__ = "0.0-stub"


class Response:
    def __init__(self, status_code, content):
        self.status_code, self.content = status_code, content


class Session:
    def request(self, method, url, **kwargs):
        return Response(200, b'{"ok": true}')
'''

CORPUS = [{"id": "p1", "text": "The warranty covers manufacturing defects for twelve months."},
          {"id": "p2", "text": "Shipping delays are handled by the logistics partner."}]


def make_discover_repo(root: Path, overrides: dict[str, str | None] | None = None) -> tuple[Path, Path]:
    """(repo, stubs dir). The stubs dir goes on PYTHONPATH, outside the repository. `overrides`
    replace (or, as None, remove) files by path under `root` ("repo/..." or "stubs/..."). The
    repository's files are added to its git index, as a user's would be tracked."""
    files = {
        "repo/pipeline/__init__.py": "",
        "repo/pipeline/main.py": MAIN,
        "repo/pipeline/retrieval.py": RETRIEVAL,
        "repo/pipeline/llm.py": LLM,
        "repo/tests/test_pipeline.py": TEST,
        "repo/data/corpus.json": json.dumps(CORPUS),
        "repo/config/settings.txt": "mode=normal\n",
        "stubs/requests/__init__.py": REQUESTS_STUB,
    }
    for rel, text in (overrides or {}).items():
        if text is None:
            files.pop(rel, None)
        else:
            files[rel] = text
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(textwrap.dedent(text).encode("utf-8") if rel.endswith(".py") else text.encode("utf-8"))
    repo = root / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, timeout=60)
    return repo, root / "stubs"
