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
        instrument: {name: bm25, package: rank_bm25, kind: retriever}   # version read at run time
        inputs: [intake]                  # optional; the default is the previous stage
        files: [data/corpus.json]         # optional; read with ctx.read_external
        trust: operator-authored
        rederivable: "true"
    approved_boundaries: []               # required, even when empty
    ci:                                   # required: how the workflow runs the pipeline
      install: pip install --require-hashes -r requirements.lock
      run: python -m pipeline.demo
      baseline: runs/baseline

`instrument` needs a `kind` (retriever, chunker, model...), because the SDK's Instrument does. An intake stage (no `function`) may carry an
`instrument` too; without one, its instrument is the SDK call that records the inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from onetrace_ci.plan import PlanError, find_open_questions, names_nobody, read_document

TRUST_CLASSES = ("operator-authored", "model-generated", "externally-sourced")
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_MODULE_RE = re.compile(rf"{_IDENT}(?:\.{_IDENT})*")
_STAGE_KEYS = frozenset({"name", "function", "memory_inputs", "trust", "rederivable",
                         "rederivable_note", "instrument", "inputs", "files", "config", "constants",
                         "repeats", "instances"})
_ENTRY_KEYS = ("entry", "run_dir", "stages", "corpus")
_DOTTED_RE = re.compile(rf"{_IDENT}(?:\.{_IDENT})*")
_FUNCTION_RE = re.compile(rf"{_IDENT}(?:\.{_IDENT})*:{_IDENT}(?:\.{_IDENT})*")
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
    #: Literal settings a person wrote, recorded as the instrument's configuration. None: none.
    config: dict | None = None
    #: Names of parameters (or dotted attributes of one) to record as constants at run time.
    constants: tuple[str, ...] = ()
    #: A stage called more than once in a run, one call after another (a loop): each call is
    #: numbered by the SDK (D1a).
    repeats: bool = False
    #: Named instances of a stage whose calls may overlap: one per call site (D1a).
    instances: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CorpusSpec:
    source: str
    stages: tuple[str, ...]
    #: The ingest stage whose output is the chunk index (the SDK's `corpus_from(..., index_stage=)`).
    #: No default: None when the plan names none.
    index_stage: str | None = None


@dataclass(frozen=True, slots=True)
class EntrySpec:
    """One run type: its entry function, where its runs go, its stages and its corpus link."""
    entry: FunctionRef
    run_dir: str
    stages: tuple[Stage, ...]
    corpus: CorpusSpec | None = None


@dataclass(frozen=True, slots=True)
class CiSpec:
    install: str
    run: str
    baseline: str


@dataclass(frozen=True, slots=True)
class SignSpec:
    key_env: str
    when_key_missing: str


@dataclass(frozen=True, slots=True)
class AnchorSpec:
    source: str
    config: str
    when: str


@dataclass(frozen=True, slots=True)
class TrustSpec:
    file: str
    untrusted_signature: str


@dataclass(frozen=True, slots=True)
class InstrumentPlan:
    source: str
    approved_by: str
    entry: FunctionRef
    run_dir: str
    stages: tuple[Stage, ...]
    approved_boundaries: tuple[str, ...]
    ci: CiSpec
    #: None when the block is absent; "none" when a person chose none explicitly.
    sign: SignSpec | str | None = None
    anchor: AnchorSpec | str | None = None
    trust: TrustSpec | None = None
    corpus: CorpusSpec | None = None
    #: Every entry, in the plan's order: one for a plan without `entries:`. `entry`, `run_dir`,
    #: `stages` and `corpus` above are the first entry's.
    entries: tuple[EntrySpec, ...] = ()


class _Problems:
    def __init__(self):
        self.items: list[str] = []

    def add(self, field: str, why: str):
        self.items.append(f"plan field {field}: {why}")


def _open(value) -> bool:
    """A `DECIDE:` question a person has not answered yet."""
    return isinstance(value, str) and value.lstrip().startswith("DECIDE:")


def _find_decides(value, path: str, problems: _Problems):
    for where, question in find_open_questions(value, path):
        problems.add(where, f"still an open question ({question!r}); a person answers it "
                            f"before anything is generated")


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


def _stage(raw, i: int, names_so_far: list[str], problems: _Problems, prefix: str = "") -> Stage | None:
    at = f"{prefix}stages[{i}]"
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
        if ikind is None and "kind" not in ri:
            problems.add(f"{at}.instrument.kind", f"missing for stage {name!r}; the SDK's Instrument "
                                                  f"requires a kind (retriever, chunker, model...), so a "
                                                  f"person names it")
        if iname and ipkg and ikind:
            instrument = InstrumentSpec(iname, ipkg, ikind)

    if "inputs" in raw:
        inputs = _names(raw, "inputs", f"{at}.inputs", problems)
        for up in inputs:
            if up not in names_so_far:
                problems.add(f"{at}.inputs", f"{up!r} is not a stage declared before this one")
    else:
        inputs = (names_so_far[-1],) if names_so_far else ()

    config = None
    if "config" in raw:
        rc = raw["config"]
        if not isinstance(rc, dict):
            problems.add(f"{at}.config", f"must be a mapping of literal settings, got {rc!r}")
        else:
            for key, value in rc.items():
                if isinstance(value, bool):
                    problems.add(f"{at}.config.{key}", f"{value!r} is a boolean, which the SDK does not "
                                                       f"yet record as a setting; quote it (\"true\") to "
                                                       f"record the text")
                elif not isinstance(value, str):
                    problems.add(f"{at}.config.{key}", f"must be a literal value, got {value!r}")
            config = dict(rc)
    constants = _names(raw, "constants", f"{at}.constants", problems)
    for c in constants:
        if not _DOTTED_RE.fullmatch(c):
            problems.add(f"{at}.constants", f"{c!r} is not a parameter name or a dotted attribute of one")

    repeats = False
    rr = raw.get("repeats")
    if rr is True or rr == "true":
        repeats = True
    elif rr is not None and rr is not False and rr != "false":
        problems.add(f"{at}.repeats", f"must be true or false, got {rr!r}")
    instances = _names(raw, "instances", f"{at}.instances", problems)
    for dup in sorted({x for x in instances if instances.count(x) > 1}):
        problems.add(f"{at}.instances", f"{dup!r} is named twice; each instance names one call")

    if name is None:
        return None
    return Stage(i, name, function, memory_inputs, trust, rederivable or "", note, instrument,
                 inputs, files, config=config, constants=constants, repeats=repeats, instances=instances)


_BLOCKS = {
    "sign": {"key_env": None, "when_key_missing": ("unsigned", "refuse")},
    "anchor": {"source": None, "config": None, "when": ("main", "every-run", "none")},
    "trust": {"file": None, "untrusted_signature": ("review", "fail")},
}
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _looks_like_key_material(value: str) -> str | None:
    """Why `value` looks like a key rather than the name of an environment variable, or None."""
    if "BEGIN" in value and "-----" in value:
        return "it holds a PEM header"
    if not _ENV_NAME_RE.fullmatch(value):
        if "/" in value or "\\" in value or value.endswith((".pem", ".key")):
            return "it is a path"
        return "it is not an environment variable name"
    if len(value) >= 32 and "_" not in value and any(c.islower() for c in value)             and any(c.isupper() for c in value):
        return "it has the length and alphabet of an encoded key"
    return None


def _block(values: dict, name: str, problems: _Problems):
    """A `sign`, `anchor` or `trust` block: absent -> None; `none` -> "none"; else the fields,
    every one of them required (each is either a person's decision or a name only they know)."""
    raw = values.get(name)
    if raw is None:
        return None
    if raw == "none" and name != "trust":
        return "none"
    if isinstance(raw, str) and raw.lstrip().startswith("DECIDE:"):
        return None                          # already named as an open question
    if not isinstance(raw, dict):
        problems.add(name, f"must be a mapping of {sorted(_BLOCKS[name])}"
                           + (" or `none`" if name != "trust" else "") + f", got {raw!r}")
        return None
    fields = _BLOCKS[name]
    for key in sorted(set(raw) - set(fields)):
        problems.add(f"{name}.{key}", f"not a {name} field (the fields are {sorted(fields)})")
    got = {}
    for key, allowed in fields.items():
        value = _text(raw, key, f"{name}.{key}", problems, required=True)
        if value is None:
            continue
        if allowed is not None and value not in allowed:
            problems.add(f"{name}.{key}", f"{value!r} is not one of {list(allowed)}; a person decides")
            continue
        got[key] = value
    if name == "sign" and "key_env" in got:
        why = _looks_like_key_material(got["key_env"])
        if why:
            problems.add("sign.key_env", f"must name the environment variable that holds the key, "
                                         f"never the key itself, and this value looks like key "
                                         f"material ({why}); the plan must never carry a key")
            got.pop("key_env")
    for key in ("config", "file"):
        if key in got:
            _relative_path(got[key], f"{name}.{key}", problems)
    if len(got) != len(fields):
        return None
    return {"sign": SignSpec, "anchor": AnchorSpec, "trust": TrustSpec}[name](**got)


def parse_instrument_plan(text: str, *, source: str) -> InstrumentPlan:
    values = read_document(text, source=source)
    problems = _Problems()
    _find_decides(values, "", problems)

    approved_by = _text(values, "approved_by", "approved_by", problems, required=True)
    if approved_by is not None and names_nobody(approved_by):
        problems.add("approved_by", f"{approved_by!r} names nobody; a person writes who approves")
        approved_by = None

    entries: list[tuple[int, EntrySpec]] = []
    if "entries" in values:
        #: Each entry is one run type; a plan names them there, or one at the top level.
        for key in _ENTRY_KEYS:
            if key in values:
                problems.add(key, f"a plan with entries has no top-level {key}; it belongs inside each of entries")
        raw_entries = values.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            problems.add("entries", f"must be a list of entries, each a mapping of {list(_ENTRY_KEYS)}, "
                                    f"got {raw_entries!r}")
        else:
            for i, raw in enumerate(raw_entries):
                if not isinstance(raw, dict):
                    problems.add(f"entries[{i}]", f"must be a mapping of {list(_ENTRY_KEYS)}, got {raw!r}")
                    continue
                for key in sorted(set(raw) - set(_ENTRY_KEYS)):
                    problems.add(f"entries[{i}].{key}", f"not an entries field (the fields are {list(_ENTRY_KEYS)})")
                spec = _entry(raw, f"entries[{i}].", problems)
                if spec is not None:
                    entries.append((i, spec))
            _check_entries(entries, problems)
    else:
        spec = _entry(values, "", problems)
        if spec is not None:
            entries.append((0, spec))

    if "approved_boundaries" not in values:
        problems.add("approved_boundaries", "missing; a person states the approved boundaries, "
                                            "even when there are none (`approved_boundaries: []`)")
        boundaries: tuple[str, ...] = ()
    else:
        boundaries = _names(values, "approved_boundaries", "approved_boundaries", problems)

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

    sign = _block(values, "sign", problems)
    anchor = _block(values, "anchor", problems)
    trust = _block(values, "trust", problems)
    if sign is not None and sign != "none" and "trust" not in values:
        problems.add("trust", "missing; a plan that signs runs names the trust file of recorder "
                              "keys, and whether an untrusted signature is review or fail")

    if problems.items:
        raise PlanRefused(source, problems.items)
    specs = tuple(spec for _, spec in entries)
    first = specs[0]
    return InstrumentPlan(source, approved_by, first.entry, first.run_dir, first.stages, boundaries, ci,
                          sign, anchor, trust, first.corpus, entries=specs)


def _entry(values: dict, prefix: str, problems: _Problems) -> EntrySpec | None:
    """One entry's fields, from the top level of a plan (prefix "") or from one of its entries."""
    entry = None
    entry_text = _text(values, "entry", f"{prefix}entry", problems, required=True)
    if entry_text is not None:
        entry = _function_ref(entry_text, f"{prefix}entry", problems)

    field = f"{prefix}run_dir"
    run_dir = _text(values, "run_dir", field, problems, required=True)
    if run_dir is not None and _relative_path(run_dir.replace("{run_id}", "x"), field, problems):
        if set(re.findall(r"\{([^}]*)\}", run_dir)) - {"run_id"} or "{" in run_dir.replace("{run_id}", ""):
            problems.add(field, f"{run_dir!r}: the only placeholder is {{run_id}}")
        elif "{run_id}" not in run_dir:
            problems.add(field, f"{run_dir!r} has no {{run_id}}; each run is written to a folder of "
                                f"its own, and a second run into the same folder is refused")

    stages: list[Stage] = []
    raw_stages = values.get("stages")
    if not raw_stages:
        problems.add(f"{prefix}stages", "missing; a person lists the stages, in the order they run")
    elif not isinstance(raw_stages, list):
        problems.add(f"{prefix}stages", f"must be a list of stages, got {raw_stages!r}")
    else:
        names: list[str] = []
        for i, raw in enumerate(raw_stages):
            stage = _stage(raw, i, names, problems, prefix)
            if stage is not None:
                stages.append(stage)
                names.append(stage.name)

    corpus = _corpus(values.get("corpus"), prefix, stages, problems)
    if entry is None or run_dir is None:
        return None
    return EntrySpec(entry, run_dir, tuple(stages), corpus)


def _corpus(raw_corpus, prefix: str, stages: list[Stage], problems: _Problems) -> CorpusSpec | None:
    if raw_corpus is None or _open(raw_corpus):
        return None
    at = f"{prefix}corpus"
    if not isinstance(raw_corpus, dict):
        problems.add(at, f"must be a mapping {{from, stages, index_stage}}, got {raw_corpus!r}")
        return None
    #: Each answered field is checked; one still holding a `DECIDE:` question is named once, as
    #: an open question, and not also as a wrong value.
    for key in sorted(set(raw_corpus) - {"from", "stages", "index_stage"}):
        problems.add(f"{at}.{key}", "not a corpus field (the fields are from, stages and index_stage)")
    source_ = None
    if not _open(raw_corpus.get("from")):
        source_ = _text(raw_corpus, "from", f"{at}.from", problems, required=True)
        #: A run folder in the repository, as E2 has it.
        if source_ is not None and not _relative_path(source_, f"{at}.from", problems):
            source_ = None
    linked: tuple[str, ...] = ()
    raw_linked = raw_corpus.get("stages")
    if not (_open(raw_linked) or (isinstance(raw_linked, list) and any(_open(s) for s in raw_linked))):
        linked = _names(raw_corpus, "stages", f"{at}.stages", problems)
        if not linked:
            problems.add(f"{at}.stages", "missing; a person names the stages the link is recorded on")
        planned = {s.name for s in stages}
        for s in linked:
            if s not in planned:
                problems.add(f"{at}.stages", f"{s!r} is not a planned stage")
    #: No default, as in the SDK: without it, the link is recorded bare. It names a stage of the
    #: ingest run: in a plan with entries, a stage of another entry (checked there); a
    #: function's `module:name` is not a stage name.
    index_stage = None
    if not _open(raw_corpus.get("index_stage")):
        index_stage = _text(raw_corpus, "index_stage", f"{at}.index_stage", problems, required=False)
        if index_stage is not None and _FUNCTION_RE.fullmatch(index_stage):
            problems.add(f"{at}.index_stage", f"{index_stage!r} names a function; name the ingest stage "
                                              f"(its name in the ingest's plan) whose output is the chunk index")
            index_stage = None
    if source_ and linked:
        return CorpusSpec(source_, linked, index_stage)
    return None


def _check_entries(entries: list[tuple[int, EntrySpec]], problems: _Problems) -> None:
    """What holds across a plan's entries: each has its own entry function and its own
    run_dir, and an index_stage names a stage of another entry (the ingest's)."""
    for n, (i, spec) in enumerate(entries):
        for j, other in entries[:n]:
            if spec.entry == other.entry:
                problems.add(f"entries[{i}].entry", f"{spec.entry} is also entries[{j}]'s entry; each entry "
                                                    f"is one run type")
            #: As paths: `runs/{run_id}` and `runs/{run_id}/` are one folder.
            if PurePosixPath(spec.run_dir) == PurePosixPath(other.run_dir):
                problems.add(f"entries[{i}].run_dir", f"{spec.run_dir!r} is also entries[{j}]'s; each entry's "
                                                      f"runs are written to a folder of their own")
    for i, spec in entries:
        if spec.corpus is None or spec.corpus.index_stage is None:
            continue
        name = spec.corpus.index_stage
        others = {s.name for j, e in entries if j != i for s in e.stages}
        if name in others:
            continue
        if name in {s.name for s in spec.stages}:
            why = (f"{name!r} is a stage of this entry; index_stage names the ingest stage whose output is "
                   f"the chunk index, a stage of another entry")
        else:
            why = (f"{name!r} is not a stage of another entry (theirs are "
                   f"{sorted(others) if others else 'none'})")
        problems.add(f"entries[{i}].corpus.index_stage", why)


def load_instrument_plan(path: Path) -> InstrumentPlan:
    if not path.is_file():
        raise PlanError(f"{path}: no such plan file")
    return parse_instrument_plan(path.read_text(encoding="utf-8"), source=str(path))
