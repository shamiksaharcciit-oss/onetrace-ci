# onetrace-ci errors

Every refusal from an onetrace-ci command ends with a `fix:` line and a link to one of the
sections below. Each section says what the refusal means and what to do about it.

## plan-syntax

The plan file uses YAML the plan reader refuses: a construct two YAML readers could read differently (a block scalar, an anchor, an unquoted yes/no/on/off or date, a control character), or one it does not support.

**Fix:** Rewrite the named line inside the plan's YAML subset: plain `key: value`, block lists, one-line flow lists and mappings; quote a value to keep it as text.

## open-question

A field still holds a `DECIDE:` question, as `onetrace-ci discover` or `init-ci` drafts them. Nothing is generated, and no gate passes, until a person answers it.

**Fix:** Answer the question in the named field and delete the `DECIDE:` text; a person decides it.

## key-material

`sign.key_env` looks like a key rather than the name of the environment variable that holds one. The plan is committed, so it must never carry a key.

**Fix:** Put the name of the environment variable that holds the key in `sign.key_env`, and the key itself in your CI secret store.

## decorator-style

The plan asks for several entries, a stage that repeats, or a stage's named instances, which only decorator output (onetrace 0.2.0's `@ot.run` and `@ot.stage`) generates; the wrapper style does not.

**Fix:** Generate this plan as decorators (onetrace 0.2.0's `@ot.run` and `@ot.stage`), which carry several entries, repeats and instances; the wrapper style does not.

## needs-sdk

The plan asks for code (signing, anchoring, settings, a corpus link) that this version of onetrace-ci does not generate yet.

**Fix:** Leave this out of the plan for now (or set it to `none`); it is generated once onetrace-ci is checked against the SDK version that provides it.

## plan-path

A path in the plan or on the command line is absolute, climbs out of the repository, uses backslashes, or is a wildcard pattern.

**Fix:** Write a relative path inside the repository, with forward slashes and no `..` or wildcards.

## run-dir

`run_dir` has no `{run_id}`, or the workflow's run would land on the committed baseline. Each run needs a folder of its own.

**Fix:** Give `run_dir` a `{run_id}` placeholder (for example `runs/{run_id}`) and keep the baseline somewhere else.

## files-missing

A file the plan names (or the plan itself) is not where it says.

**Fix:** Create the named file in the repository, or correct the path in the plan.

## missing-field

A field that carries meaning is missing. The tool writes the boilerplate; a person writes the meaning, and nothing is filled in by guessing.

**Fix:** Write the named field in the plan; the tool never fills in meaning.

## invalid-value

A field holds a value it does not accept, or the plan has a field this format does not know.

**Fix:** Correct the named field: the message says which values it accepts.

## class-method

The plan names a method. Only module-level functions can be stages, because a method's owner cannot be resolved safely.

**Fix:** Make the stage a module-level function (a method can call it), and name that function.

## unresolvable

The function the plan names cannot be found by reading the code: its module is missing, it is not defined at the top level, or a star import or a module that is both a file and a package makes it ambiguous.

**Fix:** Point the plan at a module-level `def` that exists under --repo, imported by name in the entry module (no star imports, and no module that is both a file and a package).

## not-a-function

A stage is a lambda, a generator, an async function, a class or an assigned value, or is the entry function itself, or shares its function with another stage.

**Fix:** Make each stage a plain, named, synchronous `def` that returns one value, distinct from the entry function and from the other stages.

## nested-stage

One stage is called inside another: in its arguments, or from inside its body.

**Fix:** Call each stage once, on its own, from the entry function; don't call one stage from inside another.

## dynamic-dispatch

A stage is reached through a variable, a table or `getattr`, so which function runs cannot be read from the code.

**Fix:** Call the stage directly by name (`retrieve(q)`), not through a variable, table or getattr.

## shadowed

A parameter or local of the entry function has the same name as a stage, so the call site may not call the stage.

**Fix:** Rename the parameter or local that hides the stage's name in the entry function.

## star-import

A `from module import *` makes the names a module uses impossible to resolve.

**Fix:** Import the names you use explicitly (`from pkg import retrieve`).

## stage-placement

A stage call might not run exactly once in the order written: it is in a loop, a branch, a try block, an assert (which `python -O` removes), a lambda or comprehension, or shares a statement with another stage call.

**Fix:** Call each stage exactly once, in a statement of its own, at the top level of the entry function: not in a loop, branch, try block, assert, lambda or comprehension.

## stage-order

A planned stage is never called, called more than once, or called out of the plan's order.

**Fix:** Call every planned stage once, in the order the plan lists them.

## memory-input

`memory_inputs` names something that is not a parameter of the entry function.

**Fix:** Name a parameter of the entry function in `memory_inputs`, or add the parameter.

## recorder-present

The code already creates a Recorder, at module level or in the entry function.

**Fix:** Remove the hand-written Recorder, or keep the hand instrumentation and use `init-ci` instead of `instrument`.

## reserved-name

The entry module uses a name starting with `_onetrace`, which the generated code reserves.

**Fix:** Rename the name that starts with `_onetrace`.

## source-encoding

The entry module is not valid Python, or not UTF-8. The patch is written in UTF-8.

**Fix:** Make the file valid UTF-8 Python (fix the named line, or convert the encoding).

## instrumented-other-plan

The code was instrumented from a different plan. A second patch over it would be a guess.

**Fix:** Revert the earlier patch (`git apply -R` it), then run instrument again.

## will-not-overwrite

A file the command would write already exists. onetrace-ci never overwrites a file.

**Fix:** Move the existing file aside (or choose another path), then run the command again.

## not-a-directory

A run or baseline path is not a folder.

**Fix:** Point the option at an existing run folder (one with a MANIFEST.json).

## discover-command

The command discover observes failed, was not found, or was not given. Discovery drafts only from fixtures that pass: a failing run shows what went wrong, not what the pipeline does.

**Fix:** Give discover, after `--`, a command that runs your fixtures, finishes, and passes on its own (for example `-- pytest tests/test_pipeline.py`).

## discover-entry-unseen

The command passed, but discovery saw nothing to draft from: the observer never loaded in its interpreter, or the command never called the function `--entry` names. A draft from a run discovery never saw would look like an answer, so it refuses instead.

**Fix:** Name in --entry the function your fixtures call (`module.path:function`), and run a Python command that honours PYTHONPATH (without -E or -I) and exits normally.

## discover-git

The repository is inside a git work tree, but git could not list the files it tracks (git is not installed, or it refuses the folder). Discovery writes a file's name only if git tracks it, and it doesn't guess from what happens to exist, so it refuses.

**Fix:** Make `git ls-files` work in the repository (install git, or allow the folder with `git config --global --add safe.directory <path>`), then run discover again.
