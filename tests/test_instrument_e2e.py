"""The whole path, for real: build the patch, apply it with `git apply`, run the instrumented
pipeline, and check what it recorded with `onetrace-verify` and the gate."""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

import pytest

from onetrace_ci.gate import _console_script, run_gate
from onetrace_ci.instrument import Refused, build_patch, main
from tests.instrument_fixtures import (PLAN, forget_pipeline_modules,
                                       real_read_memory, make_repo)

QUESTION = "what does the warranty cover"


def _apply(repo: Path, patch_text: str, where: Path) -> None:
    where.write_bytes(patch_text.encode("utf-8"))
    applied = subprocess.run(["git", "apply", str(where)], cwd=repo, capture_output=True, text=True,
                             timeout=60)
    assert applied.returncode == 0, applied.stderr


@pytest.fixture
def run_pipeline(monkeypatch):
    """`run_pipeline(repo, run_id, question) -> value`: import the repo's `pipeline.main` afresh
    and call `run`, with ONETRACE_RUN_ID set."""
    added = []

    def _run(repo: Path, run_id: str, question: str = QUESTION):
        monkeypatch.setenv("ONETRACE_RUN_ID", run_id)
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
            added.append(str(repo))
        forget_pipeline_modules()
        module = importlib.import_module("pipeline.main")
        return module.run(question)

    yield _run
    for p in added:
        sys.path.remove(p)
    forget_pipeline_modules()


@pytest.fixture
def instrumented(tmp_path):
    repo = make_repo(tmp_path / "repo")
    result = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    _apply(repo, result.patch, tmp_path / "instrument.patch")
    return repo


def _receipts(run: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((run / "receipts").glob("*.json"))]


#: onetrace 0.1.2's published wheels, as PyPI lists them: `ctx.read_memory` ships in 0.1.2.
PINNED = {"onetrace": ("0.1.2", "77c1e735f5c33a351b99c9e0d9926390277f3e14cfb400b5fb3e7fe4b69a55b4"),
          "onetrace-verify": ("0.1.2", "e69188e6486de0e669b24005f355acf5823afeee2aa123c81bc8c1bef58930cc")}


def test_the_lock_pins_onetrace_0_1_2_by_its_published_wheel_hashes(examined):
    lock = (Path(__file__).resolve().parents[1] / "requirements.lock").read_text(encoding="utf-8")
    examined(len(PINNED), "packages pinned")
    for name, (version, sha) in PINNED.items():
        entry = lock.split(f"\n{name}=={version} \\\n", 1)
        assert len(entry) == 2, f"{name}=={version} is not pinned"
        assert entry[1].splitlines()[0].strip() == f"--hash=sha256:{sha}", name


def test_intake_runs_on_onetrace_s_own_read_memory_never_a_stand_in(examined):
    import importlib.metadata
    from onetrace.emit import StageContext
    examined(1, "installed onetrace")
    assert importlib.metadata.version("onetrace") == "0.1.2"
    sdk_own = getattr(StageContext, "read_memory", None)
    assert sdk_own is not None
    # The end-to-end tests run after the fixture: what they call is what it leaves in place.
    assert real_read_memory() == "onetrace's own ctx.read_memory"
    assert StageContext.read_memory is sdk_own
    assert not getattr(StageContext.read_memory, "_stand_in", False)


def test_the_applied_patch_runs_and_its_record_verifies(instrumented, run_pipeline, examined):
    which = real_read_memory()
    run_pipeline(instrumented, "t1")
    run = instrumented / "runs" / "t1"
    verify = subprocess.run([_console_script("onetrace-verify"), "--require-artifacts", str(run)],
                            capture_output=True, text=True, timeout=120)
    receipts = _receipts(run)
    examined(len(receipts), f"receipts written by the instrumented run ({which})")
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert [r["stage"]["name"] for r in receipts] == ["intake", "retrieve", "answer"]

    intake, retrieve, answer = receipts
    q = QUESTION.encode("utf-8")
    assert intake["inputs"] == [{"bytes": str(len(q)), "digest": "sha256:" + hashlib.sha256(q).hexdigest(),
                                 "media_type": "text/plain", "name": "request",
                                 "trust_class": "externally-sourced"}]
    assert [o["name"] for o in intake["outputs"]] == ["intake.json"]
    assert intake["instrument"]["id"] == "onetrace.read_memory"
    assert intake["instrument"]["version"] == importlib.metadata.version("onetrace")

    assert [(i["name"], i["trust_class"]) for i in retrieve["inputs"]] == [
        ("intake.json", "operator-authored"), ("data/corpus.json", "operator-authored")]
    assert [o["name"] for o in retrieve["outputs"]] == ["retrieve.json"]
    assert retrieve["instrument"]["id"] == "word-overlap"
    assert retrieve["instrument"]["kind"] == "python-package"
    assert retrieve["instrument"]["version"] == importlib.metadata.version("onetrace-verify")

    assert [i["name"] for i in answer["inputs"]] == ["retrieve.json"]
    assert [o["name"] for o in answer["outputs"]] == ["answer.json"]
    assert answer["instrument"]["rederivable"] == "false"
    assert answer["instrument"]["rederivable_note"] == "a stand-in for a hosted model"
    assert all(r["emission"]["policy"] == "fail-closed" for r in receipts)


def test_no_memory_input_value_is_stored_only_its_digest(instrumented, run_pipeline, examined):
    real_read_memory()
    secret_question = "a question whose words must not be stored verbatim"
    run_pipeline(instrumented, "t2", secret_question)
    run = instrumented / "runs" / "t2"
    intake_files = [p for p in run.rglob("*") if p.is_file() and "intake" in str(p)]
    examined(len(intake_files), "files the intake stage wrote")
    for p in intake_files:
        assert secret_question not in p.read_text(encoding="utf-8")


def test_the_instrumented_run_returns_what_the_original_returned(tmp_path, instrumented, run_pipeline,
                                                                 examined):
    real_read_memory()
    original = make_repo(tmp_path / "original")
    before = run_pipeline(original, "unused")
    after = run_pipeline(instrumented, "t3")
    examined(1, "the pipeline's own return value, before and after")
    assert after == before


def test_running_the_command_twice_gives_an_empty_second_patch(tmp_path, examined, capsys):
    repo = make_repo(tmp_path / "repo")
    first, second = tmp_path / "first.patch", tmp_path / "second.patch"
    args = ["--style", "wrappers", "--plan", str(repo / "onetrace-plan.yaml"), "--repo", str(repo)]
    assert main(args + ["--out", str(first)]) == 0
    _apply(repo, first.read_text(encoding="utf-8"), tmp_path / "applied.patch")
    capsys.readouterr()
    assert main(args + ["--out", str(second)]) == 0
    printed = capsys.readouterr().out
    examined(1, "the second run's patch file")
    assert first.stat().st_size > 0
    assert second.read_bytes() == b""
    assert "nothing to change" in printed


def test_a_different_plan_on_instrumented_code_is_refused_not_silently_skipped(instrumented, examined):
    plan = instrumented / "onetrace-plan.yaml"
    plan.write_text(PLAN.replace('rederivable_note: "a stand-in for a hosted model"',
                                 'rederivable_note: "a different note"'), encoding="utf-8")
    examined(1, "a changed plan over instrumented code")
    with pytest.raises(Refused, match=r"pipeline[/\\]main\.py.*different plan"):
        build_patch(plan_path=plan, repo=instrumented, style="wrappers")


def test_an_exception_in_a_stage_is_recorded_raised_and_the_run_still_closed(tmp_path, run_pipeline,
                                                                              examined):
    real_read_memory()
    repo = make_repo(tmp_path / "repo", {
        "pipeline/llm.py": "def answer(request, passages):\n    raise ValueError('the model is down')\n"})
    result = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    _apply(repo, result.patch, tmp_path / "instrument.patch")
    with pytest.raises(ValueError, match="the model is down"):
        run_pipeline(repo, "t4")
    run = repo / "runs" / "t4"
    receipts = _receipts(run)
    examined(len(receipts), "receipts written before and at the failing stage")
    assert receipts[-1]["stage"]["name"] == "answer"
    assert receipts[-1]["outcome"]["class"] == "error"
    assert receipts[-1]["outcome"]["status"] == "ValueError"
    assert (run / "MANIFEST.json").is_file(), "close() did not run"


def test_the_gate_passes_an_instrumented_run_against_its_own_baseline(instrumented, run_pipeline,
                                                                      tmp_path, examined):
    """What the generated workflow runs: a committed baseline, a new run, the gate."""
    real_read_memory()
    run_pipeline(instrumented, "baseline")
    run_pipeline(instrumented, "onetrace-ci-candidate")
    exit_code, findings = run_gate(run=instrumented / "runs" / "onetrace-ci-candidate",
                                   baseline=instrumented / "runs" / "baseline",
                                   plan_path=instrumented / "onetrace-plan.yaml", runner=None,
                                   out=tmp_path / "gate-out", review_exit_zero=False)
    examined(len(findings), "gate findings for an unchanged instrumented run")
    assert exit_code == 0, [(f.check, f.verdict, f.detail) for f in findings]


def test_the_gate_reviews_a_changed_memory_input_at_the_intake_stage(instrumented, run_pipeline,
                                                                     tmp_path, examined):
    real_read_memory()
    run_pipeline(instrumented, "baseline")
    run_pipeline(instrumented, "onetrace-ci-candidate", "what does the shipping policy say")
    exit_code, findings = run_gate(run=instrumented / "runs" / "onetrace-ci-candidate",
                                   baseline=instrumented / "runs" / "baseline",
                                   plan_path=instrumented / "onetrace-plan.yaml", runner=None,
                                   out=tmp_path / "gate-out", review_exit_zero=False)
    checks = {f.check: f for f in findings}
    examined(len(findings), "gate findings for a changed question")
    assert exit_code == 2
    assert checks["diff"].detail == "first difference at stage 'intake'"
