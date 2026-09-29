"""The docs are checked against the code. Every code block either runs or is marked
`<!-- not executed -->`; every onetrace-ci command and flag a block names exists; every verdict the
docs name is one the gate gives."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from onetrace_ci.gate import FAIL, PASS, REVIEW, WARN
from onetrace_ci.instrument_plan import parse_instrument_plan
from onetrace_ci.plan import parse_plan_text
from tests.instrument_fixtures import make_repo

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "AGENTS.md", "docs/agents/ci.md", "docs/errors.md"]
BLOCK_RE = re.compile(r"(?:^(<!-- [^>]+ -->)\n)?^```(\w*)\n(.*?)^```", re.M | re.S)
SUBCOMMANDS = {"gate": ["gate"], "baseline": ["baseline", "propose"], "instrument": ["instrument"],
               "init-ci": ["init-ci"], "discover": ["discover"]}


def _blocks():
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for m in BLOCK_RE.finditer(text):
            line = text.count("\n", 0, m.start(2) if m.group(1) is None else m.start()) + 1
            yield doc, line, m.group(1), m.group(2), m.group(3)


BLOCKS = list(_blocks())


def test_the_docs_have_code_blocks_to_check(examined):
    examined(len(BLOCKS), "code blocks in the docs")


@pytest.mark.parametrize("doc,line,marker,lang,body", BLOCKS,
                         ids=[f"{b[0]}:{b[1]}" for b in BLOCKS])
def test_each_block_runs_or_is_marked(doc, line, marker, lang, body, examined):
    examined(1, f"the code block at {doc}:{line}")
    if lang == "yaml":
        #: A plan block runs by being read: an instrument plan must be complete, a gate plan valid.
        if "stages:" in body:
            parse_instrument_plan(body, source=f"{doc}:{line}")
        else:
            parse_plan_text(body, source=f"{doc}:{line}")
        return
    assert marker in ("<!-- not executed -->", "<!-- run in a fixture repo -->"), (
        f"{doc}:{line}: a code block that is not a plan must run or say <!-- not executed -->")


def _help(sub: list[str]) -> str:
    r = subprocess.run([sys.executable, "-m", "onetrace_ci", *sub, "--help"], capture_output=True,
                       text=True, timeout=60)
    return r.stdout


def test_every_command_and_flag_named_in_a_block_exists(examined):
    checked = 0
    for doc, line, _, lang, body in BLOCKS:
        for cmd in re.findall(r"onetrace-ci (?:[^\n\\]|\\\n)*", body):
            #: Split as a shell would, so a quoted argument's own flags are not taken for ours.
            words = shlex.split(cmd.replace("\\\n", " "))
            sub = words[1]
            assert sub in SUBCOMMANDS, f"{doc}:{line}: `onetrace-ci {sub}` is not a command"
            usage = _help(SUBCOMMANDS[sub])
            for flag in (w.split("=")[0] for w in words if w.startswith("--")):
                assert flag in usage, f"{doc}:{line}: {flag} is not a flag of onetrace-ci {sub}"
                checked += 1
    examined(checked, "flags named in the docs' code blocks")


def test_every_verdict_the_docs_name_is_one_the_gate_gives(examined):
    verdicts = {PASS, FAIL, REVIEW, WARN}
    named = []
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        named += [(doc, w) for w in re.findall(r"\*\*`(\w+)`\*\*", text)]
    examined(len(named), "verdicts named in bold in the docs")
    for doc, word in named:
        assert word in verdicts, f"{doc}: `{word}` is not a verdict the gate gives"


def test_the_blocks_marked_to_run_do_run(tmp_path, examined):
    """In one fixture repository, in document order: the README's instrument walkthrough."""
    repo = make_repo(tmp_path / "repo")
    ran = 0
    for doc, line, marker, _, body in BLOCKS:
        if marker != "<!-- run in a fixture repo -->":
            continue
        for command in body.strip().splitlines():
            argv = shlex.split(command)
            if argv[0] == "onetrace-ci":
                argv = [sys.executable, "-m", "onetrace_ci", *argv[1:]]
            r = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, f"{doc}:{line}: {command}\n{r.stdout}\n{r.stderr}"
            ran += 1
    examined(ran, "commands run from the docs")
