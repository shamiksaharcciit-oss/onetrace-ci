"""`onetrace-ci gate`: phase 1 of the CI instrumenter. Turns onetrace's
existing commands into one CI verdict. Calls the installed `onetrace-verify`
and `onetrace` as
subprocesses -- never their internals -- and maps each exit code exactly as
the gate table says. `onetrace` itself is never modified and never
imported as a library here on purpose: a subprocess boundary is the same
"call the verb, read its own exit code and report" discipline the SDK's own
`diff`/`localize`/`reproduce` already use against the verifier, one level up.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from onetrace_ci.errors import format_refusal
from onetrace_ci.plan import Plan, PlanError, load_plan

PASS, FAIL, REVIEW, WARN = "pass", "fail", "review", "warn"
_RANK = {PASS: 0, WARN: 1, REVIEW: 2, FAIL: 3}


def _console_script(name: str) -> str:
    """Resolve `name` (`onetrace-verify` or `onetrace`) relative to
    `sys.executable`'s OWN Scripts/bin directory -- never bare PATH
    resolution. A bare name would let an unrelated, ambient install
    elsewhere on PATH silently answer instead of the one actually pinned
    alongside this gate (`requirements.lock`) -- found the hard way, when a
    stray system-wide install answered a subprocess call meant for this
    project's own venv and made a plain `--version` call hang.
    """
    scripts_dir = Path(sys.executable).parent
    suffix = ".exe" if sys.platform == "win32" else ""
    candidate = scripts_dir / f"{name}{suffix}"
    return str(candidate) if candidate.is_file() else name


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    command: str
    exit_code: int | None
    report_path: str | None
    verdict: str
    detail: str = ""

    def to_json(self) -> dict:
        return asdict(self)


class GateError(RuntimeError):
    """The gate itself could not run at all (a bad run/baseline path, a
    plan file outside the supported subset) -- distinct from a FAIL
    finding, which means the gate ran and found something wrong.
    """


#: A CI gate that can hang is worse than one that fails fast and loud -- a
#: stuck subprocess (an unrelated, ambient install answering instead of the
#: pinned one; a genuinely wedged process) must never block a pipeline
#: forever. `TimeoutExpired` is not caught here: it is deliberately allowed
#: to propagate out of `run_gate` as an uncaught exception, which is a
#: harder failure than any FAIL finding -- exactly right for "the gate itself
#: could not run", not "the gate ran and found something wrong".
_SUBPROCESS_TIMEOUT_SECONDS = 120


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=_SUBPROCESS_TIMEOUT_SECONDS)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _coverage_finding(run: Path, plan: Plan) -> Finding:
    manifest_path = run / "MANIFEST.json"
    manifest = _read_json(manifest_path)
    unapproved = []
    for entry in (manifest.get("boundaries") or []):
        name = entry.get("name")
        if name not in plan.approved_boundaries:
            unapproved.append(("boundary", name, entry.get("note", "")))
    for entry in (manifest.get("gaps") or []):
        name = entry.get("stage")
        if name not in plan.approved_boundaries:
            unapproved.append(("gap", name, entry.get("reason", "")))
    if unapproved:
        detail = "; ".join(f"{kind} at {name!r}: {note}" for kind, name, note in unapproved)
        return Finding(check="coverage", command=f"read {manifest_path}", exit_code=None,
                      report_path=str(manifest_path), verdict=FAIL,
                      detail=f"not in approved_boundaries: {detail}")
    return Finding(check="coverage", command=f"read {manifest_path}", exit_code=None,
                  report_path=str(manifest_path), verdict=PASS,
                  detail="every boundary/gap (if any) is in approved_boundaries")


def _undeclared_fields(run: Path) -> tuple[int, list[str]]:
    """(receipts read, fields nobody stated). Decorated code records a field left out by a
    person as its cautious default value, and names it in the stage's `assertions.undeclared`
    list (for example `["trust", "rederivable"]`); that list is what is read here."""
    fields, count = [], 0
    for p in sorted((run / "receipts").glob("*.json")):
        receipt = _read_json(p)
        count += 1
        stage = (receipt.get("stage") or {}).get("name")
        undeclared = (receipt.get("assertions") or {}).get("undeclared") or []
        fields += [f"stage {stage!r}: {field}" for field in undeclared]
    return count, fields


def _declared_finding(run: Path, plan: Plan) -> Finding:
    count, fields = _undeclared_fields(run)
    command = f"read {run / 'receipts'}"
    if plan.require_declared and fields:
        return Finding(check="declared", command=command, exit_code=None, report_path=None,
                       verdict=FAIL, detail="undeclared, and the plan requires every field declared "
                                            "(require_declared: true): " + "; ".join(fields))
    if plan.require_declared:
        return Finding(check="declared", command=command, exit_code=None, report_path=None,
                       verdict=PASS, detail=f"every trust class and re-derivability is declared "
                                            f"({count} receipts read)")
    listed = (": " + "; ".join(fields)) if fields else ""
    return Finding(check="declared", command=command, exit_code=None, report_path=None, verdict=PASS,
                   detail=f"{len(fields)} undeclared field(s){listed} (require_declared: false)")


def _verify_finding(run: Path) -> Finding:
    argv = [_console_script("onetrace-verify"), "--require-artifacts", str(run)]
    result = _run(argv)
    verdict = PASS if result.returncode == 0 else FAIL
    return Finding(check="verify", command=" ".join(argv), exit_code=result.returncode,
                  report_path=None, verdict=verdict,
                  detail=result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "")


def _differs_sentence(differs: dict) -> str:
    """The diff report's `differs` ({field: [old, new]}) as words: "field old -> new", one
    clause per field in key order. "->" rather than an arrow character, so the line prints
    on any console encoding."""
    parts = []
    for field in sorted(differs):
        value = differs[field]
        if isinstance(value, (list, tuple)) and len(value) == 2:
            parts.append(f"{field} {value[0]} -> {value[1]}")
        else:
            parts.append(f"{field} {value}")
    return "; ".join(parts)


def _diff_findings(baseline: Path, run: Path, out: Path, plan: Plan) -> list[Finding]:
    diff_out = out / "diff"
    argv = [_console_script("onetrace"), "diff", str(baseline), str(run), "--out", str(diff_out), "--quiet"]
    result = _run(argv)
    report_path = diff_out / "diff.json"
    findings: list[Finding] = []
    command = " ".join(argv)

    if result.returncode in (0, 1):
        report = _read_json(report_path)
        annotations = {a["stage"]: a for a in report.get("annotations", [])}
        same_stages = {s["stage"] for s in report.get("ladder", []) if s["verdict"] == "same"}
        for stage in sorted(same_stages & annotations.keys()):
            findings.append(Finding(
                check=f"diff: instrument/config annotation at stage {stage!r}",
                command=command, exit_code=result.returncode, report_path=str(report_path),
                verdict=REVIEW, detail=_differs_sentence(annotations[stage]["differs"])))

    if result.returncode == 0:
        findings.append(Finding(check="diff", command=command, exit_code=0,
                                report_path=str(report_path), verdict=PASS,
                                detail="identical"))
    elif result.returncode == 1:
        report = _read_json(report_path)
        first = report.get("first_difference") or {}
        first_stage = first.get("stage") if isinstance(first, dict) else first
        findings.append(Finding(check="diff", command=command, exit_code=1,
                                report_path=str(report_path), verdict=REVIEW,
                                detail=f"first difference at stage {first_stage!r}"))
        localize_out = out / "localize"
        localize_argv = [_console_script("onetrace"), "localize", str(baseline), str(run),
                         "--out", str(localize_out), "--quiet"]
        localize_result = _run(localize_argv)
        findings.append(Finding(check="localize", command=" ".join(localize_argv),
                                exit_code=localize_result.returncode,
                                report_path=str(localize_out / "localize.json"),
                                verdict=REVIEW, detail="attached for the reviewer"))
    elif result.returncode == 2:
        findings.append(Finding(check="diff", command=command, exit_code=2,
                                report_path=str(report_path) if report_path.is_file() else None,
                                verdict=FAIL, detail="not comparable"))
    elif result.returncode == 3:
        findings.append(Finding(check="diff", command=command, exit_code=3,
                                report_path=str(report_path) if report_path.is_file() else None,
                                verdict=FAIL, detail="refused"))
    elif result.returncode == 4:
        report = _read_json(report_path)
        for stage_entry in report.get("could_not_check_stages", []) or []:
            stage = stage_entry if isinstance(stage_entry, str) else stage_entry.get("stage")
            waived = stage in plan.known_limits
            findings.append(Finding(
                check=f"diff: could not check stage {stage!r}", command=command,
                exit_code=4, report_path=str(report_path),
                verdict=PASS if waived else FAIL,
                detail="in known_limits" if waived else "not in known_limits"))
        if not report.get("could_not_check_stages"):
            findings.append(Finding(check="diff", command=command, exit_code=4,
                                    report_path=str(report_path), verdict=FAIL,
                                    detail="could not check, and named no stage to waive"))
    else:
        findings.append(Finding(check="diff", command=command, exit_code=result.returncode,
                                report_path=None, verdict=FAIL,
                                detail=f"unrecognised exit code {result.returncode}"))
    return findings


def _reproduce_finding(run: Path, runner: Path | None, out: Path) -> Finding:
    reproduce_out = out / "reproduce"
    argv = [_console_script("onetrace"), "reproduce", str(run), "--out", str(reproduce_out), "--quiet"]
    if runner is not None:
        argv[3:3] = ["--runner", str(runner)]
    result = _run(argv)
    report_path = reproduce_out / "reproduce.json"
    if result.returncode == 0:
        verdict, detail = PASS, "all REPRODUCED"
    elif result.returncode == 1:
        verdict, detail = FAIL, "at least one stage DIVERGED"
    elif result.returncode == 4:
        verdict, detail = WARN, "at least one stage COULD NOT CHECK, none DIVERGED"
    else:
        #: Not named in the gate table at all (2/3, "refused"/"not applicable") --
        #: nothing unchecked is ever a pass, so an exit this table does not
        #: name is treated as a fail, not silently passed through.
        verdict, detail = FAIL, f"exit {result.returncode}, not named in the gate table"
    return Finding(check="reproduce", command=" ".join(argv), exit_code=result.returncode,
                  report_path=str(report_path) if report_path.is_file() else None,
                  verdict=verdict, detail=detail)


def run_gate(*, run: Path, baseline: Path, plan_path: Path, runner: Path | None,
            out: Path, review_exit_zero: bool) -> tuple[int, list[Finding]]:
    """Returns `(exit_code, findings)`. Raises `GateError` for anything that
    stops the gate from running at all -- never for a finding the gate
    itself produced, which is reported, not raised.
    """
    out.mkdir(parents=True, exist_ok=True)
    if not run.is_dir():
        raise GateError(f"--run {run}: not a directory")
    if not baseline.is_dir():
        raise GateError(f"--baseline {baseline}: not a directory")

    plan = load_plan(plan_path)
    findings: list[Finding] = []

    if not plan.approved_by.strip():
        findings.append(Finding(check="plan.approved_by", command=f"read {plan_path}",
                                exit_code=None, report_path=str(plan_path), verdict=FAIL,
                                detail="missing or empty -- a gate against an unapproved "
                                      "plan proves nothing"))

    for field, question in plan.open_questions:
        findings.append(Finding(check=f"plan.{field}", command=f"read {plan_path}", exit_code=None,
                                report_path=str(plan_path), verdict=FAIL,
                                detail=f"still an open DECIDE: question ({question!r}); a person "
                                       f"answers it before a gate against this plan can pass"))

    findings.append(_verify_finding(run))
    findings.append(_coverage_finding(run, plan))
    findings.append(_declared_finding(run, plan))
    findings.extend(_diff_findings(baseline, run, out, plan))
    if plan.reproduce:
        findings.append(_reproduce_finding(run, runner, out))

    worst = max((_RANK[f.verdict] for f in findings), default=0)
    if worst == _RANK[FAIL]:
        exit_code = 1
    elif worst == _RANK[REVIEW]:
        exit_code = 0 if review_exit_zero else 2
    else:
        exit_code = 0

    verdict_path = out / "verdict.json"
    verdict_path.write_text(json.dumps({
        "format": "onetrace-ci-verdict/0.1",
        "exit": exit_code,
        #: The folders, as given: with one command for every entry, runs share one id.
        "run": str(run),
        "baseline": str(baseline),
        "plan": {"ignored_keys": list(plan.ignored_keys), "approved_by": plan.approved_by,
                 **({"ungated": list(plan.ungated)} if plan.ungated else {})},
        "findings": [f.to_json() for f in findings],
    }, indent=1, sort_keys=True), encoding="utf-8")

    summary_lines = ["# onetrace-ci gate", "",
                     f"**verdict:** {'PASS' if exit_code == 0 else 'REVIEW REQUIRED' if exit_code == 2 else 'FAIL'} (exit {exit_code})",
                     "",
                     f"**run:** `{run}`, against **baseline:** `{baseline}`",
                     ""]
    if plan.ignored_keys:
        summary_lines.append(f"Ignored plan key(s), not part of this schema: {list(plan.ignored_keys)}")
        summary_lines.append("")
    for entry in plan.ungated:
        summary_lines.append(f"Not gated (ci.baseline is null): {entry}")
    if plan.ungated:
        summary_lines.append("")
    summary_lines.append("| check | verdict | detail |")
    summary_lines.append("|---|---|---|")
    for f in findings:
        summary_lines.append(f"| {f.check} | {f.verdict} | {f.detail} |")
    (out / "summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    return exit_code, findings


#: A query run's corpus link: the ingest run it read. onetrace names a changed link "corpus link"
#: and never counts it as a setting, so neither does the summary line.
LINK_KEY = "assertions.corpus_manifest"


def _short(digest) -> str:
    """A digest as onetrace's reports shorten it, its first 12 hex characters; `(none)` for none."""
    return "(none)" if digest is None else str(digest).removeprefix("sha256:")[:12]


def _link_change(differs: dict) -> str:
    """A changed corpus link as `old -> new`, by short digests; empty when it didn't change."""
    value = differs.get(LINK_KEY)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{_short(value[0])} -> {_short(value[1])}"
    return ""


def _setting_changes(differs: dict) -> str:
    """The settings a diff annotation says changed, as `name old -> new`; digests and the corpus
    link (not a setting) left out."""
    parts = []
    for field_name in sorted(differs):
        if field_name.endswith("_digest") or field_name == LINK_KEY:
            continue
        value = differs[field_name]
        name = field_name.removeprefix("assertions.constants.")
        if isinstance(value, (list, tuple)) and len(value) == 2:
            parts.append(f"{name} {value[0]} -> {value[1]}")
        else:
            parts.append(f"{name} {value}")
    return "; ".join(parts)


def summary_line(findings: list[Finding], out: Path) -> str:
    """What happened, in one line, for a person reading the log: printed first, in human mode
    only. The first failing check; else the first difference and the setting that changed; else
    how many stages are the same as the baseline."""
    fails = [f for f in findings if f.verdict == FAIL]
    if fails:
        return f"fail: {fails[0].check}: {fails[0].detail}"
    report = out / "diff" / "diff.json"
    data = _read_json(report) if report.is_file() else {}
    annotated = {a.get("stage"): a.get("differs") or {} for a in data.get("annotations") or []}
    changes = {stage: _setting_changes(d) for stage, d in annotated.items()}
    links = {stage: _link_change(d) for stage, d in annotated.items()}
    diff = next((f for f in findings if f.check == "diff"), None)
    if diff is not None and diff.verdict == REVIEW:
        first = data.get("first_difference") or {}
        stage = first.get("stage") if isinstance(first, dict) else first
        what = "; ".join(p for p in (changes.get(stage), links.get(stage) and f"corpus link {links[stage]}") if p)
        return f'review: first difference at stage "{stage}"' + (f" ({what})" if what else "")
    reviews = [f for f in findings if f.verdict == REVIEW]
    if reviews:
        same = [s["stage"] for s in data.get("ladder") or [] if s.get("verdict") == "same"
                and (changes.get(s["stage"]) or links.get(s["stage"]))]
        if same:
            stage = same[0]
            if not changes.get(stage):        # the corpus link alone: not an instrument or config change
                return f'review: corpus link changed at stage "{stage}" (corpus link {links[stage]})'
            line = f'review: instrument or config changed at stage "{stage}" ({changes[stage]})'
            return line + (f"; corpus link changed ({links[stage]})" if links.get(stage) else "")
        return f"review: {reviews[0].check}: {reviews[0].detail}"
    count = sum(1 for s in data.get("ladder") or [] if s.get("verdict") == "same")
    line = f"pass: {count} stages same as baseline"
    warns = [f for f in findings if f.verdict == WARN]
    return line + (f" (warn: {warns[0].check}: {warns[0].detail})" if warns else "")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="onetrace-ci gate")
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runner", type=Path, default=None)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--review-exit", type=int, default=2, choices=(0, 2))
    args = parser.parse_args(argv)

    try:
        exit_code, findings = run_gate(
            run=args.run, baseline=args.baseline, plan_path=args.plan,
            runner=args.runner, out=args.out, review_exit_zero=(args.review_exit == 0))
    except (GateError, PlanError) as e:
        print(format_refusal("gate", [str(e)]), file=sys.stderr)
        return 1
    print(summary_line(findings, args.out))
    for f in findings:
        print(f"[{f.verdict.upper():6}] {f.check}: {f.detail}")
    return exit_code
