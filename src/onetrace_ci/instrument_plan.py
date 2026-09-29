"""The plan `onetrace-ci instrument` reads: the same `onetrace-plan.yaml` the gate reads, with
the keys that say how the pipeline is instrumented.

THE RULE
--------
The tool writes the boilerplate; people own the meaning. Every field that carries meaning is
written by a person: the stage names and their order, the trust class of what a stage reads,
whether a stage can be re-derived, the boundaries, and the files. If one is missing, the plan
is refused and the field is named. Nothing is filled in by guessing, and a `DECIDE:` question
left in any field (as `onetrace-ci discover` drafts them) is refused the same way.

Every problem in a plan is collected and named in one refusal, so a person fixing a plan sees
all of them at once.

THE FORMAT
----------
    approved_by: alice                    # required
    entry: pipeline.main:run              # required: the function that is one run
    run_dir: runs/{run_id}                # required: where each run is written
    stages:                               # required, in the order they run
      - name: intake                      # a stage without a function records memory inputs
        memory_inputs: [request]          #   entry parameters, recorded with ctx.read_memory
        trust: externally-sourced         #   required when a stage reads memory inputs or files
        rederivable: "true"               #   required on every stage
      - name: retrieve
        function: pipeline.retrieval:retrieve
        instrument: {name: bm25, package: rank_bm25}   # version read at run time
        inputs: [intake]                  # optional; the default is the previous stage
        files: [data/corpus.json]         # optional; read with ctx.read_external
        trust: operator-authored
        rederivable: "true"
    approved_boundaries: []               # required, even when empty
    ci:                                   # required: how the workflow runs the pipeline
      install: pip install --require-hashes -r requirements.lock
      run: python -m pipeline.demo
      baseline: runs/baseline

`instrument` also takes an optional `kind`; without one, the kind is `python-package`, which
states only how the version is found. An intake stage (no `function`) may carry an
`instrument` too; without one, its instrument is the SDK call that records the inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from onetrace_ci.plan import PlanError, names_nobody, read_document

TRUST_CLASSES = ("operator-authored", "model-generated", "externally-sourced")
DEFAULT_INSTRUMENT_KIND = "python-package"
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_MODULE_RE = re.compile(rf"{_IDENT}(?:\.{_IDENT})*")
_STAGE_KEYS = frozenset({"name", "function", "memory_inputs", "trust", "rederivable",
                         "rederivable_note", "instrument", "inputs", "files"})
_INSTRUMENT_KEYS = frozenset({"name", "package", "kind"})
_CI_KEYS = frozenset({"install", "run", "baseline"})
_GLOB_CHARS = set("*?[]")


class PlanRefused(PlanError):
    """The plan is readable but not complete or not consistent. Names every problem."""

    def __init__(self, source: str, problems: list[str]):
        self.source, self.problems = source, list(problems)
        super().__init__(f"{source}: the plan is refused:\n  " + "\n  ".join(self.problems))


@dataclass(frozen=True, slots=True)
class FunctionRef:
    module: str
    function: str

    def __str__(self) -> str:
        return f"{self.module}:{self.function}"


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    name: str
    package: str
    kind: str


@dataclass(frozen=True, slots=True)
class Stage:
    index: int
    name: str
    function: FunctionRef | None
    memory_inputs: tuple[str, ...]
    trust: str | None
    rederivable: str
    rederivable_note: str | None
    instrument: InstrumentSpec | None
    inputs: tuple[str, ...]
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CiSpec:
    install: str
    run: str
    baseline: str


@dataclass(frozen=True, slots=True)
class InstrumentPlan:
    source: str
    approved_by: str
    entry: FunctionRef
    run_dir: str
    stages: tuple[Stage, ...]
    approved_boundaries: tuple[str, ...]
    ci: CiSpec


class _Problems:
    def __init__(self):
        self.items: list[str] = []

    def add(self, field: str, why: str):
        self.items.append(f"plan field {field}: {why}")


def _find_decides(value, path: str, problems: _Problems):
    if isinstance(value, str) and value.lstrip().startswith("DECIDE:"):
        problems.add(path, f"still an open question ({value.strip()!r}); a person answers it "
                           f"before anything is generated")
    elif isinstance(value, dict):
        for k, v in value.items():
            _find_decides(v, f"{path}.{k}" if path else k, problems)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _find_decides(v, f"{path}[{i}]", problems)


def _text(values: dict, key: str, path: str, problems: _Problems, *, required: bool) -> str | None:
    v = values.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            problems.add(path, "missing; a person writes it")
        return None
    if not isinstance(v, str):
        problems.add(path, f"must be text, got {v!r}")
        return None
    return v


def _names(values: dict, key: str, path: str, problems: _Problems) -> tuple[str, ...]:
    v = values.get(key)
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        problems.add(path, f"must be a list of names, got {v!r}")
        return ()
    return tuple(v)


def _function_ref(text: str, path: str, problems: _Problems) -> FunctionRef | None:
    module, sep, function = text.partition(":")
    if not sep or not _MODULE_RE.fullmatch(module):
        problems.add(path, f"{text!r} is not `module.path:function`")
        return None
    if "." in function and all(re.fullmatch(_IDENT, p) for p in function.split(".")):
        problems.add(path, f"{text!r} names a class method; only a module-level function can "
                           f"be a stage, because a method's owner cannot be resolved safely")
        return None
    if not re.fullmatch(_IDENT, function):
        problems.add(path, f"{text!r} is not `module.path:function`")
        return None
    return FunctionRef(module, function)


def _relative_path(text: str, path: str, problems: _Problems) -> bool:
    if "\\" in text:
        problems.add(path, f"{text!r} holds a backslash; plan paths use forward slashes, and a "
                           f"backslash would let `..\\` leave the repository on Windows")
        return False
    p = PurePosixPath(text)
    if p.is_absolute() or re.match(r"^[A-Za-z]:", text):
        problems.add(path, f"{text!r} is absolute; name a file inside the repository")
        return False
    if ".." in p.parts:
        problems.add(path, f"{text!r} leaves the repository")
        return False
    if _GLOB_CHARS & set(text):
        problems.add(path, f"{text!r} is a pattern; name each file")
        return False
    return True


def _stage(raw, i: int, names_so_far: list[str], problems: _Problems) -> Stage | None:
    at = f"stages[{i}]"
    if not isinstance(raw, dict):
        problems.add(at, f"must be a mapping, got {raw!r}")
        return None
    for key in sorted(set(raw) - _STAGE_KEYS):
        problems.add(f"{at}.{key}", f"not a stage field (the fields are {sorted(_STAGE_KEYS)})")

    name = _text(raw, "name", f"{at}.name", problems, required=True)
    if name is not None and name in names_so_far:
        problems.add(f"{at}.name", f"{name!r} names an earlier stage too; stage names are unique")

    function = None
    fn_text = _text(raw, "function", f"{at}.function", problems, required=False)
    if fn_text is not None:
        function = _function_ref(fn_text, f"{at}.function", problems)
    memory_inputs = _names(raw, "memory_inputs", f"{at}.memory_inputs", problems)
    for m in memory_inputs:
        if not re.fullmatch(_IDENT, m):
            problems.add(f"{at}.memory_inputs", f"{m!r} is not a parameter name")
    if fn_text is None and not memory_inputs:
        problems.add(f"{at}.function", "missing; a stage runs a function, or (with no function) "
                                       "records memory_inputs")
    if fn_text is not None and memory_inputs:
        problems.add(f"{at}.memory_inputs", "only a stage without a function records memory "
                                            "inputs; put them on an intake stage before this one")

    files = _names(raw, "files", f"{at}.files", problems)
    files = tuple(f for f in files if _relative_path(f, f"{at}.files", problems))

    trust = _text(raw, "trust", f"{at}.trust", problems, required=bool(files or memory_inputs))
    if trust is not None:
        if trust == "secret":
            problems.add(f"{at}.trust", "'secret' inputs cannot be recorded: the SDK refuses to "
                                        "compute a digest over them")
        elif trust not in TRUST_CLASSES:
            problems.add(f"{at}.trust", f"{trust!r} is not one of {list(TRUST_CLASSES)}")

    rederivable = None
    rv = raw.get("rederivable")
    if rv is None:
        problems.add(f"{at}.rederivable", "missing; a person says whether this stage can be "
                                          "re-derived (true or false)")
    elif rv is True or rv == "true":
        rederivable = "true"
    elif rv is False or rv == "false":
        rederivable = "false"
    else:
        problems.add(f"{at}.rederivable", f"must be true or false, got {rv!r}")
    note = _text(raw, "rederivable_note", f"{at}.rederivable_note", problems, required=False)

    instrument = None
    ri = raw.get("instrument")
    if ri is None:
        if fn_text is not None:
            problems.add(f"{at}.instrument", "missing; a function stage names its instrument "
                                             "({name, package})")
    elif not isinstance(ri, dict):
        problems.add(f"{at}.instrument", f"must be a mapping {{name, package}}, got {ri!r}")
    else:
        for key in sorted(set(ri) - _INSTRUMENT_KEYS):
            problems.add(f"{at}.instrument.{key}", "not an instrument field (the fields are "
                                                   "name, package and kind; the version is read "
                                                   "at run time, never written)")
        iname = _text(ri, "name", f"{at}.instrument.name", problems, required=True)
        ipkg = _text(ri, "package", f"{at}.instrument.package", problems, required=True)
        ikind = _text(ri, "kind", f"{at}.instrument.kind", problems, required=False)
        if iname and ipkg:
            instrument = InstrumentSpec(iname, ipkg, ikind or DEFAULT_INSTRUMENT_KIND)

    if "inputs" in raw:
        inputs = _names(raw, "inputs", f"{at}.inputs", problems)
        for up in inputs:
            if up not in names_so_far:
                problems.add(f"{at}.inputs", f"{up!r} is not a stage declared before this one")
    else:
        inputs = (names_so_far[-1],) if names_so_far else ()

    if name is None:
        return None
    return Stage(i, name, function, memory_inputs, trust, rederivable or "", note, instrument,
                 inputs, files)


def parse_instrument_plan(text: str, *, source: str) -> InstrumentPlan:
    values = read_document(text, source=source)
    problems = _Problems()
    _find_decides(values, "", problems)

    approved_by = _text(values, "approved_by", "approved_by", problems, required=True)
    if approved_by is not None and names_nobody(approved_by):
        problems.add("approved_by", f"{approved_by!r} names nobody; a person writes who approves")
        approved_by = None

    entry = None
    entry_text = _text(values, "entry", "entry", problems, required=True)
    if entry_text is not None:
        entry = _function_ref(entry_text, "entry", problems)

    run_dir = _text(values, "run_dir", "run_dir", problems, required=True)
    if run_dir is not None and _relative_path(run_dir.replace("{run_id}", "x"), "run_dir", problems):
        if set(re.findall(r"\{([^}]*)\}", run_dir)) - {"run_id"} or "{" in run_dir.replace("{run_id}", ""):
            problems.add("run_dir", f"{run_dir!r}: the only placeholder is {{run_id}}")
        elif "{run_id}" not in run_dir:
            problems.add("run_dir", f"{run_dir!r} has no {{run_id}}; each run is written to a folder of "
                                    f"its own, and a second run into the same folder is refused")

    if "approved_boundaries" not in values:
        problems.add("approved_boundaries", "missing; a person states the approved boundaries, "
                                            "even when there are none (`approved_boundaries: []`)")
        boundaries: tuple[str, ...] = ()
    else:
        boundaries = _names(values, "approved_boundaries", "approved_boundaries", problems)

    stages: list[Stage] = []
    raw_stages = values.get("stages")
    if not raw_stages:
        problems.add("stages", "missing; a person lists the stages, in the order they run")
    elif not isinstance(raw_stages, list):
        problems.add("stages", f"must be a list of stages, got {raw_stages!r}")
    else:
        names: list[str] = []
        for i, raw in enumerate(raw_stages):
            stage = _stage(raw, i, names, problems)
            if stage is not None:
                stages.append(stage)
                names.append(stage.name)

    ci = None
    raw_ci = values.get("ci")
    if raw_ci is None:
        problems.add("ci", "missing; a person says how the workflow installs and runs the "
                           "pipeline, and where the committed baseline is")
    elif not isinstance(raw_ci, dict):
        problems.add("ci", f"must be a mapping {{install, run, baseline}}, got {raw_ci!r}")
    else:
        for key in sorted(set(raw_ci) - _CI_KEYS):
            problems.add(f"ci.{key}", "not a ci field (the fields are install, run and baseline)")
        install = _text(raw_ci, "install", "ci.install", problems, required=True)
        run = _text(raw_ci, "run", "ci.run", problems, required=True)
        baseline = _text(raw_ci, "baseline", "ci.baseline", problems, required=True)
        if baseline is not None:
            _relative_path(baseline, "ci.baseline", problems)
        if install and run and baseline:
            ci = CiSpec(install, run, baseline)

    if problems.items:
        raise PlanRefused(source, problems.items)
    return InstrumentPlan(source, approved_by, entry, run_dir, tuple(stages), boundaries, ci)


def load_instrument_plan(path: Path) -> InstrumentPlan:
    if not path.is_file():
        raise PlanError(f"{path}: no such plan file")
    return parse_instrument_plan(path.read_text(encoding="utf-8"), source=str(path))
