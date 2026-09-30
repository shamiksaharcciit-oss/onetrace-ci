"""Every refusal names its fix.

A refusal from any onetrace-ci command is printed with a `fix:` line (what to do about it) and a
`see:` line (the section of `docs/errors.md` that explains it). The codes below are the anchors
of that page. Each refusal message is matched to its code by what it says; a message that
matches no code is a defect, and `tests/test_errors.py` fails on it.
"""
from __future__ import annotations

import re

ERRORS_PAGE = "docs/errors.md"

#: (code, pattern the message matches, fix). Ordered: the first match wins, so the specific
#: patterns come before the general ones.
CODES: list[tuple[str, str, str]] = [
    ("plan-syntax", r": line \d+: |not valid YAML|outside the supported subset",
     "Rewrite the named line inside the plan's YAML subset: plain `key: value`, block lists, "
     "one-line flow lists and mappings; quote a value to keep it as text."),
    ("open-question", r"DECIDE:|still an open question",
     "Answer the question in the named field and delete the `DECIDE:` text; a person decides it."),
    ("key-material", r"never the key itself",
     "Put the name of the environment variable that holds the key in `sign.key_env`, and the "
     "key itself in your CI secret store."),
    ("decorator-waits", r"\bwaits for\b",
     "Leave this form out for now: onetrace-ci generates it once the SDK carries it (or once the "
     "decision the message names is made). Until then, plan one entry without parameters, call each "
     "stage from one place at a time, and name no instances or corpus link."),
    ("decorator-style", r"generated in decorator style only",
     "Generate this plan as decorators (onetrace 0.2.0's `@ot.run` and `@ot.stage`), which carry "
     "several entries, repeats and instances; the wrapper style does not."),
    ("needs-sdk", r"onetrace 0\.2\.0",
     "Leave this out of the plan for now (or set it to `none`); it is generated once onetrace-ci "
     "is checked against the SDK version that provides it."),
    ("plan-path", r"backslash|is absolute|leaves the repository|is a pattern; name each file|"
                  r"must be a path inside --repo|the plan is outside --repo",
     "Write a relative path inside the repository, with forward slashes and no `..` or wildcards."),
    ("run-dir", r"\{run_id\}|would be written onto the baseline|would be written to .* which is the committed baseline|"
                r"would be written inside",
     "Give `run_dir` a `{run_id}` placeholder (for example `runs/{run_id}`) and keep the baseline "
     "somewhere else."),
    ("files-missing", r"does not exist under|no such plan file",
     "Create the named file in the repository, or correct the path in the plan."),
    ("missing-field", r"missing; |missing for stage|names nobody",
     "Write the named field in the plan; the tool never fills in meaning."),
    ("invalid-value", r"is not one of|must be true or false|is a boolean, which the SDK|must be text|"
                      r"must be a list|must be a mapping|must be a literal value|not a stage field|"
                      r"not an instrument field|not a ci field|not a corpus field|not a sign field|"
                      r"not an anchor field|not a trust field|is not a parameter name|"
                      r"is not `module\.path:function`|names an earlier stage too|"
                      r"is not a stage declared before|is not a planned stage|only a stage without a function|"
                      r"cannot be recorded: the SDK refuses|the only placeholder|several entries|"
                      r"give the same generated name|a stage without a function has no parameters|"
                      r"names a function; name the ingest stage|a plan with entries has no top-level|"
                      r"is also entries\[|not an entries field|is a stage of this entry|"
                      r"is not a stage of another entry|is named twice; each instance|"
                      r"\.constants: '[^']*' cannot be resolved|one baseline path for \d+ entries|"
                      r"must be a path, or null|is the baseline of both|planned differently|"
                      r"would gate nothing",
     "Correct the named field: the message says which values it accepts."),
    ("class-method", r"names a class method",
     "Make the stage a module-level function (a method can call it), and name that function."),
    #: Before `unresolvable`: a shadowed name's message also says it "cannot be resolved".
    ("shadowed", r"shadowed",
     "Rename the parameter or local that hides the stage's name in the entry function."),
    ("unresolvable", r"no module file|has no top-level function|cannot be resolved|is both .* and",
     "Point the plan at a module-level `def` that exists under --repo, imported by name in the "
     "entry module (no star imports, and no module that is both a file and a package)."),
    ("not-a-function", r"is a lambda|is a generator|is an async|is a class, not|bound by assignment|"
                       r"is the entry function itself|two stages cannot share one function",
     "Make each stage a named `def` that returns one value (not a lambda, generator or class; "
     "`async def` in decorator style only), distinct from the entry function and from the other "
     "stages; and make the entry function one that returns, not a generator."),
    ("other-entry-stage", r"which is a stage of another entry",
     "Plan the function as a stage of this entry too, planned alike (the same name and meaning "
     "fields), or don't call it from this entry: an `@ot.run` refuses a stage it does not declare."),
    ("already-decorated", r"is already decorated as",
     "Make the existing `@ot.stage` agree with the plan: the same stage name, on the function the "
     "plan names (or remove it). onetrace-ci replaces a matching decorator's arguments; it never "
     "renames or moves one."),
    ("nested-stage", r"nested stage calls? (are|is) refused|inside its own body|inside the arguments of",
     "Call each stage once, on its own, from the entry function; don't call one stage from inside "
     "another."),
    ("dynamic-dispatch", r"dynamic dispatch",
     "Call the stage directly by name, in the entry function's own body (`retrieve(q)`; `await "
     "retrieve(q)` for an async stage), not through a variable, table, getattr, helper, nested "
     "function or lambda. An async stage may be given to one asyncio task "
     "(`asyncio.create_task(retrieve(q))`)."),
    ("star-import", r"star import",
     "Import the names you use explicitly (`from pkg import retrieve`)."),
    ("stage-placement", r"at the top level of|in one statement|called conditionally|inside an assert|"
                        r"between .* which are called in one statement|comes after the last function stage",
     "Call each stage exactly once, in a statement of its own, at the top level of the entry "
     "function: not in a loop, branch, try block, assert, lambda or comprehension."),
    ("repeats", r"needs `repeats: true`",
     "If the stage runs more than once per run, one call after another, add `repeats: true` to it in "
     "the plan (the SDK numbers each call); otherwise call it from one place."),
    ("stage-order", r"never called directly|more than once|is called before stage",
     "Call every planned stage directly from the entry function; in wrapper style, call each once, "
     "in the order the plan lists them."),
    ("memory-input", r"is not a parameter of|is not listed in any stage's memory_inputs",
     "List every parameter of the entry function in the intake stage's `memory_inputs` (the SDK "
     "records each one), and only parameters."),
    ("recorder-present", r"module-level Recorder|already creates a Recorder",
     "Remove the hand-written Recorder, or keep the hand instrumentation and use `init-ci` "
     "instead of `instrument`."),
    ("reserved-name", r"which the generated code reserves|is reserved: in decorator style",
     "Rename it: the generated code reserves names starting with `_onetrace` (wrapper style), and "
     "the name `ot` in the modules it decorates and the stage name `intake` (decorator style)."),
    ("source-encoding", r"not valid Python|declares the .* encoding|not valid UTF-8",
     "Make the file valid UTF-8 Python (fix the named line, or convert the encoding)."),
    ("instrumented-other-plan", r"already instrumented from a different plan|already instrumented in wrapper style",
     "Revert the earlier patch (`git apply -R` it), then run instrument again."),
    ("will-not-overwrite", r"already exists",
     "Move the existing file aside (or choose another path), then run the command again."),
    ("not-a-directory", r"not a directory",
     "Point the option at an existing run folder (one with a MANIFEST.json)."),
    ("discover-command", r"the command exited \d+|the command .* was not found|the command to observe is missing|"
                         r"the command did not finish within",
     "Give discover, after `--`, a command that runs your fixtures, finishes, and passes on its own "
     "(for example `-- pytest tests/test_pipeline.py`)."),
    ("discover-entry-unseen", r"the observer never loaded|the command never called",
     "Name in --entry the function your fixtures call (`module.path:function`), and run a Python "
     "command that honours PYTHONPATH (without -E or -I) and exits normally."),
    ("discover-git", r"git could not list the files it tracks",
     "Make `git ls-files` work in the repository (install git, or allow the folder with "
     "`git config --global --add safe.directory <path>`), then run discover again."),
]
_COMPILED = [(code, re.compile(pattern), fix) for code, pattern, fix in CODES]


def explain(problem: str) -> tuple[str, str] | None:
    """(code, fix) for a refusal message, or None if no code covers it (a defect)."""
    for code, pattern, fix in _COMPILED:
        if pattern.search(problem):
            return code, fix
    return None


def format_refusal(command: str, problems: list[str]) -> str:
    """The refusal as printed: each problem, then its fix and the page that explains it."""
    lines = [f"onetrace-ci {command}: refused:"]
    for problem in problems:
        lines.append(f"  {problem}")
        found = explain(problem)
        if found is not None:
            code, fix = found
            lines.append(f"    fix: {fix}")
            lines.append(f"    see: {ERRORS_PAGE}#{code}")
    return "\n".join(lines)
