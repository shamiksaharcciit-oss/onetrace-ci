"""`onetrace-ci baseline propose`. The gate never writes, updates or
regenerates a baseline -- this is the one place a NEW candidate baseline is
produced, and even here it is written for a human to review and commit,
never committed by this tool itself.

A NOTE ON SCOPE, HONESTLY STATED
----------------------------------
The originally documented CLI line is `onetrace-ci baseline propose --from R
--out B.new`. Producing "the diff that justifies it" needs to know what
`B.new` is proposed to REPLACE, which that two-flag form does not name on
its own -- an optional `--baseline B` is added here for exactly that reason.
Without it, there is no prior baseline to compare against at all (a
project's very first one), so `propose` writes the candidate and says so,
rather than failing on a comparison that cannot exist yet.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from onetrace_ci.gate import _SUBPROCESS_TIMEOUT_SECONDS, _console_script


class BaselineError(RuntimeError):
    pass


def propose(*, from_run: Path, baseline: Path | None, out: Path) -> Path | None:
    """Copies `from_run` to `out` (refusing if `out` already exists -- a
    candidate baseline is never silently overwritten). With `baseline`
    given, also writes the diff against it alongside `out`, at `out`'s own
    `.diff` sibling, and returns that directory. With `baseline` omitted
    (a project's first baseline), writes only the candidate and returns
    `None` -- there is nothing yet to compare it against. Never touches
    `baseline` itself.
    """
    if not from_run.is_dir():
        raise BaselineError(f"--from {from_run}: not a directory")
    if baseline is not None and not baseline.is_dir():
        raise BaselineError(f"--baseline {baseline}: not a directory")
    if out.exists():
        raise BaselineError(f"--out {out}: already exists -- a candidate baseline "
                            f"is never written over an existing path")

    shutil.copytree(from_run, out)

    if baseline is None:
        return None

    diff_out = out.parent / f"{out.name}.diff"
    argv = [_console_script("onetrace"), "diff", str(baseline), str(out),
            "--out", str(diff_out), "--quiet"]
    subprocess.run(argv, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_SECONDS)
    return diff_out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="onetrace-ci baseline propose")
    parser.add_argument("--from", dest="from_run", required=True, type=Path)
    parser.add_argument("--baseline", required=False, default=None, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        diff_out = propose(from_run=args.from_run, baseline=args.baseline, out=args.out)
    except BaselineError as e:
        print(f"onetrace-ci baseline propose: refused: {e}", file=sys.stderr)
        return 1
    print(f"candidate baseline written to {args.out}")
    if diff_out is None:
        print("first baseline, nothing to compare")
    else:
        print(f"diff against the current baseline written to {diff_out}")
    print("nothing has been committed -- review, then commit the new baseline by hand")
    return 0
