"""The generated patch for the fixture pipeline, byte for byte. A change to what `instrument`
generates shows up here as a diff to review, never silently. Regenerate the golden file only
on purpose."""
from __future__ import annotations

from pathlib import Path

from onetrace_ci.instrument import build_patch
from tests.instrument_fixtures import make_repo

GOLDEN = Path(__file__).resolve().parent / "golden" / "fixture_instrument.patch"


def test_the_fixture_patch_is_byte_identical_to_the_golden_file(tmp_path, examined):
    repo = make_repo(tmp_path / "repo")
    patch = build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers").patch.encode("utf-8")
    golden = GOLDEN.read_bytes()
    examined(len(golden.splitlines()), "lines of the golden patch")
    assert patch == golden
