"""Regression tests for dogfood findings (ADR-0011 amendment). All data is fictional."""
from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeRouter, FakeStore, settings

from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.mapper import best_option, looks_fabricated, map_fields
from job_apply_mcp.config import PolicyCfg
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.policy.gates import GateContext, evaluate


class P(FakeProfile):
    def __init__(self, **v):
        self.VALUES = {**FakeProfile.VALUES, **v}


def got(fields, **v):
    r = map_fields(fields, P(**v))
    return {a.key: a.value for a in r.answers}, [f.key for f in r.unresolved], r


def test_pronouns_never_cross_to_opposite_option():
    f = F("p", "Pronouns", FieldKind.select, options=["she/her", "they/them"])
    ans, unres, _ = got([f], pronouns="He/him/his")
    assert ans == {} and unres == ["p"]
    f2 = F("p", "Pronouns", FieldKind.select, options=["She/Her", "He/Him/His", "They/Them"])
    assert got([f2], pronouns="He/him")[0] == {"p": "He/Him/His"}
    ans, unres, _ = got([F("p", "Pronouns", FieldKind.select, options=["she/her", "Prefer not to say"])],
                        pronouns="He/him/his")
    assert ans == {} and unres == ["p"]  # #119: no guess, and never a decline for an answered topic


def test_demographics_only_from_profile_demographics():
    f = F("s", "Sexual orientation", FieldKind.select, options=["Straight", "Prefer not to say"])
    assert got([f])[1] == ["s"]  # no profile value -> unresolved, no guess
    f = F("g", "Gender", FieldKind.select, options=["Male", "Female", "Decline"])
    assert got([f], gender="Female")[0] == {"g": "Female"}


def test_relocate_yes_never_picks_negated_option():
    opts = ["I am not able to relocate", "Yes, I am willing to relocate"]
    f = F("r", "Are you willing to relocate?", FieldKind.select, options=opts)
    assert got([f], willing_to_relocate="Yes")[0] == {"r": "Yes, I am willing to relocate"}
    only_neg = F("r", "Are you willing to relocate?", FieldKind.select, options=opts[:1] + ["Maybe"])
    assert got([only_neg], willing_to_relocate="Yes")[1] == ["r"]
    assert best_option("No", opts) == "I am not able to relocate"
    assert best_option("I can't relocate", ["I cannot relocate", "I can relocate"]) == "I cannot relocate"
    assert best_option("I can relocate", ["I cannot relocate", "I can relocate"]) == "I can relocate"


def test_how_did_you_hear_preference_never_referral():
    opts = ["Referral", "Employee Referral", "Job board", "LinkedIn"]
    f = F("h", "How did you hear about us?", FieldKind.select, options=opts)
    assert got([f], referral_source="Job board")[0] == {"h": "Job board"}
    assert got([f])[0] == {"h": "Job board"}  # default preference list
    only = F("h", "How did you hear about us?", FieldKind.select, options=["Referral", "Employee Referral"])
    assert got([only], referral_source="Job board")[1] == ["h"]
    assert got([only], referral_source="Employee referral")[0] == {"h": "Employee Referral"}
    r = map_fields([f], P(), referral_preference=["LinkedIn"])
    assert r.answers[0].value == "LinkedIn"


def test_fabricated_values_rejected():
    for v in ("https://example.com/application", "user@example.org", "555-0100", "https://yourdomain.com",
              "<your url>", "123-456-7890", "yourusername"):
        assert looks_fabricated(v), v
    assert not looks_fabricated("https://acme-labs.dev/ada") and not looks_fabricated("ada@initech.test")
    ans, unres, _ = got([F("u", "Link to your best project")])
    assert ans == {} and unres == ["u"]


def eng(form, router=None, profile=None):
    return ApplyEngine(settings(), resolve_adapter=lambda r: FakeAdapter(form=form), profile=profile or P(),
                       router=router, gates=FakeGates(), store=FakeStore())


async def test_llm_url_placeholder_and_url_questions_never_answered():
    form = ApplicationForm(ref=POSTING.ref, fields=[F("u", "Share a URL showing your work"),
                                                   F("n", "Nickname")])
    r = FakeRouter(short={"u": {"value": "https://example.com/application", "confidence": 0.99},
                          "n": {"value": "https://example.com/x", "confidence": 0.99}})
    out = await eng(form, r).plan(POSTING.ref)
    assert out.plan.answers == [] and set(out.plan.unresolved) == {"u", "n"}
    assert not r.calls or '"u"' not in r.calls[0][1]  # URL question never sent to the model (ADR-0040: no call at all)


def test_geo_fields_derived_or_omitted_never_tasks():
    fields = [F("latitude", "latitude", FieldKind.unknown, required=True),
              F("longitude", "longitude", FieldKind.unknown, required=False)]

    class NoGeo(P):
        def lookup(self, key):
            return None if key in ("lat", "lon") else super().lookup(key)

    r = map_fields(fields, NoGeo())
    assert r.answers == [] and r.unresolved == [] and len(r.omitted) == 2

    class Geo(P):
        def lookup(self, key):
            return {"lat": 32.5, "lon": -110.9}.get(key) or super().lookup(key)

    r = map_fields(fields, Geo())
    assert {a.key: a.value for a in r.answers} == {"latitude": "32.5", "longitude": "-110.9"}


async def test_geo_not_client_task_and_submit_not_blocked():
    form = ApplicationForm(ref=POSTING.ref, fields=[F("latitude", "latitude", FieldKind.unknown, required=True),
                                                   F("first_name", "First Name")])
    e = eng(form, FakeRouter())
    out = await e.plan(POSTING.ref)
    assert out.client_tasks == [] and out.plan.unresolved == []
    res = await e.submit(POSTING.ref, {}, out.approval_id, dry_run=True)
    assert res.status == "dry_run"


def test_phd_graduate_titles_and_exclude_patterns():
    cfg = PolicyCfg()
    for t in ("AI Research Engineer, New PhD Graduate", "Scientist (PhD Student)", "Intern - PhD candidate",
              "Engineer, New PhD"):
        assert evaluate(GateContext(title=t), cfg).decision.value == "skip", t
    assert evaluate(GateContext(title="Software Engineer"), cfg).decision.value == "submit"
    assert evaluate(GateContext(title="Engineer, New PhD Graduate"), PolicyCfg(has_phd=True)).decision.value == "submit"
    cfg = PolicyCfg(exclude_title_patterns=[r"\bresearch\b"])
    assert evaluate(GateContext(title="Applied Research Engineer"), cfg).decision.value == "skip"
    assert evaluate(GateContext(title="Platform Engineer"), cfg).decision.value == "submit"


def test_graduation_derived_from_profile_not_llm():
    opts = ["Spring/Summer 2027", "Fall 2026", "Spring 2026", "Summer 2026"]
    f = F("g", "Expected graduation term", FieldKind.select, options=opts)
    assert got([f], graduation="2026-05")[0] == {"g": "Spring 2026"}
    assert got([F("g", "Expected graduation term", FieldKind.select, options=["Spring/Summer 2027"])],
               graduation="2026-05")[1] == ["g"]
    assert got([F("g", "Graduation date", FieldKind.text)], graduation="2026-05")[0] == {"g": "May 2026"}
    assert got([F("g", "Graduation year", FieldKind.text)], graduation="2026-05")[0] == {"g": "2026"}
    assert got([F("g", "Graduation date", FieldKind.date)], graduation="2026-05")[0] == {"g": "2026-05"}


async def test_graduation_never_asked_of_llm():
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("g", "Anticipated graduation term", FieldKind.select, options=["Spring/Summer 2027", "Fall 2027"])])
    r = FakeRouter(short={"g": {"value": "Spring/Summer 2027", "confidence": 0.99}})
    out = await eng(form, r).plan(POSTING.ref)
    assert out.plan.answers == [] and out.plan.unresolved == ["g"]
    assert not r.calls or '"g"' not in r.calls[0][1]


async def test_attestations_and_ai_questions_need_human():
    labels = ["Do you use AI tools in your work?", "I certify that the information above is accurate",
              "I cannot participate in foreign talent programs. By selecting Yes I confirm."]
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F(f"q{i}", lab, FieldKind.select, options=["Yes", "No"]) for i, lab in enumerate(labels)])
    r = FakeRouter(short={f"q{i}": {"value": "Yes", "confidence": 1} for i in range(3)})
    e = eng(form, r)
    out = await e.plan(POSTING.ref)
    assert out.plan.answers == [] and len(out.plan.unresolved) == 3
    assert all(t.get("needs_human") for t in out.client_tasks) and not r.calls
    row = (await e.apply_batch([POSTING.ref], dry_run=True))[0]
    assert row["status"] == "needs_human"


async def test_explicit_profile_answer_allows_attestation():
    class PA(P):
        def lookup(self, key):
            return "No" if "ai tools" in key else super().lookup(key)
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("q", "Do you use AI tools in your work?", FieldKind.select, options=["Yes", "No"])])
    out = await eng(form, FakeRouter(), PA()).plan(POSTING.ref)
    assert {a.key: a.value for a in out.plan.answers} == {"q": "No"}


async def test_ungrounded_experience_claims_rejected():
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("a", "Do you have experience with Kubernetes?", FieldKind.select, options=["Yes", "No"]),
        F("b", "Do you have experience with Python?", FieldKind.select, options=["Yes", "No"]),
        F("c", "Favorite tool", FieldKind.text, max_length=40),
        F("d", "Preferred team size", FieldKind.text, max_length=20)])
    r = FakeRouter(short={"a": {"value": "Yes", "confidence": 0.99}, "b": {"value": "Yes", "confidence": 0.99},
                          "c": {"value": "bazel", "confidence": 0.9}, "d": {"value": "12", "confidence": 0.9}})
    out = await eng(form, r).plan(POSTING.ref)
    ans = {a.key: a.value for a in out.plan.answers}
    # ADR-0040: "experience with X" is not on the short-answer allowlist (the model would state a fact), so even the
    # grounded Python question goes to the owner; only the allowlisted "Favorite tool" is answered
    assert ans == {"c": "bazel"}
    assert set(out.plan.unresolved) == {"a", "b", "d"}
