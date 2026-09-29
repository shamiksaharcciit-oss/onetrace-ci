"""From what the observer saw and what the code says, a proposed stage map: candidate stages in
their observed order, each with its reasons, their inputs and packages, proposed boundaries,
reads no stage explains, and the branches the fixtures never took. Confidence is a list of
reasons, never a percentage. Nothing here decides meaning."""
from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Stage:
    function: str | None          # module:function; None for a proposed intake stage
    name: str
    site: str = ""
    calls: int = 0
    args: dict = field(default_factory=dict)
    returned: str | None = None
    returned_none: bool = False
    flavour: str | None = None    # "async", "generator" or "async generator"
    memory_inputs: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    unnamed_paths: set[str] = field(default_factory=set)
    outside_files: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    env_unnamed: bool = False
    http: list[str] = field(default_factory=list)
    subprocesses: list[str | None] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    packages: list[tuple[str, str, str]] = field(default_factory=list)
    settings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def unnamed_files(self) -> int:
        return len(self.unnamed_paths)


@dataclass
class Discovery:
    entry: str
    command: list[str]
    returncode: int
    stages: list[Stage]
    unaccounted: list[str]
    unexercised: list[str]
    boundaries: list[str]
    observer_errors: int = 0
    observer_failures: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    processes: int = 0            # the Python processes observed
    locked: dict = field(default_factory=dict)   # normalized distribution name -> (version, lock file)
    others: list[str] = field(default_factory=list)   # what each other entry did, for the report
    corpus: list[str] = field(default_factory=list)   # the corpus question's evidence, one per other entry


_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;\\#]+)")


def normalized(name: str) -> str:
    """A distribution name as package indexes compare them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(repo: Path) -> dict:
    """The `name==version` pins of the pip-style lock files at the repository's root
    (`requirements*.txt` and `*.lock`), by normalized name. Other lock formats are not read."""
    locked = {}
    for path in sorted([*repo.glob("requirements*.txt"), *repo.glob("*.lock")]):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            m = _PIN.match(line)
            if m:
                locked.setdefault(normalized(m.group(1)), (m.group(2), path.name))
    return locked


def _suggest(function: str) -> str:
    """A suggested stage name, from the function's own name; a person may rename it."""
    return function.rpartition(":")[2].rpartition(".")[2].replace("_", " ")


def unnamed(function: str | None) -> bool:
    """A function in a code file made during the run, whose name discovery does not write."""
    return bool(function) and function.startswith("<")


def _unique_names(stages: list[Stage]) -> None:
    """A suggested name that two stages would share (two modules' `load`), or `intake` (the
    name of the stage that records the entry's parameters), is qualified by its module."""
    counts = Counter(s.name for s in stages)
    for s in stages:
        if counts[s.name] > 1 or s.name == "intake":
            new = f"{s.function.partition(':')[0].rpartition('.')[2]} {s.name}"
            why = ("'intake' names the stage that records the entry's parameters" if s.name == "intake"
                   else f"another stage's function is also named {s.name!r}")
            s.reasons.append(f"named {new!r} because {why}")
            s.name = new
    counts = Counter(s.name for s in stages)
    for s in stages:
        if counts[s.name] > 1:
            s.name = s.function.replace(":", " ").replace(".", " ")


def _describe_http(e: dict) -> str:
    host = e.get("host") or "a host whose name is not written in the code"
    return f"calls {host} over HTTP ({e.get('library')})"


def _unnamed_file(e: dict) -> str:
    if e.get("where") == "untracked":
        return "a file not tracked by git (its name is not recorded)"
    if e.get("where") == "package":
        return f"a file of the installed package {e.get('package')}"
    return "a file outside the repository"


def _file(e: dict, verb: str) -> str:
    return f"the file `{e['name']}` is {verb}" if e.get("name") else f"{_unnamed_file(e)} is {verb}"


_WHAT = {
    "file-read": lambda e: _file(e, "read"),
    "file-write": lambda e: _file(e, "written"),
    "env": lambda e: (f"the environment variable `{e['name']}` is read" if e.get("name") else
                      "an environment variable whose name is not written in the code is read"),
    "http": _describe_http,
    "subprocess": lambda e: (f"the program `{e['program']}` is run" if e.get("program") else
                             "a program whose name is not written in the code is run"),
}

_FLAVOURS = {
    "async": "it is async, and instrument does not wrap async functions yet",
    "generator": "it is a generator, and instrument does not wrap generators",
    "async generator": "it is an async generator, and instrument does not wrap generators",
}


def infer(repo: Path, entry: str, command: list[str], returncode: int, events: list[dict],
          lines: dict, others: list[str] = ()) -> Discovery:
    #: Events from a run of another entry are marked with its index; everything below but the
    #: other entries' summary reads only the planned entry's.
    observed, events = events, [e for e in events if not e.get("entry")]
    entry_args = dict(next((e["args"] for e in events if e["kind"] == "entry"), []))
    #: The packages the process that ran the entry imported: under a launcher (tox, a test
    #: controller) that is a child process, and the launcher's own imports are not the pipeline's.
    process, entry_process, by_process = 0, None, {}
    for e in events:
        if e["kind"] == "process":
            process = e["index"]
        elif e["kind"] == "entry" and entry_process is None:
            entry_process = process
        elif e["kind"] == "packages":
            by_process.setdefault(process, e["packages"])
    packages = by_process.get(entry_process, next(iter(by_process.values()), {}))
    failures = [e for e in events if e["kind"] == "observer-error"]

    stages: dict[str, Stage] = {}
    for e in (e for e in events if e["kind"] == "call"):
        s = stages.get(e["function"])
        if s is None:
            s = stages[e["function"]] = Stage(e["function"], _suggest(e["function"]), site=e["site"],
                                              args=dict(e["args"]), returned=e["returned"],
                                              returned_none=bool(e.get("returned_none")),
                                              flavour=e.get("flavour"))
        s.calls += 1
    ordered = list(stages.values())
    for k, s in enumerate((s for s in ordered if unnamed(s.function)), start=1):
        s.name = f"unnamed stage {k}"
        s.reasons.append("its function is in a code file made during the run, so neither is named")
    _unique_names([s for s in ordered if not unnamed(s.function)])

    # The data flow, joined by fingerprint: an argument is the entry's own parameter, or the
    # output of an earlier stage, or neither (and then nothing would record it). None and the
    # booleans say nothing about where a value came from, and are never joined.
    memory: list[str] = []
    for i, s in enumerate(ordered):
        for param, fp in s.args.items():
            if fp.startswith("unrecorded:"):
                s.reasons.append(f"its argument {param!r} could not be fingerprinted "
                                 f"(a {fp.partition(':')[2]})")
                continue
            if not fp.startswith("fp:"):
                what = "None" if fp == "none" else "a boolean"
                s.reasons.append(f"its argument {param!r} is {what}, which is never joined to another "
                                 f"stage's output")
                continue
            upstream = next((t for t in reversed(ordered[:i]) if t.returned == fp), None)
            source_param = next((p for p, efp in entry_args.items() if efp == fp), None)
            if upstream is not None:
                if upstream.name not in s.inputs:
                    s.inputs.append(upstream.name)
                s.reasons.append(f"its argument {param!r} is the output of {upstream.name}")
                reason = f"its output is used by {s.name}"
                if reason not in upstream.reasons:
                    upstream.reasons.append(reason)
            elif source_param is not None:
                if source_param not in memory:
                    memory.append(source_param)
                if "intake" not in s.inputs:
                    s.inputs.insert(0, "intake")
                s.reasons.append(f"its argument {param!r} is the entry parameter {source_param!r}")
            else:
                unknown = [t.name for t in ordered[:i] if str(t.returned).startswith("unrecorded:")]
                if unknown:
                    s.reasons.append(f"its argument {param!r} matches neither the entry's parameters nor "
                                     f"an earlier stage's recorded output; {_list(unknown)}'s output could "
                                     f"not be fingerprinted, so it may come from there")
                else:
                    s.reasons.append(f"its argument {param!r} comes from neither the entry's parameters "
                                     f"nor an earlier stage, so nothing would record it")

    for s in ordered:
        if s.returned_none and not any(r.startswith("its output is used by") for r in s.reasons):
            s.reasons.append("returned nothing, and no later stage uses its output: it may be a "
                             "check, not a stage")

    unaccounted: list[str] = []
    passed: dict[str, list] = {}
    for e in events:
        kind = e["kind"]
        if kind == "passed-call":
            sites, calls = passed.setdefault(e["function"], [[], 0])
            if e["site"] not in sites:
                sites.append(e["site"])
            passed[e["function"]][1] = calls + 1
            continue
        if kind == "elsewhere-call":
            unaccounted.append(f"{e['function']} ran in another thread or task while the entry was running; "
                               f"it is not proposed as a stage, because the entry does not call it directly")
            continue
        if kind not in _WHAT and kind != "exception":
            continue
        s = stages.get(e.get("stage")) if e.get("stage") else None
        if s is None:
            if kind in _WHAT:
                if e.get("in_run") == "elsewhere":
                    where = "in another thread or task while the entry was running"
                elif e.get("in_run") and e.get("inside"):
                    where = f"in the entry function itself (inside {e['inside']}, which is passed through)"
                elif e.get("in_run"):
                    where = "in the entry function itself"
                else:
                    where = "outside any run (at import, or before or after the entry ran)"
                by = f" (by the installed package {e['via']})" if e.get("via") else ""
                unaccounted.append(f"{_WHAT[kind](e)}{by} at {e['site']}, {where}")
            continue
        if kind == "file-read":
            if e.get("name"):
                if e["name"] not in s.files:
                    s.files.append(e["name"])
            elif e.get("where") == "untracked":
                s.unnamed_paths.add(e.get("path", ""))
            elif _unnamed_file(e) not in s.outside_files:
                s.outside_files.append(_unnamed_file(e))
        elif kind == "env":
            if e.get("name") is None:
                s.env_unnamed = True
            elif e["name"] not in s.env:
                s.env.append(e["name"])
        elif kind == "http" and _describe_http(e) not in s.http:
            s.http.append(_describe_http(e))
        elif kind == "subprocess" and e.get("program") not in s.subprocesses:
            s.subprocesses.append(e.get("program"))          # None: a name not written in the code
        elif kind == "exception" and e["type"] not in s.exceptions:
            s.exceptions.append(e["type"])

    unexercised = _read_code(repo, entry, ordered, lines, packages)

    boundaries = []
    for s in ordered:
        head = f"called by {entry.rpartition(':')[2]} at {s.site}" + (f", {s.calls} times" if s.calls > 1 else "")
        shape = []
        if s.flavour in _FLAVOURS:
            shape.append(_FLAVOURS[s.flavour])
        if "." in s.function.partition(":")[2]:
            shape.append("it is a method, and instrument wraps only module-level functions")
        s.reasons[:0] = [head, *shape]
        s.reasons += [f"reads {f}" for f in s.files]
        if s.unnamed_files == 1:
            s.reasons.append("reads a file not tracked by git (its name is not recorded)")
        elif s.unnamed_files:
            s.reasons.append(f"reads {s.unnamed_files} files not tracked by git (their names are not recorded)")
        s.reasons += [f"reads {f}" for f in s.outside_files]
        s.reasons += [f"reads the environment variable {n}" for n in s.env]
        if s.env_unnamed:
            s.reasons.append("reads an environment variable whose name is not written in the code")
        s.reasons += s.http
        s.reasons += [f"runs the program {p}" if p else "runs a program whose name is not written in the code"
                      for p in s.subprocesses]
        s.reasons += [f"raised {t} (its message is not recorded)" for t in s.exceptions]
        if s.http or s.outside_files:
            boundaries.append(f"{s.name}: " + "; ".join(s.http + [f"reads {f}" for f in s.outside_files])
                              + "; the record cannot see past it")
    if memory:
        intake = Stage(None, "intake", memory_inputs=memory)
        users = [s.name for s in ordered if "intake" in s.inputs]
        intake.reasons.append(f"records the entry parameter(s) {', '.join(repr(m) for m in memory)}, "
                              f"which {' and '.join(users)} receive")
        ordered.insert(0, intake)
    caller = entry.rpartition(":")[2]
    passed_lines = [f"{function} was called by {caller} at {_list(sites)}"
                    + (f", {n} times in all" if n > len(sites) else "")
                    + "; calls it makes count as the entry's own, and it is not proposed: instrument wraps "
                      "only module-level functions" for function, (sites, n) in passed.items()]
    other_lines, corpus = _other_entries(entry, list(others), observed, stages)
    return Discovery(entry, command, returncode, ordered, list(dict.fromkeys(unaccounted)),
                     unexercised, boundaries, sum(e.get("count", 1) for e in failures),
                     [e["where"] for e in failures], passed_lines, locked=locked_versions(repo),
                     others=other_lines, corpus=corpus)


def _files(written: dict) -> tuple[str, str]:
    """("2 files", "(`data/a.json`; 1 not tracked by git, under data/)") for file events by path."""
    events = list(written.values())
    named = sorted(e["name"] for e in events if e.get("name"))
    untracked = [e for e in events if not e.get("name") and e.get("where") == "untracked"]
    outside = len(events) - len(named) - len(untracked)
    parts = [_list([f"`{n}`" for n in named])] if named else []
    if untracked:
        dirs = list(dict.fromkeys(e.get("under") or "the repository root" for e in untracked))
        head = "not tracked by git" if len(untracked) == len(events) else f"{len(untracked)} not tracked by git"
        parts.append(f"{head}, under {_list(dirs)}")
    if outside:
        parts.append(f"{outside} outside the repository")
    return f"{len(events)} file{'' if len(events) == 1 else 's'}", "(" + "; ".join(parts) + ")"


def _other_entries(entry: str, others: list[str], events: list[dict], stages: dict) -> tuple[list[str], list[str]]:
    """For each entry other than the planned one (an ingest run, say): what it did, for the
    report, and whether a run of the planned entry read a file after the other wrote it, for the
    corpus question. Reads and writes are joined by path, and only those made through `open()`
    are seen: what was written is not fingerprinted, so this says the query read the file at the
    path the ingest wrote to, not that it read what the ingest wrote there. Order is the events
    file's own within a process, and the observer's clock between processes."""
    caller = entry.rpartition(":")[2]
    process, order = 0, {}
    for n, e in enumerate(events):
        if e["kind"] == "process":
            process = e["index"]
        order[id(e)] = (process, n, e.get("at", 0))

    def after(read: dict, write: dict) -> bool:
        r, w = order[id(read)], order[id(write)]
        return r[1] > w[1] if r[0] == w[0] else r[2] > w[2]

    reads: dict[str, list[tuple[dict, str]]] = {}    # path fingerprint -> [(read, who)] in the planned entry's runs
    for e in events:
        if e["kind"] == "file-read" and not e.get("entry") and e.get("in_run") and e.get("path"):
            s = stages.get(e.get("stage")) if e.get("stage") else None
            if s is not None:
                who = s.name
            elif e["in_run"] == "elsewhere":
                who = f"code in another thread or task while {caller} ran"
            else:
                who = f"{caller} itself"
            reads.setdefault(e["path"], []).append((e, who))
    lines, evidence = [], []
    for i, other in enumerate(others, start=1):
        own = [e for e in events if e.get("entry") == i]
        runs = [e for e in own if e["kind"] == "entry"]
        nested = sum(1 for e in runs if e.get("within_first"))
        called = list(dict.fromkeys(e["function"] for e in own if e["kind"] == "call"))
        read, written = {}, {}
        for e in own:
            if e["kind"] == "file-read" and e.get("path"):
                read.setdefault(e["path"], e)
            elif e["kind"] == "file-write" and e.get("path"):
                written.setdefault(e["path"], e)            # its first write
        joined, stale, who, who_stale = {}, {}, [], []
        for path, write in written.items():
            seen = reads.get(path, [])
            later = [w for r, w in seen if after(r, write)]
            earlier = [w for r, w in seen if not after(r, write)]
            if later:
                joined[path] = write
                who += later
            elif earlier:
                stale[path] = write
                who_stale += earlier
        who, who_stale = list(dict.fromkeys(who)), list(dict.fromkeys(who_stale))

        times = "once" if len(runs) == 1 else f"{len(runs)} times"
        if nested == len(runs):
            line = (f"`{other}` ran {times}, inside a run of `{entry}`, where what it did is recorded as that "
                    f"run's own.")
            lines.append(line)
            evidence.append(f"{other} ran only inside runs of {entry}, where what it did is the query's own")
            continue
        line = f"`{other}` ran {times}"
        line += (f", {nested} of them inside a run of `{entry}`, where what it did is recorded as that run's "
                 f"own. " if nested else ". ")
        line += f"It called {_list(called)}. " if called else "It called no function of the repository directly. "
        if read:
            count, detail = _files(read)
            line += f"It read {count} {detail}. "
        if not written:
            line += "It wrote no file."
        else:
            count, detail = _files(written)
            if not joined:
                line += (f"It wrote {count} {detail}; no read of {'it' if len(written) == 1 else 'them'} was seen "
                         f"after the write in a run of `{entry}`.")
            else:
                share = "it" if len(written) == 1 else ("them all" if len(joined) == len(written) else f"{len(joined)} of them")
                line += f"It wrote {count} {detail}, and {_list(who)} read {share} afterwards."
        lines.append(line.rstrip())
        if joined:
            count, detail = _files(joined)
            said = f"{_list(who)} read {count} that {other} wrote {detail}"
        else:
            said = (f"no read of a file {other} wrote was seen after the write (only reads through open() are "
                    f"observed, matched by path)")
        if stale:
            count, detail = _files(stale)
            where = "a path" if len(stale) == 1 else "paths"
            said += f"; {_list(who_stale)} read {count} at {where} {other} later wrote to {detail}, before it wrote there"
        evidence.append(said)
    return lines, evidence


def _list(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


#: A keyword whose name suggests a credential (matched in snake case, so `authToken` counts):
#: its literal value is never copied.
_CREDENTIAL = re.compile(r"(^|_)(api_?key|key|keys|token|tokens|secret|secrets|password|passwd|pwd|pass|"
                         r"passphrase|auth|authorization|credential|credentials|cookie|signature|"
                         r"private_?key|dsn)($|_)")
#: A URL holding a password: scheme://user:password@host (a password may hold an unescaped `/`)
_USERINFO = re.compile(r"://[^/\s@:]*:[^\s@]*@")
_LONGEST = 80


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _setting(cst, mod, arg, site) -> str | None:
    """A keyword argument as a setting a person may want recorded: a short literal with its
    value, or a value read from the environment by the variable's name only (never its value);
    else None. A literal that may be a credential, spans lines or is long is named, not copied."""
    if arg.keyword is None:
        return None
    name, v = arg.keyword.value, arg.value
    at = f"(at {mod.at(site)})"
    literal = isinstance(v, (cst.Integer, cst.Float, cst.SimpleString)) or (
        isinstance(v, cst.Name) and v.value in ("True", "False"))
    if literal:
        text = v.evaluated_value if isinstance(v, cst.SimpleString) else mod.code(v)
        text = text.decode("latin-1") if isinstance(text, bytes) else str(text)
        if _CREDENTIAL.search(_snake(name)) or _looks_like_key(text):
            return f"{name} is a literal in the code; its value is not copied, as it may be a credential {at}"
        if "\n" in mod.code(v) or "\r" in mod.code(v):
            return f"{name} is a multi-line string in the code; its value is not copied {at}"
        if len(text) > _LONGEST:
            return f"{name} is a string of {len(text)} characters in the code; its value is not copied {at}"
        if any(c in text for c in "`<>") or not text.isprintable():
            return f"{name} is a string in the code; its value is not copied, as it would change how the report reads {at}"
        return f"{name}={mod.code(v)} {at}"
    env = _environment_name(cst, mod, v)
    if env is not None:
        return f"{name} from the environment variable {env} {at}"
    return None


def _looks_like_key(text: str) -> bool:
    if "-----BEGIN" in text or _USERINFO.search(text):
        return True
    return (len(text) >= 32 and not any(c.isspace() for c in text) and any(c.isdigit() for c in text)
            and any(c.islower() for c in text) and any(c.isupper() for c in text))


def _environment_name(cst, mod, v) -> str | None:
    """The variable a value is read from, for `os.environ["X"]`, `os.environ.get("X", ...)` and
    `os.getenv("X", ...)`, or None. Only the literal name in the code is looked at."""
    def literal(node):
        if isinstance(node, cst.SimpleString):
            text = node.evaluated_value
            return text if isinstance(text, str) else None
        return None

    if isinstance(v, cst.Subscript) and "environ" in mod.code(v.value) and v.slice:
        element = v.slice[0].slice
        return literal(element.value) if isinstance(element, cst.Index) else None
    if isinstance(v, cst.Call) and v.args:
        callee = mod.code(v.func)
        if callee.endswith("environ.get") or callee.endswith("getenv"):
            return literal(v.args[0].value)
    return None


def _read_code(repo: Path, entry: str, stages: list[Stage], lines: dict, packages: dict) -> list[str]:
    """Read the code: each stage's third-party imports and literal settings (attached to the
    stages), and the branches, in the entry function and the stage functions, whose first
    statement never ran (returned)."""
    import libcst as cst

    from onetrace_ci.instrument import _Source
    src = _Source(repo.resolve())
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    unexercised: list[str] = []

    def load_def(ref: str):
        module, _, name = ref.partition(":")
        mod = src.load(module)
        if mod is None:
            return None, None
        b = src.bindings(mod).get(name)
        return mod, (b.node if b is not None and b.kind in ("def", "asyncdef") else None)

    def branches(mod, fn):
        ran = lines.get(mod.rel, set())
        span = mod.positions[fn]
        if not any(span.start.line <= n <= span.end.line for n in ran):
            return                                # the function itself never ran

        found = []

        def check(keyword, node, block, header_runs=True):
            """A body on the line of an `if`, `for`, `while`, `except` or `case` shares that line
            with code that runs either way, so line numbers can't tell whether it ran."""
            at = mod.at(node)
            if isinstance(block, cst.IndentedBlock):
                if block.body and mod.line(block.body[0]) not in ran:
                    found.append(f"{at}: the `{keyword}` body never ran")
            elif header_runs and mod.line(node) in ran:
                found.append(f"{at}: the `{keyword}` body is on the `{keyword}` line, so whether it ran is not known")
            elif mod.line(block) not in ran:
                found.append(f"{at}: the `{keyword}` body never ran")

        class V(cst.CSTVisitor):
            def visit_FunctionDef(self, node):
                return node is fn

            def visit_If(self, node):
                check("if", node, node.body)
                if isinstance(node.orelse, cst.Else):
                    check("else", node.orelse, node.orelse.body, header_runs=False)

            def visit_ExceptHandler(self, node):
                check("except", node, node.body)

            def visit_For(self, node):
                check("for", node, node.body)

            def visit_While(self, node):
                check("while", node, node.body)

            def visit_MatchCase(self, node):
                check("case", node, node.body)

        fn.visit(V())
        unexercised.extend(found)

    named = [s for s in stages if not unnamed(s.function)]
    for ref in [entry] + [s.function for s in named]:
        mod, fn = load_def(ref)
        if fn is not None:
            branches(mod, fn)

    for s in named:
        mod, fn = load_def(s.function)
        if mod is None:
            continue
        for name, b in src.bindings(mod).items():
            if b is None or b.kind not in ("module", "from"):
                continue
            top = (b.target or name).split(".")[0]
            if not top or top in stdlib or src.load(top) is not None:
                continue
            dist, version = packages.get(top, [top, "not seen in the observed environment"])
            if (top, dist, version) not in s.packages:
                s.packages.append((top, dist, version))
        if fn is None:
            continue

        class Settings(cst.CSTVisitor):
            def visit_Arg(self, node, mod=mod, s=s):
                found = _setting(cst, mod, node, node)
                if found is not None:
                    s.settings.append(found)

        fn.visit(Settings())

    # The keyword arguments where the entry function calls each stage.
    entry_mod, entry_fn = load_def(entry)
    if entry_fn is not None:
        wanted = {}
        for s in named:
            rel, _, line = s.site.rpartition(":")
            if rel == entry_mod.rel and line.isdigit():
                wanted[(int(line), s.function.rpartition(":")[2].rpartition(".")[2])] = s

        class CallSites(cst.CSTVisitor):
            def visit_Call(self, node):
                callee = node.func.attr.value if isinstance(node.func, cst.Attribute) else (
                    node.func.value if isinstance(node.func, cst.Name) else None)
                s = wanted.get((entry_mod.line(node), callee))
                if s is None:
                    return
                for arg in node.args:
                    found = _setting(cst, entry_mod, arg, node)
                    if found is not None:
                        s.settings.append(found)

        entry_fn.visit(CallSites())
    return list(dict.fromkeys(unexercised))
