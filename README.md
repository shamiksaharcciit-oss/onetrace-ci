# onetrace-ci

The onetrace CI instrumenter. `onetrace-ci gate` turns `onetrace`'s existing
commands (`onetrace-verify`, `onetrace diff`, `onetrace localize`,
`onetrace reproduce`) into one CI verdict for code that is already
instrumented. `onetrace-ci instrument` writes a reviewable patch that
instruments a pipeline from a plan a person wrote and approved — see
[Instrument from a plan](#instrument-from-a-plan). **`onetrace` itself is
never modified**. This package depends on `onetrace` and on LibCST (for
`instrument`), both pinned by hash — see `requirements.lock`. Licensed
Apache-2.0 — see `LICENSE`.

## What this does *not* claim

**The gate proves your records are consistent. It does not prove your
output is correct.** `onetrace-verify` and `onetrace diff` check that a run's
receipts are internally consistent and that today's output matches a
declared baseline byte-for-byte; neither one, nor this gate, judges whether
that output is *right*. A `pass` means "this run's own record checks out
and matches what you previously approved," never "the answer is correct."

## What waits for the next onetrace release

Some of what a plan can ask for needs an `onetrace` SDK release that does not
exist yet. **These are planned, not released.** Until that release exists,
onetrace-ci refuses them, with a message that says so, and generates nothing
for them:

- **Signing and anchoring.** A plan's `sign` or `anchor` block other than
  `none` is refused by `onetrace-ci instrument`. Runs are unsigned and
  unanchored.
- **Recording settings and the corpus link.** A stage's `config` or
  `constants`, and the plan's `corpus`, are refused by `onetrace-ci
  instrument`.
- **Decorators.** `onetrace-ci instrument` writes plain wrapper code. Code
  that uses the SDK's decorators is not generated, and a plan with several
  `entries` is refused.

Everything else here works with `onetrace` 0.1.1, the version
`requirements.lock` pins. `onetrace-ci discover` asks about signing,
anchoring and trust in its drafts; answer `none` until the release exists.

## Install

<!-- not executed -->
```
pip install --require-hashes -r requirements.lock
pip install --no-deps .
```

## A first gate run, walked through

You need three things: a run to check, a baseline to check it against, and
a plan file naming who approved that baseline. If you already have two
`onetrace` runs on disk (`run/` and `baseline/`) and a plan:

```yaml
# onetrace-plan.yaml
approved_by: alice
```

<!-- not executed -->
```
onetrace-ci gate --run run/ --baseline baseline/ --plan onetrace-plan.yaml --out gate-out/
```

Read the verdict from the exit code, or from `gate-out/summary.md`:

- **exit 0 — pass.** Every check agreed: the record is consistent, the
  output matches the baseline, and any declared boundaries are the ones the
  plan already approved.
- **exit 2 — review required.** Something changed in a way the plan hasn't
  pre-approved — a differing output, or an instrument/config annotation on
  an otherwise-matching stage — and a person should look at
  `gate-out/summary.md` (and, if a stage genuinely diverged,
  `gate-out/localize/localize.json`) before deciding. `--review-exit 0`
  makes this exit `0` instead, if you'd rather review results asynchronously
  than block a merge on them.
- **exit 1 — fail.** Something is wrong outright: the record itself doesn't
  verify, an unapproved boundary or gap exists, the plan names no approver,
  or the output diverged in a way the plan doesn't waive.

`gate-out/verdict.json` carries every individual finding — which check
produced it, the exact command and exit code, and the report file it came
from — so a CI step can act on more than the one overall number if it needs
to.

## `onetrace-ci gate`

<!-- not executed -->
```
onetrace-ci gate --run R --baseline B --plan P [--runner J] --out D [--review-exit 0|2]
```

Calls `onetrace-verify --require-artifacts`, `onetrace diff`, `onetrace
localize` (only when `diff` diverges) and `onetrace reproduce` (only when
the plan says `reproduce: true`) as subprocesses, and maps each exit code:

| Command | Exit | Gate |
| --- | --- | --- |
| verify with `--require-artifacts` | nonzero | fail |
| coverage (a run's own boundaries/gaps) | not in `approved_boundaries` | fail |
| diff | 0 | pass |
| diff | 1 | review, with `localize` output attached |
| diff | 2 | fail |
| diff | 3 | fail |
| diff | 4 | fail, unless the stage is in `known_limits` |
| a `same` stage with an instrument/config annotation | — | review |
| reproduce (only when `reproduce: true`) | 1 | fail |
| reproduce | 4 | warn |

**The gate's own exit:** `0` pass, `1` fail, `2` review required.
`--review-exit 0` makes a review-only result advisory (exit `0`). Nothing
unchecked is ever a pass. Writes `D/verdict.json` (every finding, its
command, exit code and report path) and `D/summary.md`.

**The gate never writes, updates or regenerates a baseline.**

## The plan file, `onetrace-plan.yaml`

Read with a deliberately restricted YAML-subset reader (see
`src/onetrace_ci/yamlsubset.py` for exactly what it supports and refuses):
block mappings and lists, one-line flow lists and mappings of plain or
quoted scalars, and comments. What it accepts it reads exactly as a real
YAML parser does, except that it never produces a number. A construct
outside that subset (a block scalar, an anchor or alias, a tag, a nested
flow collection, a duplicate key) is refused outright, naming the line,
never misread as something else. The gate reads five keys:

```yaml
format: onetrace-ci-plan/0.1
approved_boundaries:
  - retrieve
known_limits:
  - embed
reproduce: false
approved_by: alice
```

The keys `onetrace-ci instrument` reads (`entry`, `run_dir`, `stages`, `ci`)
belong to the same file; the gate does not use them. Any other top-level key
is ignored for gating purposes, but named in the summary. A missing or empty
`approved_by` is a **fail**: a gate against an unapproved plan proves
nothing.

`require_declared` decides what the gate does with a field nobody declared.
Code decorated by hand that leaves out a trust class or re-derivability
records the cautious default value, and names the field in the stage's
`assertions.undeclared` list. `true` fails a run in which any stage's list
is non-empty, naming the stages and the fields; `false` reports their count.
It defaults to `true` in a plan with `stages` (one that `onetrace-ci
instrument` reads; its generated code always passes explicit values, so
`true` only catches a later hand edit), and to `false` in a plan without
them (the plan `onetrace-ci init-ci` writes for code decorated by hand says
`false`).

## `onetrace-ci baseline propose`

<!-- not executed -->
```
onetrace-ci baseline propose --from R [--baseline B] --out B.new
```

Writes a candidate baseline (a copy of `R`) for a human to review and
commit — never commits it itself. With `--baseline B` given, also writes
the diff against the current baseline that justifies replacing it, at
`B.new.diff/`; `B` itself is never written to. Without `--baseline`
(a project's very first baseline), writes only the candidate and says so —
there is nothing yet to compare it against.

## The GitHub Action

`action.yml`, a composite action. Runs the gate, appends `summary.md` to the
job summary, and uploads the gate's report directory as a workflow artifact,
kept for 14 days. Needs only `contents: read` — no PR comment and no SARIF in
phase 1, since both need write permissions a fork PR does not have.

**The artifact is readable by anyone who can read the repository**, so in a
public repository, by anyone. It holds only what the gate report holds: stage
names, digests, verdicts and settings. It never holds the run itself, whose
stored outputs are your pipeline's data.

## Instrument from a plan

<!-- run in a fixture repo -->
```
onetrace-ci instrument --plan onetrace-plan.yaml --repo . --out instrument.patch
```

**The patch records what your plan names; it does not find stages you
didn't list.** The tool writes the boilerplate; you own the meaning.

A person writes the plan: which functions are stages and in what order,
where each stage's inputs come from and how far they are trusted, whether
each stage can be re-derived, and which boundaries are approved. The command
turns that plan into a **patch — never an edit in place** — and prints what
the patch will change. Applying it is your step:

<!-- run in a fixture repo -->
```
git apply instrument.patch
```

Run again on code it has already instrumented from the same plan, it writes
an empty patch. Run on code instrumented from a *different* plan, it refuses
and asks you to revert the earlier patch first.

### The plan

```yaml
approved_by: alice
entry: pipeline.main:run            # the function that is one run
run_dir: runs/{run_id}              # where each run is written
stages:
  - name: intake                    # no function: records entry parameters
    memory_inputs: [request]        #   digests only, with ctx.read_memory
    trust: externally-sourced
    rederivable: "true"
  - name: retrieve
    function: pipeline.retrieval:retrieve
    instrument: {name: bm25, package: rank_bm25, kind: retriever}   # version read at run time
    inputs: [intake]                # optional; the default is the previous stage
    files: [data/corpus.json]       # read with ctx.read_external
    trust: operator-authored
    rederivable: "true"
  - name: answer
    function: pipeline.llm:answer
    instrument: {name: model-call, package: openai, kind: model}
    rederivable: "false"
    rederivable_note: "hosted model; sampling not reproducible"
approved_boundaries: []
ci:
  install: pip install --require-hashes -r requirements.lock
  run: python -m pipeline.demo      # runs the pipeline on your CI fixtures
  baseline: runs/baseline           # the committed baseline run
```

**Every field that carries meaning is written by a person**: the stages and
their order, each trust class, whether each stage can be re-derived, the
approved boundaries (even when there are none), and the files. If one is
missing, the command refuses and names the field — all of them at once — and
never fills one in by guessing. A `DECIDE:` question left in any field is
refused the same way. Every `instrument` names its `kind` (retriever,
chunker, model…), because the SDK's `Instrument` requires one.

### What the patch does

In the entry module only:

- **one `Recorder` per run**, created inside the entry function, with
  `close()` in a `finally`. Never a module-level recorder;
- **each planned function becomes a stage, in the declared order.** Its call
  site in the entry function is pointed at a wrapper, defined inside the
  entry function, that reads the stage's inputs (the outputs of the stages
  it names, and its files, with `ctx.read_external`), calls your function
  unchanged, and stores its return value as the stage's output — bytes as
  they are, text as UTF-8, anything else as sorted JSON. A return value JSON
  cannot hold stops the run, loudly;
- **an intake stage** records the entry parameters it names with
  `ctx.read_memory`: their digests, never their values (onetrace 0.1.2);
- **instrument versions** are read at run time with
  `importlib.metadata.version(package)`, never written into the code;
- every run gets its id from `ONETRACE_RUN_ID` when that is set, and a new
  id otherwise, and is written under the repository it runs from, in
  `run_dir` (which must hold `{run_id}`, so each run has its own folder).

Three limits follow from that, stated plainly:

- **The pipeline runs from its checkout.** Installed somewhere without its
  plan (a non-editable install), the instrumented entry function stops at
  once and says so, rather than writing runs into `site-packages`.
- **`ci.run` runs the pipeline once.** The workflow gates one run against
  one baseline; a second call in the same process with the same
  `ONETRACE_RUN_ID` is refused by the SDK, because a run folder is written
  only once.
- **Stage values must be recordable.** A stage whose arguments or return
  value JSON cannot hold (a `set`, an arbitrary object, `NaN`) stops the run
  with an error that names the stage. The un-instrumented code would have
  carried on; a record that silently skipped the value would not be a
  record of the run.

It also adds `.github/workflows/onetrace.yml`, which installs and runs the
pipeline (`ci.install`, `ci.run`, with `ONETRACE_RUN_ID=onetrace-ci-candidate`)
and then runs the `onetrace-ci gate` action against `ci.baseline`. Its only
permission is `contents: read`, and every action it uses is pinned to a
commit. It adds nothing else.

To make the first baseline, run the instrumented pipeline once, then
`onetrace-ci baseline propose --from runs/<that run> --out runs/baseline`,
review it, and commit it.

### What it refuses

Anything it cannot handle safely, with the file and line (or the plan
field), and no patch is written:

- a missing meaning field in the plan, or a `DECIDE:` question left open;
- a function it cannot resolve, a class method, a lambda, a generator, an
  async function, or a name bound to anything but a function definition;
- a nested stage call — in the entry function's arguments, or one stage's
  body calling another;
- a stage called conditionally (`if`, `and`/`or`, a chained comparison), in a
  loop, in a `try` block, in an `assert` (which `python -O` removes), in a
  lambda or comprehension, more than once, out of the declared order, or
  never at all — and two stages called in one statement, so the order they
  run in is always the order they are written;
- a stage used as a value, or any other dynamic dispatch (`getattr(...)()`,
  `table[key]()`), a stage's name shadowed by a parameter or a local, and a
  star import;
- a module that exists both as a file and as a package;
- a `Recorder` already created at module level, or inside the entry function;
- a name the generated code reserves (`_onetrace_…`);
- an entry module that is not UTF-8, and plan paths with backslashes.

## Discover the stages first

When you don't know a pipeline's stages yet, let your own tests show them:

<!-- not executed -->
```sh
onetrace-ci discover --entry pipeline.main:run -- pytest tests/test_pipeline.py
```

It runs the command you give it, unchanged, with an observer loaded for that
command only. It sees what your fixtures do and nothing else: it never calls
production, and never runs the pipeline on inputs of its own. It writes three
files, and refuses to run if any of them already exists: it never overwrites
a draft someone may have started answering.

- **`onetrace-plan.draft.yaml`**, a plan in the format `instrument` reads: the
  functions your entry function called, in the order it called them, each
  stage's inputs from the data that flowed between them, and the files each
  one read. **Every field that carries meaning is a `DECIDE:` question** —
  who approves, trust, whether a stage can be re-derived and the note that
  goes with it, the boundaries, the instrument's kind — and `instrument`
  refuses the plan until a person has answered every one. Answer them in a
  copy named `onetrace-plan.yaml`.
- **`discovery-report.md`**, which opens with the number of questions still
  open, then lists first the reads no stage explains (a file, an HTTP call or
  an environment variable read outside every stage), then each stage with the
  reasons behind it — never a score — the proposed boundaries, the
  branches your fixtures never took, and the settings seen in the code: a
  short literal keyword argument with its value, and one read from the
  environment by the variable's name only, never its value. A literal that
  may be a credential, spans lines or is long is named without its value.
  Each stage's settings are drafted as a question, not recorded.
- **`discovery-events.jsonl`**, what the observer saw. It holds
  **fingerprints only**: no data value, no environment value, no exception
  message (types only). Fingerprints are keyed by a random key made for that
  one discovery and never written down, so they join events within it but
  can't be looked up afterwards.

**No name that could carry a value is written either.**

- A file is named only if git tracks it. Outside a git work tree, it is
  named if it existed before the run. Inside one where git can't list its
  files, discovery refuses rather than guess.
- A code file is also named if it existed before the run, so code you are
  still writing keeps its name. A code file made during the run is not, and
  a stage in one is proposed as "unnamed stage", with its `function` a
  question.
- A file outside the repository is named only by the installed package it
  belongs to.
- An environment variable, a host or a program is named only if the code
  that used it writes the name: your repository's code on the stack (not
  your tests, not the test runner), or the library code it called to do so.
- Anything else is described by what it is ("a file not tracked by git").
  A stage that reads such a file has its `files` drafted as a question.

**What discovery does not claim:**

- **Discovery is not evidence.** Observation is data about the fixtures that
  ran. It never makes anything pass.
- **It sees only the code paths the fixtures exercised.** The branches that
  weren't run are listed, and coverage stays incomplete until a person
  approves the boundaries.
- **Nothing it drafts is a decision.** The report opens with the number of
  `DECIDE:` questions still open.

It observes file reads and writes, environment reads, `subprocess.run`, HTTP
calls through `requests` or `httpx` (naming `openai` or `anthropic` when
their code made the call), exceptions, and the calls your entry function
makes, directly or through a lambda, a comprehension or a decorator. It does
not yet take in LangChain or LlamaIndex callbacks or OpenTelemetry spans.

- A function run in another thread or task while the entry runs is listed
  among the reads no stage explains, with what it read, but not proposed as
  a stage: the entry doesn't call it directly.
- A nested function or a callable object the entry calls (a factory's
  closure, a decorator's wrapper) is passed through: calls it makes count as
  the entry's. The report lists it under "Called by the entry, but not
  proposed".
- A method, an async function or a generator is proposed with that fact as
  a reason: `instrument` wraps only plain module-level functions. What a
  generator yields is never fingerprinted.
- If the entry calls no module-level function of the repository directly,
  the report says so first: no stage was proposed.
- The observer loads in the command's interpreter and in every Python
  process it starts, and each keeps its own events.
- The observer never changes what your program does. It never reads a
  streamed response body, and if it can't record something, the program
  carries on and the report says it may be incomplete.
- A program that inspects its own builtins can tell it is being observed:
  under the observer, `inspect.isbuiltin(open)` is false.
- A file written is recorded by its path's fingerprint, not its content, so
  discovery can't join two stages through a file one writes and another
  reads.

## What this is not

No PR comment, no SARIF annotation, no PyPI or Marketplace publication.
Neither `discover` nor `instrument` decides meaning: a person writes it.

## License

Copyright 2026 Shamik Saha. Licensed under Apache-2.0; see LICENSE.
