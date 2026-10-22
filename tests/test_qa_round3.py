"""ADR-0040: voluntary self-identification, narrowed attestation gate, export-control citizenship countries and
public-sector qualifiers for prior_employee. Fictional profile only (Alex Rivera, Springfield, Freedonia)."""
from typing import ClassVar

from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeRouter, FakeStore, settings

from job_apply_mcp.apply.adapters import PolicyGates, ProfileView
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.mapper import (
    about_this_company,
    auth_core,
    best_option,
    eeo_option_ok,
    exact_option,
    location_is_us,
    map_fields,
    never_llm,
    privacy_ack_value,
)
from job_apply_mcp.config import PolicyCfg, Settings
from job_apply_mcp.core.models import ApplicationForm, Decision, FieldKind
from job_apply_mcp.policy.gates import GateContext, stop_phrase_gate, stop_phrase_hit
from job_apply_mcp.profile import Authorization, Contact, Demographics, Preferences, Profile

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
    assert mapped([opt("x", "Gender", ["Man", "Non-binary"])], p)[1] == ["x"]   # "Woman" has no exact option


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


# ---------------------------------------------------------------- review: EEO isolation (ADR-0040)


def full(demo=None, ack=True, answers=None):
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city="Springfield"),
                               demographics=Demographics(**(demo or DEMO | {"race_ethnicity": "Asian"})),
                               authorization=Authorization(work_authorized=True, requires_sponsorship=False, citizen=True),
                               preferences=Preferences(acknowledge_privacy_notices=ack),
                               answers=answers or {}), Settings())


def test_eeo_labels_never_take_another_rules_answer():
    p = full()
    assert mapped([opt("x", "Gender (required for US citizens)", ["Yes", "No"])], p)[1] == ["x"]
    assert mapped([opt("x", "Gender (required for US citizens)", DISABILITY)], p)[0] == {"x": "I do not want to answer"}
    assert mapped([opt("x", "Gender (required for US citizens)", GENDER)], p)[0] == {"x": "Decline to self-identify"}
    assert mapped([opt("x", "Are you a U.S. citizen or protected veteran?", ["Yes", "No"])], p)[1] == ["x"]
    assert mapped([opt("x", "Are you authorized to work in the US? (veterans: see below)", ["Yes", "No"])], p)[1] == ["x"]
    assert mapped([opt("x", "Have you ever worked for Acme Robotics? (EEO data)", ["Yes", "No"])], p)[1] == ["x"]
    # a saved [answers] entry never answers an EEO label
    q = full(answers={"races": "Asian", "privacy notice": "I acknowledge"})
    assert mapped([opt("x", "Races", RACE)], q)[0] == {"x": "Asian"}          # the [demographics] value, plain label
    assert mapped([opt("x", "Races you have worked with", RACE[:-1])], q)[1] == ["x"]


def test_privacy_notice_mentioning_a_category_never_gets_a_category_or_ack():
    p = full()
    for label in ["Privacy notice - races", "Privacy notice - racial data", "Privacy policy (ethnicities)",
                  "Applicant privacy notice: genders", "Privacy notice for veterans", "Privacy notice - disabilities"]:
        ans, unres, _, _ = mapped([opt("x", label, RACE[:-1] + ["I acknowledge"])], p)
        assert "Asian" not in ans.values() and "I acknowledge" not in ans.values(), label
        assert unres == ["x"], label
    # a plain privacy notice whose options name a category is refused too
    assert privacy_ack_value(opt("x", "Applicant Privacy Notice", ["I acknowledge", "Asian"])) is None
    assert privacy_ack_value(opt("x", "Applicant Privacy Notice", ["I acknowledge", "Woman"])) is None


def test_eeo_word_forms_are_classified():
    p = full()
    for label in ["Races", "Racial identity", "Ethnicities", "Genders", "Veterans", "Disabilities", "Ethnic background"]:
        ans, _, skipped, _ = mapped([opt("x", label, ["Yes", "No"])], p)
        assert ans == {} and skipped == ["x"], label


def test_option_domain():
    assert not eeo_option_ok("Yes, I have a disability", "gender", DISABILITY + GENDER)
    assert not eeo_option_ok("Asian", "gender", GENDER + ["Asian"])
    assert not eeo_option_ok("Woman", "race", RACE + ["Woman"])
    assert not eeo_option_ok("Yes", "race", RACE + ["Yes"])
    assert eeo_option_ok("Hispanic or Latino", "race", RACE + ["Hispanic or Latino"])
    assert eeo_option_ok("No", "hispanic", YND)
    assert eeo_option_ok("Decline to self-identify", "gender", GENDER)


# ---------------------------------------------------------------- review: required markers never exempt
def test_required_markers_never_exempt_an_attestation():
    for label in ["This is my own work Required", "This is my own work - required", "My own work, required",
                  "No AI - required", "No AI Required", "No AI/ML required", "Without the use of AI - required",
                  "AI policy - required", "AI Policy (required)", "AI Policy, required",
                  "I certify this is my own work, required", "I certify this is my own work *",
                  "This application must be personally completed - needed"]:
        v = gate(form_text=label)
        assert v is not None and v[0] == Decision.needs_human, label
    assert gate(text="No AI - required.") is not None                 # no requirement noun: stops in posting text too
    assert gate(form_text="No AI experience required") is not None    # labels never get the requirement exemption
    assert stop_phrase_hit("No A.I. was used", ["A.I."]) == "A.I."     # abbreviations are not sentence ends
    assert stop_phrase_hit("Are you a U.S. person?", ["U.S. person"]) == "U.S. person"


# ---------------------------------------------------------------- #57 / live QA: US authorization and sponsorship
SPONS_EG = "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?"
SPONS_ETC = "Will you now or in the future require employment visa sponsorship (e.g., H-1B, TN, etc.)?"
SPONS_ITAR = ("Will you now or in the future require employment visa sponsorship (e.g., H-1B, H-4, TN, OPT, CPT etc.)?"
              "\n\n**If you are currently on OPT, CPT, H-4 Visa you do NOT qualify as a U.S Person status as defined by "
              "ITAR**")
AUTH_JOB_COUNTRY = "Are you authorized to work in the country where the job is located?"
SPONS_THIS_COUNTRY = "Will you now or in the future require sponsorship for employment visa status in this country?"


def auth(label, job_in_us=False, options=("Yes", "No")):
    r = map_fields([opt("q", label, list(options), required=True)], full(), company="Acme Robotics", job_in_us=job_in_us)
    return {a.key: a.value for a in r.answers}.get("q")


def test_real_us_sponsorship_labels_are_answered():
    for label in [SPONS_EG, SPONS_ETC, SPONS_ITAR, "Will you now or in the future require sponsorship (i.e. OPT, CPT)?",
                  "Do you require visa sponsorship such as H-1B or TN?",
                  "Will you require immigration sponsorship for work authorization?"]:
        assert auth(label) == "No", label
    assert auth("Are you authorized to work in the United States?") == "Yes"


def test_examples_and_notes_that_could_be_another_place_stay_unresolved():
    for label in ["Will you require visa sponsorship (e.g., UK Skilled Worker visa)?",
                  "Will you require visa sponsorship (e.g. to work in London)?",
                  "Will you require visa sponsorship? Note: this role is based in Toronto.",
                  "Will you require visa sponsorship? This applies to our Berlin office",
                  "Will you now or in the future require sponsorship for employment visa status to work in this location?",
                  AUTH_JOB_COUNTRY, SPONS_THIS_COUNTRY]:
        assert auth(label) is None, label
    assert auth_core("Are you authorized to work in Canada (e.g., H-1B)?") is None


def test_job_country_phrases_resolve_only_for_a_us_only_posting():
    assert auth(AUTH_JOB_COUNTRY, job_in_us=True) == "Yes"
    assert auth(SPONS_THIS_COUNTRY, job_in_us=True) == "No"
    assert location_is_us("New York, NY (HQ)") and location_is_us("Springfield, IL or Remote - US")
    for loc in ["London, UK", "Remote", "", "New York, NY; Toronto, ON", "Springfield, IL / Berlin, Germany"]:
        assert not location_is_us(loc), loc


# ---------------------------------------------------------------- #58 residence: deterministic only
CONTINENTAL = "Do you currently reside within the continental United States?"


def resident(region, country="United States", label=CONTINENTAL):
    p = ProfileView(Profile(contact=Contact(first_name="Alex", city="Springfield", region=region, country=country)),
                    Settings())
    r = map_fields([opt("q", label, ["Yes", "No"], required=True)], p)
    return {a.key: a.value for a in r.answers}.get("q")


def test_continental_us_residence_from_profile_location():
    assert resident("IL") == "Yes"
    assert resident("Hawaii") == "No"
    assert resident("") is None                                 # continental needs a configured state
    assert resident("ON", country="Canada") == "No"
    assert resident("", label="Do you currently reside in the United States?") == "Yes"
    assert resident("IL", label="Do you currently reside within the continental United States or Canada?") is None
    assert resident("IL", label="Will you reside in the United States after graduation?") is None


NEVER_LLM_LABELS = ["EXPORT COMPLIANCE", "Are you a U.S. person under ITAR/EAR?", "Country of citizenship and date",
                    "Do you hold a security clearance?", "Do you currently reside within the continental United States?",
                    "Are you willing to commute to our office?", "Which time zone do you work in?",
                    "Are you open to a hybrid schedule?", "Where are you located?", "Are you open to relocation?"]


async def test_legal_and_place_questions_never_reach_the_llm():
    fields = [F(f"k{i}", lab, FieldKind.text, max_length=80) for i, lab in enumerate(NEVER_LLM_LABELS)]
    fields.append(F("export_opt", "Status", FieldKind.select, options=['I am currently a "U.S. Person"', "I am not"]))
    fields.append(F("tool", "Favorite build tool", required=False))
    router = FakeRouter(short={f.key: "No" for f in fields})
    e = ApplyEngine(settings(), resolve_adapter=lambda ref: FakeAdapter(form=ApplicationForm(ref=POSTING.ref, fields=fields)),
                    profile=FakeProfile(), router=router, gates=FakeGates(), store=FakeStore(),
                    driver_factory=lambda n: None)
    out = await e.plan(POSTING.ref)
    prompts = " ".join(p for _, p in router.calls)
    for f in fields[:-1]:
        assert never_llm(f), f.label
        assert f.label not in prompts and f.key in out.plan.unresolved, f.label
    assert not any(a.source.startswith("llm") and a.key != "tool" for a in out.plan.answers)


# ---------------------------------------------------------------- #56 privacy-notice acknowledgement
def test_real_privacy_notice_acknowledgements_are_applied():
    p = full(ack=True)
    cases = [("Privacy Notice", ["I have read and acknowledge the privacy notice below"]),
             ("Applicant Privacy Notice", ["I acknowledge receipt of the Applicant Privacy Notice."]),
             ("Acme Robotics Applicant Privacy Notice *",
              ["I acknowledge receipt of the Acme Robotics Applicant Privacy Notice. (required)"])]
    for label, options in cases:
        r = map_fields([opt("q", label, options, required=True)], p, company="Acme Robotics")
        assert [(a.value, a.source) for a in r.answers] == [(options[0], "rule:privacy_ack")], label
    r = map_fields([opt("q", "Privacy Notice", ["I have read and acknowledge the privacy notice below"])], full(ack=False))
    assert r.answers == []                                      # opt-in still required


def test_privacy_notice_adversarial_still_refused():
    p = full(ack=True)
    for label, options in [("Privacy notice - races", ["I acknowledge"]),
                           ("Privacy notice for California residents", ["I acknowledge"]),
                           ("Privacy Notice", ["I acknowledge receipt and agree to receive marketing texts"]),
                           ("Privacy Notice", ["I acknowledge the notice below and certify my answers are true"]),
                           ("Privacy Notice and visa sponsorship", ["I acknowledge"]),
                           ("Other Company Applicant Privacy Notice", ["I acknowledge receipt"])]:
        r = map_fields([opt("q", label, options, required=True)], p, company="Acme Robotics")
        assert r.answers == [], label


# ---------------------------------------------------------------- option matching: no substring snaps


def test_demographic_pairs_never_cross():
    assert best_option("Female", ["Male", "Decline"]) is None
    assert best_option("Woman", ["Man", "Prefer not to say"]) is None
    assert best_option("Man", ["Woman"]) is None
    assert best_option("Asian", ["Asian Indian", "White"]) is None
    assert best_option("Asian Indian", ["Asian", "White"]) is None
    assert exact_option("Female", ["Male", "Female"]) == "Female"
    assert exact_option("Woman", ["Male", "Female"]) == "Female"          # explicit synonym
    assert exact_option("Man", ["Male", "Female"]) == "Male"
    assert exact_option("Female", ["Male"]) is None
    assert exact_option("Asian", ["Asian Indian", "White"]) is None
    p = full({"gender": "Female", "race_ethnicity": "Asian"})
    assert mapped([opt("g", "Gender", ["Male", "Female"])], p)[0] == {"g": "Female"}
    assert mapped([opt("g", "Gender", ["Male"])], p)[1] == ["g"]
    assert mapped([opt("r", "Race", ["Asian Indian", "White"])], p)[1] == ["r"]


def test_containment_is_word_bounded_elsewhere():
    assert best_option("Java", ["JavaScript", "Python"]) is None
    assert best_option("Indeed", ["Indeed.com", "LinkedIn"]) == "Indeed.com"
    assert best_option("Senior Software Engineer", ["Engineer", "Manager"]) == "Engineer"
