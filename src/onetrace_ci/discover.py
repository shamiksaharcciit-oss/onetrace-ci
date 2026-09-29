"""`onetrace-ci discover`: watch a pipeline's own tests or fixtures run, read its code, and draft
the plan `onetrace-ci instrument` consumes, with every field that carries meaning left as a
`DECIDE:` question for a person.

    onetrace-ci discover --entry pipeline.main:run -- pytest tests/test_pipeline.py

It runs the given command unchanged, with the observer loaded for that command only. It never
calls production and never runs the pipeline on inputs of its own: it sees what the fixtures do,
and nothing else. It writes three files: `onetrace-plan.draft.yaml`, `discovery-report.md` and
`discovery-events.jsonl`, which holds fingerprints only. Discovery is not evidence, and nothing
it drafts is a decision.

`--entry` may be given more than once, when the fixtures also run an ingest: the plan is drafted
for the first, and asks (`corpus`) which ingest run it reads.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from onetrace_ci.discover_infer import Discovery, infer, normalized, unnamed
from onetrace_ci.errors import format_refusal

_PREFIX = "ONETRACE_CI_DISCOVER_"
#: The observer is copied into the command's interpreter as a file, never imported here:
#: importing it would install its hooks in this process.
OBSERVER = Path(__file__).with_name("_observer.py")
DRAFT, REPORT, EVENTS = "onetrace-plan.draft.yaml", "discovery-report.md", "discovery-events.jsonl"


@dataclass
class ObserveResult:
    returncode: int
    events: list[dict]
    output: str = ""
    lines: dict[str, set[int]] = field(default_factory=dict)
    processes: int = 0            # the Python processes observed: the command's and those it started


class DiscoverRefused(Exception):
    """Discovery cannot run, or cannot draft from what ran. The message names why."""


def entry_file(repo: Path, entry: str) -> Path:
    from onetrace_ci.instrument import Refused, _Source
    module, sep, function = entry.partition(":")
    if not sep or not module or not function:
        raise DiscoverRefused(f"--entry {entry!r} is not `module.path:function`")
    src = _Source(repo.resolve())
    try:
        path, looked = src.module_file(module)
    except Refused as e:
        raise DiscoverRefused(str(e)) from None
    if path is None:
        raise DiscoverRefused(f"--entry {entry}: no module file for {module!r} (looked for {', '.join(looked)})")
    try:
        mod = src.load(module)
    except Refused as e:
        raise DiscoverRefused(str(e)) from None
    binding = src.bindings(mod).get(function) if mod is not None else None
    if binding is None or binding.kind not in ("def", "asyncdef"):
        rel = path.relative_to(repo.resolve()).as_posix()
        raise DiscoverRefused(f"--entry {entry}: {rel} has no top-level function {function}")
    return path


def _existing(repo: Path) -> list[str]:
    """The files under `repo` now, other than in `.git` and virtual environments."""
    found = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git" and not (Path(dirpath) / d / "pyvenv.cfg").is_file()]
        found += [(Path(dirpath) / f).relative_to(repo).as_posix() for f in filenames]
    return found


def known_names(repo: Path) -> dict[str, list[str]]:
    """The names discovery may write. `files`: those git tracks, or, outside a git work tree,
    those that exist before the run. `code`: those, and the .py files that exist before the run
    (code a person is still writing). Inside a git work tree where git can't list its files,
    discovery is refused: no name is guessed from what happens to exist."""
    existing = _existing(repo)
    code = [p for p in existing if p.endswith(".py")]
    try:
        listed = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", "--cached"], capture_output=True,
                                timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        listed, failure = None, f"git could not be run ({type(e).__name__})"
    else:
        failure = f"git exited {listed.returncode}: {listed.stderr.decode('utf-8', 'replace').strip()[:200]}"
    if listed is not None and listed.returncode == 0:
        return {"files": [p for p in listed.stdout.decode("utf-8", "replace").split("\0") if p], "code": code}
    if any((d / ".git").exists() for d in (repo, *repo.parents)):
        raise DiscoverRefused(f"git could not list the files it tracks in {repo} ({failure}); inside a git "
                              f"work tree, discovery names a file only if git tracks it")
    return {"files": existing, "code": code}


#: How long the observed command may run, in seconds. A command still running then is stopped,
#: and discovery refuses: a fixture that doesn't finish tells nothing about the pipeline.
COMMAND_TIMEOUT = 1800


def observe(command: list[str], *, repo: Path, entry: str, events_path: Path,
            env: dict | None = None, timeout: int | None = None, others: list[str] = ()) -> ObserveResult:
    """Run `command` in `repo`, unchanged, with the observer loaded, and return what it recorded.
    `events_path` is written afresh. `timeout` defaults to `COMMAND_TIMEOUT`. `others` are
    further entries (an ingest run, say), observed in the same command; an event in a run of
    one is marked with its index, counting from 1."""
    timeout = COMMAND_TIMEOUT if timeout is None else timeout
    repo = repo.resolve()
    entries = [[str(entry_file(repo, e)), e.partition(":")[2]] for e in (entry, *others)]
    names = known_names(repo)
    events_path = Path(events_path).resolve()
    events_path.parent.mkdir(parents=True, exist_ok=True)
    base = dict(os.environ if env is None else env)
    with tempfile.TemporaryDirectory(prefix="onetrace-ci-discover-") as site:
        shutil.copyfile(OBSERVER, Path(site) / "sitecustomize.py")
        known = Path(site) / "known-names.json"
        known.write_bytes(json.dumps(names).encode("utf-8"))
        parts = Path(site) / "events"                 # this discovery's own; nothing else is in it
        parts.mkdir()
        child = dict(base)
        child[_PREFIX + "KNOWN"] = str(known)
        child["PYTHONPATH"] = os.pathsep.join(p for p in (site, base.get("PYTHONPATH", "")) if p)
        child[_PREFIX + "REPO"] = str(repo)
        child[_PREFIX + "KEY"] = secrets.token_hex(32)      # this run's only; never written
        child[_PREFIX + "EVENTS"] = str(parts)
        child[_PREFIX + "ENTRIES"] = json.dumps(entries)
        try:
            done = subprocess.run(command, cwd=repo, env=child, capture_output=True, text=True,
                                  timeout=timeout)
        except FileNotFoundError:
            raise DiscoverRefused(f"the command {command[0]!r} was not found") from None
        except subprocess.TimeoutExpired:
            raise DiscoverRefused(f"the command did not finish within {timeout} seconds, and was stopped; "
                                  f"discovery drafts only from fixtures that finish") from None
        #: Each Python process the command ran wrote its own file. They are joined in the order
        #: the processes started, each after a `process` line, so that what one process saw
        #: (the packages it imported, say) is never taken for another's.
        written = sorted(parts.iterdir())
        joined = b"".join(json.dumps({"kind": "process", "index": i}).encode("utf-8") + b"\n" + p.read_bytes()
                          for i, p in enumerate(written))
    events_path.write_bytes(joined)
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]
    lines: dict[str, set[int]] = {}
    for e in events:
        if e["kind"] == "lines":
            lines.setdefault(e["file"], set()).update(e["lines"])
    return ObserveResult(done.returncode, events, (done.stdout or "") + (done.stderr or ""), lines, len(written))


# ------------------------------------------------------------------ the draft plan

_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_./:\-]*")
_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "none", "~"}


def _q(value) -> str:
    """A scalar the plan reader reads back exactly: plain when it is a simple name or path,
    double-quoted otherwise."""
    if value is None:
        return "null"
    if value is True or value is False:
        return "true" if value else "false"
    text = str(value)
    if _PLAIN.fullmatch(text) and text.lower() not in _WORDS:
        return text
    return json.dumps(text, ensure_ascii=False)


def _flow(values) -> str:
    return "[" + ", ".join(_q(v) for v in values) + "]"


def _flow_map(pairs) -> str:
    return "{" + ", ".join(f"{k}: {_q(v)}" for k, v in pairs) + "}"


def decide(question: str) -> str:
    return "DECIDE: " + question


_NOTE = "what should the record say about re-deriving {name}? (delete this line for no note)"


def render_draft(d: Discovery) -> str:
    lines = [
        "# Drafted by `onetrace-ci discover`. Every `DECIDE:` is a question for a person;",
        "# `onetrace-ci instrument` refuses this plan until each one is answered. Nothing here is",
        "# a decision: it is what the fixtures did, and what the code says.",
        f"approved_by: {_q(decide('who approves this plan?'))}",
        f"entry: {_q(d.entry)}",
        "run_dir: runs/{run_id}",
        "stages:",
    ]
    for s in d.stages:
        lines.append(f"  - name: {_q(s.name)}")
        if s.function is None:
            lines.append(f"    memory_inputs: {_flow(s.memory_inputs)}")
            verb = "is" if len(s.memory_inputs) == 1 else "are"
            lines.append(f"    trust: {_q(decide(f'how far {verb} {_list(s.memory_inputs)} trusted: operator-authored, model-generated or externally-sourced?'))}")
            lines.append(f"    rederivable: {_q(decide('can the intake be re-derived?'))}")
            lines.append(f"    rederivable_note: {_q(decide(_NOTE.format(name='the intake')))}")
            continue
        if unnamed(s.function):
            lines.append(f"    function: {_q(decide('which function is this? it is in a code file made during the run, whose name discovery does not write'))}")
        else:
            lines.append(f"    function: {_q(s.function)}")
        #: A package is proposed only when the observed environment has it installed: the
        #: generated code reads its version from there at run time.
        installed = [p for p in s.packages if not p[2].startswith("not seen")]
        if len(installed) == 1 and len(s.packages) == 1:
            package = installed[0][1]
        elif s.packages:
            seen = ", ".join(f"{dist} ({version})" for _, dist, version in s.packages)
            package = decide(f"which installed package versions {s.name}? its module uses {seen}")
        else:
            package = decide(f"which installed package versions {s.name}? its module imports no "
                             f"third-party package; name the one its code ships in")
        lines.append(f"    instrument: {_flow_map([('name', s.name.replace(' ', '-')), ('package', package), ('kind', decide(f'what kind of instrument is {s.name}? (retriever, chunker, model...)'))])}")
        lines.append(f"    inputs: {_flow(s.inputs)}")
        if s.unnamed_files:
            #: A file whose name discovery does not write can't be listed, so which files the
            #: stage records is a question.
            named = f"{_list(s.files)} and " if s.files else ""
            count = f"{s.unnamed_files} file{'s' if s.unnamed_files > 1 else ''} not tracked by git"
            lines.append(f"    files: {_q(decide(f'which files should {s.name} record? it reads {named}{count} (names not recorded)'))}")
            lines.append(f"    trust: {_q(decide(f'how far are the files {s.name} reads trusted: operator-authored, model-generated or externally-sourced?'))}")
        elif s.files:
            lines.append(f"    files: {_flow(s.files)}")
            lines.append(f"    trust: {_q(decide(f'is {_list(s.files)} operator-authored, model-generated or externally-sourced?'))}")
        evidence = f" It {'; '.join(s.http)}." if s.http else ""
        lines.append(f"    rederivable: {_q(decide(f'can {s.name} be re-derived?{evidence}'))}")
        lines.append(f"    rederivable_note: {_q(decide(_NOTE.format(name=s.name)))}")
        if s.settings:
            lines.append(f"    config: {_q(decide('record these settings? observed ' + '; '.join(s.settings)))}")
    if d.corpus:
        #: Discovery doesn't guess which ingest run a query reads: it says what it saw.
        lines.append(f"corpus: {_q(decide('which ingest run does the query read? ' + '; '.join(d.corpus) + '. Answer with from: (a run folder or a manifest digest) and stages: (where the link is recorded), or delete this line for no link'))}")
    boundary_q = ("approve these boundaries? " + " | ".join(d.boundaries)) if d.boundaries else \
        "are there boundaries to approve? none were seen"
    lines += [
        f"approved_boundaries: {_q(decide(boundary_q))}",
        "ci:",
        f"  install: {_q(decide('how does CI install the pipeline?'))}",
        f"  run: {_q(decide('which command runs the pipeline once on its CI fixtures? discovery ran: ' + ' '.join(d.command)))}",
        f"  baseline: {_q(decide('where will the committed baseline run be?'))}",
        f"require_declared: {_q(decide('should the gate fail a run with any field nobody declared?'))}",
        f"sign: {_q(decide('sign runs? If yes, which env var will hold the key, and should a run without the key refuse or run unsigned?'))}",
        f"anchor: {_q(decide('anchor runs? If yes, which configured source, and on main only or every run?'))}",
        f"trust: {_q(decide('which trust file lists your recorder keys, and is an untrusted signature review or fail?'))}",
    ]
    return "\n".join(lines) + "\n"


def _list(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ------------------------------------------------------------------ the report

def render_report(d: Discovery, open_questions: int) -> str:
    out = [
        "# Discovery report",
        "",
        f"**{open_questions} `DECIDE:` questions are still open.** Nothing in this draft is a decision.",
        "",
        "Discovery is not evidence. It observed the command below run the fixtures, and reports what "
        "they did; that never makes a gate pass. It sees only the code paths the fixtures exercised. "
        "The branches they never took are listed below, and coverage stays incomplete until a person "
        "approves the boundaries. Nothing it drafts is a decision. Every field that carries meaning is "
        f"a `DECIDE:` question in `{DRAFT}`.",
        "",
        f"- Command: `{' '.join(d.command)}` (exit {d.returncode})",
        f"- Entry: `{d.entry}`",
        f"- Events: `{EVENTS}` (fingerprints only: no value, no environment value, no exception message)",
        f"- Python processes observed: {d.processes} (the command's own, and any it started)",
    ]
    if d.observer_errors:
        out.append(f"- **The observer failed to record {d.observer_errors} event(s)** ({'; '.join(d.observer_failures)}); "
                   f"this report may be incomplete. The observed program ran unchanged.")
    out += [
        "",
        "## Reads no stage explains",
        "",
    ]
    out += [f"- {u}" for u in d.unaccounted] or ["None."]
    out += ["", "## Stages", "", "In the order the fixtures ran them. Each reason is something observed "
            "or read in the code; there is no score.", ""]
    if not any(s.function for s in d.stages):
        called = ("; what it did call is listed under \"Called by the entry, but not proposed\"" if d.passed else "")
        out += ["**No stage was proposed.** The entry called no module-level function of the repository "
                f"directly{called}. Any that ran in another thread or task are listed under the reads no "
                "stage explains.", ""]
    for i, s in enumerate(d.stages, start=1):
        title = f"### {i}. {s.name}" + (f": `{s.function}`" if s.function else " (proposed)")
        out += [title, ""] + [f"- {r}" for r in s.reasons] + [""]
    if d.passed:
        out += ["## Called by the entry, but not proposed", ""] + [f"- {p}" for p in d.passed] + [""]
    if d.others:
        out += ["## Other entries", "", "Observed in the same command, so that a file one writes can be joined, "
                f"by its path, to a read in a run of `{d.entry}`. The draft plans only `{d.entry}`.", ""]
        out += [f"- {o}" for o in d.others] + [""]
    out += ["## Proposed boundaries", ""]
    out += [f"- {b}" for b in d.boundaries] or ["None seen."]
    out += ["", "## Branches the fixtures never took", ""]
    out += [f"- {u}" for u in d.unexercised] or ["None: every branch in the entry and stage functions ran."]
    out += ["", "## Packages", ""]
    pk = []
    for s in d.stages:
        for _, dist, version in s.packages:
            line = f"- {s.name}: {dist} {version}"
            locked, lock_file = d.locked.get(normalized(dist), (None, None))
            if locked is not None and locked != version and not version.startswith("not seen"):
                line += (f" ({lock_file} names {locked}; the installed {version} is what ran, and is what is "
                         f"recorded)")
            pk.append(line)
    out += pk or ["None: no stage imports a third-party package."]
    out += ["", "## Settings seen in the code", ""]
    st = [f"- {s.name}: {x}" for s in d.stages for x in s.settings]
    out += st or ["None."]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ the command

def discover(*, entry: str, command: list[str], repo: Path, out_dir: Path,
             others: list[str] = ()) -> tuple[Discovery, int]:
    """Observe, infer, and write the three files into `out_dir`. Returns (discovery, open questions).
    `others` are further entries: see `observe`."""
    from onetrace_ci.plan import find_open_questions, read_document
    repo = repo.resolve()
    out_dir = out_dir.resolve()
    for name in (DRAFT, REPORT, EVENTS):
        if (out_dir / name).exists():
            raise DiscoverRefused(f"{out_dir / name} already exists; discover never overwrites a file "
                                  f"(a person may have started answering it)")
    with tempfile.TemporaryDirectory(prefix="onetrace-ci-events-") as tmp:
        events_tmp = Path(tmp) / EVENTS
        #: Two spellings of one function (`pipeline.main` and `src.pipeline.main`) are one entry.
        resolved = {(entry_file(repo, entry), entry.partition(":")[2]): entry}
        for other in others:
            resolved.setdefault((entry_file(repo, other), other.partition(":")[2]), other)
        others = list(resolved.values())[1:]
        result = observe(command, repo=repo, entry=entry, events_path=events_tmp, others=others)
        if result.returncode != 0:
            raise DiscoverRefused(f"the command exited {result.returncode}; discovery drafts only from "
                                  f"fixtures that pass")
        kinds = {e["kind"] for e in result.events}
        if "packages" not in kinds:
            raise DiscoverRefused("the observer never loaded in the command: its interpreter ignored "
                                  "PYTHONPATH (-E or -I), it is not Python, or it ended without running "
                                  "its exit handlers")
        ran = {e.get("entry", 0) for e in result.events if e["kind"] == "entry"}
        if 0 not in ran:
            raise DiscoverRefused(f"the command never called {entry}, so there is nothing to draft from")
        for i, other in enumerate(others, start=1):
            if i not in ran:
                raise DiscoverRefused(f"the command never called {other}, given by --entry; discovery asks "
                                      f"which of its runs {entry} reads only from a command that runs both")
        d = infer(repo, entry, command, result.returncode, result.events, result.lines, others)
        d.processes = result.processes
        draft = render_draft(d)
        count = len(find_open_questions(read_document(draft, source=DRAFT)))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / DRAFT).write_bytes(draft.encode("utf-8"))
        (out_dir / REPORT).write_bytes(render_report(d, count).encode("utf-8"))
        shutil.copyfile(events_tmp, out_dir / EVENTS)
    return d, count


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index("--") if "--" in argv else len(argv)
    parser = argparse.ArgumentParser(
        prog="onetrace-ci discover",
        usage="onetrace-ci discover --entry module:function [--entry module:function ...] [--repo R] "
              "[--out-dir D] -- CMD...")
    parser.add_argument("--entry", required=True, action="append",
                        help="module.path:function, the function that is one run. The plan is drafted for "
                             "the first; give another (an ingest run) to be asked which of its runs the first reads")
    parser.add_argument("--repo", default=".", type=Path)
    parser.add_argument("--out-dir", default=".", type=Path)
    args = parser.parse_args(argv[:split])       # --help, and a missing --entry, are handled here
    command = argv[split + 1:]
    if not command:
        print(format_refusal("discover", ["the command to observe is missing: put it after `--`, "
                                          "for example `-- pytest tests/test_pipeline.py`"]), file=sys.stderr)
        return 1
    entry, *others = dict.fromkeys(args.entry)
    try:
        d, count = discover(entry=entry, command=command, repo=args.repo, out_dir=args.out_dir, others=others)
    except DiscoverRefused as e:
        print(format_refusal("discover", [str(e)]), file=sys.stderr)
        return 1
    print(f"onetrace-ci discover: {len(d.stages)} candidate stages, {len(d.unaccounted)} reads no stage "
          f"explains, {len(d.unexercised)} branches the fixtures never took")
    print(f"wrote {args.out_dir / DRAFT}, {args.out_dir / REPORT} and {args.out_dir / EVENTS}")
    print(f"{count} DECIDE: questions are open; nothing in the draft is a decision")
    print(f"next: answer them in a copy named onetrace-plan.yaml, then onetrace-ci instrument --plan "
          f"onetrace-plan.yaml --repo {args.repo} --out instrument.patch")
    return 0
