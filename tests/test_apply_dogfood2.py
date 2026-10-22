"""Dogfood round 2 (ADR-0011 Amendment 2). All data is fictional."""
from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeStore, settings

from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.mapper import best_option, map_fields
from job_apply_mcp.apply.salary import compute_ask, format_ask
from job_apply_mcp.config import PolicyCfg
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.profile import Profile

RELO = ["I am local to the Springfield area",
        "I am open to relocating and coming into the office 3 days a week", "I am not able to relocate"]


def _map(f, **v):
    p = FakeProfile()
    p.VALUES = {**FakeProfile.VALUES, **v}
    r = map_fields([f], p)
    return {a.key: a.value for a in r.answers}, [x.key for x in r.unresolved]


def test_relocation_yes_picks_open_option_no_picks_negative():
    f = F("r", "Are you able to relocate?", FieldKind.select, options=RELO)
    assert _map(f, willing_to_relocate="Yes")[0] == {"r": RELO[1]}
    assert _map(f, willing_to_relocate="No")[0] == {"r": RELO[2]}
    assert best_option("Yes", ["Sure thing", "Yes, I am willing to relocate", "No"]) == "Yes, I am willing to relocate"
    assert best_option("Yes", ["I am local", "I am not able to relocate"]) is None  # neutral/negative only
    assert best_option("Yes", ["I am willing to relocate", "I am happy to relocate"]) is None  # tie -> unresolved


def test_in_person_matches_in_office_pattern():
    p = Profile(answers={"in office": "Yes"})
    q = "Are you willing and excited for a 5 day in person office culture?"
    assert p.answer_for(q) == "Yes"
    assert p.answer_for("Can you work on-site five days?") == "Yes"
    assert p.answer_for("Do you like cats?") is None
    custom = Profile(answers={"hybrid": "Yes"}, answer_synonyms=[["hybrid", "flex schedule"]])
    assert custom.answer_for("Is a flex schedule OK?") == "Yes"


def _post(salary, loc="Springfield"):
    return POSTING.model_copy(update={"salary": salary, "location": loc})


def test_salary_range_midpoint_floor_cap_and_col():
    cfg = PolicyCfg(base_salary=100_000, col_multipliers={"metropolis": 1.2})
    d = compute_ask(cfg, _post("$100,000 - $120,000"))
    assert d.ask == 110_000 and d.audit["anchor_kind"] == "midpoint"
    d = compute_ask(cfg, _post("$100,000 - $120,000", "Metropolis, XX"))
    assert d.ask == 120_000  # floor 120k, capped at posted top
    d = compute_ask(PolicyCfg(base_salary=200_000), _post("$100,000 - $120,000"))
    assert d.ask == 120_000  # never above the top
    d = compute_ask(cfg, _post("$80,000 - $160,000"))  # spans levels -> lower-middle (80k + 0.33*80k)
    assert d.ask == 106_000 and d.audit["anchor_kind"] == "lower_middle"


def test_salary_no_range_and_formats():
    cfg = PolicyCfg(base_salary=100_000, default_ask=115_000, col_multipliers={"metropolis": 1.3})
    assert compute_ask(cfg, _post(None)).ask == 115_000
    assert compute_ask(cfg, _post(None, "Metropolis")).ask == 130_000
    assert compute_ask(PolicyCfg(), _post(None)) is None
    ask = 115_000
    assert format_ask(F("s", "Salary requirements"), ask) == "$115,000"
    assert format_ask(F("s", "Expected salary", FieldKind.number), ask) == "115000"


async def test_engine_answers_salary_deterministically_with_audit():
    s = settings()
    s.policy.base_salary, s.policy.default_ask = 100_000, 110_000
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("a", "Desired Total Compensation?"), F("b", "Salary requirements"), F("c", "Expected salary", FieldKind.number),
        F("d", "What is your current salary?")])
    adapter = FakeAdapter(form=form, posting=_post("$100,000 - $130,000"))
    out = await ApplyEngine(s, resolve_adapter=lambda r: adapter, profile=FakeProfile(), router=None,
                            gates=FakeGates(), store=FakeStore()).plan(POSTING.ref)
    ans = {a.key: a.value for a in out.plan.answers}
    assert ans == {"a": "$115,000", "b": "$115,000", "c": "115000"}
    assert "d" in out.plan.unresolved  # current salary is never auto-answered
    assert out.plan.audit["salary"]["rule"] == "range" and out.compact()["audit"]["salary"]["ask"] == 115_000
