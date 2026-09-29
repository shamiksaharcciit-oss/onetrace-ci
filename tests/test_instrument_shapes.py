"""Code shapes `instrument` must accept, and what its patch must keep; several found by an
independent review."""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys

import pytest

from onetrace_ci.instrument import WORKFLOW_PATH, build_patch
from tests.instrument_fixtures import (MAIN, forget_pipeline_modules,
                                       install_read_memory_if_missing, make_repo)


def _apply(repo, patch_text, tmp_path):
    p = tmp_path / "instrument.patch"
    p.write_bytes(patch_text.encode("utf-8"))
    r = subprocess.run(["git", "apply", str(p)], cwd=repo, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def _body(body: str) -> str:
    old = "    passages = retrieve(request)\n    return answer(request, passages)\n"
    return MAIN.replace(old, body)


def test_a_package_that_imports_its_own_submodule_resolves(tmp_path, examined):
    """`from . import retrieval` in the package, `retrieval.retrieve(...)` in the entry."""
    repo = make_repo(tmp_path / "repo", {
        "pipeline/__init__.py": "from . import retrieval\n",
        "pipeline/main.py": MAIN.replace("from pipeline.retrieval import retrieve\n",
                                         "from pipeline import retrieval\n")
                                .replace("retrieve(request)", "retrieval.retrieve(request)")})
    result = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    examined(1, "a patch for a package that imports its own submodule")
    assert "_onetrace_stage_retrieve(request)" in result.patch


def test_keywords_and_attributes_named_like_a_stage_are_not_stage_uses(tmp_path, examined):
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": "import types\n" + _body(
        "    passages = retrieve(request)\n    opts = dict(answer=1)\n"
        "    ns = types.SimpleNamespace(retrieve=2)\n    _ = ns.retrieve, opts\n"
        "    return answer(request, passages)\n")})
    examined(1, "an entry with a keyword and an attribute named like stages")
    build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")


def test_comments_on_the_def_line_and_at_the_end_of_the_body_are_kept(tmp_path, examined):
    repo = make_repo(tmp_path / "repo", {"pipeline/main.py": _body(
        "    passages = retrieve(request)\n    return answer(request, passages)\n    # the end of run\n")
        .replace("def run(request):", "def run(request):  # noqa: D401")})
    result = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    _apply(repo, result.patch, tmp_path)
    source = (repo / "pipeline" / "main.py").read_text(encoding="utf-8")
    examined(2, "comments the patch must keep")
    assert "def run(request):  # noqa: D401" in source
    assert "# the end of run" in source


def test_a_crlf_entry_module_gets_crlf_throughout(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    main = repo / "pipeline" / "main.py"
    main.write_bytes(main.read_bytes().replace(b"\n", b"\r\n"))
    result = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    _apply(repo, result.patch, tmp_path)
    data = main.read_bytes()
    examined(data.count(b"\n"), "line endings in the patched CRLF module")
    assert data.count(b"\n") == data.count(b"\r\n")


def test_idempotent_when_git_checks_the_workflow_out_with_crlf(tmp_path, examined):
    """Git with autocrlf=true (the Git for Windows default) checks the workflow out with CRLF."""
    repo = make_repo(tmp_path / "repo")
    _apply(repo, build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers").patch, tmp_path)
    wf = repo / WORKFLOW_PATH
    wf.write_bytes(wf.read_bytes().replace(b"\n", b"\r\n"))
    again = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
    examined(1, "a second run over a CRLF workflow")
    assert (again.patch, again.files) == ("", [])


@pytest.fixture
def importer():
    added = []

    def _import(path):
        sys.path.insert(0, str(path))
        added.append(str(path))
        forget_pipeline_modules()
        return importlib.import_module("pipeline.main")

    yield _import
    for p in added:
        sys.path.remove(p)
    forget_pipeline_modules()


def test_a_module_not_running_from_its_repository_says_so(tmp_path, monkeypatch, importer, examined):
    """Installed somewhere without its plan (a non-editable install), the run would be written
    under site-packages; it stops instead, and says why."""
    install_read_memory_if_missing()
    repo = make_repo(tmp_path / "repo")
    _apply(repo, build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers").patch, tmp_path)
    elsewhere = tmp_path / "site-packages"
    shutil.copytree(repo / "pipeline", elsewhere / "pipeline")
    monkeypatch.setenv("ONETRACE_RUN_ID", "x")
    module = importer(elsewhere)
    examined(1, "a run started outside its repository")
    with pytest.raises(RuntimeError, match="not running from its repository"):
        module.run("what does the warranty cover")
    assert not (elsewhere / "runs").exists()


def test_a_stage_value_json_cannot_hold_stops_the_run_naming_the_stage(tmp_path, monkeypatch, importer,
                                                                       examined):
    install_read_memory_if_missing()
    repo = make_repo(tmp_path / "repo", {"pipeline/llm.py":
                                         "def answer(request, passages):\n    return {'tags': {'a', 'b'}}\n"})
    _apply(repo, build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers").patch, tmp_path)
    monkeypatch.setenv("ONETRACE_RUN_ID", "x")
    module = importer(repo)
    examined(1, "a stage returning a set")
    with pytest.raises(TypeError, match=r"stage 'answer'.*set"):
        module.run("what does the warranty cover")
