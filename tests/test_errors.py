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
            build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo)
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
        "instrument": _cli("instrument", "--plan", "onetrace-plan.yaml", "--repo", ".", "--out", "p", cwd=repo),
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
