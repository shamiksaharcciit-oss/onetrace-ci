# Working on onetrace-ci

For coding agents (and people) changing this repository. For adding onetrace-ci to *your*
project, see [docs/agents/ci.md](docs/agents/ci.md).

## Run the tests

<!-- not executed -->
```sh
pip install --require-hashes -r requirements.lock
pip install --require-hashes -r requirements-test.lock
pip install --no-deps -e .
python -m pytest -q
```

The tests of the framework integrations (LangChain, LlamaIndex, OpenTelemetry) skip unless the
frameworks are installed from their test-only lock. Run them on their own, as the integrations
job does: the rest of the suite stands in for `requests` with a stub, and assumes it isn't
installed, which the frameworks' own dependencies (and LangSmith's pytest plugin) contradict.

<!-- not executed -->
```sh
pip install --require-hashes -r requirements-integrations.lock
ONETRACE_CI_REQUIRE_INTEGRATIONS=1 python -m pytest -q tests/test_discover_integrations.py
```

Every test reports how many items it examined (the `examined` fixture); a check that looks at
nothing is a failure, not a pass. Write the test first. For any change to what the tool
generates, to the gate, or to the plan reader, also show that the test catches the defect it
guards against: break the code on purpose, watch the test fail, restore it.

## The rule that matters most: never guess meaning

The tool writes the boilerplate; people own the meaning. Trust classes, whether a stage can be
re-derived, the approved boundaries, which files a stage reads, who approved a plan: if a person
has not written it, the tool refuses and names the field. It never fills one in, and it never
treats a `DECIDE:` question as answered. A change that makes the tool infer any of these is
wrong, however convenient.

## This repository is public

- Nothing private goes in: no identifiers of work items or decisions, no names of the people
  or systems that coordinate the work, no dates in prose, and no personal names other than the
  copyright line and the `authors` field that are already here.
- Before every push, scan the whole history (every commit, message and diff) for words that
  must not be published and for private identifiers. The scanning tools and their word lists
  live outside this repository and are never committed to it.
- Never rewrite history that has been pushed: no force-push, no rebase of pushed commits.
  Fix forward with a new commit.
- Pull before you push.
- A plan never carries a secret or a key; only the name of the environment variable that
  holds one.

## Where things are

- `src/onetrace_ci/gate.py`: `onetrace-ci gate`, one CI verdict from onetrace's own commands.
- `src/onetrace_ci/yamlsubset.py`, `plan.py`, `instrument_plan.py`: the plan file.
- `src/onetrace_ci/instrument.py`: `onetrace-ci instrument`, a plan into a patch.
- `src/onetrace_ci/init_ci.py`: `onetrace-ci init-ci`, the workflow for code instrumented by hand.
- `src/onetrace_ci/discover.py`, `discover_infer.py`, `_observer.py`: `onetrace-ci discover`. The
  observer is copied into the observed command's interpreter as a file; never import it.
- `src/onetrace_ci/errors.py` and `docs/errors.md`: every refusal's fix.
