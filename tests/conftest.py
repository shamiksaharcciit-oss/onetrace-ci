"""Real onetrace runs, built with the SDK's own `Recorder` -- no mocking of
`onetrace`/`onetrace-verify`, since the entire point of this package is the
mapping between their real exit codes/reports and one CI verdict.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_run(out_dir: Path, run_id: str, *, retrieve_version="1.0.0",
               retrieve_text="same text", answer_ok=True,
               boundary: str | None = None) -> None:
    from onetrace.emit import Instrument, Recorder, Refusal

    rec = Recorder(str(out_dir), run_id=run_id, declared_stages=["retrieve", "answer"],
                   manifest=Path(__file__), policy="fail-open",
                   boundaries=([{"name": boundary, "kind": "external service",
                                "note": "test fixture boundary"}] if boundary else None))

    @rec.stage("retrieve", Instrument("word-overlap", "retriever", retrieve_version,
                                      {"top_k": "1"}))
    def retrieve(ctx):
        ctx.constant("top_k", "1")
        return ctx.write_json("retrieved.json", {"id": "p1", "text": retrieve_text})

    @rec.stage("answer", Instrument("extractive", "answerer", "1.0.0",
                                    {"method": "extractive"}))
    def answer(ctx, retrieved_artifact):
        hit = ctx.read_json(retrieved_artifact)
        ctx.constant("method", "extractive")
        if not answer_ok:
            raise Refusal("test fixture refusal", "answer_ok=False")
        return ctx.write_json("answer.json", {"answer": hit["text"]})

    answer(retrieve())
    rec.close()


@pytest.fixture
def make_run(tmp_path):
    """`make_run(name, **kwargs) -> Path` -- a fresh real run under `tmp_path`."""
    def _make(name: str, **kwargs) -> Path:
        out = tmp_path / name
        _write_run(out, name, **kwargs)
        return out
    return _make


@pytest.fixture
def write_plan(tmp_path):
    def _write(name: str, text: str) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path
    return _write


# ---------------------------------------------------------- the standing rule

@pytest.fixture
def examined(request):
    """Report how many items this check looked at. Zero is a failure of the
    check. See `tests/examined.py` for why this is a recorded count rather
    than a habit.
    """
    from tests.examined import record

    def report(count, what):
        return record(request.node.nodeid, count, what)

    return report


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    from tests.examined import summary_lines

    terminalreporter.write_line("")
    for line in summary_lines():
        terminalreporter.write_line(line)
