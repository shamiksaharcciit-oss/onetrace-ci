"""What the decorator style relies on the SDK for, tested against onetrace 0.2.0's candidate.

Each test raises `CandidateMissing` while the installed onetrace has no decorator API, and is an
expected failure for that reason only: any other failure, once the candidate is installed, is a
real one. When the candidate carries what a test asks for, the test passes, and the expected
failure is turned into a plain test (and what it guards is generated).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from onetrace_ci.instrument import CANDIDATE_RUN_ID, build_patch
from tests.decorator_fixtures import make_repo


class CandidateMissing(Exception):
    """The installed onetrace has no decorator API: these tests need 0.2.0's candidate."""


def _candidate():
    import onetrace
    missing = [name for name in ("run", "stage", "pkg") if not hasattr(onetrace, name)]
    if missing:
        raise CandidateMissing(f"onetrace {getattr(onetrace, '__version__', '?')} has no {missing}; these tests "
                               f"run against onetrace 0.2.0's candidate")
    return onetrace


def _run_module(tmp_path: Path, source: str, run_id: str) -> Path:
    """Write `source` as a module, run its `run()` with ONETRACE_RUN_ID set, and return the run."""
    (tmp_path / "prog.py").write_text(textwrap.dedent(source), encoding="utf-8")
    done = subprocess.run([sys.executable, "-c", "import prog; prog.run()"], cwd=tmp_path, capture_output=True,
                          text=True, timeout=120, env=dict(os.environ, ONETRACE_RUN_ID=run_id))
    assert done.returncode == 0, done.stderr
    return tmp_path / "runs" / run_id


def _receipts(run: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((run / "receipts").glob("*.json"))]


@pytest.mark.xfail(strict=True, raises=CandidateMissing,
                   reason="whether onetrace 0.2.0 records a parameter left to its default is for its candidate "
                          "to show; until then onetrace-ci asks for the stage's trust")
def test_the_sdk_records_a_parameter_left_to_its_default(tmp_path, examined):
    _candidate()
    run = _run_module(tmp_path, '''
        import onetrace as ot

        @ot.stage("answer", instrument=ot.pkg("x", "onetrace", kind="answerer"), trust="operator-authored",
                  rederivable=True)
        def answer(passages, style="short"):
            return {"passages": passages, "style": style}

        @ot.run(stages=["answer"], run_dir="runs/{run_id}")
        def run():
            return answer(["p"])
        ''', "defaults")
    answer = next(r for r in _receipts(run) if r.get("stage") == "answer")
    examined(len(answer["inputs"]), "inputs of the stage called without its defaulted parameter")
    assert "style" in [i.get("name") for i in answer["inputs"]]


@pytest.mark.xfail(strict=True, raises=CandidateMissing,
                   reason="a stage run in another thread waits for the SDK to show it is recorded: a pool's "
                          "thread does not inherit the caller's context unless it is copied")
def test_a_stage_run_in_a_thread_pool_is_recorded_in_the_run(tmp_path, examined):
    _candidate()
    run = _run_module(tmp_path, '''
        import concurrent.futures
        import onetrace as ot

        @ot.stage("retrieve", instrument=ot.pkg("x", "onetrace", kind="retriever"), rederivable=True)
        def retrieve():
            return ["passage"]

        @ot.run(stages=["retrieve"], run_dir="runs/{run_id}")
        def run():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(retrieve).result()
        ''', "threads")
    stages = [r.get("stage") for r in _receipts(run)]
    examined(len(stages), "receipts of a run whose stage ran in a pool's thread")
    assert "retrieve" in stages


@pytest.mark.xfail(strict=True, raises=CandidateMissing,
                   reason="the workflow names each run by ONETRACE_RUN_ID, which onetrace 0.2.0's @ot.run is "
                          "to honour; until its candidate does, the gate cannot find a decorated run")
def test_the_workflow_s_run_is_where_its_gate_looks(tmp_path, examined):
    """The generated workflow runs the pipeline with ONETRACE_RUN_ID set, then gates the run at
    `runs/onetrace-ci-candidate`. `init-ci`'s workflow, for code decorated by hand, has the same
    run step and gate, so this covers it too."""
    _candidate()
    repo = make_repo(tmp_path / "repo")
    patch = tmp_path / "p.patch"
    patch.write_bytes(build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo).patch.encode("utf-8"))
    subprocess.run(["git", "-C", str(repo), "apply", str(patch)], check=True, capture_output=True, timeout=60)
    ran = subprocess.run([sys.executable, "-c", "from pipeline.main import run; print(run())"], cwd=repo,
                         capture_output=True, text=True, timeout=120,
                         env=dict(os.environ, ONETRACE_RUN_ID=CANDIDATE_RUN_ID))
    assert ran.returncode == 0, ran.stderr
    run = repo / "runs" / CANDIDATE_RUN_ID
    examined(1, "the workflow's candidate run")
    assert (run / "MANIFEST.json").is_file(), sorted(p.name for p in (repo / "runs").glob("*"))
    cli = [sys.executable, "-m", "onetrace_ci"]
    proposed = subprocess.run([*cli, "baseline", "propose", "--from", str(run), "--out", str(repo / "runs" / "baseline")],
                              capture_output=True, text=True, timeout=120)
    assert proposed.returncode == 0, proposed.stderr
    gated = subprocess.run([*cli, "gate", "--run", str(run), "--baseline", str(repo / "runs" / "baseline"),
                            "--plan", str(repo / "onetrace-plan.yaml"), "--out", str(tmp_path / "out")],
                           capture_output=True, text=True, timeout=300)
    assert gated.returncode == 0, gated.stdout + gated.stderr
