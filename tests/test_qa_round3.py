"""ADR-0040: voluntary self-identification, narrowed attestation gate, export-control citizenship countries and
public-sector qualifiers for prior_employee. Fictional profile only (Alex Rivera, Springfield, Freedonia)."""
from typing import ClassVar

from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeRouter, FakeStore, settings

from job_apply_mcp.apply.adapters import PolicyGates, ProfileView
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.mapper import about_this_company, map_fields
from job_apply_mcp.config import PolicyCfg, Settings
from job_apply_mcp.core.models import ApplicationForm, Decision, FieldKind
from job_apply_mcp.policy.gates import GateContext, stop_phrase_gate, stop_phrase_hit
from job_apply_mcp.profile import Authorization, Contact, Demographics, Profile

YND = ["Yes", "No", "Decline to self-identify"]
GENDER = ["Man", "Woman", "Non-binary", "Decline to self-identify"]
RACE = ["Asian", "Black or African American", "White", "Two or more races", "Decline to self-identify"]
VETERAN = ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
           "I don't wish to answer"]
DISABILITY = ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"]


def view(demo=None, citizenship="", answers=None):
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city="Springfield"),
                               demographics=Demographics(**(demo or {})), answers=answers or {},
                               authorization=Authorization(citizenship=citizenship)), Settings())


DEMO = {"gender": "Woman", "hispanic_latino": "No", "race_ethnicity": "Two or more races",
        "veteran_status": "I am not a protected veteran", "disability_status": "Decline to self-identify"}


def opt(key, label, options, required=False, kind=FieldKind.select):
    return F(key, label, kind, required=required, options=options)


def mapped(fields, p):
    r = map_fields(fields, p, company="Acme Robotics")
    return {a.key: a.value for a in r.answers}, [f.key for f in r.unresolved], [f.key for f in r.skipped_voluntary], r


# ---------------------------------------------------------------- #47 voluntary self-identification
def test_profile_demographics_fill_the_voluntary_block():
    fields = [opt("g", "Gender", GENDER), opt("h", "Are you Hispanic/Latino?", YND), opt("r", "Race", RACE),
              opt("v", "Veteran Status", VETERAN), opt("d", "Disability Status", DISABILITY)]
    ans, unres, skipped, _ = mapped(fields, view(DEMO))
    assert ans == {"g": "Woman", "h": "No", "r": "Two or more races", "v": "I am not a protected veteran",
                   "d": "I do not want to answer"}
    assert unres == [] and skipped == []


def test_default_profile_declines_everywhere():
    fields = [opt("g", "Gender", GENDER), opt("h", "Are you Hispanic/Latino?", YND), opt("v", "Veteran Status", VETERAN)]
    ans, _, _, _ = mapped(fields, view())
    assert ans == {"g": "Decline to self-identify", "h": "Decline to self-identify", "v": "I don't wish to answer"}


def test_hispanic_question_uses_its_own_value_not_race():
    p = view({"race_ethnicity": "Hispanic or Latino"})          # hispanic_latino left at the decline default
    assert mapped([opt("h", "Are you Hispanic or Latino?", YND)], p)[0] == {"h": "Decline to self-identify"}
    assert mapped([opt("h", "Are you Hispanic or Latino?", ["Yes", "No"])], p)[1] == ["h"]   # no decline option


def test_gender_if_required_only_for_required_questions():
    p = view({"gender": "Decline to self-identify", "gender_if_required": "Woman"})
    assert mapped([opt("g", "Gender", GENDER)], p)[0] == {"g": "Decline to self-identify"}
    assert mapped([opt("g", "Gender", GENDER, required=True)], p)[0] == {"g": "Woman"}


def test_no_profile_value_is_listed_as_skipped_voluntary():
    class Empty(FakeProfile):
        VALUES: ClassVar[dict] = {}
    fields = [opt("h", "Are you Hispanic/Latino?", ["Yes", "No"]),
              opt("i", "Do you have a physical or mental impairment that limits a major life activity?", ["Yes", "No"]),
              opt("a", "Are you a veteran or active member of the United States Armed Forces?", ["Yes", "No"]),
              opt("req", "Gender", ["Man", "Woman"], required=True)]
    _, unres, skipped, _ = mapped(fields, Empty())
    assert unres == ["h", "i", "a", "req"] and skipped == ["h", "i", "a"]   # required ones stay client tasks


def test_adversarial_labels_never_take_a_real_value():
    p = view(DEMO)
    cases = [opt("x", "Gender of your emergency contact", ["Man", "Woman"]),
             opt("x", "Is your company a veteran-owned business? (I am not a protected veteran)", VETERAN[:2]),
             opt("x", "Race and gender", RACE[:-1]),
             opt("x", "Are you Hispanic/Latino? If yes, which country of origin?", ["Yes", "No"]),
             opt("x", "Are you a veteran or active member of the United States Armed Forces?", ["Yes", "No"])]
    for f in cases:
        ans, unres, skipped, _ = mapped([f], p)
        assert ans == {} and unres == ["x"] and skipped == ["x"], f.label
    # with a decline option the safe answer is to decline, never the profile value
    assert mapped([opt("x", "Race and gender", RACE)], p)[0] == {"x": "Decline to self-identify"}
    assert mapped([opt("x", "Gender", ["Man", "Female"])], p)[1] == ["x"]       # "Woman" has no exact option


async def test_engine_lists_skipped_voluntary_and_never_sends_them_to_the_llm():
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("first_name", "First Name"), opt("h", "Are you Hispanic/Latino?", ["Yes", "No"]),
        opt("i", "Do you have a physical or mental impairment?", ["Yes", "No"]),
        F("tool", "Favorite build tool", required=False)])
    router = FakeRouter(short={"tool": "make"})
    e = ApplyEngine(settings(), resolve_adapter=lambda ref: FakeAdapter(form=form), profile=FakeProfile(),
                    router=router, gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    out = await e.plan(POSTING.ref)
    c = out.compact()
    assert c["skipped_voluntary"] == {"h": "Are you Hispanic/Latino?", "i": "Do you have a physical or mental impairment?"}
    assert "h" not in out.plan.unresolved and "i" not in out.plan.unresolved
    assert not any("impairment" in p or "Hispanic" in p for _, p in router.calls)


# ---------------------------------------------------------------- #48 attestation gate
def gate(form_text="", text=""):
    return stop_phrase_gate(GateContext(text=text, form_text=form_text), PolicyCfg())


def test_real_attestations_still_stop():
    for label in ["I confirm I personally completed this application.", "This application is my own work",
                  "I completed this application without the use of AI", "No AI allowed in your answers",
                  "Please read our AI policy for candidates"]:
        v = gate(form_text=f"First Name\n{label}")
        assert v is not None and v[0] == Decision.needs_human and label[:40] in v[1], label
    assert gate(text="Applications must be personally completed.")[0] == Decision.needs_human
    assert gate(text="5 years of experience, no AI.")[0] == Decision.needs_human


def test_unrelated_text_does_not_stop():
    assert gate(form_text="What is your aim? Is there no aim here?\nTechno aid volunteering") is None
    assert gate(form_text="Did you use any tools? | No\nAI tools you like") is None         # never across two fields
    assert gate(text="No AI experience required. No AI/ML background needed.") is None
    assert gate(text="Our piano air conditioning team") is None
    assert stop_phrase_hit("No AI allowed", ["no ai"]) == "no ai"
    assert stop_phrase_hit("Casino airline", ["no ai"]) is None


async def test_policy_gates_scan_options_per_field():
    pg = PolicyGates(Settings())
    bad = ApplicationForm(ref=POSTING.ref, fields=[F("a", "Authorship", FieldKind.select,
                                                    options=["I personally completed this application"])])
    ok = ApplicationForm(ref=POSTING.ref, fields=[F("a", "Used tools?", FieldKind.select, options=["No"]),
                                                   F("b", "AI tools you enjoy")])
    assert (await pg.evaluate(POSTING, bad)).decision == Decision.needs_human
    assert (await pg.evaluate(POSTING, ok)).decision == Decision.submit


def test_engine_stop_hits_are_whole_word():
    e = ApplyEngine(settings(), resolve_adapter=lambda ref: None, profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    form = ApplicationForm(ref=POSTING.ref, fields=[F("a", "Is there no aim?"), F("b", "Casino airline experience")])
    assert e._stop_hits(form) == []
    form2 = ApplicationForm(ref=POSTING.ref, fields=[F("a", "I personally completed this application")])
    assert e._stop_hits(form2) == ["I personally completed this application"]


# ---------------------------------------------------------------- #48 export-control citizenship countries
def test_citizenship_countries_prefilled_from_profile_fact_only():
    p = view(citizenship="Freedonia")
    for label in ["Country of citizenship", "List all countries of citizenship",
                  "Export control: please list your country(s) of citizenship"]:
        assert mapped([F("c", label)], p)[0] == {"c": "Freedonia"}, label
    assert mapped([opt("c", "Country of citizenship", ["Freedonia", "Sylvania"])], p)[0] == {"c": "Freedonia"}
    assert mapped([opt("c", "Country of citizenship", ["Sylvania", "Other"])], p)[1] == ["c"]   # never a near option
    assert mapped([F("c", "Country of citizenship")], view())[1] == ["c"]                      # no fact -> unresolved


def test_citizenship_with_other_parts_is_unresolved_with_a_hint():
    p = view(citizenship="Freedonia")
    label = "List countries of citizenship/permanent residence and when obtained"
    ans, unres, _, r = mapped([F("c", label)], p)
    assert ans == {} and unres == ["c"] and r.hints["c"]["citizenship"] == "Freedonia"


def test_us_person_and_adversarial_citizenship_labels_stay_unresolved():
    p = view(citizenship="Freedonia")
    for label in ["Are you a U.S. person under ITAR?", "Are you a U.S. person as defined by export control laws?",
                  "Country of citizenship of your spouse", "List former countries of citizenship",
                  "Countries of citizenship (US citizens: write USA)", "Country of citizenship and date obtained"]:
        ans, unres, _, _ = mapped([F("c", label)], p)
        assert ans == {} and unres == ["c"], label
    # an explicit [answers] fact is still the owner's own answer
    q = view(citizenship="Freedonia", answers={"person under itar": "No"})
    assert mapped([F("c", "Are you a U.S. person under ITAR?")], q)[0] == {"c": "No"}


# ---------------------------------------------------------------- #51 public-sector qualifier words
def test_public_sector_company_names_never_answer_prior_employee():
    label = "Have you ever worked for {}?"
    for co in ["Springfield County", "City Robotics", "Municipal Data Co", "Police Tech Labs", "Shelby Township Tools",
               "Borough Analytics", "Sheriff Systems", "Springfield Transit Authority", "Springfield School District",
               "Springfield University", "Armed Forces Bank", "USAF Labs"]:
        assert not about_this_company(label.format(co), co), co
    assert about_this_company("Have you ever worked for Acme Robotics?", "Acme Robotics")
