"""The `onetrace-ci` command line, run as a real subprocess."""
from __future__ import annotations

import subprocess
import sys

from tests.instrument_fixtures import make_repo


def _cli(*args, cwd=None):
    return subprocess.run([sys.executable, "-m", "onetrace_ci", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def test_usage_names_every_command(examined):
    result = _cli()
    commands = ["gate", "baseline propose", "instrument", "init-ci", "discover"]
    examined(len(commands), "commands the usage text must name")
    assert result.returncode == 2
    for c in commands:
        assert f"onetrace-ci {c}" in result.stderr


def test_instrument_runs_from_the_command_line(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    result = _cli("instrument", "--style", "wrappers", "--plan", "onetrace-plan.yaml", "--repo", ".", "--out", "instrument.patch",
                  cwd=repo)
    examined(1, "the patch file written by the command line")
    assert result.returncode == 0, result.stderr
    assert (repo / "instrument.patch").read_text(encoding="utf-8").startswith("diff --git a/pipeline/main.py")
    assert "nothing was changed in place" in result.stdout


def test_instrument_refusal_exits_one_from_the_command_line(tmp_path, examined):
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": "approved_by: alice\n"})
    result = _cli("instrument", "--style", "wrappers", "--plan", "onetrace-plan.yaml", "--repo", ".", "--out", "x.patch", cwd=repo)
    examined(1, "a refused plan run through the command line")
    assert result.returncode == 1
    assert "plan field entry:" in result.stderr
    assert not (repo / "x.patch").exists()
