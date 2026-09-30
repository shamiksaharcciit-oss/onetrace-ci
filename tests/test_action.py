"""The composite action's own steps, like the generated workflow's, are pinned to commits; and the
commit the generated workflow pins the gate to is one this branch's history holds."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from onetrace_ci.instrument import GATE_ACTION

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "action.yml"


def test_the_gate_the_generated_workflow_pins_is_a_commit_of_this_history(examined):
    """A pin to a commit outside the history that gets pushed would name a commit the remote
    doesn't have, and every workflow `instrument` or `init-ci` writes would fail at the gate."""
    sha = GATE_ACTION.rpartition("@")[2]
    shallow = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-shallow-repository"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    examined(1, "the gate commit the generated workflow pins")
    assert shallow == "false", "this check needs the full history (tests.yml checks out with fetch-depth: 0)"
    ancestor = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", sha, "HEAD"],
                              capture_output=True, timeout=60)
    assert ancestor.returncode == 0, f"the generated workflow pins {sha[:12]}, which is not in this branch's history"


def test_every_action_the_composite_action_uses_is_pinned_to_a_commit(examined):
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    examined(len(uses), "actions the composite action uses")
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a commit"


def test_the_action_uploads_only_the_gate_report_and_for_a_stated_time(examined):
    """The artifact holds only what the gate report holds (stage names, digests, verdicts and
    settings), never the run's stored outputs, which are the pipeline's data; in a public
    repository anyone can read it. It is kept for a stated time."""
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    uploads = [s for s in steps if "upload-artifact" in s.get("uses", "")]
    examined(len(uploads), "upload steps in the composite action")
    for step in uploads:
        paths = [p.strip() for p in str(step["with"]["path"]).splitlines() if p.strip()]
        assert paths == ["${{ inputs.out }}"], paths
        assert 1 <= int(step["with"]["retention-days"]) <= 14


def test_each_gate_step_uploads_its_report_under_its_own_name(examined):
    """Two gate steps in one job (one per entry) upload two reports; an artifact name is unique
    in a workflow run, so each step names its own. The default keeps the one gate's name."""
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    uploads = [s for s in action["runs"]["steps"] if "upload-artifact" in s.get("uses", "")]
    examined(len(uploads), "upload steps in the composite action")
    assert action["inputs"]["report-name"]["default"] == "onetrace-ci-gate-report"
    assert uploads and all(s["with"]["name"] == "${{ inputs.report-name }}" for s in uploads)


def test_the_pinned_gate_takes_every_input_the_generated_workflows_pass(examined):
    """The generated workflow runs the gate action at the commit it pins, not the action in this
    checkout: that commit must declare every input a generated workflow gives it."""
    from onetrace_ci.instrument import render_entries_workflow, render_workflow
    from onetrace_ci.instrument_plan import parse_instrument_plan
    from tests.test_ci_entries import TWO
    sha = GATE_ACTION.rpartition("@")[2]
    shown = subprocess.run(["git", "-C", str(ROOT), "show", f"{sha}:action.yml"], capture_output=True,
                           text=True, timeout=60)
    assert shown.returncode == 0, shown.stderr
    declared = set(yaml.safe_load(shown.stdout)["inputs"])
    passed = set()
    for text in (render_workflow(run_dir="runs/{run_id}", install="i", run="r", baseline="runs/b", plan_rel="p.yaml"),
                 render_entries_workflow(parse_instrument_plan(TWO, source="<t>"), "p.yaml")):
        for step in yaml.safe_load(text)["jobs"]["onetrace"]["steps"]:
            if step.get("uses", "").startswith(GATE_ACTION.partition("@")[0] + "@"):
                passed |= set(step.get("with", {}))
    examined(len(passed), "inputs the generated workflows pass to the gate")
    assert passed <= declared, f"the pinned gate {sha[:12]} does not declare {sorted(passed - declared)}"


def test_the_pinned_gate_reads_every_plan_the_generator_writes_a_workflow_for(tmp_path, examined):
    """The gate action installs onetrace-ci from the commit it is pinned to, so that commit's own
    plan reader reads the plan: every plan shape the generator writes a workflow for must read
    there, with the entries left ungated named and declared fields required."""
    import io
    import json
    import os
    import sys
    import tarfile
    from tests.instrument_fixtures import PLAN
    from tests.test_ci_entries import INGEST, INGEST_UNGATED, ONE_COMMAND, TWO
    from tests.test_instrument_decorators import TWO_ENTRIES
    sha = GATE_ACTION.rpartition("@")[2]
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", sha, "src"], capture_output=True, timeout=60)
    assert archive.returncode == 0, archive.stderr
    tarfile.open(fileobj=io.BytesIO(archive.stdout)).extractall(tmp_path)
    plans = {"one entry": PLAN, "two entries": TWO, "one command": ONE_COMMAND, "one ungated": INGEST_UNGATED,
             "two entries, decorated": TWO_ENTRIES}
    script = (
        "import json, sys\n"
        "import onetrace_ci\n"
        "from onetrace_ci.plan import parse_plan_text\n"
        "out = {'from': onetrace_ci.__file__}\n"
        "for name, text in json.load(sys.stdin).items():\n"
        "    try:\n"
        "        p = parse_plan_text(text, source=name)\n"
        "        out[name] = {'ungated': list(getattr(p, 'ungated', ['<no ungated field>'])),\n"
        "                     'require_declared': p.require_declared, 'ignored': list(p.ignored_keys)}\n"
        "    except Exception as e:\n"
        "        out[name] = {'refused': str(e)[:300]}\n"
        "print(json.dumps(out))\n")
    read = subprocess.run([sys.executable, "-c", script], input=json.dumps(plans), capture_output=True, text=True,
                          timeout=120, env=dict(os.environ, PYTHONPATH=str(tmp_path / "src")))
    assert read.returncode == 0, read.stderr
    got = json.loads(read.stdout)
    examined(len(plans), f"plans read by the pinned gate {sha[:12]}")
    assert got.pop("from").startswith(str(tmp_path)), "the pinned commit's reader was not the one imported"
    for name, result in got.items():
        assert "refused" not in result, f"the pinned gate {sha[:12]} refuses the {name} plan: {result['refused']}"
        assert result["require_declared"] is True and result["ignored"] == [], (name, result)
        assert result["ungated"] == ([INGEST] if name == "one ungated" else []), (name, result)
