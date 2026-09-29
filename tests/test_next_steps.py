"""Commands say what comes next: init-ci, instrument and baseline propose each end by printing
the next command to run."""
from __future__ import annotations

from onetrace_ci.baseline import main as baseline_main
from onetrace_ci.init_ci import main as init_ci_main
from onetrace_ci.instrument import main as instrument_main
from tests.instrument_fixtures import make_repo


def _last(capsys) -> str:
    return capsys.readouterr().out.strip().splitlines()[-1]


def test_instrument_ends_with_the_apply_command(tmp_path, capsys, examined):
    repo = make_repo(tmp_path / "repo")
    out = tmp_path / "instrument.patch"
    assert instrument_main(["--plan", str(repo / "onetrace-plan.yaml"), "--repo", str(repo),
                            "--out", str(out)]) == 0
    examined(1, "instrument's last line")
    assert _last(capsys) == f"next: git apply {out}"


def test_init_ci_ends_with_the_baseline_command(tmp_path, capsys, examined):
    assert init_ci_main(["--repo", str(tmp_path), "--install", "i", "--run", "r",
                         "--run-dir", "runs/{run_id}", "--baseline", "runs/baseline"]) == 0
    examined(1, "init-ci's last line")
    assert _last(capsys) == "next: onetrace-ci baseline propose --from runs/<run id> --out runs/baseline"


def test_baseline_propose_ends_with_the_commit_command(make_run, tmp_path, capsys, examined):
    run = make_run("candidate")
    out = tmp_path / "baseline.new"
    assert baseline_main(["--from", str(run), "--out", str(out)]) == 0
    examined(1, "baseline propose's last line")
    assert _last(capsys) == f"next: git add {out} && git commit"
