"""`onetrace-ci`: `gate`, `baseline propose` and `instrument`."""
from __future__ import annotations

import sys

from onetrace_ci import __version__

USAGE = """\
onetrace-ci {version}

  onetrace-ci --version
  onetrace-ci gate --run R --baseline B --plan P [--runner J] --out D
                    [--review-exit 0|2]
                    turn onetrace's existing commands into one CI verdict.
                    Exit 0 pass, 1 fail, 2 review required (--review-exit 0
                    makes reviews advisory). Writes D/verdict.json and
                    D/summary.md. Never writes, updates or regenerates a
                    baseline.
  onetrace-ci baseline propose --from R --out B.new
                    write a candidate baseline for a human to commit,
                    together with the diff that justifies it.
  onetrace-ci instrument --plan P --repo R --out F
                    write a patch (never an edit in place) that instruments
                    the pipeline the reviewed plan P describes, and print
                    what it will change. Exit 0 with the patch written
                    (empty when already instrumented from P), 1 refused.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        print(__version__)
        return 0
    if argv and argv[:1] == ["gate"]:
        from onetrace_ci.gate import main as gate_main
        return gate_main(argv[1:])
    if argv and argv[:2] == ["baseline", "propose"]:
        from onetrace_ci.baseline import main as baseline_main
        return baseline_main(argv[2:])
    if argv and argv[:1] == ["instrument"]:
        from onetrace_ci.instrument import main as instrument_main
        return instrument_main(argv[1:])
    print(USAGE.format(version=__version__), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
