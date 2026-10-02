"""Every refusal carries its fix: each message the commands can refuse with maps to a code, each
code has its section in docs/errors.md, and each command prints `fix:` and `see:` lines."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from onetrace_ci.errors import CODES, ERRORS_PAGE, explain
from onetrace_ci.instrument import Refused, build_patch
from onetrace_ci.instrument_plan import PlanRefused, parse_instrument_plan
from onetrace_ci.yamlsubset import SubsetError, parse
from tests import (test_instrument_plan, test_instrument_plan_blocks, test_instrument_plan_settings,
                   test_instrument_refusals, test_yamlsubset)
from tests.instrument_fixtures import PLAN, make_repo

ROOT = Path(__file__).resolve().parents[1]


def _refusal_messages(tmp_path_factory) -> list[str]:
    """Every refusal the suite's own refusal cases produce, one message per problem."""
    messages = []
    for name, (overrides, _) in test_instrument_refusals.CASES.items():
        repo = make_repo(tmp_path_factory.mktemp("r"), overrides)
        with pytest.raises(Refused) as caught:
            build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
        messages.extend(caught.value.problems)
    for module, extra in ((test_instrument_plan, None), (test_instrument_plan_blocks, PLAN),
                          (test_instrument_plan_settings, None)):
        for case in module.REFUSALS.values():
            text = case[0] if extra is None else extra + case[0]
            with pytest.raises(PlanRefused) as caught:
                parse_instrument_plan(text, source="<t>")
            messages.extend(caught.value.problems)
    from onetrace_ci.discover import DiscoverRefused, discover, entry_file
    for bad in ("pipeline.main", "pipeline.nowhere:run"):
        with pytest.raises(DiscoverRefused) as caught:
            entry_file(make_repo(tmp_path_factory.mktemp("d")), bad)
        messages.append(str(caught.value))
    import sys as _sys
    with pytest.raises(DiscoverRefused) as caught:
        discover(entry="pipeline.main:run", command=[_sys.executable, "-c", "raise SystemExit(3)"],
                 repo=make_repo(tmp_path_factory.mktemp("d")), out_dir=tmp_path_factory.mktemp("o"))
    messages.append(str(caught.value))
    for text, _ in test_yamlsubset.REFUSED.values():
        with pytest.raises(SubsetError) as caught:
            parse(text, source="<t>")
        messages.append(str(caught.value))
    return messages


def test_every_refusal_message_maps_to_a_code_with_a_fix(tmp_path_factory, examined):
    messages = _refusal_messages(tmp_path_factory)
    examined(len(messages), "refusal messages from the suite's refusal cases")
    unmapped = [m for m in messages if explain(m) is None]
    assert unmapped == [], "refusals without a fix:\n" + "\n".join(unmapped)


def test_every_code_has_its_section_in_the_errors_page(examined):
    page = (ROOT / ERRORS_PAGE).read_text(encoding="utf-8")
    headings = set(re.findall(r"^## (\S+)$", page, re.M))
    examined(len(CODES), "error codes")
    assert {code for code, _, _ in CODES} <= headings
    assert headings <= {code for code, _, _ in CODES}, "the page documents a code the tool never uses"


def _cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "onetrace_ci", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120)


def _assert_fixed(stderr: str):
    problems = [l for l in stderr.splitlines() if l.startswith("  ") and not l.startswith("    ")]
    assert problems, stderr
    for p in problems:
        block = stderr.split(p, 1)[1].splitlines()[1:3]
        assert block[0].startswith("    fix: ") and block[1].startswith(f"    see: {ERRORS_PAGE}#"), stderr


def test_each_command_prints_the_fix_with_its_refusal(tmp_path, examined):
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": PLAN.replace("    trust: operator-authored\n", "")})
    results = {
        "instrument": _cli("instrument", "--style", "wrappers", "--plan", "onetrace-plan.yaml", "--repo", ".", "--out", "p", cwd=repo),
        "gate": _cli("gate", "--run", "nowhere", "--baseline", "nowhere", "--plan", "onetrace-plan.yaml",
                     "--out", "g", cwd=repo),
        "gate, plan outside the subset": _cli("gate", "--run", ".", "--baseline", ".", "--plan", "bad.yaml",
                                              "--out", "g2", cwd=_write(repo, "bad.yaml", "a: |\n  x\n")),
        "baseline propose": _cli("baseline", "propose", "--from", "nowhere", "--out", "b", cwd=repo),
        "init-ci": _cli("init-ci", "--install", "i", "--run", "r", "--run-dir", "runs/{run_id}",
                        "--baseline", "runs/b", "--plan", "../outside.yaml", cwd=repo),
    }
    examined(len(results), "commands refusing through the command line")
    for name, r in results.items():
        assert r.returncode == 1, (name, r.stderr)
        assert "refused" in r.stderr, (name, r.stderr)
        _assert_fixed(r.stderr)


def _write(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    return repo


# --- every refusal site in the code, read statically ---------------------------------------------
#
# The test above checks the messages the suite's own refusal cases produce. A refusal the suite has
# no case for would still print with no `fix:` line, so this one reads the code itself: every
# `raise <refusal>(message)` and every `problems.add(field, message)` in `src/onetrace_ci`. Each
# message's literal text, with every `{...}` part filled by a sample value, must match a code. A
# message passed on from another refusal is checked where it was made.

SRC = Path(__file__).resolve().parents[1] / "src" / "onetrace_ci"
REFUSALS = {"BaselineError", "DiscoverRefused", "GateError", "Refused", "PlanRefused", "PlanError",
            "SubsetError"}
SAMPLES = ("1", "x", "'x'", "a.py")      # a {...} part as a number, a word, a quoted name, a path
PASSED_ON = "passed on"                  # a message made by another refusal, checked where it was made


def _texts(node, function, sample):
    """The message texts an expression can produce, with each {...} part as `sample`; PASSED_ON
    for a message passed on from another refusal; None for a form this reader doesn't know."""
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return ["".join(v.value if isinstance(v, ast.Constant) else sample for v in node.values)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _texts(node.left, function, sample), _texts(node.right, function, sample)
        if left is None or right is None or PASSED_ON in (left, right):
            return None
        return [a + b for a in left for b in right]
    if isinstance(node, ast.IfExp):
        body, orelse = _texts(node.body, function, sample), _texts(node.orelse, function, sample)
        return None if body is None or orelse is None else body + orelse
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for element in node.elts:
            got = _texts(element, function, sample)
            if got is None:
                return None
            out += [] if got == PASSED_ON else got
        return out
    if isinstance(node, ast.ListComp):
        return _texts(node.elt, function, sample)
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "str":
        return PASSED_ON                                     # str(e) of a caught refusal
    if isinstance(node, ast.Attribute) and node.attr in ("problems", "items"):
        return PASSED_ON                                     # e.problems, problems.items
    if isinstance(node, ast.Name):
        if node.id in ("problems", "waits"):
            return PASSED_ON                                 # a list built by problems.add or a refusal
        assigned = [n.value for n in ast.walk(function) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == node.id for t in n.targets)]
        out = []
        for value in assigned:                               # every value the name is given here
            got = _texts(value, function, sample)
            if got is None or got == PASSED_ON:
                return None
            out += got
        return out or None
    return None


def refusal_sites():
    """(file, line, field text or None, the message expression, its function) for every refusal
    site in src/onetrace_ci."""
    import ast
    sites = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] + [tree]:
            for n in ast.walk(function):
                if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and n.exc.args \
                        and getattr(n.exc.func, "id", None) in REFUSALS:
                    sites.append((path.name, n.lineno, None, n.exc.args[0] if n.exc.func.id != "PlanRefused"
                                  else n.exc.args[-1], function))
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add" \
                        and getattr(n.func.value, "id", None) == "problems" and len(n.args) >= 2:
                    sites.append((path.name, n.lineno, n.args[0], n.args[1], function))
    seen, unique = set(), []
    for site in sites:                                       # a site inside a function is found twice
        if site[:2] not in seen:
            seen.add(site[:2])
            unique.append(site)
    return unique


def _unmapped(site):
    """Why a refusal site has no code, or None when every text it can produce maps to one."""
    import ast
    file, line, field, message, function = site
    found_any = False
    for sample in SAMPLES:
        texts = _texts(message, function, sample)
        if texts is None:
            return f"{file}:{line}: a message this test can't read; write it as a literal or an f-string"
        if texts == PASSED_ON:
            return None
        if field is not None:                                # problems.add prints "plan field <field>: <why>"
            field_texts = _texts(field, function, sample)
            prefix = field_texts[0] if isinstance(field_texts, list) and field_texts else sample
            texts = [f"plan field {prefix}: {t}" for t in texts]
        if texts and all(explain(t) for t in texts):
            found_any = True
            break
    return None if found_any else f"{file}:{line}: no code matches {texts[0]!r}"


def test_every_refusal_site_in_the_code_maps_to_a_code_with_a_fix(examined):
    sites = refusal_sites()
    examined(len(sites), "refusal sites in src/onetrace_ci")
    assert len(sites) > 100                                  # the reader found them, not nothing
    unmapped = [why for why in map(_unmapped, sites) if why]
    assert unmapped == [], "\n".join(unmapped)


def test_a_refusal_no_code_covers_still_prints_a_fix_and_the_errors_page(examined):
    from onetrace_ci.errors import format_refusal
    text = format_refusal("gate", ["a refusal no code covers, made up for this test"])
    examined(1, "refusal formatted")
    lines = text.splitlines()
    assert lines[1] == "  a refusal no code covers, made up for this test"
    assert lines[2].startswith("    fix: ") and ERRORS_PAGE in lines[2]
    assert lines[3] == f"    see: {ERRORS_PAGE}"
