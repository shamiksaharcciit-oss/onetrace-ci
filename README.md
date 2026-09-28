# onetrace-ci

Phase 1 of the onetrace CI instrumenter: a gate for code that is **already
instrumented**. No observer, no inference, no codemod — those are phases 2
and 3. This turns `onetrace`'s existing commands (`onetrace-verify`,
`onetrace diff`, `onetrace localize`, `onetrace reproduce`) into one CI
verdict. **`onetrace` itself is never modified**, and this package depends
on nothing outside the standard library besides `onetrace` (pinned by
hash — see `requirements.lock`). Licensed Apache-2.0 — see `LICENSE`.

## What this does *not* claim

**The gate proves your records are consistent. It does not prove your
output is correct.** `onetrace-verify` and `onetrace diff` check that a run's
receipts are internally consistent and that today's output matches a
declared baseline byte-for-byte; neither one, nor this gate, judges whether
that output is *right*. A `pass` means "this run's own record checks out
and matches what you previously approved," never "the answer is correct."

## Install

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

Read with a deliberately restricted, stdlib-only YAML-subset parser (see
`src/onetrace_ci/plan.py` for exactly what it supports and refuses) — a real
YAML library would be a dependency this package's own scope forbids. A
construct outside that subset (a block scalar, an anchor, a nested mapping,
a flow-style list) is refused outright, naming the line, never misread as
something else. Five keys are read:

```yaml
format: onetrace-ci-plan/0.1
approved_boundaries:
  - retrieve
known_limits:
  - embed
reproduce: false
approved_by: alice
```

Any other top-level key is ignored for gating purposes, but named in the
summary. A missing or empty `approved_by` is a **fail**: a gate against an
unapproved plan proves nothing.

## `onetrace-ci baseline propose`

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
job summary, uploads the run and report directories as an artifact. Needs
only `contents: read` — no PR comment and no SARIF in phase 1, since both
need write permissions a fork PR does not have.

## What this is not

No observer (nothing watches your pipeline or infers instrumentation for
you — you call `onetrace`'s own SDK yourself, in your own code, same as
today). No codemod. No PR comment, no SARIF annotation, no PyPI or
Marketplace publication in this phase.

## License

Apache-2.0. See `LICENSE`.
