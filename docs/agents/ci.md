# onetrace-ci for coding agents

How an agent adds a CI gate to a project that records its runs with onetrace, and what it does
with each result. The gate proves a run's records are consistent and match an approved
baseline; it does not prove the output is correct.

## Which command

- **The code is already instrumented** (by hand, or with onetrace's decorators): run
  `onetrace-ci init-ci`. It writes the workflow and a minimal plan, and nothing else.
- **The code is not instrumented yet, and a person has written a plan** naming the stages:
  run `onetrace-ci instrument`. It writes a patch for review, never an edit in place. Its
  default style writes onetrace 0.2.0's decorators; with a released onetrace 0.1.x, pass
  `--style wrappers`.
- **Nobody knows the stages yet**: run `onetrace-ci discover` with the project's own tests. It
  drafts the plan, with every meaning field a `DECIDE:` question. Show the person the report
  and the questions; never answer them yourself.

Never write a plan's meaning fields yourself. Who approves the baseline, trust classes and
re-derivability are a person's answers. Leave a `DECIDE:` question for them, and say so.

## Set up the gate

<!-- not executed -->
```sh
onetrace-ci init-ci --install "pip install --require-hashes -r requirements.lock" \
  --run "python -m mypipeline.demo" --run-dir "runs/{run_id}" --baseline runs/baseline
```

Then ask a person to answer `approved_by` in `onetrace-plan.yaml`: the gate fails every run
until they do. Run the pipeline once, and propose that run as the baseline:

<!-- not executed -->
```sh
onetrace-ci baseline propose --from runs/<run id> --out runs/baseline
git add runs/baseline && git commit
```

## Read the result

The gate's first line says what happened. Its exit code is the verdict:

- **`pass`** (exit 0): the run's records check out and match the baseline. Nothing to do.
- **`review`** (exit 2): something changed, and a person decides whether it was meant to.
  For example `review: first difference at stage "split" (chunk_size 512 -> 256)`.
  - In the pull request, say which stage changed and why, from the change you made: "this PR
    changes the chunk size, so `split` and everything after it differ as intended."
  - If the change was not intended, fix the code, not the baseline.
  - **Never regenerate the baseline to make the gate pass** unless a person asked for a new
    baseline. A new baseline is a person's approval of the new output.
- **`fail`** (exit 1): something is wrong outright: the record does not verify, a boundary is
  not approved, or the plan has an open question. Fix what the first line names.

Every refusal ends with a `fix:` line and a link to its section of
[docs/errors.md](../errors.md). Follow the fix; don't work around the refusal.
