"""`onetrace-ci instrument`: turn a reviewed plan into a small patch that instruments a pipeline.

    onetrace-ci instrument --plan onetrace-plan.yaml --repo . --out instrument.patch

It writes a patch, never an edit in place, and prints what the patch will change. Applying it
(`git apply instrument.patch`) is the user's step. Run on code it has already instrumented from
the same plan, it writes an empty patch.

The tool writes the boilerplate; people own the meaning. The patch records what the plan
names. It does not find stages the plan does not list.

WHAT THE PATCH DOES
-------------------
In the entry module only, it adds one delimited block of helpers after the imports, and
rewrites the entry function:

- one `Recorder` per run, created inside the entry function, with `close()` in a `finally`;
  never a module-level recorder;
- each planned function becomes a stage, in the declared order: the call site in the entry
  function is pointed at a wrapper, defined inside the entry function, that reads the stage's
  inputs (the artifacts of the stages it names, and its files with `ctx.read_external`), calls
  the original function unchanged, and stores its return value as the stage's output;
- a stage without a function (an intake stage) records the entry parameters the plan names
  with `ctx.read_memory`: their digests, never their values;
- instrument versions are read at run time with `importlib.metadata.version(package)`, never
  written into the code.

It also adds `.github/workflows/onetrace.yml`, which runs the pipeline and then the
`onetrace-ci gate` action against the committed baseline, with `contents: read` and every
action pinned to a commit.

WHAT IT REFUSES
---------------
Anything it cannot handle safely is refused with the file and line (or the plan field), and no
patch is written: a missing meaning field in the plan; a function it cannot resolve; a nested
stage call (in the entry's arguments, or one stage's body calling another); a stage called
conditionally, in a loop, in a lambda or comprehension, more than once, or out of order; a
stage used as a value, or any other dynamic dispatch; a stage that is a lambda, a generator,
an async function, or not a function definition at all; a Recorder already created at module
level or inside the entry function.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from onetrace_ci.errors import format_refusal
from onetrace_ci.instrument_plan import (FunctionRef, InstrumentPlan, PlanError, PlanRefused, SignSpec,
                                         Stage, load_instrument_plan)

#: The gate action the generated workflow runs, pinned to a commit, never a branch: the commit
#: whose plan reader refuses what two YAML readers would read differently, and whose own
#: action pins its steps to commits.
GATE_ACTION = "shamiksaharcciit-oss/onetrace-ci@fe9a3109bb44db35f39f877a40d5c1b725705a78"
CHECKOUT_ACTION = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"          # v4.4.0
SETUP_PYTHON_ACTION = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"  # v5.6.0
WORKFLOW_PATH = ".github/workflows/onetrace.yml"
CANDIDATE_RUN_ID = "onetrace-ci-candidate"
MARKER = "# onetrace-ci instrument: generated from the plan"
_MARKER_RE = re.compile(re.escape(MARKER) + r" \(sha256:([0-9a-f]{16})\)")
PREFIX = "_onetrace_"
_RESERVED = ("_onetrace", "_Onetrace", "_ONETRACE")
_ANCHOR_REASON = "epoch anchoring is not implemented"
#: A fixed table, not `mimetypes`, whose answers depend on the machine it runs on.
MEDIA_TYPES = {".json": "application/json", ".jsonl": "application/jsonl", ".txt": "text/plain",
               ".md": "text/markdown", ".html": "text/html", ".htm": "text/html",
               ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".pdf": "application/pdf",
               ".xml": "application/xml", ".yaml": "application/yaml", ".yml": "application/yaml"}


class Refused(Exception):
    """The command cannot produce a safe patch. The message names every file and line, or plan
    field, that stands in the way."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n  ".join(self.problems))


@dataclass
class Result:
    patch: str
    files: list[str]
    summary: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ source model

@dataclass
class _Module:
    name: str
    path: Path
    rel: str
    raw: bytes
    tree: object          # libcst.Module (the MetadataWrapper's own copy)
    positions: object     # node -> CodeRange

    def line(self, node) -> int:
        return self.positions[node].start.line

    def at(self, node) -> str:
        return f"{self.rel}:{self.line(node)}"

    def code(self, node) -> str:
        return self.tree.code_for_node(node)


@dataclass
class _Binding:
    kind: str                  # "module" | "from" | "def" | "asyncdef" | "class" | "assign"
    target: str = ""           # module name ("module", "from")
    name: str = ""             # imported name ("from")
    node: object = None        # the defining node ("def", "asyncdef", "class", "assign")


@dataclass
class _Target:
    """A name resolved through imports to where it is defined."""
    module: _Module
    name: str
    binding: _Binding

    @property
    def key(self) -> tuple[str, str]:
        return (self.module.name, self.name)


class _Source:
    """Parses and caches the repository's modules, and resolves names through their imports."""

    def __init__(self, repo: Path):
        import libcst as cst
        from libcst.metadata import MetadataWrapper, PositionProvider

        self.cst, self._wrapper, self._positions = cst, MetadataWrapper, PositionProvider
        self.repo = repo
        self.modules: dict[str, _Module | None] = {}
        self._bindings: dict[str, dict[str, _Binding | None]] = {}

    def module_file(self, name: str) -> tuple[Path | None, list[str]]:
        parts = name.split(".")
        looked, hits = [], []
        for base in (self.repo, self.repo / "src"):
            #: Both are checked: where a module is a file and a package at once, Python imports the
            #: package, and reading the file instead would analyse code that never runs.
            for cand in (base.joinpath(*parts[:-1], parts[-1] + ".py"), base.joinpath(*parts, "__init__.py")):
                looked.append(cand.relative_to(self.repo).as_posix())
                if cand.is_file():
                    hits.append(cand)
        if len(hits) > 1:
            raise Refused(f"module {name!r} is both {hits[0].relative_to(self.repo).as_posix()} and "
                          f"{hits[1].relative_to(self.repo).as_posix()}; which one runs depends on "
                          f"the path, so it cannot be resolved safely")
        return (hits[0] if hits else None), looked

    def load(self, name: str) -> _Module | None:
        if name in self.modules:
            return self.modules[name]
        path, _ = self.module_file(name)
        if path is None:
            self.modules[name] = None
            return None
        raw = path.read_bytes()
        rel = path.relative_to(self.repo).as_posix()
        try:
            parsed = self.cst.parse_module(raw)
        except self.cst.ParserSyntaxError as e:
            #: LibCST reports a tokenizer error at line 1 wherever it is; Python's own compiler
            #: knows the line.
            line = e.raw_line
            try:
                compile(raw, rel, "exec", dont_inherit=True)
            except SyntaxError as se:
                line = se.lineno or line
            raise Refused(f"{rel}:{line}: not valid Python: {e.message}")
        wrapper = self._wrapper(parsed)
        module = _Module(name, path, rel, raw, wrapper.module, wrapper.resolve(self._positions))
        self.modules[name] = module
        return module

    def _package(self, module: _Module) -> str:
        return module.name if module.path.name == "__init__.py" else module.name.rpartition(".")[0]

    def _from_target(self, module: _Module, node) -> str:
        cst = self.cst
        dotted = module.code(node.module) if node.module is not None else ""
        level = len(node.relative)
        if not level:
            return dotted
        base = self._package(module).split(".") if self._package(module) else []
        if level - 1 > len(base):
            return ""
        base = base[:len(base) - (level - 1)]
        return ".".join([*base, dotted] if dotted else base)

    def bindings(self, module: _Module) -> dict[str, _Binding | None]:
        """Top-level names of `module`. A name bound two different ways maps to None."""
        if module.name in self._bindings:
            return self._bindings[module.name]
        cst = self.cst
        found: dict[str, _Binding | None] = {}

        def bind(name: str, b: _Binding):
            if name in found and found[name] != b:
                found[name] = None
            else:
                found[name] = b

        def walk(stmts):
            for stmt in stmts:
                if isinstance(stmt, cst.FunctionDef):
                    bind(stmt.name.value, _Binding("asyncdef" if stmt.asynchronous else "def", node=stmt))
                elif isinstance(stmt, cst.ClassDef):
                    bind(stmt.name.value, _Binding("class", node=stmt))
                elif isinstance(stmt, cst.SimpleStatementLine):
                    for small in stmt.body:
                        if isinstance(small, cst.Import):
                            for alias in small.names:
                                dotted = module.code(alias.name)
                                if alias.asname is not None:
                                    bind(alias.asname.name.value, _Binding("module", target=dotted))
                                else:
                                    head = dotted.split(".")[0]
                                    bind(head, _Binding("module", target=head))
                        elif isinstance(small, cst.ImportFrom):
                            target = self._from_target(module, small)
                            if isinstance(small.names, cst.ImportStar):
                                found["*"] = _Binding("from", target=target, name="*", node=stmt)
                                continue
                            for alias in small.names:
                                name = module.code(alias.name)
                                local = alias.asname.name.value if alias.asname is not None else name
                                bind(local, _Binding("from", target=target, name=name))
                        elif isinstance(small, (cst.Assign, cst.AnnAssign)):
                            targets = ([t.target for t in small.targets] if isinstance(small, cst.Assign)
                                       else [small.target])
                            for t in targets:
                                if isinstance(t, cst.Name):
                                    bind(t.value, _Binding("assign", node=stmt))
                elif isinstance(stmt, (cst.If, cst.Try, cst.With)):
                    for block in _blocks(cst, stmt):
                        walk(block)

        walk(module.tree.body)
        self._bindings[module.name] = found
        return found

    def _resolve_from(self, b: _Binding, module_name: str, name: str, depth: int) -> _Target | None:
        """As Python does: `from pkg import name` is pkg's own attribute when pkg defines one,
        and otherwise the submodule pkg.name (which `from . import name` in pkg re-exports)."""
        sub = self.load(f"{b.target}.{b.name}") if b.target else None
        owner = self.load(b.target) if b.target else None
        own = self.bindings(owner).get(b.name) if owner is not None else None
        if sub is not None and (own is None or own.kind in ("from", "module")):
            return _Target(sub, "", _Binding("module", target=sub.name))
        if (b.target, b.name) == (module_name, name):
            return None
        return self.resolve(b.target, b.name, depth + 1)

    def resolve(self, module_name: str, name: str, depth: int = 0) -> _Target | None:
        """Follow `name` in `module_name` through re-exports to where it is defined."""
        module = self.load(module_name)
        if module is None or depth > 8:
            return None
        b = self.bindings(module).get(name)
        if b is None:
            if name in self.bindings(module):
                return None                    # bound two different ways
            sub = self.load(f"{module_name}.{name}")
            return None if sub is None else _Target(sub, "", _Binding("module", target=sub.name))
        if b.kind == "from":
            return self._resolve_from(b, module_name, name, depth)
        if b.kind == "module":
            sub = self.load(b.target)
            return None if sub is None else _Target(sub, "", b)
        return _Target(module, name, b)

    def local_imports(self, module: _Module, fn) -> dict[str, _Binding]:
        """Names bound by import statements inside the function `fn` (a stage's body may import
        what it calls)."""
        cst, found = self.cst, {}
        src = self

        class V(cst.CSTVisitor):
            def visit_Import(self, node):
                for alias in node.names:
                    dotted = module.code(alias.name)
                    if alias.asname is not None:
                        found[alias.asname.name.value] = _Binding("module", target=dotted)
                    else:
                        found[dotted.split(".")[0]] = _Binding("module", target=dotted.split(".")[0])

            def visit_ImportFrom(self, node):
                if isinstance(node.names, cst.ImportStar):
                    return
                target = src._from_target(module, node)
                for alias in node.names:
                    name = module.code(alias.name)
                    local = alias.asname.name.value if alias.asname is not None else name
                    found[local] = _Binding("from", target=target, name=name)

        fn.visit(V())
        return found

    def head(self, expr) -> str | None:
        """The leftmost name of `name` or `a.b.c`, or None for anything else."""
        while isinstance(expr, self.cst.Attribute):
            expr = expr.value
        return expr.value if isinstance(expr, self.cst.Name) else None

    def resolve_expr(self, module: _Module, expr, local_names: frozenset = frozenset(),
                     extra: dict | None = None) -> _Target | None:
        """Resolve a callee written as `name` or `a.b.c` in `module` to its definition. Names in
        `extra` (imports inside a function) are looked up before the module's own."""
        cst = self.cst
        chain = []
        while isinstance(expr, cst.Attribute):
            chain.append(expr.attr.value)
            expr = expr.value
        if not isinstance(expr, cst.Name) or expr.value in local_names:
            return None
        chain.append(expr.value)
        chain.reverse()
        head, rest = chain[0], chain[1:]
        b = (extra or {}).get(head) or self.bindings(module).get(head)
        if b is None:
            return None
        if b.kind in ("def", "asyncdef", "class", "assign"):
            return _Target(module, head, b) if not rest else None
        if b.kind == "module":
            mod_name = b.target
        elif extra and head in extra:
            t = self._resolve_from(b, module.name, head, 0)
            if t is None:
                return None
            if t.name:
                return t if not rest else None
            mod_name = t.module.name
        else:
            t = self.resolve(module.name, head)
            if t is None:
                return None
            if t.name:                                   # a function or other value
                return t if not rest else None
            mod_name = t.module.name
        while len(rest) > 1:
            mod_name = f"{mod_name}.{rest.pop(0)}"
        if not rest:
            return None
        return self.resolve(mod_name, rest[0])


def _blocks(cst, stmt):
    """The statement lists directly inside a compound statement."""
    out = []
    for attr in ("body", "orelse", "finalbody"):
        part = getattr(stmt, attr, None)
        if part is None:
            continue
        if isinstance(part, (cst.Else, cst.Finally)):
            part = part.body
        elif isinstance(part, cst.If):         # elif
            out.extend(_blocks(cst, part))
            continue
        if isinstance(part, cst.IndentedBlock):
            out.append(list(part.body))
        elif isinstance(part, cst.SimpleStatementSuite):
            out.append([part])
    for handler in getattr(stmt, "handlers", ()) or ():
        out.append(list(handler.body.body) if isinstance(handler.body, cst.IndentedBlock) else [])
    return out


def _is_generator(cst, fn) -> bool:
    """True if `fn`'s own body yields (not a nested function's or lambda's)."""
    found = False

    class V(cst.CSTVisitor):
        def visit_FunctionDef(self, node):
            return node is fn
        def visit_Lambda(self, node):
            return False
        def visit_ClassDef(self, node):
            return False
        def visit_Yield(self, node):
            nonlocal found
            found = True

    fn.visit(V())
    return found


def _recorder_calls(cst, node, aliases: frozenset = frozenset()) -> list:
    """Calls under `node` to anything named `Recorder`, or to a name the module imported a
    `Recorder` under (`from onetrace.emit import Recorder as Rec`)."""
    out = []
    names = {"Recorder", *aliases}

    class V(cst.CSTVisitor):
        def visit_Call(self, call):
            f = call.func
            if (isinstance(f, cst.Name) and f.value in names) or \
                    (isinstance(f, cst.Attribute) and f.attr.value == "Recorder"):
                out.append(call)

    node.visit(V())
    return out


def _recorder_aliases(src: "_Source", module: _Module) -> frozenset:
    return frozenset(local for local, b in src.bindings(module).items()
                     if b is not None and b.kind == "from" and b.name == "Recorder")


def _module_level_recorders(cst, module: _Module, aliases: frozenset) -> list[str]:
    hits = []
    for stmt in module.tree.body:
        if isinstance(stmt, cst.FunctionDef):
            continue
        if isinstance(stmt, cst.ClassDef):
            inner = [s for s in stmt.body.body if not isinstance(s, cst.FunctionDef)] \
                if isinstance(stmt.body, cst.IndentedBlock) else []
            for s in inner:
                hits.extend(module.at(c) for c in _recorder_calls(cst, s, aliases))
            continue
        hits.extend(module.at(c) for c in _recorder_calls(cst, stmt, aliases))
    return hits


# ------------------------------------------------------------------ analysis

@dataclass
class _StageCall:
    stage: Stage
    call: object
    stmt_index: int
    order: int              # position among stage calls in source order


@dataclass
class _Analysis:
    plan: InstrumentPlan
    plan_rel: str
    entry_module: _Module
    entry_fn: object
    calls: list[_StageCall]
    callee_code: dict[str, str]         # stage name -> the callee as written at its call site
    notes: list[str]


def _slug_ident(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "stage"


def _slug_file(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "stage"


def _params(cst, fn) -> list[str]:
    p = fn.params
    names = [x.name.value for x in (*p.posonly_params, *p.params, *p.kwonly_params)]
    for star in (p.star_arg, p.star_kwarg):
        if isinstance(star, cst.Param):
            names.append(star.name.value)
    return names


def _analyse(src: _Source, plan: InstrumentPlan, plan_rel: str) -> _Analysis:
    cst = src.cst
    problems: list[str] = []

    # The entry function.
    entry_path, looked = src.module_file(plan.entry.module)
    if entry_path is None:
        raise Refused(f"plan field entry: {plan.entry}: no module file for {plan.entry.module!r} "
                      f"(looked for {', '.join(looked)})")
    entry_mod = src.load(plan.entry.module)
    eb = src.bindings(entry_mod).get(plan.entry.function)
    if eb is None or eb.kind not in ("def", "asyncdef"):
        raise Refused(f"plan field entry: {plan.entry}: {entry_mod.rel} has no top-level function "
                      f"{plan.entry.function!r}")
    entry_fn = eb.node
    if eb.kind == "asyncdef":
        problems.append(f"{entry_mod.at(entry_fn)}: the entry function {plan.entry} is async; "
                        f"only a plain function can own the run's Recorder")
    if _is_generator(cst, entry_fn):
        problems.append(f"{entry_mod.at(entry_fn)}: the entry function {plan.entry} is a generator; "
                        f"its run has no single end to close the Recorder at")

    # Every function stage resolves to a plain function definition.
    targets: dict[str, _Target] = {}
    for stage in plan.stages:
        if stage.function is None:
            continue
        at = f"plan field stages[{stage.index}].function"
        ref = stage.function
        path, looked = src.module_file(ref.module)
        if path is None:
            problems.append(f"{at}: {ref}: no module file for {ref.module!r} (looked for {', '.join(looked)})")
            continue
        t = src.resolve(ref.module, ref.function)
        mod = src.load(ref.module)
        if t is None or not t.name:
            problems.append(f"{at}: {ref}: {mod.rel} has no top-level function {ref.function!r} "
                            f"that can be resolved")
            continue
        b = t.binding
        if b.kind == "assign":
            value = b.node.body[0].value if isinstance(b.node, cst.SimpleStatementLine) else None
            if isinstance(value, cst.Lambda):
                problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is a lambda; "
                                f"a stage must be a named function definition")
            else:
                problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is bound by "
                                f"assignment, not a function definition, so what it calls cannot be "
                                f"resolved safely")
            continue
        if b.kind == "class":
            problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is a class, not a "
                            f"function definition")
            continue
        if b.kind == "asyncdef":
            problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is an async "
                            f"function; only plain functions can be stages")
            continue
        if _is_generator(cst, b.node):
            problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is a generator; a "
                            f"stage returns one value, and a generator returns before its work is done")
            continue
        if b.node is entry_fn:
            problems.append(f"{at}: {ref} is the entry function itself; the entry function owns the "
                            f"run, and stages are the functions it calls")
            continue
        for other_name, other in targets.items():
            if other.key == t.key:
                problems.append(f"{at}: {ref} is also stage {other_name!r}'s function; two stages "
                                f"cannot share one function, because their calls cannot be told apart")
        targets[stage.name] = t
    # Every constants name hangs off a parameter of its stage function.
    for stage in plan.stages:
        if stage.constants and stage.function is None:
            problems.append(f"plan field stages[{stage.index}].constants: a stage without a function has "
                            f"no parameters to record constants from")
        if not stage.constants or stage.name not in targets:
            continue
        t = targets[stage.name]
        params = _params(cst, t.binding.node)
        for c in stage.constants:
            if c.split(".")[0] not in params:
                problems.append(f"{t.module.at(t.binding.node)}: plan field stages[{stage.index}].constants: "
                                f"{c!r} cannot be resolved: it is not a parameter of "
                                f"{stage.function.function}, or a dotted attribute of one (its parameters "
                                f"are {params})")
    if problems:
        raise Refused(problems)

    by_key = {t.key: name for name, t in targets.items()}

    # No stage calls another stage from inside its own body.
    for name, t in targets.items():
        local = src.local_imports(t.module, t.binding.node)

        class V(cst.CSTVisitor):
            def visit_Call(self, call, t=t, name=name, local=local):
                callee = src.resolve_expr(t.module, call.func, extra=local)
                if callee is not None and callee.key in by_key:
                    problems.append(f"{t.module.at(call)}: stage {name!r} calls stage "
                                    f"{by_key[callee.key]!r} inside its own body; nested stage calls "
                                    f"are refused, because the inner one would run inside the outer "
                                    f"stage and out of the declared order")
        t.binding.node.visit(V())

    # No Recorder at module level, anywhere the run touches; and no star import, which makes
    # the names a module uses impossible to resolve.
    involved = {entry_mod.name: entry_mod, **{t.module.name: t.module for t in targets.values()}}
    for module in involved.values():
        star = src.bindings(module).get("*")
        if star is not None and star.node is not None:
            problems.append(f"{module.at(star.node)}: a star import makes the names this module "
                            f"uses unresolvable (a later one can rebind a stage's name); import "
                            f"names explicitly")
        for where in _module_level_recorders(cst, module, _recorder_aliases(src, module)):
            problems.append(f"{where}: a module-level Recorder is already present; a recorder "
                            f"belongs to one run, so onetrace-ci creates it inside the entry function")
    for call in _recorder_calls(cst, entry_fn, _recorder_aliases(src, entry_mod)):
        problems.append(f"{entry_mod.at(call)}: {plan.entry} already creates a Recorder; this "
                        f"code is already instrumented by hand")

    # No name the generated code would shadow.
    class Names(cst.CSTVisitor):
        def visit_Name(self, node):
            if node.value.startswith(_RESERVED):
                problems.append(f"{entry_mod.at(node)}: the name {node.value!r} starts with "
                                f"{PREFIX!r}, which the generated code reserves")
    entry_mod.tree.visit(Names())

    # Memory inputs are parameters of the entry function.
    params = _params(cst, entry_fn)
    for stage in plan.stages:
        for m in stage.memory_inputs:
            if m not in params:
                problems.append(f"plan field stages[{stage.index}].memory_inputs: {m!r} is not a "
                                f"parameter of {plan.entry.function} ({entry_mod.at(entry_fn)}); "
                                f"its parameters are {params}")
    if problems:
        raise Refused(problems)

    # The entry body: every stage called directly, once, in order, at the top level.
    body = entry_fn.body
    stmts = list(body.body) if isinstance(body, cst.IndentedBlock) else [cst.SimpleStatementLine(body=body.body)]
    calls: list[_StageCall] = []
    callee_code: dict[str, str] = {}
    notes: list[str] = []
    stage_by_name = {s.name: s for s in plan.stages}
    counter = [0]
    compound_names = {cst.For: "a for loop", cst.While: "a while loop", cst.If: "an if block",
                      cst.With: "a with block", cst.Try: "a try block", cst.TryStar: "a try block",
                      cst.FunctionDef: "a nested function", cst.ClassDef: "a class body",
                      cst.Match: "a match block"}
    shadowing = _local_names(cst, entry_fn) | set(params)

    class Body(cst.CSTVisitor):
        """Finds the stage calls in one top-level statement of the entry function, and refuses
        every place a call might not run exactly once, in the order written."""

        def __init__(self, index: int):
            super().__init__()
            self.index, self.context, self.callees = index, [], set()

        def _push(self, why):
            self.context.append(why)

        def _pop(self, _node=None):
            self.context.pop()

        def visit_Lambda(self, node):
            self._push("inside a lambda")

        def visit_ListComp(self, node):
            self._push("inside a comprehension")

        def visit_SetComp(self, node):
            self._push("inside a comprehension")

        def visit_DictComp(self, node):
            self._push("inside a comprehension")

        def visit_GeneratorExp(self, node):
            self._push("inside a generator expression")

        def visit_IfExp(self, node):
            self._push("called conditionally")

        def visit_BooleanOperation(self, node):
            self._push("called conditionally")

        def visit_Comparison(self, node):
            #: `a < b < c` stops early, so the later operands may never be evaluated.
            self._push("called conditionally" if len(node.comparisons) > 1 else None)

        def visit_Assert(self, node):
            self._push("inside an assert, which `python -O` removes")

        leave_Lambda = leave_ListComp = leave_SetComp = leave_DictComp = leave_GeneratorExp = _pop
        leave_IfExp = leave_BooleanOperation = leave_Comparison = leave_Assert = _pop

        def visit_Arg(self, node):
            #: A keyword's name is not a use of anything; only its value is.
            if node.keyword is not None:
                node.value.visit(self)
                return False

        def visit_Call(self, call):
            f = call.func
            self.callees.add(id(f))
            dynamic = isinstance(f, cst.Subscript) or (
                isinstance(f, cst.Call) and isinstance(f.func, cst.Name)
                and f.func.value in ("getattr", "globals", "locals", "vars", "eval", "__import__"))
            if dynamic:
                problems.append(f"{entry_mod.at(call)}: a call through {entry_mod.code(f)!r} is "
                                f"dynamic dispatch; which function it runs cannot be resolved safely")
            t = src.resolve_expr(entry_mod, f)
            if t is None or t.key not in by_key:
                self.context.append(None)
                return
            name = by_key[t.key]
            if src.head(f) in shadowing:
                problems.append(f"{entry_mod.at(call)}: {src.head(f)!r} names stage {name!r} at module "
                                f"level, but is shadowed in {plan.entry.function} by a parameter or a "
                                f"local of that name; which function it calls cannot be resolved "
                                f"statically")
                self.context.append(None)
                return
            where = next((c for c in reversed(self.context) if c is not None and not isinstance(c, tuple)), None)
            outer = next((c for c in reversed(self.context) if isinstance(c, tuple)), None)
            if outer is not None:
                problems.append(f"{entry_mod.at(call)}: stage {name!r} is called inside the arguments of "
                                f"stage {outer[1]!r}; nested stage calls are refused")
            elif where is not None:
                problems.append(f"{entry_mod.at(call)}: stage {name!r} is {where}; a stage must be "
                                f"called exactly once, at the top level of {plan.entry.function}")
            else:
                calls.append(_StageCall(stage_by_name[name], call, self.index, counter[0]))
                counter[0] += 1
                callee_code.setdefault(name, entry_mod.code(f))
            self.context.append(("stage", name))

        def leave_Call(self, call):
            self.context.pop()

        def visit_Name(self, node):
            if id(node) in self.callees or node.value in shadowing:
                return
            t = src.resolve_expr(entry_mod, node)
            if t is not None and t.key in by_key:
                problems.append(f"{entry_mod.at(node)}: stage {by_key[t.key]!r} is used as a value, "
                                f"not called; that is dynamic dispatch, and what finally runs it "
                                f"cannot be resolved safely")

        def visit_Attribute(self, node):
            if id(node) not in self.callees and src.head(node) not in shadowing:
                t = src.resolve_expr(entry_mod, node)
                if t is not None and t.key in by_key:
                    problems.append(f"{entry_mod.at(node)}: stage {by_key[t.key]!r} is used as a value, "
                                    f"not called; that is dynamic dispatch")
                    return False
            #: The attribute's own name (`.retrieve` in `opts.retrieve`) is not a use of anything;
            #: its value may hold a call (`retrieve(q).upper()`).
            node.value.visit(self)
            return False

    for index, stmt in enumerate(stmts):
        if index == 0 and _is_docstring(cst, stmt):
            continue
        visitor = Body(index)
        kind = next((why for cls, why in compound_names.items() if isinstance(stmt, cls)), None)
        if kind is not None:
            visitor.context.append(f"inside {kind}")
        stmt.visit(visitor)
        here = [c for c in calls if c.stmt_index == index]
        if len(here) > 1:
            problems.append(f"{entry_mod.at(here[0].call)}: stages "
                            + " and ".join(repr(c.stage.name) for c in here)
                            + " are called in one statement; call each in a statement of its own, so "
                              "the order they run in is the order they are written")

    by_stage: dict[str, list[_StageCall]] = {}
    for c in calls:
        by_stage.setdefault(c.stage.name, []).append(c)
    for stage in plan.stages:
        if stage.function is None:
            continue
        found = by_stage.get(stage.name, [])
        if not found and not any(f"stage {stage.name!r}" in p for p in problems):
            problems.append(f"stage {stage.name!r} ({stage.function}) is never called directly in "
                            f"{plan.entry.function} ({entry_mod.rel}); the plan's stages are the "
                            f"functions the entry function calls")
        elif len(found) > 1:
            problems.append(f"stage {stage.name!r} is called more than once in {plan.entry.function}: "
                            + ", ".join(entry_mod.at(c.call) for c in found)
                            + "; each stage runs exactly once per run")
    if problems:
        raise Refused(problems)

    ordered = sorted(calls, key=lambda c: c.order)
    declared = [s.name for s in plan.stages if s.function is not None]
    for got, want in zip(ordered, declared):
        if got.stage.name != want:
            problems.append(f"{entry_mod.at(got.call)}: stage {got.stage.name!r} is called before "
                            f"stage {want!r}, but the plan runs {want!r} first")
            break
    # An intake between two function stages needs a statement boundary between their calls.
    for i, stage in enumerate(plan.stages):
        if stage.function is not None:
            continue
        before = [c for c in ordered if c.stage.index < i]
        after = [c for c in ordered if c.stage.index > i]
        if before and after and before[-1].stmt_index == after[0].stmt_index:
            problems.append(f"{entry_mod.at(after[0].call)}: the plan puts intake stage {stage.name!r} "
                            f"between {before[-1].stage.name!r} and {after[0].stage.name!r}, which are "
                            f"called in one statement")
        if before and not after and isinstance(stmts[before[-1].stmt_index], cst.SimpleStatementLine) \
                and any(isinstance(s, cst.Return) for s in stmts[before[-1].stmt_index].body):
            problems.append(f"{entry_mod.at(before[-1].call)}: intake stage {stage.name!r} comes after "
                            f"the last function stage, whose call is in a return statement")
    if problems:
        raise Refused(problems)

    memory = {m for s in plan.stages for m in s.memory_inputs}
    for c in ordered:
        for arg in c.call.args:
            if isinstance(arg.value, cst.Name) and arg.value.value in params and arg.value.value not in memory:
                notes.append(f"note: {entry_mod.at(c.call)}: stage {c.stage.name!r} is passed the entry "
                             f"parameter {arg.value.value!r}, which no stage records (it is not in any "
                             f"memory_inputs)")
    return _Analysis(plan, plan_rel, entry_mod, entry_fn, ordered, callee_code, notes)


def _local_names(cst, fn) -> set[str]:
    """Names the function `fn` binds itself (assignments, loop and with targets, imports, nested
    definitions, exception names, `global`), not looking inside nested functions or classes."""
    names: set[str] = set()

    def targets(node):
        if isinstance(node, cst.Name):
            names.add(node.value)
        elif isinstance(node, (cst.Tuple, cst.List)):
            for el in node.elements:
                targets(el.value)
        elif isinstance(node, cst.StarredElement):
            targets(node.value)

    class V(cst.CSTVisitor):
        def visit_FunctionDef(self, node):
            if node is fn:
                return True
            names.add(node.name.value)
            return False

        def visit_ClassDef(self, node):
            names.add(node.name.value)
            return False

        def visit_Lambda(self, node):
            return False

        def visit_AssignTarget(self, node):
            targets(node.target)

        def visit_AnnAssign(self, node):
            targets(node.target)

        def visit_AugAssign(self, node):
            targets(node.target)

        def visit_For(self, node):
            targets(node.target)

        def visit_AsName(self, node):
            targets(node.name)

        def visit_NamedExpr(self, node):
            targets(node.target)

        def visit_ImportAlias(self, node):
            if node.asname is None:
                names.add(fn_code(node.name).split(".")[0])

        def visit_Global(self, node):
            names.update(n.name.value for n in node.names)

        def visit_Nonlocal(self, node):
            names.update(n.name.value for n in node.names)

    def fn_code(node):
        return cst.Module(body=[]).code_for_node(node)

    fn.visit(V())
    return names


def _is_docstring(cst, stmt) -> bool:
    return (isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1
            and isinstance(stmt.body[0], cst.Expr)
            and isinstance(stmt.body[0].value, (cst.SimpleString, cst.ConcatenatedString)))


# ------------------------------------------------------------------ generation

def plan_fingerprint(plan: InstrumentPlan, plan_rel: str) -> str:
    """What the generated code depends on, digested: a different plan gives different code."""
    doc = {
        "entry": str(plan.entry), "run_dir": plan.run_dir, "plan": plan_rel,
        "ci": [plan.ci.install, plan.ci.run, plan.ci.baseline],
        "stages": [[s.name, str(s.function) if s.function else None, list(s.memory_inputs), s.trust,
                    s.rederivable, s.rederivable_note,
                    [s.instrument.name, s.instrument.package, s.instrument.kind] if s.instrument else None,
                    list(s.inputs), list(s.files)] for s in plan.stages],
        "gate": GATE_ACTION,
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


_HELPERS = '''\
{marker} (sha256:{fingerprint}).
# Change the plan and run `onetrace-ci instrument` again, rather than editing this by hand.
import hashlib as _onetrace_hashlib
import importlib.metadata as _onetrace_metadata
import json as _onetrace_json
import os as _onetrace_os
import time as _onetrace_time
from pathlib import Path as _OnetracePath

from onetrace.emit import Instrument as _OnetraceInstrument
from onetrace.emit import Recorder as _OnetraceRecorder

_ONETRACE_ROOT = _OnetracePath(__file__).resolve().parents[{depth}]
_ONETRACE_PLAN = {plan}


def _onetrace_root():
    """The repository this module runs from: where its plan is. A run is written under it."""
    if not (_ONETRACE_ROOT / _ONETRACE_PLAN).is_file():
        raise RuntimeError(
            "onetrace: {{}} is not running from its repository (no {{}} under {{}}); run it from a "
            "checkout, or install it in editable mode".format(__file__, _ONETRACE_PLAN, _ONETRACE_ROOT))
    return _ONETRACE_ROOT


def _onetrace_run_id():
    """ONETRACE_RUN_ID when it is set; otherwise a new id, sortable and safe in a path."""
    return _onetrace_os.environ.get("ONETRACE_RUN_ID") or "{{}}-{{}}".format(
        _onetrace_time.strftime("%Y%m%dT%H%M%SZ", _onetrace_time.gmtime()),
        _onetrace_os.urandom(4).hex())


def _onetrace_bytes(value):
    """A stage value as bytes and a media type: bytes as they are, text as UTF-8, anything
    else as sorted, compact JSON. A value JSON cannot hold stops the run, loudly."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value), "application/octet-stream"
    if isinstance(value, str):
        return value.encode("utf-8"), "text/plain"
    text = _onetrace_json.dumps(value, sort_keys=True, ensure_ascii=False,
                                separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8"), "application/json"


def _onetrace_write(ctx, stem, value):
    """Store a stage's return value as its output artifact."""
    try:
        data, media_type = _onetrace_bytes(value)
    except (TypeError, ValueError) as e:
        raise TypeError("onetrace: stage {{!r}} returned a value that cannot be recorded ({{}}); return "
                        "bytes, text, or JSON-compatible data".format(ctx.name, e)) from e
    suffix = {{"application/json": ".json", "text/plain": ".txt"}}.get(media_type, ".bin")
    return ctx.write(stem + suffix, data, media_type)


def _onetrace_read_memory(ctx, name, value, trust_class):
    """Record an in-memory input by its digest; the value is not stored. The one place this
    code calls ctx.read_memory."""
    try:
        data, media_type = _onetrace_bytes(value)
    except (TypeError, ValueError) as e:
        raise TypeError("onetrace: memory input {{!r}} of stage {{!r}} cannot be recorded ({{}}); pass "
                        "bytes, text, or JSON-compatible data".format(name, ctx.name, e)) from e
    ctx.read_memory(data, media_type, name=name, trust_class=trust_class)
    digest = "sha256:" + _onetrace_hashlib.sha256(data).hexdigest()
    return {{"bytes": str(len(data)), "digest": digest, "media_type": media_type}}
'''


def _instrument_expr(stage: Stage) -> str:
    if stage.instrument is not None:
        i = stage.instrument
        head = (f"{_q(i.name)}, {_q(i.kind)}, _onetrace_metadata.version({_q(i.package)}),\n"
                f"    {{\"package\": {_q(i.package)}}}")
    else:
        mem = ", ".join(_q(m) for m in stage.memory_inputs)
        head = (f"\"onetrace.read_memory\", \"intake\", _onetrace_metadata.version(\"onetrace\"),\n"
                f"    {{\"memory_inputs\": [{mem}]}}")
    tail = f"rederivable={_q(stage.rederivable)}"
    if stage.rederivable_note:
        tail += f", rederivable_note={_q(stage.rederivable_note)}"
    return f"_OnetraceInstrument(\n    {head}, {tail})"


def _wrapper_source(stage: Stage, callee: str | None) -> str:
    ident, stem = _slug_ident(stage.name), _slug_file(stage.name)
    lines = [f"@_onetrace_rec.stage({_q(stage.name)}, {_instrument_expr(stage)})"]
    if stage.function is not None:
        lines.append(f"def _onetrace_stage_{ident}(_onetrace_ctx, *_onetrace_args, **_onetrace_kwargs):")
    else:
        lines.append(f"def _onetrace_stage_{ident}(_onetrace_ctx):")
    for up in stage.inputs:
        lines.append(f"    _onetrace_ctx.read(_onetrace_out[{_q(up)}])")
    for f in stage.files:
        media = MEDIA_TYPES.get(Path(f).suffix.lower(), "application/octet-stream")
        lines.append(f"    _onetrace_ctx.read_external(_ONETRACE_ROOT / {_q(f)}, {_q(media)},")
        lines.append(f"                                name={_q(f)}, trust_class={_q(stage.trust)})")
    if stage.function is not None:
        lines.append(f"    _onetrace_value = {callee}(*_onetrace_args, **_onetrace_kwargs)")
        lines.append(f"    _onetrace_out[{_q(stage.name)}] = _onetrace_write(_onetrace_ctx, {_q(stem)}, "
                     f"_onetrace_value)")
        lines.append("    return _onetrace_value")
    else:
        lines.append("    _onetrace_memory = {")
        for m in stage.memory_inputs:
            lines.append(f"        {_q(m)}: _onetrace_read_memory(_onetrace_ctx, {_q(m)}, {m}, "
                         f"{_q(stage.trust)}),")
        lines.append("    }")
        lines.append(f"    _onetrace_out[{_q(stage.name)}] = _onetrace_ctx.write_json(")
        lines.append(f"        {_q(stem + '.json')}, {{\"memory_inputs\": _onetrace_memory}})")
    return "\n".join(lines) + "\n"


def _run_dir_expr(run_dir: str) -> str:
    if "{run_id}" in run_dir:
        return f"_onetrace_root() / {_q(run_dir)}.format(run_id=_onetrace_run)"
    return f"_onetrace_root() / {_q(run_dir)}"


def _transform(src: _Source, a: _Analysis, fingerprint: str) -> bytes:
    cst = src.cst
    plan, mod, fn = a.plan, a.entry_module, a.entry_fn

    # 1. The helpers, after the module's leading docstring and imports.
    depth = len(Path(mod.rel).parts) - 1
    text = _HELPERS.format(marker=MARKER, fingerprint=fingerprint, depth=depth, plan=_q(a.plan_rel))
    #: Parsed with the module's own line ending, so the multi-line docstrings match it.
    helpers = cst.parse_module(text.replace("\n", mod.tree.default_newline))
    first = helpers.body[0]
    #: The marker comment is the parsed block's module header; it moves onto its first statement.
    helpers_body = [first.with_changes(leading_lines=[cst.EmptyLine(), cst.EmptyLine(), *helpers.header,
                                                      *first.leading_lines]),
                    *helpers.body[1:]]
    insert_at = 0
    for i, stmt in enumerate(mod.tree.body):
        if i == 0 and _is_docstring(cst, stmt):
            insert_at = 1
            continue
        if isinstance(stmt, cst.SimpleStatementLine) and all(
                isinstance(s, (cst.Import, cst.ImportFrom)) for s in stmt.body):
            insert_at = i + 1
            continue
        break

    # 2. The entry function's new body.
    body = fn.body
    stmts = list(body.body) if isinstance(body, cst.IndentedBlock) else [cst.SimpleStatementLine(body=body.body)]
    docstring = [stmts.pop(0)] if stmts and _is_docstring(cst, stmts[0]) else []
    offset = len(docstring)
    stage_calls = {id(c.call): c.stage for c in a.calls}

    class Rename(cst.CSTTransformer):
        def leave_Call(self, original, updated):
            stage = stage_calls.get(id(original))
            if stage is None:
                return updated
            return updated.with_changes(func=cst.Name(f"_onetrace_stage_{_slug_ident(stage.name)}"))

    rewritten = [s.visit(Rename()) for s in stmts]

    # Intake stages go where the plan puts them: before the statement holding the next function
    # stage's call, at the very start when they come first, and after the last call when last.
    before: dict[int, list[Stage]] = {}
    after_last: list[Stage] = []
    for i, stage in enumerate(plan.stages):
        if stage.function is not None:
            continue
        nxt = [c for c in a.calls if c.stage.index > i]
        prv = [c for c in a.calls if c.stage.index < i]
        if not prv:
            before.setdefault(-1, []).append(stage)
        elif nxt:
            before.setdefault(nxt[0].stmt_index - offset, []).append(stage)
        else:
            after_last.append(stage)
    last_stmt = (max(c.stmt_index for c in a.calls) - offset) if a.calls else -1

    def intake_call(stage):
        return cst.parse_statement(f"_onetrace_stage_{_slug_ident(stage.name)}()\n")

    new_stmts = [intake_call(s) for s in before.get(-1, [])]
    for i, stmt in enumerate(rewritten):
        new_stmts.extend(intake_call(s) for s in before.get(i, []))
        new_stmts.append(stmt)
        if i == last_stmt:
            new_stmts.extend(intake_call(s) for s in after_last)

    wrappers = [cst.parse_statement(_wrapper_source(s, a.callee_code.get(s.name))) for s in plan.stages]
    blank = cst.EmptyLine(indent=False)
    wrappers = [w if i == 0 else w.with_changes(leading_lines=[blank, *w.leading_lines])
                for i, w in enumerate(wrappers)]
    if new_stmts:
        new_stmts[0] = new_stmts[0].with_changes(leading_lines=[blank, *new_stmts[0].leading_lines])
    declared = ", ".join(_q(s.name) for s in plan.stages)
    prologue = cst.parse_module(
        "_onetrace_run = _onetrace_run_id()\n"
        "_onetrace_rec = _OnetraceRecorder(\n"
        f"    {_run_dir_expr(plan.run_dir)},\n"
        f"    declared_stages=[{declared}],\n"
        f"    manifest=_OnetracePath(__file__), policy=\"fail-closed\",\n"
        f"    anchor_reason={_q(_ANCHOR_REASON)}, run_id=_onetrace_run)\n"
        "_onetrace_out = {}\n").body
    #: Comments stay where they were: one on the `def` line stays there, and comments after the
    #: last statement of the body stay after the last of the user's statements.
    if isinstance(body, cst.IndentedBlock):
        header, footer = body.header, body.footer
    else:
        header, footer = body.trailing_whitespace, ()
    try_stmt = cst.Try(
        body=cst.IndentedBlock(body=[*wrappers, *new_stmts], footer=footer),
        finalbody=cst.Finally(body=cst.IndentedBlock(body=[cst.parse_statement("_onetrace_rec.close()\n")])))
    new_fn = fn.with_changes(body=cst.IndentedBlock(body=[*docstring, *prologue, try_stmt], header=header))

    class Replace(cst.CSTTransformer):
        def leave_FunctionDef(self, original, updated):
            return new_fn if original is fn else updated

    new_tree = mod.tree.visit(Replace())
    rest = list(new_tree.body[insert_at:])
    if rest:
        #: Two blank lines between the generated block and whatever follows it.
        lead = rest[0].leading_lines
        have = next((i for i, line in enumerate(lead) if line.comment is not None), len(lead))
        rest[0] = rest[0].with_changes(
            leading_lines=[cst.EmptyLine(indent=False)] * max(0, 2 - have) + list(lead))
    new_tree = new_tree.with_changes(body=[*new_tree.body[:insert_at], *helpers_body, *rest])
    return new_tree.bytes


def workflow_text(plan: InstrumentPlan, plan_rel: str) -> str:
    return render_workflow(run_dir=plan.run_dir, install=plan.ci.install, run=plan.ci.run,
                           baseline=plan.ci.baseline, plan_rel=plan_rel)


def render_workflow(*, run_dir: str, install: str, run: str, baseline: str, plan_rel: str,
                    made_by: str = "`onetrace-ci instrument` from the plan") -> str:
    """The workflow file. `instrument` and `init-ci` write the same one, apart from the first
    comment line naming which of them wrote it."""
    candidate = run_dir.replace("{run_id}", CANDIDATE_RUN_ID)

    def block(cmd: str) -> str:
        return "|\n" + "".join(f"          {line}\n" for line in cmd.splitlines())

    return (
        f"# Generated by {made_by}. It runs the pipeline, then the\n"
        "# onetrace-ci gate against the committed baseline. The gate proves the run's records are\n"
        "# consistent and match the baseline; it does not prove the pipeline's output is correct.\n"
        "name: onetrace\n"
        "\n"
        "on:\n"
        "  push:\n"
        "  pull_request:\n"
        "\n"
        "permissions:\n"
        "  contents: read\n"
        "\n"
        "jobs:\n"
        "  onetrace:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        f"      - uses: {CHECKOUT_ACTION}  # v4.4.0\n"
        f"      - uses: {SETUP_PYTHON_ACTION}  # v5.6.0\n"
        "        with:\n"
        "          python-version: \"3.12\"\n"
        "      - name: Install the pipeline\n"
        f"        run: {block(install)}"
        "      - name: Run the pipeline\n"
        "        env:\n"
        f"          ONETRACE_RUN_ID: {CANDIDATE_RUN_ID}\n"
        f"        run: {block(run)}"
        "      - name: onetrace-ci gate\n"
        f"        uses: {GATE_ACTION}\n"
        "        with:\n"
        f"          run: {_q(candidate)}\n"
        f"          baseline: {_q(baseline)}\n"
        f"          plan: {_q(plan_rel)}\n"
    )


# ------------------------------------------------------------------ the patch

def _split_lines(text: str) -> list[str]:
    """Split on newlines only (as git does); `str.splitlines` also splits on form feeds."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _file_diff(rel: str, old: str | None, new: str) -> str:
    out = [f"diff --git a/{rel} b/{rel}\n"]
    if old is None:
        out.append("new file mode 100644\n")
    fromfile = "/dev/null" if old is None else f"a/{rel}"
    for line in difflib.unified_diff(_split_lines(old or ""), _split_lines(new), fromfile, f"b/{rel}"):
        if line[:1] in (" ", "-", "+") and not line.startswith(("--- ", "+++ ")) and not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        out.append(line)
    return "".join(out)


_CODING_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def _utf8_source(raw: bytes, rel: str) -> str:
    """The entry module as text. The patch is UTF-8, so a module in another encoding (declared
    with a coding line, PEP 263) is refused rather than rewritten in the wrong one."""
    for no, line in enumerate(raw.splitlines()[:2], start=1):
        m = _CODING_RE.match(line)
        if m and m.group(1).decode("ascii", "replace").lower().replace("_", "-") not in ("utf-8", "utf8"):
            raise Refused(f"{rel}:{no}: the file declares the {m.group(1).decode('ascii', 'replace')} "
                          f"encoding; onetrace-ci writes UTF-8 patches, so convert it to UTF-8 first")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        line = raw[:e.start].count(b"\n") + 1
        raise Refused(f"{rel}:{line}: not valid UTF-8; onetrace-ci writes UTF-8 patches") from None


def build_patch(*, plan_path: Path, repo: Path) -> Result:
    """The patch for `repo`, from the plan at `plan_path`, or `Refused`. Writes nothing."""
    repo = repo.resolve()
    plan_path = plan_path.resolve()
    try:
        plan = load_instrument_plan(plan_path)
    except PlanRefused as e:
        raise Refused(e.problems) from None
    except PlanError as e:
        raise Refused(str(e)) from None
    try:
        plan_rel = plan_path.relative_to(repo).as_posix()
    except ValueError:
        raise Refused(f"{plan_path}: the plan is outside --repo {repo}; the workflow reads it from the "
                      f"repository, so it must be committed there") from None

    problems = []
    for stage in plan.stages:
        for f in stage.files:
            if not (repo / f).is_file():
                problems.append(f"plan field stages[{stage.index}].files: {f!r} does not exist under {repo}")
    idents: dict[str, str] = {}
    for stage in plan.stages:
        ident = _slug_ident(stage.name)
        if ident in idents:
            problems.append(f"plan field stages[{stage.index}].name: {stage.name!r} and {idents[ident]!r} "
                            f"give the same generated name ({ident!r}); rename one")
        idents[ident] = stage.name
    if isinstance(plan.sign, SignSpec) and plan.trust is not None and not (repo / plan.trust.file).is_file():
        problems.append(f"plan field trust.file: {plan.trust.file!r} does not exist under {repo}; which "
                        f"keys are trusted is yours to decide, so onetrace-ci never creates it")
    for block, spec, what in (("sign", plan.sign, "sign_with="), ("anchor", plan.anchor, "the anchor step")):
        if spec is not None and spec != "none":
            problems.append(f"plan field {block}: signing and anchoring generate code for onetrace 0.2.0 "
                            f"({what}), and this build has not yet been checked against that interface; "
                            f"until it is, a {block} block other than `none` is refused rather than "
                            f"generated against a guess")
    candidate = plan.run_dir.replace("{run_id}", CANDIDATE_RUN_ID).rstrip("/")
    if candidate == plan.ci.baseline.rstrip("/"):
        problems.append(f"plan fields run_dir and ci.baseline: the workflow's run would be written to "
                        f"{candidate!r}, which is the committed baseline")
    if problems:
        raise Refused(problems)

    src = _Source(repo)
    fingerprint = plan_fingerprint(plan, plan_rel)
    entry_path, looked = src.module_file(plan.entry.module)
    if entry_path is None:
        raise Refused(f"plan field entry: {plan.entry}: no module file for {plan.entry.module!r} "
                      f"(looked for {', '.join(looked)})")
    entry_rel = entry_path.relative_to(repo).as_posix()
    entry_text = _utf8_source(entry_path.read_bytes(), entry_rel)
    workflow = workflow_text(plan, plan_rel)
    workflow_file = repo / WORKFLOW_PATH

    diffs, files, summary = [], [], []
    existing = _MARKER_RE.search(entry_text)
    if existing:
        if existing.group(1) != fingerprint:
            raise Refused(f"{entry_rel}: already instrumented from a different plan (sha256:"
                          f"{existing.group(1)}; this plan is sha256:{fingerprint}). Revert the earlier "
                          f"patch, then run onetrace-ci instrument again")
        summary.append(f"{entry_rel}: already instrumented from this plan (sha256:{fingerprint})")
    else:
        analysis = _analyse(src, plan, plan_rel)
        settings = [f"stages[{s.index}].{k}" for s in plan.stages for k in ("config", "constants")
                    if getattr(s, k)] + (["corpus"] if plan.corpus else [])
        if settings:
            raise Refused([f"plan field {f}: recording settings and corpus links generates code for "
                           f"onetrace 0.2.0 (ot.pkg config, ot.constant, ot.corpus_from), and this build "
                           f"has not yet been checked against that interface; until it is, a plan with "
                           f"them is refused rather than generated against a guess" for f in settings])
        new_entry = _transform(src, analysis, fingerprint).decode("utf-8")
        diffs.append(_file_diff(entry_rel, entry_text, new_entry))
        files.append(entry_rel)
        summary.extend(_describe(analysis, entry_rel))

    if workflow_file.is_file():
        #: Git may check it out with CRLF (autocrlf=true, the Git for Windows default).
        current = workflow_file.read_bytes().decode("utf-8").replace("\r\n", "\n")
        if current != workflow:
            raise Refused(f"{WORKFLOW_PATH}: already exists and differs from the workflow this plan "
                          f"generates; onetrace-ci never overwrites it. Move it aside, then run again")
        summary.append(f"{WORKFLOW_PATH}: already generated from this plan")
    else:
        diffs.append(_file_diff(WORKFLOW_PATH, None, workflow))
        files.append(WORKFLOW_PATH)
        summary.append(f"{WORKFLOW_PATH}: new. Runs the pipeline with ONETRACE_RUN_ID={CANDIDATE_RUN_ID}, "
                       f"then the gate against {plan.ci.baseline}; permissions contents: read; the gate "
                       f"action is pinned to {GATE_ACTION.split('@')[1][:12]}")
    header = [f"onetrace-ci instrument: plan {plan_rel}, approved by {plan.approved_by}, "
              f"{len(plan.stages)} stages"]
    if plan.sign in (None, "none") and plan.anchor in (None, "none"):
        why = ("the plan has no sign or anchor block" if plan.sign is None and plan.anchor is None
               else "the plan chose none")
        header.append(f"runs will be unsigned and unanchored: {why}")
    bare = [s.name for s in plan.stages if s.function is not None and not s.config and not s.constants]
    if bare:
        header.append("stages that record no settings (no config or constants in the plan): "
                      + ", ".join(repr(n) for n in bare))
    return Result("".join(diffs), files, header + summary)


def _describe(a: _Analysis, entry_rel: str) -> list[str]:
    plan = a.plan
    lines = [f"{entry_rel}: {plan.entry.function}() gets one Recorder per run, created inside it and "
             f"closed in a finally; each run is written to {plan.run_dir}"]
    call_at = {c.stage.name: a.entry_module.at(c.call) for c in a.calls}
    width = max(len(s.name) for s in plan.stages)
    for s in plan.stages:
        parts = []
        if s.function is None:
            parts.append(f"records memory input(s) {', '.join(repr(m) for m in s.memory_inputs)} with "
                         f"ctx.read_memory, digest only ({s.trust})")
        else:
            parts.append(f"wraps {s.function}, called at {call_at[s.name]}")
        if s.inputs:
            parts.append(f"reads the output of {', '.join(repr(u) for u in s.inputs)}")
        for f in s.files:
            parts.append(f"reads {f} ({s.trust})")
        if s.instrument is not None:
            parts.append(f"instrument {s.instrument.name}, version of {s.instrument.package} read at run time")
        parts.append(f"rederivable: {s.rederivable}")
        lines.append(f"  {s.index + 1}. {s.name:<{width}}  " + "; ".join(parts))
    lines.extend(a.notes)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onetrace-ci instrument")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_patch(plan_path=args.plan, repo=args.repo)
    except Refused as e:
        print(format_refusal("instrument", e.problems), file=sys.stderr)
        return 1
    for line in result.summary:
        print(line)
    args.out.write_bytes(result.patch.encode("utf-8"))
    if result.files:
        print(f"wrote {args.out} ({len(result.files)} files); nothing was changed in place. Review "
              f"it, then apply it.")
        print(f"next: git apply {args.out}")
    else:
        print(f"nothing to change: wrote an empty {args.out}; nothing was changed in place.")
        print("next: commit, and the workflow runs the gate on the next push")
    return 0
