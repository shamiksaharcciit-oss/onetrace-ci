"""No test can hang the job: every subprocess a test starts has a timeout, and discovery stops a
command that doesn't finish, with a refusal instead of a wait."""
from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

from onetrace_ci import discover

TESTS = Path(__file__).resolve().parent
TIMED = {"run", "call", "check_call", "check_output"}


def _untimed(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
            continue
        name = node.func.attr
        if name == "Popen":
            found.append(f"{path.name}:{node.lineno}: subprocess.Popen, which takes no timeout")
        elif name in TIMED and not any(k.arg == "timeout" for k in node.keywords):
            found.append(f"{path.name}:{node.lineno}: subprocess.{name} without a timeout")
    return found


def test_every_subprocess_a_test_starts_has_a_timeout(examined):
    files = sorted(TESTS.glob("*.py"))
    examined(len(files), "test files")
    assert [u for f in files for u in _untimed(f)] == []


def test_a_command_that_does_not_finish_is_refused_after_the_timeout(tmp_path, monkeypatch, capsys, examined):
    from tests.discover_fixtures import make_discover_repo
    repo, _ = make_discover_repo(tmp_path)
    monkeypatch.setattr(discover, "COMMAND_TIMEOUT", 3)
    started = time.monotonic()
    rc = discover.main(["--entry", "pipeline.main:run", "--repo", str(repo), "--out-dir", str(tmp_path / "out"),
                        "--", sys.executable, "-c", "import time; time.sleep(60)"])
    elapsed = time.monotonic() - started
    err = capsys.readouterr().err
    examined(1, "a discovery over a command that doesn't finish")
    assert rc == 1 and "did not finish within 3 seconds" in err and "fix:" in err
    assert elapsed < 30
    assert not (tmp_path / "out" / "onetrace-plan.draft.yaml").exists()
