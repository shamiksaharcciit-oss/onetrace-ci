"""The plan's `sign`, `anchor` and `trust` blocks. Absent, nothing changes. Present, every field
a person decides is required, and the plan never carries a key."""
from __future__ import annotations

import pytest

from onetrace_ci.instrument import Refused, build_patch, main
from onetrace_ci.instrument_plan import PlanRefused, parse_instrument_plan
from tests.instrument_fixtures import PLAN, make_repo

SIGN = "sign:\n  key_env: ONETRACE_SIGNING_KEY\n  when_key_missing: unsigned\n"
ANCHOR = "anchor:\n  source: example-tsa\n  config: .onetrace/anchor.toml\n  when: main\n"
TRUST = "trust:\n  file: .onetrace/trusted_keys.txt\n  untrusted_signature: review\n"


def test_absent_blocks_mean_unsigned_and_unanchored_and_say_so(tmp_path, examined, capsys):
    plan = parse_instrument_plan(PLAN, source="<t>")
    assert (plan.sign, plan.anchor, plan.trust) == (None, None, None)
    repo = make_repo(tmp_path / "repo")
    main(["--style", "wrappers", "--plan", str(repo / "onetrace-plan.yaml"), "--repo", str(repo), "--out", str(tmp_path / "p")])
    lines = [l for l in capsys.readouterr().out.splitlines() if "unsigned" in l]
    examined(len(lines), "lines about signing and anchoring")
    assert lines == ["runs will be unsigned and unanchored: the plan has no sign or anchor block"]


def test_none_is_an_explicit_choice(examined):
    plan = parse_instrument_plan(PLAN + "sign: none\nanchor: none\n", source="<t>")
    examined(2, "explicit none choices")
    assert (plan.sign, plan.anchor) == ("none", "none")


def test_complete_blocks_read_every_field(examined):
    plan = parse_instrument_plan(PLAN + SIGN + ANCHOR + TRUST, source="<t>")
    examined(3, "blocks read")
    assert (plan.sign.key_env, plan.sign.when_key_missing) == ("ONETRACE_SIGNING_KEY", "unsigned")
    assert (plan.anchor.source, plan.anchor.config, plan.anchor.when) == (
        "example-tsa", ".onetrace/anchor.toml", "main")
    assert (plan.trust.file, plan.trust.untrusted_signature) == (".onetrace/trusted_keys.txt", "review")


PEM = "-----BEGIN PRIVATE KEY-----"
REFUSALS = {
    "sign without when_key_missing": (SIGN.replace("  when_key_missing: unsigned\n", "") + TRUST, "sign.when_key_missing"),
    "sign with an unknown when_key_missing": (SIGN.replace("unsigned", "sometimes") + TRUST, "sign.when_key_missing"),
    "sign without key_env": (SIGN.replace("  key_env: ONETRACE_SIGNING_KEY\n", "") + TRUST, "sign.key_env"),
    "key_env holding a PEM header": (SIGN.replace("ONETRACE_SIGNING_KEY", f'"{PEM}"') + TRUST, "sign.key_env"),
    "key_env holding base64 of key length": (SIGN.replace("ONETRACE_SIGNING_KEY", "q83vEjRWeJq83vEjRWeJq83vEjRWeJq83vEjRWeJq8Q") + TRUST, "sign.key_env"),
    "key_env holding a path": (SIGN.replace("ONETRACE_SIGNING_KEY", "keys/ci.pem") + TRUST, "sign.key_env"),
    "sign without a trust block": (SIGN, "trust"),
    "anchor without when": (ANCHOR.replace("  when: main\n", ""), "anchor.when"),
    "anchor with an unknown when": (ANCHOR.replace("when: main", "when: weekly"), "anchor.when"),
    "anchor without a source": (ANCHOR.replace("  source: example-tsa\n", ""), "anchor.source"),
    "trust without untrusted_signature": (TRUST.replace("  untrusted_signature: review\n", ""), "trust.untrusted_signature"),
    "trust with an unknown untrusted_signature": (TRUST.replace("review", "pass"), "trust.untrusted_signature"),
    "trust without a file": (TRUST.replace("  file: .onetrace/trusted_keys.txt\n", ""), "trust.file"),
    "a DECIDE: left in a block": ('sign: "DECIDE: sign runs?"\n', "sign"),
    "an unknown field in a block": (SIGN.replace("  key_env:", "  key_value: x\n  key_env:") + TRUST, "sign.key_value"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_each_block_refusal_names_its_field(case, examined):
    extra, field = REFUSALS[case]
    examined(1, f"refused block: {case}")
    with pytest.raises(PlanRefused) as caught:
        parse_instrument_plan(PLAN + extra, source="<t>")
    assert f"plan field {field}:" in str(caught.value), str(caught.value)


def test_a_trust_file_missing_from_the_repo_is_refused(tmp_path, examined):
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": PLAN + SIGN + TRUST})
    examined(1, "a plan whose trust file does not exist")
    with pytest.raises(Refused, match=r"plan field trust\.file: '\.onetrace/trusted_keys\.txt' does not exist"):
        build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")


def test_signing_or_anchoring_waits_for_the_sdk_interface_it_needs(tmp_path, examined):
    """PROVISIONAL, until the onetrace 0.2.0 candidate is here to be checked against: a plan
    that asks for signing or anchoring is refused plainly, rather than generating code against
    an interface nobody has checked."""
    repo = make_repo(tmp_path / "repo", {"onetrace-plan.yaml": PLAN + SIGN + TRUST,
                                         ".onetrace/trusted_keys.txt": "ci-test-key  ed25519:AAAA\n"})
    examined(1, "a complete signing plan")
    with pytest.raises(Refused, match=r"plan field sign: .*onetrace 0\.2\.0"):
        build_patch(plan_path=repo / "onetrace-plan.yaml", repo=repo, style="wrappers")
