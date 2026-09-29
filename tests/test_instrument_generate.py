"""What the generated patch contains, checked on the patched source itself (parsed with `ast`)
rather than on the generator's own intentions."""
from __future__ import annotations

import ast
import importlib.metadata
import re
import subprocess

import pytest
import yaml

from onetrace_ci.instrument import GATE_ACTION, build_patch
from tests.instrument_fixtures import make_repo

SHA_RE = re.compile(r"[0-9a-f]{40}")


@pytest.fixture
def patched(tmp_path):
    """(repo, result, patched entry source, patched entry AST), with the patch applied by git."""
    repo = make_repo(tmp_path / "repo")
    result = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo)
    patch = tmp_path / "instrument.patch"
    patch.write_bytes(result.patch.encode("utf-8"))
    applied = subprocess.run(["git", "apply", "--verbose", str(patch)], cwd=repo,
                             capture_output=True, text=True, timeout=60)
    assert applied.returncode == 0, applied.stderr
    source = (repo / "pipeline" / "main.py").read_text(encoding="utf-8")
    return repo, result, source, ast.parse(source)


def _calls(node, name: str) -> list[ast.Call]:
    """Calls whose callee is `name` or ends in `.name`."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == name) or (isinstance(f, ast.Attribute) and f.attr == name):
                out.append(n)
    return out


def _entry(tree: ast.Module) -> ast.FunctionDef:
    [fn] = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"]
    return fn


def test_the_patch_changes_the_entry_module_and_adds_the_workflow_and_nothing_else(patched, examined):
    repo, result, _, _ = patched
    examined(len(result.files), "files the patch touches")
    assert result.files == ["pipeline/main.py", ".github/workflows/onetrace.yml"]
    for untouched in ("pipeline/retrieval.py", "pipeline/llm.py", "pipeline/__init__.py"):
        assert f"a/{untouched}" not in result.patch


def test_one_recorder_per_run_created_inside_the_entry_and_closed_in_finally(patched, examined):
    _, _, _, tree = patched
    module_level = [n for stmt in tree.body if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    for n in _calls(stmt, "_OnetraceRecorder") + _calls(stmt, "Recorder")]
    entry = _entry(tree)
    inside = _calls(entry, "_OnetraceRecorder")
    examined(len(inside), "Recorder constructions inside the entry function")
    assert module_level == [], "a Recorder is constructed at module level"
    assert len(inside) == 1

    #: The construction is a top-level statement of the entry, immediately followed (after the
    #: artifacts dict) by the try whose finally closes that same recorder.
    body = entry.body[1:] if ast.get_docstring(entry) else entry.body
    assign = next(s for s in body if isinstance(s, ast.Assign) and _calls(s, "_OnetraceRecorder"))
    rec_name = assign.targets[0].id
    [try_stmt] = [s for s in body if isinstance(s, ast.Try)]
    assert body.index(try_stmt) > body.index(assign)
    closes = [c for c in _calls(ast.Module(body=try_stmt.finalbody, type_ignores=[]), "close")
              if isinstance(c.func.value, ast.Name) and c.func.value.id == rec_name]
    assert len(closes) == 1


def test_the_patch_adds_no_trailing_whitespace_and_two_blank_lines_after_the_helpers(patched, examined):
    _, result, source, _ = patched
    added = [l for l in result.patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
    examined(len(added), "lines the patch adds")
    assert [l for l in added if l != l.rstrip()] == []
    lines = source.splitlines()
    first_after = next(i for i, l in enumerate(lines) if l.startswith("def run("))
    assert lines[first_after - 2:first_after] == ["", ""]


def test_the_docstring_stays_first(patched, examined):
    _, _, _, tree = patched
    examined(1, "the entry function's docstring")
    assert ast.get_docstring(_entry(tree)) == "Retrieve, then answer."


def test_stages_are_declared_and_run_in_the_plans_order(patched, examined):
    _, _, _, tree = patched
    entry = _entry(tree)
    [rec_call] = _calls(entry, "_OnetraceRecorder")
    declared = next(k.value for k in rec_call.keywords if k.arg == "declared_stages")
    assert [e.value for e in declared.elts] == ["intake", "retrieve", "answer"]

    [try_stmt] = [s for s in entry.body if isinstance(s, ast.Try)]
    order = []
    for stmt in try_stmt.body:
        if isinstance(stmt, ast.FunctionDef):
            continue
        for c in ast.walk(stmt):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id.startswith("_onetrace_stage_"):
                order.append((c.lineno, c.func.id.removeprefix("_onetrace_stage_")))
    examined(len(order), "stage calls in the rewritten entry body")
    assert [name for _, name in sorted(order)] == ["intake", "retrieve", "answer"]


def test_memory_inputs_are_recorded_through_one_function_only(patched, examined):
    """`ctx.read_memory` (onetrace 0.1.2) is called in exactly one place in the generated code,
    so a change to its signature is a one-line change."""
    _, _, _, tree = patched
    reads = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "read_memory"]
    examined(len(reads), "read_memory references in the patched module")
    assert len(reads) == 1
    [holder] = [f for f in tree.body if isinstance(f, ast.FunctionDef)
                and any(n is reads[0] for n in ast.walk(f))]
    assert holder.name == "_onetrace_read_memory"
    [call] = _calls(_entry(tree), "_onetrace_read_memory")
    args = [a.value if isinstance(a, ast.Constant) else getattr(a, "id", None) for a in call.args[1:]]
    assert args == ["request", "request", "externally-sourced"]


def test_planned_files_are_read_with_read_external_and_their_trust_class(patched, examined):
    _, _, _, tree = patched
    calls = _calls(_entry(tree), "read_external")
    examined(len(calls), "read_external calls")
    assert len(calls) == 1
    kw = {k.arg: k.value.value for k in calls[0].keywords}
    assert kw == {"name": "data/corpus.json", "trust_class": "operator-authored"}
    assert calls[0].args[1].value == "application/json"


def test_instrument_versions_are_read_at_run_time_and_never_typed(patched, examined):
    _, _, source, tree = patched
    instruments = _calls(_entry(tree), "_OnetraceInstrument")
    examined(len(instruments), "Instrument constructions")
    assert len(instruments) == 3
    packages = []
    for call in instruments:
        version = call.args[2]
        assert isinstance(version, ast.Call), ast.dump(version)
        assert ast.unparse(version.func) == "_onetrace_metadata.version"
        packages.append(version.args[0].value)
    assert packages == ["onetrace", "onetrace-verify", "onetrace"]
    for pkg in set(packages):
        installed = importlib.metadata.version(pkg)
        assert f'"{installed}"' not in source and f"'{installed}'" not in source


def test_rederivability_and_its_note_come_from_the_plan(patched, examined):
    _, _, _, tree = patched
    found = {}
    for call in _calls(_entry(tree), "_OnetraceInstrument"):
        kw = {k.arg: k.value.value for k in call.keywords}
        found[call.args[0].value] = kw
    examined(len(found), "instruments checked for rederivability")
    assert found["word-overlap"] == {"rederivable": "true"}
    assert found["extractive"] == {"rederivable": "false",
                                   "rederivable_note": "a stand-in for a hosted model"}


def test_the_workflow_runs_the_pipeline_then_the_gate_with_contents_read(patched, examined):
    repo, _, _, _ = patched
    wf = yaml.safe_load((repo / ".github" / "workflows" / "onetrace.yml").read_text(encoding="utf-8"))
    assert wf["permissions"] == {"contents": "read"}
    [job] = wf["jobs"].values()
    steps = job["steps"]
    examined(len(steps), "workflow steps")
    uses = [s["uses"] for s in steps if "uses" in s]
    for ref in uses:
        assert SHA_RE.fullmatch(ref.split("@", 1)[1]), f"{ref} is not pinned to a commit SHA"
    assert uses[-1] == GATE_ACTION and re.fullmatch(r"[\w.-]+/onetrace-ci@[0-9a-f]{40}", GATE_ACTION)
    names = [s.get("name", s.get("uses")) for s in steps]
    run_i = names.index("Run the pipeline")
    gate_i = names.index("onetrace-ci gate")
    assert names.index("Install the pipeline") < run_i < gate_i
    assert steps[run_i]["env"] == {"ONETRACE_RUN_ID": "onetrace-ci-candidate"}
    assert steps[run_i]["run"].strip() == \
        "python -c \"from pipeline.main import run; run('what does the warranty cover')\""
    assert steps[gate_i]["with"] == {"run": "runs/onetrace-ci-candidate", "baseline": "runs/baseline",
                                     "plan": "onetrace-plan.yaml"}


def test_what_it_will_change_is_printed(tmp_path, examined, capsys):
    from onetrace_ci.instrument import main

    repo = make_repo(tmp_path / "repo")
    out = tmp_path / "instrument.patch"
    rc = main(["--plan", str(repo / "onetrace-plan.yaml"), "--repo", str(repo), "--out", str(out)])
    printed = capsys.readouterr().out
    wanted = ["pipeline/main.py", ".github/workflows/onetrace.yml", "intake", "retrieve", "answer",
              "data/corpus.json", "ctx.read_memory", "nothing was changed in place"]
    examined(len(wanted), "things the summary must mention")
    assert rc == 0 and out.is_file()
    for w in wanted:
        assert w in printed, f"{w!r} not in:\n{printed}"
    #: Printing is all it does to the repository: the source is untouched.
    assert "_onetrace_" not in (repo / "pipeline" / "main.py").read_text(encoding="utf-8")
