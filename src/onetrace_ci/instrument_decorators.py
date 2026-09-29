"""`onetrace-ci instrument --style decorators` (the default): the plan as onetrace 0.2.0's
decorators.

The entry function gets `@ot.run(stages=[...], run_dir=..., declared_edges=[...])`: the plan's
stages named explicitly, in the plan's order, and the plan's edges as the approved topology. Each
stage function gets `@ot.stage(name, instrument=ot.pkg(...), files=..., trust=...,
rederivable=..., note=...)`, carrying the plan's meaning fields. A stage's `constants` become
`ot.constant(...)` calls at the top of its body, the only edit made inside a body. Every module
it touches gets one `import onetrace as ot`. Nothing else changes: the calls stay as they are
written, and the SDK records each one.

Generated code passes every meaning field explicitly, so a run of it never lists a field in
`assertions.undeclared`. That is why a stage given anything that cannot be seen to be another
stage's return value, unchanged, needs its `trust` stated in the plan: the SDK records such an
argument as an in-memory input, with the stage's trust class.

Every stage is called directly in the entry function's own body, where its calls can be counted
and their arguments read. A stage reached any other way (through a variable, a helper, a nested
function or lambda, or as an async call not awaited where it is made) is refused as dynamic
dispatch: how often it runs, and whether its calls overlap, cannot be seen. A stage may be handed
to one concurrent call (`pool.submit(retrieve, q)`).

Run again on code it decorated from the same plan, it writes an empty patch. A decorator whose
arguments differ from the plan is replaced, and the patch shows the old and the new.

Not generated yet, and refused with the reason:
- an entry with parameters: its intake's `rederivable` and note need `@ot.run` arguments that
  onetrace 0.2.0's candidate does not carry yet;
- calls of one stage that may overlap, and a stage's named instances: each call needs
  `stage.instance(name)`, which the candidate does not carry yet either;
- a corpus link: the SDK names no form yet for the stages that record it;
- several entries: which run the generated workflow gates, and against which baseline, is not
  settled yet.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field

from onetrace_ci.instrument import (_MARKER_RE, Refused, _is_docstring, _is_generator,
                                    _local_names, _Module, _module_level_recorders, _params,
                                    _recorder_aliases, _recorder_calls, _Source, _Target, _utf8_source)
from onetrace_ci.instrument_plan import InstrumentPlan, Stage

#: The name generated code imports the SDK under.
ALIAS = "ot"
IMPORT = f"import onetrace as {ALIAS}"
#: The stage name the SDK gives the entry's parameters.
INTAKE = "intake"
#: Calls whose arguments may run at the same time as other code: a stage called, or handed over,
#: inside one may overlap another call of it. Matched by the called name (`pool.submit`,
#: `asyncio.gather`, `tg.start_soon`); the builtin `map` is sequential, and is not one of them.
CONCURRENT = frozenset({"gather", "create_task", "ensure_future", "submit", "map", "starmap",
                        "imap", "imap_unordered", "map_async", "starmap_async", "apply_async",
                        "to_thread", "run_in_executor", "run_coroutine_threadsafe", "TaskGroup",
                        "Thread", "Process", "start_new_thread", "as_completed", "wait", "wait_for",
                        "shield", "start_soon", "start", "spawn", "run_sync"})
#: Of those, the ones that call what they are given many times.
_MANY = frozenset({"map", "starmap", "imap", "imap_unordered", "map_async", "starmap_async"})
#: Calls that run the coroutine they are given, once: an async stage's call may be their argument.
_RUNNERS = frozenset({"run", "run_until_complete"})
#: How far calls are followed into the repository's own functions.
_DEPTH = 8
_WAITS = "That form waits for onetrace 0.2.0's candidate, which does not carry it yet"


# ------------------------------------------------------------------ what is emitted

def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _literal(value) -> str:
    """A value the plan wrote, as Python source: text, numbers, booleans, lists and mappings."""
    if isinstance(value, str):
        return _q(value)
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_q(str(k))}: {_literal(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"a plan value of type {type(value).__name__}")


def _call(head: str, args: list[str]) -> str:
    """`head(`, then one argument per line, then `)`: readable, and a changed field shows in the
    patch as one changed line."""
    return f"{head}(\n" + "".join(f"    {a},\n" for a in args) + ")"


def stage_decorator(stage: Stage) -> str:
    i = stage.instrument
    pkg = f"{ALIAS}.pkg({_q(i.name)}, {_q(i.package)}, kind={_q(i.kind)}"
    if stage.config:
        pkg += f", config={_literal(stage.config)}"
    args = [_q(stage.name), f"instrument={pkg})"]
    if stage.files:
        args.append("files=[" + ", ".join(_q(f) for f in stage.files) + "]")
    if stage.trust:
        args.append(f"trust={_q(stage.trust)}")
    args.append(f"rederivable={'True' if stage.rederivable == 'true' else 'False'}")
    if stage.rederivable_note:
        args.append(f"note={_q(stage.rederivable_note)}")
    return _call(f"{ALIAS}.stage", args)


def edges(plan: InstrumentPlan) -> list[tuple[str, str]]:
    """The plan's edges between function stages: each stage's `inputs`, in the plan's order."""
    names = {s.name for s in plan.stages if s.function is not None}
    return [(up, s.name) for s in plan.stages if s.function is not None for up in s.inputs if up in names]


def run_decorator(plan: InstrumentPlan) -> str:
    stages = [s.name for s in plan.stages if s.function is not None]
    args = ["stages=[" + ", ".join(_q(n) for n in stages) + "]", f"run_dir={_q(plan.run_dir)}"]
    found = edges(plan)
    if found:
        args.append("declared_edges=[" + ", ".join(f'{{"from": {_q(a)}, "to": {_q(b)}}}' for a, b in found) + "]")
    return _call(f"{ALIAS}.run", args)


def constant_lines(stage: Stage) -> list[str]:
    return [f"{ALIAS}.constant({_q(c)}, {c})" for c in stage.constants]


# ------------------------------------------------------------------ analysis

@dataclass
class _Ref:
    """One place the entry function names a stage: a call, or the stage handed over as a value."""
    stage: str
    node: object
    call: object | None             # the call, when the stage is called here
    repeats: str | None             # why this place may run more than once, if it may
    concurrent: str | None          # the concurrent call it is inside, if any
    #: The call's result is run where it is made: awaited, or given straight to a runner or a
    #: concurrent call. What an async stage's call must be, so when it runs can be seen.
    direct: bool = True


@dataclass
class _Analysis:
    plan: InstrumentPlan
    entry_module: _Module
    entry_fn: object
    targets: dict[str, _Target]
    refs: dict[str, list[_Ref]] = field(default_factory=dict)


def plan_waits(plan: InstrumentPlan) -> list[str]:
    """What the plan asks for that decorator output does not generate yet."""
    found = []
    if len(plan.entries) > 1:
        found.append("plan field entries: generating several entries waits for a decision on which run "
                     "the generated workflow gates, and against which baseline (the plan's ci block names "
                     "one); until then, plan one entry")
    if plan.corpus is not None:
        found.append("plan field corpus: a corpus link waits for the SDK to name how the stages that "
                     "record it are given (ot.corpus_from takes the ingest run and index_stage only); "
                     "until then, leave corpus out of the plan")
    for stage in plan.stages:
        if stage.instances:
            fn = stage.function.function if stage.function else stage.name
            found.append(f"plan field stages[{stage.index}].instances: each named instance is called as "
                         f"`{fn}.instance(name)(...)`. {_WAITS}; until it does, leave instances out and "
                         f"call the stage from one place at a time")
    return found


def _sdk_decorator(src: _Source, module: _Module, expr) -> tuple[str | None, object | None]:
    """("run" | "stage", the call or None) when `expr` is onetrace's decorator, else (None, None)."""
    cst = src.cst
    call = expr if isinstance(expr, cst.Call) else None
    f = call.func if call is not None else expr
    if isinstance(f, cst.Attribute) and isinstance(f.value, cst.Name) and f.attr.value in ("run", "stage"):
        b = src.bindings(module).get(f.value.value)
        if b is not None and b.kind == "module" and b.target == "onetrace":
            return f.attr.value, call
    if isinstance(f, cst.Name):
        b = src.bindings(module).get(f.value)
        if b is not None and b.kind == "from" and b.target == "onetrace" and b.name in ("run", "stage"):
            return b.name, call
    return None, None


def _stage_name(module: _Module, call) -> str | None:
    """The name an existing `@ot.stage("name", ...)` gives, when it is written as a literal."""
    if call is None or not call.args or call.args[0].keyword is not None:
        return None
    try:
        value = ast.literal_eval(module.code(call.args[0].value))
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _alias_bindings(cst, module: _Module, constant_fns: set[int]) -> list:
    """Nodes that bind the name `ot` where generated code would use it: at module level, inside a
    stage function that gets `ot.constant(...)` calls, or through `global ot`. `import onetrace
    as ot` at module level is the one binding allowed."""
    hits = []

    class V(cst.CSTVisitor):
        def __init__(self):
            super().__init__()
            self.scopes = [None]

        def _checked(self) -> bool:
            here = self.scopes[-1]
            return here is None or id(here) in constant_fns

        def _target(self, node):
            if isinstance(node, cst.Name):
                if node.value == ALIAS and self._checked():
                    hits.append(node)
            elif isinstance(node, (cst.Tuple, cst.List)):
                for el in node.elements:
                    self._target(el.value)
            elif isinstance(node, cst.StarredElement):
                self._target(node.value)

        def visit_FunctionDef(self, node):
            self._target(node.name)
            self.scopes.append(node)

        def leave_FunctionDef(self, node):
            self.scopes.pop()

        def visit_Lambda(self, node):
            self.scopes.append(node)

        def leave_Lambda(self, node):
            self.scopes.pop()

        def visit_ClassDef(self, node):
            self._target(node.name)
            self.scopes.append(node)

        def leave_ClassDef(self, node):
            self.scopes.pop()

        def visit_Param(self, node):
            self._target(node.name)

        def visit_AssignTarget(self, node):
            self._target(node.target)

        def visit_AnnAssign(self, node):
            self._target(node.target)

        def visit_AugAssign(self, node):
            self._target(node.target)

        def visit_For(self, node):
            self._target(node.target)

        def visit_CompFor(self, node):
            self._target(node.target)

        def visit_NamedExpr(self, node):
            self._target(node.target)

        def visit_WithItem(self, node):
            if node.asname is not None:
                self._target(node.asname.name)

        def visit_ExceptHandler(self, node):
            if node.name is not None:
                self._target(node.name.name)

        def visit_Import(self, node):
            for alias in node.names:
                dotted = module.code(alias.name)
                local = alias.asname.name.value if alias.asname is not None else dotted.split(".")[0]
                allowed = dotted == "onetrace" and alias.asname is not None and self.scopes[-1] is None
                if local == ALIAS and not allowed and self._checked():
                    hits.append(alias)

        def visit_ImportFrom(self, node):
            if isinstance(node.names, cst.ImportStar):
                return
            for alias in node.names:
                local = alias.asname.name.value if alias.asname is not None else module.code(alias.name)
                if local == ALIAS and self._checked():
                    hits.append(alias)

        def visit_Global(self, node):
            hits.extend(n for n in node.names if n.name.value == ALIAS)

    module.tree.visit(V())
    return hits


def analyse(src: _Source, plan: InstrumentPlan) -> _Analysis:
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
    if _MARKER_RE.search(entry_mod.raw.decode("utf-8", "replace")):
        raise Refused(f"{entry_mod.rel}: already instrumented in wrapper style; revert that patch "
                      f"(git apply -R) before generating decorators, so the run is not recorded twice")
    if _is_generator(cst, entry_fn):
        problems.append(f"{entry_mod.at(entry_fn)}: the entry function {plan.entry} is a generator; "
                        f"its run has no single end to close the Recorder at")

    # Every function stage resolves to a function definition, sync or async.
    targets: dict[str, _Target] = {}
    for stage in plan.stages:
        if stage.function is None:
            continue
        at = f"plan field stages[{stage.index}]"
        if stage.name == INTAKE:
            problems.append(f"{at}.name: {INTAKE!r} is reserved: in decorator style the SDK records the "
                            f"entry's parameters as the stage {INTAKE!r}; give this function stage "
                            f"another name")
        ref = stage.function
        path, looked = src.module_file(ref.module)
        if path is None:
            problems.append(f"{at}.function: {ref}: no module file for {ref.module!r} (looked for "
                            f"{', '.join(looked)})")
            continue
        t = src.resolve(ref.module, ref.function)
        mod = src.load(ref.module)
        if t is None or not t.name:
            problems.append(f"{at}.function: {ref}: {mod.rel} has no top-level function {ref.function!r} "
                            f"that can be resolved")
            continue
        b = t.binding
        if b.kind == "assign":
            value = b.node.body[0].value if isinstance(b.node, cst.SimpleStatementLine) else None
            if isinstance(value, cst.Lambda):
                problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is a lambda; a "
                                f"stage must be a named function definition")
            else:
                problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is bound by "
                                f"assignment, not a function definition, so it cannot be decorated")
            continue
        if b.kind == "class":
            problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is a class, not a "
                            f"function definition")
            continue
        if _is_generator(cst, b.node):
            kind = "an async generator" if b.kind == "asyncdef" else "a generator"
            problems.append(f"{t.module.at(b.node)}: stage {stage.name!r} ({ref}) is {kind}; a stage "
                            f"returns one value, and the SDK refuses to decorate a generator")
            continue
        if b.node is entry_fn:
            problems.append(f"{at}.function: {ref} is the entry function itself; the entry function owns "
                            f"the run, and stages are the functions it calls")
            continue
        for other_name, other in targets.items():
            if other.key == t.key:
                problems.append(f"{at}.function: {ref} is also stage {other_name!r}'s function; two "
                                f"stages cannot share one function, because one decorator names one stage")
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
    by_node = {id(t.binding.node): name for name, t in targets.items()}

    # No stage runs another: not in its own body, not through a function of the repository it
    # calls, and not by being handed one.
    for name, t in targets.items():
        for node, module, other, is_call, via in _stage_uses(src, t.module, t.binding.node, by_key):
            where = "inside its own body" if not via else "through " + ", then ".join(via)
            problems.append(f"{module.at(node)}: stage {name!r} {'calls' if is_call else 'uses'} stage "
                            f"{other!r} {where}; nested stage calls are refused, because the inner stage "
                            f"would run inside the outer one")

    # The modules the patch touches: no star import, no Recorder of their own, the name `ot`
    # free, and no `@ot.stage` the plan does not match. The modules the entry imports are
    # checked for such an `@ot.stage` too: the SDK refuses a stage the run does not declare.
    involved = {entry_mod.name: entry_mod, **{t.module.name: t.module for t in targets.values()}}
    constant_fns = {id(targets[s.name].binding.node) for s in plan.stages if s.constants and s.name in targets}
    for module in [*involved.values(), *_imported(src, entry_mod, involved)]:
        if module.name not in involved:
            problems.extend(_decorator_problems(src, module, by_node))
            continue
        star = src.bindings(module).get("*")
        if star is not None and star.node is not None:
            problems.append(f"{module.at(star.node)}: a star import makes the names this module uses "
                            f"unresolvable (a later one can rebind a stage's name); import names explicitly")
        for where in _module_level_recorders(cst, module, _recorder_aliases(src, module)):
            problems.append(f"{where}: a module-level Recorder is already present; @ot.run creates the "
                            f"run's recorder, so a second one would record the run twice")
        for node in _alias_bindings(cst, module, constant_fns):
            problems.append(f"{module.at(node)}: this binds the name {ALIAS!r}, which the generated code "
                            f"reserves for `{IMPORT}`; rename it")
        problems.extend(_decorator_problems(src, module, by_node))
    for call in _recorder_calls(cst, entry_fn, _recorder_aliases(src, entry_mod)):
        problems.append(f"{entry_mod.at(call)}: {plan.entry} already creates a Recorder; @ot.run creates "
                        f"the run's recorder, so this code is already instrumented by hand")

    # The entry's parameters: every one is recorded by the SDK, so the plan lists each.
    params = _params(cst, entry_fn)
    listed = {m for s in plan.stages for m in s.memory_inputs}
    for stage in plan.stages:
        for m in stage.memory_inputs:
            if m not in params:
                problems.append(f"plan field stages[{stage.index}].memory_inputs: {m!r} is not a "
                                f"parameter of {plan.entry.function} ({entry_mod.at(entry_fn)}); "
                                f"its parameters are {params}")
    for p in params:
        if p not in listed:
            problems.append(f"{entry_mod.at(entry_fn)}: the parameter {p!r} of {plan.entry.function} is not "
                            f"listed in any stage's memory_inputs; the SDK records every parameter of the "
                            f"entry, so the plan lists each, in its intake stage's memory_inputs")
    if params:
        problems.append(f"{entry_mod.at(entry_fn)}: {plan.entry.function} takes parameters, which the SDK "
                        f"records as the stage {INTAKE!r}, described by @ot.run(trust=, rederivable=, "
                        f"note=). {_WAITS}")
    if problems:
        raise Refused(problems)

    a = _Analysis(plan, entry_mod, entry_fn, targets)
    _find_refs(src, a, by_key, problems)
    _check_refs(src, a, problems)
    if problems:
        raise Refused(problems)
    return a


def _decorator_problems(src: _Source, module: _Module, by_node: dict[int, str]) -> list[str]:
    """Each `@ot.stage` in `module` that the plan does not match: another stage's name, a name
    that is not a literal, a function the plan names no stage for, or two on one function."""
    cst, problems = src.cst, []
    for stmt in module.tree.body:
        if not isinstance(stmt, cst.FunctionDef):
            continue
        decorated = [(d, *_sdk_decorator(src, module, d.decorator)) for d in stmt.decorators]
        stages = [(d, call) for d, kind, call in decorated if kind == "stage"]
        planned = by_node.get(id(stmt))
        for d, call in stages:
            name = _stage_name(module, call)
            shown = repr(name) if name is not None else f"with {module.code(d.decorator)!r}"
            if name is None:
                problems.append(f"{module.at(d)}: {stmt.name.value} is already decorated as a stage "
                                f"{shown}, whose name onetrace-ci cannot read; write the name as a "
                                f"literal, or remove the decorator")
            elif planned is None:
                problems.append(f"{module.at(d)}: {stmt.name.value} is already decorated as stage "
                                f"{shown}, but the plan names no stage for it; remove the decorator, "
                                f"or plan the function as that stage")
            elif name != planned:
                problems.append(f"{module.at(d)}: {stmt.name.value} is already decorated as stage "
                                f"{shown}, but the plan names it stage {planned!r}; make them agree")
        if len(stages) > 1:
            problems.append(f"{module.at(stmt)}: {stmt.name.value} is already decorated as a stage more "
                            f"than once; one decorator names one stage")
    return problems


def _imported(src: _Source, module: _Module, skip: dict) -> list[_Module]:
    """The repository's modules that `module` imports at its top level, other than `skip`'s."""
    found: dict[str, _Module] = {}
    for b in src.bindings(module).values():
        if b is None or b.kind not in ("module", "from"):
            continue
        names = [b.target] if b.kind == "module" else [f"{b.target}.{b.name}", b.target]
        for name in names:
            try:
                loaded = src.load(name) if name else None
            except Refused:
                loaded = None            # not ours to judge: it is not a module the patch touches
            if loaded is not None:
                if loaded.name not in skip:
                    found.setdefault(loaded.name, loaded)
                break
    return list(found.values())


def _stage_uses(src: _Source, module: _Module, fn, by_key: dict, via: tuple = (), seen: set | None = None):
    """Each use of a stage in `fn`'s body, and in the bodies of the repository's functions it
    calls, transitively: (node, its module, the stage, whether it is a call, the functions on the
    way there)."""
    cst = src.cst
    seen = set() if seen is None else seen
    if (module.name, id(fn)) in seen or len(via) > _DEPTH:
        return
    seen.add((module.name, id(fn)))
    local = src.local_imports(module, fn)
    hidden = frozenset((_local_names(cst, fn) | set(_params(cst, fn))) - set(local))
    found = []

    class V(cst.CSTVisitor):
        def __init__(self):
            super().__init__()
            self.callees: set[int] = set()

        def visit_Call(self, call):
            self.callees.add(id(call.func))
            found.append((call, call.func, True))

        def visit_Arg(self, node):
            if node.keyword is not None:
                node.value.visit(self)
                return False

        def visit_Name(self, node):
            if id(node) not in self.callees:
                found.append((node, node, False))

        def visit_Attribute(self, node):
            if id(node) not in self.callees:
                found.append((node, node, False))
            node.value.visit(self)
            return False

    fn.body.visit(V())
    for node, expr, is_call in found:
        callee = src.resolve_expr(module, expr, hidden, local)
        if callee is None or not callee.name:
            continue
        if callee.key in by_key:
            yield node, module, by_key[callee.key], is_call, via
        elif callee.binding.kind in ("def", "asyncdef"):
            step = f"{callee.module.name}:{callee.name} ({callee.module.at(callee.binding.node)})"
            yield from _stage_uses(src, callee.module, callee.binding.node, by_key, (*via, step), seen)


def _find_refs(src: _Source, a: _Analysis, by_key: dict, problems: list[str]) -> None:
    """Every place the entry function names a stage, with whether that place may run more than
    once, and whether it is inside a concurrent call."""
    cst, mod, fn = src.cst, a.entry_module, a.entry_fn
    entry = a.plan.entry.function
    shadowing = _local_names(cst, fn) | set(_params(cst, fn))
    repeating = {cst.For: "in a for loop", cst.While: "in a while loop", cst.ListComp: "in a comprehension",
                 cst.SetComp: "in a comprehension", cst.DictComp: "in a comprehension",
                 cst.GeneratorExp: "in a generator expression"}
    unseen = ("how often it runs, and whether its calls overlap, cannot be seen from the entry function; "
              "that is dynamic dispatch")

    def called_name(call) -> str | None:
        f = call.func
        return f.attr.value if isinstance(f, cst.Attribute) else f.value if isinstance(f, cst.Name) else None

    def concurrent_name(call) -> str | None:
        name = called_name(call)
        if name == "map" and isinstance(call.func, cst.Name):
            return None                      # the builtin: one call after another
        return name if name in CONCURRENT else None

    class Refs(cst.CSTVisitor):
        def __init__(self):
            super().__init__()
            self.loops: list[str] = []
            self.nested: list[object] = []  # nested functions and lambdas we are inside
            self.pools: list[str | None] = []
            self.callees: set[int] = set()
            self.handed: set[int] = set()   # arguments of a concurrent call
            self.direct: set[int] = set()   # calls awaited, or given straight to a runner
            self.helpers: set[tuple] = set()

        def on_visit(self, node):
            why = next((w for cls, w in repeating.items() if isinstance(node, cls)), None)
            if why is not None:
                self.loops.append(why)
            if isinstance(node, (cst.FunctionDef, cst.Lambda)) and node is not fn:
                self.nested.append(node)
            return super().on_visit(node)

        def on_leave(self, node):
            super().on_leave(node)
            if any(isinstance(node, cls) for cls in repeating):
                self.loops.pop()
            if isinstance(node, (cst.FunctionDef, cst.Lambda)) and node is not fn:
                self.nested.pop()

        def _add(self, node, call):
            t = src.resolve_expr(mod, node)
            if t is None:
                return False
            if src.head(node) in shadowing and (t.key not in by_key or call is None):
                return t.key in by_key      # the local of that name, not the module's function
            if t.key not in by_key:
                self._helper(node, t)
                return False
            name = by_key[t.key]
            if src.head(node) in shadowing:
                problems.append(f"{mod.at(node)}: {src.head(node)!r} names stage {name!r} at module level, "
                                f"but is shadowed in {entry} by a parameter or a local of that name; which "
                                f"function it calls cannot be resolved statically")
                return True
            if self.nested:
                problems.append(f"{mod.at(node)}: stage {name!r} is {'called' if call else 'used'} inside a "
                                f"nested function or lambda in {entry}: what calls that, {unseen}. Use the "
                                f"stage in {entry}'s own body")
                return True
            if call is None and id(node) not in self.handed:
                problems.append(f"{mod.at(node)}: stage {name!r} is used as a value, not called: {unseen}. "
                                f"Call it directly, or hand it to one concurrent call (a pool's submit, "
                                f"to_thread)")
                return True
            pool = next((p for p in reversed(self.pools) if p is not None), None)
            a.refs.setdefault(name, []).append(_Ref(name, node, call, self.loops[-1] if self.loops else None,
                                                    pool, call is None or id(call) in self.direct))
            return True

        def _helper(self, node, t):
            """A function of the repository that the entry calls or hands over: a stage it runs,
            itself or through the functions it calls, is out of the entry function's sight."""
            if t.binding.kind not in ("def", "asyncdef") or t.key in self.helpers:
                return
            self.helpers.add(t.key)
            step = f"{t.module.name}:{t.name} ({t.module.at(t.binding.node)})"
            for use, module, stage, is_call, via in _stage_uses(src, t.module, t.binding.node, by_key, (step,)):
                problems.append(f"{mod.at(node)}: {entry} reaches stage {stage!r} through "
                                + ", then ".join(via) + f", at {module.at(use)}: {unseen}. Call the stage in "
                                f"{entry}'s own body")
                break

        def visit_Await(self, node):
            self.direct.add(id(node.expression))

        def visit_Call(self, call):
            self.callees.add(id(call.func))
            self._add(call.func, call)
            pool = concurrent_name(call)
            if pool is not None or called_name(call) in _RUNNERS:
                self.direct.update(id(arg.value) for arg in call.args)
            if pool is not None:
                self.handed.update(id(arg.value) for arg in call.args)
            self.pools.append(pool)

        def leave_Call(self, call):
            self.pools.pop()

        def visit_Arg(self, node):
            #: A keyword's name is not a use of anything; only its value is.
            if node.keyword is not None:
                node.value.visit(self)
                return False

        def visit_Name(self, node):
            if id(node) not in self.callees:
                self._add(node, None)

        def visit_Attribute(self, node):
            if id(node) not in self.callees and self._add(node, None):
                return False
            #: The attribute's own name (`.retrieve` in `opts.retrieve`) is not a use of anything.
            node.value.visit(self)
            return False

    fn.body.visit(Refs())


def _outputs(src: _Source, a: _Analysis, by_key_names: set[str]) -> set[str]:
    """Names in the entry function that hold a stage's return value, unchanged: every binding of
    them is `name = stage(...)` (or `name = await stage(...)`), and every use of them is as a
    stage's argument or in `return`. Anything else might change the value in place
    (`passages.reverse()`, `passages[0] = ...`, `tidy(passages)`), and the SDK then records it as
    an in-memory input: it matches a stage's return value by identity, confirmed by digest."""
    cst, mod = src.cst, a.entry_module
    good: set[str] = set()
    bad: set[str] = set(_params(cst, a.entry_fn))
    allowed: set[int] = set()        # Name nodes that are a binding, or a use that keeps the value
    uses: list = []

    def is_stage_call(expr) -> bool:
        if isinstance(expr, cst.Await):
            expr = expr.expression
        if not isinstance(expr, cst.Call):
            return False
        t = src.resolve_expr(mod, expr.func)
        return t is not None and t.key in by_key_names

    class V(cst.CSTVisitor):
        def visit_Call(self, node):
            if is_stage_call(node):
                allowed.update(id(arg.value) for arg in node.args if not arg.star)

        def visit_Return(self, node):
            if node.value is not None:
                allowed.add(id(node.value))

        def visit_AssignTarget(self, node):
            allowed.add(id(node.target))

        def visit_Arg(self, node):
            if node.keyword is not None:
                allowed.add(id(node.keyword))

        def visit_Attribute(self, node):
            allowed.add(id(node.attr))

        def visit_Name(self, node):
            uses.append(node)

        def visit_Assign(self, node):
            single = len(node.targets) == 1 and isinstance(node.targets[0].target, cst.Name)
            if single and is_stage_call(node.value):
                good.add(node.targets[0].target.value)
            else:
                for t in node.targets:
                    names(t.target)

        def visit_AnnAssign(self, node):
            names(node.target)

        def visit_AugAssign(self, node):
            names(node.target)

        def visit_For(self, node):
            names(node.target)

        def visit_CompFor(self, node):
            names(node.target)

        def visit_NamedExpr(self, node):
            names(node.target)

        def visit_WithItem(self, node):
            if node.asname is not None:
                names(node.asname.name)

        def visit_ExceptHandler(self, node):
            if node.name is not None:
                names(node.name.name)

    def names(node):
        if isinstance(node, cst.Name):
            bad.add(node.value)
        elif isinstance(node, (cst.Tuple, cst.List)):
            for el in node.elements:
                names(el.value)
        elif isinstance(node, cst.StarredElement):
            names(node.value)

    a.entry_fn.body.visit(V())
    bad.update(n.value for n in uses if n.value in good and id(n) not in allowed)
    return good - bad


def _check_refs(src: _Source, a: _Analysis, problems: list[str]) -> None:
    cst, mod, plan = src.cst, a.entry_module, a.plan
    keys = {t.key for t in a.targets.values()}
    outputs = _outputs(src, a, keys)

    def is_output(expr) -> bool:
        if isinstance(expr, cst.Await):
            expr = expr.expression
        if isinstance(expr, cst.Call):
            t = src.resolve_expr(mod, expr.func)
            return t is not None and t.key in keys
        return isinstance(expr, cst.Name) and expr.value in outputs

    for stage in plan.stages:
        if stage.function is None or stage.name not in a.targets:
            continue
        refs = a.refs.get(stage.name, [])
        at = f"plan field stages[{stage.index}]"
        if not refs:
            if not any(f"stage {stage.name!r}" in p for p in problems):
                problems.append(f"stage {stage.name!r} ({stage.function}) is never called directly in "
                                f"{plan.entry.function} ({mod.rel}); onetrace-ci checks each stage's calls "
                                f"in the entry function, so the plan's stages are the functions it calls")
            continue
        loose = [r for r in refs if not r.direct]
        if a.targets[stage.name].binding.kind == "asyncdef" and loose:
            problems.append(f"{', '.join(dict.fromkeys(mod.at(r.node) for r in loose))}: stage {stage.name!r} is async, and "
                            f"the coroutine its call returns is not awaited where it is made: when it runs, "
                            f"and whether alongside other calls of it, cannot be seen from the entry "
                            f"function; that is dynamic dispatch. Await it where it is called (`await "
                            f"{stage.function.function}(...)`)")
            continue
        sites = ", ".join(mod.at(r.node) for r in refs)
        pools = sorted({r.concurrent for r in refs if r.concurrent})
        many = len(refs) > 1 or any(r.repeats for r in refs)
        if pools and (many or any(p in _MANY for p in pools)):
            problems.append(f"{sites}: stage {stage.name!r} may overlap itself: it is called concurrently "
                            f"({', '.join(pools)}), and more than once per run; overlapping calls of one "
                            f"stage each need their own instance, `{stage.function.function}.instance(name)"
                            f"(...)`. {_WAITS}; until it does, call the stage from one place at a time")
            continue
        if many and not stage.repeats:
            why = next((r.repeats for r in refs if r.repeats), None) or f"called at {len(refs)} places"
            problems.append(f"{sites}: stage {stage.name!r} may run more than once per run ({why}); a stage "
                            f"that does needs `repeats: true` in the plan ({at}.repeats), and the SDK "
                            f"numbers each call")
            continue
        if stage.trust is not None:
            continue
        # Generated code states every meaning field. An argument that is not a stage's return
        # value, unchanged, is an in-memory input, whose trust class the plan must give; where
        # onetrace-ci cannot see that an argument is one, it asks for the trust class.
        fn = a.targets[stage.name].binding.node
        why = None
        for r in refs:
            if r.call is None:
                if _params(cst, fn):
                    why = (f"is handed over as a value at {mod.at(r.node)}, so what it is given cannot be "
                           f"seen")
                    break
                continue
            other = next((arg for arg in r.call.args if arg.star or not is_output(arg.value)), None)
            if other is not None:
                why = (f"is given {mod.code(other.value)!r} at {mod.at(r.call)}, which onetrace-ci cannot "
                       f"see to be another stage's return value, unchanged")
                break
            unbound = _defaulted(cst, fn, r.call)
            if unbound:
                why = (f"is called at {mod.at(r.call)} without its parameter {unbound[0]!r}, whose default "
                       f"the SDK may record as an argument")
                break
        if why is not None:
            problems.append(f"{at}.trust: missing; stage {stage.name!r} {why}. The SDK records an argument "
                            f"that is not a stage's return value as an in-memory input, whose trust class "
                            f"the plan states")


def _defaulted(cst, fn, call) -> list[str]:
    """The parameters of `fn` that `call` leaves to their defaults (or to an empty `*args` or
    `**kwargs`)."""
    p = fn.params
    positional = [*p.posonly_params, *p.params]
    given = sum(1 for arg in call.args if not arg.star and arg.keyword is None)
    keywords = {arg.keyword.value for arg in call.args if arg.keyword is not None}
    left = [x.name.value for i, x in enumerate(positional)
            if x.default is not None and i >= given and x.name.value not in keywords]
    left += [x.name.value for x in p.kwonly_params if x.default is not None and x.name.value not in keywords]
    left += [prefix + star.name.value for prefix, star in (("*", p.star_arg), ("**", p.star_kwarg))
             if isinstance(star, cst.Param)]
    return left


# ------------------------------------------------------------------ emission

def _decorator(cst, text: str):
    return cst.parse_module(f"@{text}\ndef _(): pass\n").body[0].decorators[0]


def _is_constant_call(cst, tree, stmt) -> bool:
    """True for a call in the form the generator writes, `ot.constant("x", x)`: its key is the
    code of its value. A person's own `ot.constant("model", "m-1")` is not one, and is kept."""
    if not (isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1
            and isinstance(stmt.body[0], cst.Expr) and isinstance(stmt.body[0].value, cst.Call)):
        return False
    call = stmt.body[0].value
    f = call.func
    if not (isinstance(f, cst.Attribute) and isinstance(f.value, cst.Name) and f.value.value == ALIAS
            and f.attr.value == "constant" and len(call.args) == 2
            and all(arg.keyword is None and not arg.star for arg in call.args)):
        return False
    try:
        key = ast.literal_eval(_code(tree, call.args[0].value))
    except (ValueError, SyntaxError):
        return False
    return key == _code(tree, call.args[1].value)


def _code(module_tree, node) -> str:
    return module_tree.code_for_node(node).replace("\r\n", "\n")


def _same(a: str, b: str) -> bool:
    """The same Python expression, however it is laid out (one line, or split by a formatter)."""
    try:
        return ast.dump(ast.parse(a.strip(), mode="eval")) == ast.dump(ast.parse(b.strip(), mode="eval"))
    except SyntaxError:
        return False


def _has_import(cst, module: _Module) -> bool:
    for stmt in module.tree.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            for small in stmt.body:
                if isinstance(small, cst.Import) and any(
                        module.code(al.name) == "onetrace" and al.asname is not None
                        and al.asname.name.value == ALIAS for al in small.names):
                    return True
    return False


def transform(src: _Source, a: _Analysis, module: _Module) -> str:
    """The module's new text: its planned functions decorated, and the import added."""
    cst, plan = src.cst, a.plan
    tree = module.tree
    wanted: dict[int, tuple[str, str, list[str] | None]] = {}
    if module is a.entry_module:
        wanted[id(a.entry_fn)] = ("run", run_decorator(plan), None)
    for stage in plan.stages:
        t = a.targets.get(stage.name)
        if t is not None and t.module is module:
            wanted[id(t.binding.node)] = ("stage", stage_decorator(stage), constant_lines(stage))

    class Decorate(cst.CSTTransformer):
        def leave_FunctionDef(self, original, updated):
            spec = wanted.get(id(original))
            if spec is None:
                return updated
            kind, text, constants = spec
            decorators = list(updated.decorators)
            at = next((i for i, d in enumerate(original.decorators)
                       if _sdk_decorator(src, module, d.decorator)[0] == kind), None)
            if at is None:
                decorators.insert(0, _decorator(cst, text))
            elif not _same(_code(tree, original.decorators[at].decorator), text):
                decorators[at] = _decorator(cst, text).with_changes(leading_lines=decorators[at].leading_lines)
            updated = updated.with_changes(decorators=decorators)
            if constants is None:
                return updated
            body = updated.body
            if isinstance(body, cst.SimpleStatementSuite):
                body = cst.IndentedBlock(body=[cst.SimpleStatementLine(body=body.body)],
                                         header=body.trailing_whitespace)
            stmts = list(body.body)
            #: A docstring written on one line with code (`def f(x): """Doc."""; return x`) goes
            #: on a line of its own, so the constants go after it and it stays the docstring.
            plain = cst.MaybeSentinel.DEFAULT
            first = stmts[0] if stmts else None
            if isinstance(first, cst.SimpleStatementLine) and len(first.body) > 1 \
                    and _is_docstring(cst, first.with_changes(body=[first.body[0]])):
                stmts[0:1] = [first.with_changes(body=[first.body[0].with_changes(semicolon=plain)]),
                              cst.SimpleStatementLine(body=list(first.body[1:]))]
            start = 1 if stmts and _is_docstring(cst, stmts[0]) else 0
            end = start
            while end < len(stmts) and _is_constant_call(cst, tree, stmts[end]):
                end += 1
            have = [_code(tree, s.body[0].value) for s in stmts[start:end]]
            if len(have) == len(constants) and all(_same(h, c) for h, c in zip(have, constants)):
                return updated
            new = [cst.parse_statement(line + "\n") for line in constants]
            if end > start and new:
                new[0] = new[0].with_changes(leading_lines=stmts[start].leading_lines)
            return updated.with_changes(body=body.with_changes(body=[*stmts[:start], *new, *stmts[end:]]))

    new_tree = tree.visit(Decorate())
    if not _has_import(cst, module):
        body = list(new_tree.body)
        insert_at = 0
        for i, stmt in enumerate(body):
            if i == 0 and _is_docstring(cst, stmt):
                insert_at = 1
                continue
            if isinstance(stmt, cst.SimpleStatementLine) and all(
                    isinstance(s, (cst.Import, cst.ImportFrom)) for s in stmt.body):
                insert_at = i + 1
                continue
            break
        imp = cst.parse_statement(IMPORT + "\n")
        if insert_at < len(body):
            nxt = body[insert_at]
            if insert_at == 0:
                #: The file's first lines (a shebang, a coding line, a comment) stay first.
                imp = imp.with_changes(leading_lines=nxt.leading_lines)
                nxt = nxt.with_changes(leading_lines=[])
            blank = next((i for i, line in enumerate(nxt.leading_lines) if line.comment is not None),
                         len(nxt.leading_lines))
            imported = isinstance(nxt, cst.SimpleStatementLine) and all(
                isinstance(s, (cst.Import, cst.ImportFrom)) for s in nxt.body)
            want = 0 if imported else 2 if isinstance(nxt, (cst.FunctionDef, cst.ClassDef)) else 1
            if blank < want:
                nxt = nxt.with_changes(leading_lines=[cst.EmptyLine(indent=False)] * (want - blank)
                                       + list(nxt.leading_lines))
            body[insert_at] = nxt
        body.insert(insert_at, imp)
        new_tree = new_tree.with_changes(body=body)
    return new_tree.code


def generate(src: _Source, plan: InstrumentPlan) -> tuple[list[tuple[str, str, str]], list[str]]:
    """((path, old text, new text) for each module that changes, summary lines), or `Refused`."""
    waits = plan_waits(plan)
    if waits:
        raise Refused(waits)
    a = analyse(src, plan)
    modules = [a.entry_module]
    for stage in plan.stages:
        t = a.targets.get(stage.name)
        if t is not None and t.module not in modules:
            modules.append(t.module)
    changes, summary = [], []
    for module in modules:
        old = _utf8_source(module.raw, module.rel)
        new = transform(src, a, module)
        if new != old:
            changes.append((module.rel, old, new))
        else:
            summary.append(f"{module.rel}: already decorated as the plan says")
    summary[:0] = _describe(a, plan)
    return changes, summary


def _describe(a: _Analysis, plan: InstrumentPlan) -> list[str]:
    found = edges(plan)
    lines = [f"{a.entry_module.rel}: {plan.entry.function}() gets @ot.run, naming the plan's "
             f"{sum(1 for s in plan.stages if s.function is not None)} stages; each run is written to "
             f"{plan.run_dir}" + (f"; declared edges: " + ", ".join(f"{x} -> {y}" for x, y in found)
                                  if found else "")]
    width = max(len(s.name) for s in plan.stages)
    for s in plan.stages:
        t = a.targets.get(s.name)
        if t is None:
            continue
        refs = a.refs.get(s.name, [])
        parts = [f"@ot.stage on {s.function} ({t.module.rel}), called at "
                 + ", ".join(a.entry_module.at(r.node) for r in refs)]
        for f in s.files:
            parts.append(f"reads {f} ({s.trust})")
        parts.append(f"instrument {s.instrument.name}, version of {s.instrument.package} read at run time")
        if s.constants:
            parts.append("records constants " + ", ".join(repr(c) for c in s.constants))
        if s.repeats:
            parts.append("repeats: each call numbered by the SDK")
        parts.append(f"rederivable: {s.rederivable}")
        lines.append(f"  {s.index + 1}. {s.name:<{width}}  " + "; ".join(parts))
    return lines
