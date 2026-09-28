"""`baseline propose` never writes, updates or regenerates the CURRENT
baseline -- only a candidate, alongside the diff that justifies it, when
there is a prior baseline to diff against at all.
"""
from __future__ import annotations

from onetrace_ci.baseline import BaselineError, propose


def test_propose_copies_the_run_and_leaves_the_old_baseline_untouched(make_run, tmp_path, examined):
    baseline = make_run("baseline")
    baseline_snapshot = {p.relative_to(baseline): p.read_bytes()
                         for p in baseline.rglob("*") if p.is_file()}
    candidate_run = make_run("candidate", retrieve_text="a new passage")
    out = tmp_path / "baseline.new"

    diff_out = propose(from_run=candidate_run, baseline=baseline, out=out)

    examined(len(baseline_snapshot), "files in the original baseline, checked byte-for-byte")
    for rel, data in baseline_snapshot.items():
        assert (baseline / rel).read_bytes() == data, f"the OLD baseline changed at {rel}"
    assert (out / "MANIFEST.json").is_file()
    assert (diff_out / "diff.json").is_file()


def test_propose_with_no_baseline_writes_the_candidate_and_nothing_to_compare(
        make_run, tmp_path, examined):
    """A project's very first baseline: `--baseline` is optional. Without
    it, `propose` writes the candidate and returns `None` rather than
    failing on a comparison that cannot exist yet.
    """
    candidate_run = make_run("candidate")
    out = tmp_path / "baseline.new"
    examined(1, "a propose call with no prior baseline at all")
    diff_out = propose(from_run=candidate_run, baseline=None, out=out)
    assert diff_out is None
    assert (out / "MANIFEST.json").is_file()


def test_cli_reports_first_baseline_nothing_to_compare(tmp_path, make_run, examined, capsys):
    from onetrace_ci.baseline import main as baseline_main

    candidate_run = make_run("candidate")
    out = tmp_path / "baseline.new"
    examined(1, "the CLI's own stdout with no --baseline given")
    rc = baseline_main(["--from", str(candidate_run), "--out", str(out)])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "first baseline, nothing to compare" in out_text


def test_propose_refuses_to_overwrite_an_existing_out_path(make_run, tmp_path, examined):
    baseline = make_run("baseline")
    candidate_run = make_run("candidate")
    out = tmp_path / "baseline.new"
    out.mkdir()
    examined(1, "a propose call whose --out already exists")
    try:
        propose(from_run=candidate_run, baseline=baseline, out=out)
        assert False, "propose did not refuse an existing --out path"
    except BaselineError:
        pass


def test_sabotage_removing_the_exists_check_would_silently_overwrite(make_run, tmp_path, examined):
    """The named sabotage: a stand-in without the `out.exists()` guard does
    NOT refuse -- proving the real function's own refusal is load-bearing.
    """
    import shutil

    def sabotaged_propose(*, from_run, baseline, out):
        shutil.copytree(from_run, out, dirs_exist_ok=True)   # sabotage: no existence check

    baseline = make_run("baseline")
    candidate_run = make_run("candidate")
    out = tmp_path / "baseline.new"
    out.mkdir()
    (out / "pre-existing-marker.txt").write_text("should not survive a real propose", encoding="utf-8")

    examined(1, "a pre-existing marker file in --out, through the sabotaged stand-in")
    sabotaged_propose(from_run=candidate_run, baseline=baseline, out=out)
    assert (out / "pre-existing-marker.txt").is_file(), "sabotage itself misbehaved"

    try:
        propose(from_run=candidate_run, baseline=baseline, out=out)
        assert False, "the real propose() did not refuse where the sabotage let it through"
    except BaselineError:
        pass
