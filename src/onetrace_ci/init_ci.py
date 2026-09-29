"""`onetrace-ci init-ci`: the no-plan path, for code decorated or instrumented by hand.

    onetrace-ci init-ci --install CMD --run CMD --run-dir DIR --baseline DIR [--plan P] [--repo R]

It writes two new files and nothing else: the workflow (the same one `onetrace-ci instrument`
writes) and a minimal plan. It asks for nothing it can guess, and guesses nothing it can't:
how to install and run the pipeline, and where its runs and baseline live, are required
options; who approves the baseline is left in the plan as a `DECIDE:` question, and the gate
fails every run until a person answers it. It never overwrites a file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path, PurePosixPath

from onetrace_ci.errors import format_refusal
from onetrace_ci.instrument import WORKFLOW_PATH, render_workflow

APPROVED_BY_QUESTION = "DECIDE: who approves the baseline this gate compares against?"

PLAN_TEXT = f"""\
# Written by `onetrace-ci init-ci`. Answer the DECIDE: question below; the gate fails every run
# against this plan until a person does.
approved_by: "{APPROVED_BY_QUESTION}"
# true makes the gate fail a run that records any field as `undeclared` (nobody stated it).
require_declared: false
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onetrace-ci init-ci")
    parser.add_argument("--install", required=True, help="how CI installs the pipeline")
    parser.add_argument("--run", required=True, help="how CI runs the pipeline on its fixtures")
    parser.add_argument("--run-dir", required=True, help="where a run is written; may hold {run_id}")
    parser.add_argument("--baseline", required=True, help="the committed baseline run")
    parser.add_argument("--plan", default="onetrace-plan.yaml", help="where to write the plan")
    parser.add_argument("--repo", default=".", type=Path)
    args = parser.parse_args(argv)

    repo = args.repo
    plan_rel = PurePosixPath(args.plan.replace("\\", "/"))
    problems = []
    if plan_rel.is_absolute() or ".." in plan_rel.parts:
        problems.append(f"--plan {args.plan}: must be a path inside --repo")
    for rel in (str(plan_rel), WORKFLOW_PATH):
        if (repo / rel).exists():
            problems.append(f"{rel}: already exists; init-ci never overwrites a file")
    if args.run_dir.replace("{run_id}", "onetrace-ci-candidate").rstrip("/") == args.baseline.rstrip("/"):
        problems.append("--run-dir and --baseline: the workflow's run would be written onto the baseline")
    if problems:
        print(format_refusal("init-ci", problems), file=sys.stderr)
        return 1

    workflow = render_workflow(run_dir=args.run_dir, install=args.install, run=args.run,
                               baseline=args.baseline, plan_rel=str(plan_rel),
                               made_by="`onetrace-ci init-ci`")
    for rel, text in ((str(plan_rel), PLAN_TEXT), (WORKFLOW_PATH, workflow)):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        print(f"wrote {rel}")
    print(f"then: answer approved_by in {plan_rel} (a DECIDE: question; the gate fails until a "
          f"person answers it), run the pipeline once, and propose its run as the baseline.")
    print(f"next: onetrace-ci baseline propose --from {args.run_dir.replace('{run_id}', '<run id>')} "
          f"--out {args.baseline}")
    return 0
