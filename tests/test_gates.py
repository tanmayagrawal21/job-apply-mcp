import pytest

from job_apply_mcp.config import PolicyCfg
from job_apply_mcp.core.models import Decision, Posting
from job_apply_mcp.policy import gates as g
from job_apply_mcp.policy.gates import GateContext, evaluate, evaluate_posting

S, H = Decision.skip, Decision.needs_human


def run(name, text="", cfg=None, **kw):
    return g.GATES[name](GateContext(text=text, **kw), cfg or PolicyCfg())


def test_years():
    cfg = PolicyCfg(max_years_required=3)
    assert run("years", "5+ years of experience with Python", cfg)[0] == S
    assert run("years", "4-6 years experience", cfg)[0] == S
    assert run("years", "Minimum of 4 years", cfg)[0] == S
    assert run("years", "1+ years of experience", cfg) is None
    assert run("years", "no requirement stated", cfg) is None
    assert run("years", "10 years of experience", PolicyCfg()) is None  # disabled when unset


def test_graduation_window_and_class_of():
    cfg = PolicyCfg(graduation="2026-05")
    ok = "Graduating between December 2025 and August 2026"
    assert run("graduation", ok, cfg) is None
    bad = "Open to students graduating between December 2026 and August 2027"
    assert run("graduation", bad, cfg)[0] == S
    assert run("graduation", "Class of 2027 only", cfg)[0] == S
    assert run("graduation", "Class of 2026", cfg) is None
    assert run("graduation", "No date here", cfg) is None
    assert run("graduation", bad, PolicyCfg(graduation="2026-05", graduation_flex_months=8)) is None
    assert run("graduation", bad, PolicyCfg()) is None


def test_sponsorship():
    cfg = PolicyCfg(requires_sponsorship=True)
    for t in ("We are unable to sponsor visas.", "No visa sponsorship available", "will not sponsor"):
        assert run("sponsorship", t, cfg)[0] == S, t
    assert run("sponsorship", "Visa sponsorship available", cfg) is None
    assert run("sponsorship", "unable to sponsor", PolicyCfg(requires_sponsorship=False)) is None
    # flags from the shared helper are honored
    assert run("sponsorship", "", cfg, flags={"no_sponsorship": ["x"]})[0] == S


def test_citizenship_clearance_itar():
    base = PolicyCfg()
    assert run("citizenship", "Must be a U.S. citizen", base)[0] == S
    assert run("citizenship", "Must be a U.S. citizen", PolicyCfg(us_citizen=True)) is None
    assert run("citizenship", "Subject to ITAR export controls", base)[0] == S
    assert run("citizenship", "Subject to ITAR", PolicyCfg(us_person=True)) is None
    assert run("citizenship", "Active TS/SCI clearance", base)[0] == S
    assert run("citizenship", "Active TS/SCI clearance", PolicyCfg(has_clearance=True)) is None
    soft = "Ability to obtain a security clearance"
    assert run("citizenship", soft, base)[0] == S
    assert run("citizenship", soft, PolicyCfg(us_citizen=True)) is None
    assert run("citizenship", "A friendly team", base) is None


def test_salary_parse():
    assert g.parse_salary("$120k - $150k") == (120000, 150000)
    assert g.parse_salary("$100,000-$130,000 USD") == (100000, 130000)
    assert g.parse_salary("$50/hr") == (104000, 104000)
    assert g.parse_salary("$40 - $60 per hour") == (83200, 124800)
    assert g.parse_salary("competitive") is None


def test_salary_gate_with_col_and_hourly():
    cfg = PolicyCfg(salary_floor=100000, col_multipliers={"san francisco": 1.3})
    assert run("salary", cfg=cfg, salary="$80k-$90k", location="Austin, TX")[0] == S
    assert run("salary", cfg=cfg, salary="$110k-$120k", location="Austin, TX") is None
    # SF multiplier lifts the floor to 130k
    assert run("salary", cfg=cfg, salary="$110k-$120k", location="San Francisco, CA")[0] == S
    assert run("salary", cfg=cfg, salary="$30/hr", location="Austin")[0] == S      # 62.4k annual
    assert run("salary", cfg=cfg, salary="$60/hr", location="Austin") is None      # 124.8k
    assert run("salary", cfg=cfg, salary="", location="Austin") is None            # unknown pay passes
    assert run("salary", cfg=PolicyCfg(), salary="$1k") is None


def test_employer_exclude():
    cfg = PolicyCfg(exclude_employer_patterns=["university", r"staffing|recruit", "(bad["])
    assert run("employer", cfg=cfg, company="State University")[0] == S
    assert run("employer", cfg=cfg, company="Acme Staffing Group")[0] == S
    assert run("employer", cfg=cfg, company="Acme Labs") is None
    assert run("employer", cfg=cfg, company="Has (bad[ name")[0] == S   # invalid regex falls back to literal


def test_stop_phrases_need_human():
    cfg = PolicyCfg()
    assert run("stop_phrases", "I certify this is my own work", cfg)[0] == H
    assert run("stop_phrases", "", cfg, form_text="Do you agree to our AI policy?")[0] == H
    assert run("stop_phrases", "Normal posting", cfg) is None
    assert run("stop_phrases", "x", PolicyCfg(stop_phrases=["custom"]), form_text="CUSTOM thing")[0] == H


def test_evaluate_combination_and_config():
    cfg = PolicyCfg(max_years_required=2, exclude_employer_patterns=["university"])
    p = Posting(ref="a:b:1", platform="a", company="Acme", title="Eng", url="u",
                description="5 years of experience. Confirm this is my own work.")
    r = evaluate_posting(p, cfg)
    assert r.decision == S and len(r.reasons) == 2 and any("attestation" in x for x in r.reasons)
    ok = Posting(ref="a:b:2", platform="a", company="Acme", title="Eng", url="u", description="Fun job")
    assert evaluate_posting(ok, cfg).decision == Decision.submit
    only_human = Posting(ref="a:b:3", platform="a", company="Acme", title="Eng", url="u",
                         description="No AI allowed")
    assert evaluate_posting(only_human, cfg).decision == H
    # disabling / reordering via config
    cfg2 = cfg.model_copy(update={"gates": ["stop_phrases"]})
    assert evaluate(GateContext(text="5 years of experience"), cfg2).decision == Decision.submit
    cfg3 = cfg.model_copy(update={"gates": ["stop_phrases", "years"]})
    assert evaluate(GateContext(text="5 years of experience, no ai"), cfg3).reasons[-1].startswith("attestation")
    with pytest.raises(ValueError):
        evaluate(GateContext(), cfg, gates=["nope"])


def test_register_custom_gate():
    g.register_gate("never", lambda c, cfg: (S, "custom"))
    try:
        assert evaluate(GateContext(), PolicyCfg(), gates=["never"]).reasons == ["custom"]
    finally:
        del g.GATES["never"]


def test_local_flags():
    f = g.local_flags("Must be a US citizen with TS/SCI. We cannot sponsor.")
    assert {"citizenship", "clearance"} <= set(f)
