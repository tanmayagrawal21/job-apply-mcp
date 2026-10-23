"""ADR-0033: second round of generic mapper coverage (legal name, location, citizenship, authorization options,
GPA, prior employment, talent-community opt-ins, strongest language, prose routing). Fictional profile only."""
import pytest
from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeRouter, FakeStore, settings

from job_apply_mcp.apply.adapters import ProfileView, same_company
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.mapper import (
    canonical_key,
    gpa_number,
    map_fields,
    pick_auth_option,
    pick_citizenship_option,
    pick_gpa_option,
)
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.profile import Authorization, Contact, Education, Profile, Work

YN = ["Yes", "No"]


def prof(**over) -> ProfileView:
    base = {
        "contact": Contact(first_name="Alex", last_name="Rivera", city="Springfield", region="IL",
                           country="United States"),
        "work": [Work(company="Initech", title="Engineer", start="2019-01", end="2021-12"),
                 Work(company="Hooli Inc", title="Engineer", start="2022-01")],
        "education": [Education(school="Example State University", degree="BS", field="CS", end="2022-05",
                                gpa="3.6"),
                      Education(school="Springfield College", degree="MS", end="2024-05", gpa="3.9")],
        "languages": ["Python", "Go", "Rust"],
        "authorization": Authorization(work_authorized=True, requires_sponsorship=True, citizenship="Canada"),
    }
    base.update(over)
    return ProfileView(Profile(**base), Settings())


def run(f, p=None, company="Acme Robotics"):
    r = map_fields([f], p or prof(), company=company)
    return ({a.key: a.value for a in r.answers}.get(f.key), [x.key for x in r.unresolved])


def sel(label, options=YN, key="q", **kw):
    return F(key, label, FieldKind.select, options=options, **kw)


# ---------------------------------------------------------------- name and location
@pytest.mark.parametrize("label", ["Full Legal Name", "Legal name", "Your full name", "Full name *", "Name"])
def test_full_name_paraphrases(label):
    assert run(F("q", label))[0] == "Alex Rivera"


def test_native_language_name_is_not_the_legal_name():
    assert run(F("q", "Full Legal Name in Native Language (e.g. Cyrillic)"))[0] is None


@pytest.mark.parametrize("label", ["Your Location", "Where are you currently located?", "Current location",
                                   "Where do you currently live?", "Where are you based?"])
def test_location_uses_the_location_field_format(label):
    assert run(F("q", label))[0] == "Springfield, IL"


def test_where_located_in_us_is_still_the_us_question():
    assert canonical_key("Are you located in the United States?") == "located_in_us"


# ---------------------------------------------------------------- citizenship
CIT = ["1) U.S. citizen or national of the United States", "2) U.S. lawful permanent resident (green card holder)",
       "3) Refugee under 8 U.S.C 1157", "4) Asylee under 8 U.S.C 1158",
       "5) Authorized to work in the United States under the DACA Program", "6) Other (please explain)"]


def test_citizenship_with_no_consistent_option_stays_unresolved():
    assert run(sel("Citizenship Status", CIT)) == (None, ["q"])


@pytest.mark.parametrize("label", ["Citizenship Status", "Citizenship", "What is your citizenship status?"])
def test_citizenship_snaps_to_non_citizen_option(label):
    opts = ["U.S. citizen", "Permanent resident", "Non-US citizen", "Prefer not to say"]
    assert run(sel(label, opts))[0] == "Non-US citizen"


def test_citizenship_polarity_never_picks_citizen_or_resident_for_a_non_us_citizen():
    for opts in (["U.S. citizen", "Permanent resident / green card", "Other"],
                 ["US Citizen", "Lawful permanent resident", "Refugee or asylee"],
                 ["I am a U.S. citizen", "I hold a green card"]):
        assert pick_citizenship_option(opts, False, True) is None
    visa = ["U.S. citizen", "Permanent resident", "Visa holder (H-1B, L-1)"]
    assert pick_citizenship_option(visa, False, True) == "Visa holder (H-1B, L-1)"
    assert pick_citizenship_option(visa, False, None) is None          # authorization unknown -> no visa guess
    assert pick_citizenship_option(visa, None, True) is None
    two_visas = ["Non-US citizen on H-1B", "Non-US citizen on F-1"]
    assert pick_citizenship_option(two_visas, False, True) is None     # ambiguous
    assert pick_citizenship_option(["U.S. citizen", "Non-US citizen"], True, True) == "U.S. citizen"


def test_citizenship_text_field_gets_the_country():
    assert run(F("q", "Citizenship status"))[0] == "Canada"


def test_us_citizen_question_derives_from_citizenship_country():
    assert run(sel("Are you a U.S. citizen?"))[0] == "No"
    us = prof(authorization=Authorization(work_authorized=True, citizenship="USA"))
    assert run(sel("Are you a U.S. citizen?"), us)[0] == "Yes"


# ---------------------------------------------------------------- authorization options (Greenhouse wording)
AUTH = ["I am authorized to work in the country due to my nationality",
        "I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa",
        "I am authorized to work in the country based on a valid work permit which needs to be sponsored by the company I work for",
        "I am not authorized to work in the country and need visa support", "Other"]
AUTH_LABEL = "Your authorization to work in the United States. Please choose the option that describes your work authorization.\n"


def test_authorization_needing_sponsorship_picks_the_sponsored_permit_option():
    assert run(sel(AUTH_LABEL, AUTH))[0] == AUTH[2]


def test_authorization_without_sponsorship_picks_the_no_sponsor_option_never_nationality():
    p = prof(authorization=Authorization(work_authorized=True, requires_sponsorship=False, citizenship="Canada"))
    assert run(sel(AUTH_LABEL, AUTH), p)[0] == AUTH[1]


def test_nationality_option_needs_citizen_true():
    assert pick_auth_option(AUTH, True, False, None) == AUTH[1]
    assert pick_auth_option([AUTH[0], AUTH[4]], True, False, None) is None
    assert pick_auth_option([AUTH[0], AUTH[4]], True, False, True) == AUTH[0]


def test_authorization_unknown_or_unauthorized_stays_unresolved():
    assert pick_auth_option(AUTH, None, True, False) is None
    assert pick_auth_option(AUTH, False, True, False) is None


# ---------------------------------------------------------------- GPA
@pytest.mark.parametrize("label", ["GPA (Undergraduate)", "What was your undergraduate GPA?",
                                   "Undergraduate grade point average"])
def test_undergrad_gpa_uses_the_bachelor_row(label):
    assert run(F("q", label))[0] == "3.6"


def test_graduate_gpa_uses_the_master_row_and_missing_level_is_unresolved():
    assert run(F("q", "GPA (Graduate)"))[0] == "3.9"
    assert run(F("q", "GPA (Doctorate)")) == (None, ["q"])
    assert run(F("q", "GPA")) == (None, ["q"])                      # two rows with different gpa: ambiguous
    only = prof(education=[Education(school="Example State University", degree="BS", gpa="3.6")])
    assert run(F("q", "Overall GPA"), only)[0] == "3.6"


def test_gpa_absent_is_never_guessed():
    p = prof(education=[Education(school="Example State University", degree="BS")])
    assert run(F("q", "GPA (Undergraduate)"), p) == (None, ["q"])


def test_gpa_options_exact_or_range_only():
    steps = ["5.00", "3.70", "3.65", "3.60", "3.55"]
    assert run(sel("GPA (Undergraduate)", steps))[0] == "3.60"
    ranges = ["3.7 - 4", "3.5 - 3.69", "3.2 - 3.49", "Below 3.0", "Prefer not to say",
              "Completed undergraduate degree outside North America"]
    assert run(sel("What was your undergraduate GPA (or equivalent)?", ranges))[0] == "3.5 - 3.69"
    assert pick_gpa_option(2.9, ranges) == "Below 3.0"
    assert pick_gpa_option(3.6, ["5.00", "4.95"]) is None             # no such option: unresolved, never rounded


def test_gpa_number_rejects_foreign_scales():
    assert gpa_number("3.6") == 3.6 and gpa_number("3.6/4.0") == 3.6
    assert gpa_number("8.5/10") is None and gpa_number("first class") is None


# ---------------------------------------------------------------- prior employment
EMPLOYED = ["I have not previously been employed at Acme Robotics",
            "I have been employed at Acme Robotics as a full-time employee",
            "I have been employed at Acme Robotics as an intern"]


@pytest.mark.parametrize("label", ["Have you previously been employed at Acme Robotics for any length of time?",
                                   "Have you ever worked for Acme Robotics?",
                                   "Are you a current or former employee of Acme Robotics?",
                                   "Were you previously employed by Acme Robotics?"])
def test_previous_employee_defaults_to_no(label):
    assert run(sel(label))[0] == "No"
    assert canonical_key(label) == "prior_employee"


def test_previous_employee_option_wording():
    assert run(sel("Have you previously been employed at Acme Robotics for any length of time?", EMPLOYED))[0] == EMPLOYED[0]


def test_previous_employee_yes_only_when_the_profile_lists_the_employer():
    assert run(sel("Have you previously been employed at Hooli?"), company="Hooli Technologies")[0] == "Yes"
    assert run(sel("Have you previously been employed at Initech?"), company="Initech LLC")[0] == "Yes"
    assert run(sel("Have you previously been employed at Hooli?"), company="")[1] == ["q"]


def test_same_company_aliases():
    assert same_company("DoorDash USA", "DoorDash") and same_company("Door Dash Inc.", "doordash")
    assert not same_company("Meta", "Metabase")
    assert not same_company("Initech", "Globex")


def test_work_with_does_not_trigger_prior_employee():
    assert canonical_key("Have you worked with Kubernetes?") != "prior_employee"


# ---------------------------------------------------------------- talent community / marketing opt-ins
@pytest.mark.parametrize("label", ["Select 'Yes' to join Acme Robotics's Talent Community and receive newsletters",
                                   "Would you like to join our talent network?",
                                   "Sign up for our newsletter"])
def test_marketing_optins_are_owner_only(label):
    # ADR-0050 (descoped): no automatic No; the owner (or a stored optin:<channel>) decides
    assert run(sel(label))[0] is None
    assert run(F("q", label, FieldKind.boolean))[0] is None


def test_consent_and_privacy_acknowledgements_are_left_alone():
    for label in ("I consent to receive marketing communications and agree to the privacy notice",
                  "Applicant Privacy Acknowledgement", "By submitting I acknowledge the Job Applicant Privacy Notice"):
        assert run(sel(label, ["Acknowledge/Confirm"]))[0] is None, label


def test_hybrid_and_relocation_support_remain_owner_decisions():
    assert run(sel("Are you willing and able to commit to the hybrid policy?"))[0] is None
    assert run(sel("Will you require relocation support?"))[0] is None


# ---------------------------------------------------------------- strongest language
LANGS = ["C", "C++", "Python", "Rust", "C#", "Java", "Golang"]


@pytest.mark.parametrize("label", ["What is your strongest coding language?", "Primary programming language",
                                   "Which programming language are you strongest in?"])
def test_strongest_language_select(label):
    assert run(sel(label, LANGS))[0] == "Python"


def test_strongest_language_text_is_the_top_language():
    assert run(F("q", "What is your strongest coding language?"))[0] == "Python"
    assert run(F("q", "What languages do you speak?")) == (None, ["q"])


# ---------------------------------------------------------------- prose routing
GOOD = ("Acme Robotics builds software that keeps warehouse fleets moving. I built a fleet scheduler in Python "
        "for a regional distributor and maintained it for two years. The scheduler cut idle time across forty robots.")


def engine(fields, router, reviewer="router"):
    form = ApplicationForm(ref=POSTING.ref, fields=fields)
    s = settings()
    s.writing.reviewer = reviewer
    return ApplyEngine(s, resolve_adapter=lambda ref: FakeAdapter(form=form), profile=FakeProfile(), router=router,
                       gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)


@pytest.mark.parametrize("label", ["What exceptional work have you done?", "Why do you want to work at Acme Robotics?",
                                   "Why this role, and why at this moment in your life?",
                                   "Describe a project you are proud of."])
@pytest.mark.parametrize("kind", [FieldKind.textarea, FieldKind.text])
async def test_required_question_labels_go_to_the_writer(label, kind):
    r = FakeRouter(prose=GOOD)
    out = await engine([F("q", label, kind)], r).plan(POSTING.ref)
    assert {a.key: a.source for a in out.plan.answers}["q"] == "llm:narrative"
    assert out.plan.unresolved == [] and out.client_tasks == []
    assert "narrative" in [c for c, _ in r.calls]


@pytest.mark.parametrize("label", ["Full Legal Name in Native Language", "Portfolio link", "How many years of Go?",
                                   "What is your preferred office location?", "Your pronouns", "Expected salary",
                                   "How did you hear about us?"])
async def test_short_factual_text_fields_never_get_llm_prose(label):
    r = FakeRouter(prose=GOOD)
    out = await engine([F("q", label, FieldKind.text)], r).plan(POSTING.ref)
    assert "llm:narrative" not in [a.source for a in out.plan.answers]
    assert "narrative" not in [c for c, _ in r.calls]


async def test_ai_tools_prose_goes_to_the_writer_but_ai_attestations_do_not():
    r = FakeRouter(prose=GOOD)
    q = "What AI tools are you currently using today and how are you using them?"
    out = await engine([F("q", q, FieldKind.textarea)], r).plan(POSTING.ref)
    assert [a.source for a in out.plan.answers] == ["llm:narrative"]
    r2 = FakeRouter(prose=GOOD)
    att = "Do you agree not to use AI in this application? Please explain your approach."
    out2 = await engine([F("q", att, FieldKind.textarea)], r2).plan(POSTING.ref)
    assert out2.plan.answers == [] and out2.client_tasks[0]["needs_human"] is True
    assert "narrative" not in [c for c, _ in r2.calls]


async def test_ai_usage_select_is_not_routed_to_the_writer():
    f = sel("Which statement best describes how you currently use AI tools?", ["Daily", "Rarely"])
    r = FakeRouter(prose=GOOD)
    out = await engine([f], r).plan(POSTING.ref)
    assert out.plan.answers == [] and out.client_tasks[0]["needs_human"] is True


async def test_failed_review_stays_a_client_task():
    bad = FakeRouter(prose=GOOD, review={"pass": False, "violations": ["unsupported claim"], "corrected": ""})
    out = await engine([F("q", "Why do you want to work at Acme Robotics?", FieldKind.text)], bad).plan(POSTING.ref)
    assert out.plan.answers == [] and out.plan.unresolved == ["q"] and out.client_tasks[0]["needs_human"] is True


async def test_client_reviewer_keeps_the_draft_as_a_task():
    out = await engine([F("q", "What exceptional work have you done?")], FakeRouter(prose=GOOD),
                       reviewer="client").plan(POSTING.ref)
    assert out.plan.answers == [] and out.client_tasks[0]["draft"] == GOOD


async def test_optional_prose_question_text_is_not_written():
    out = await engine([F("q", "Why do you want to work at Acme Robotics?", FieldKind.text, required=False)],
                       FakeRouter(prose=GOOD)).plan(POSTING.ref)
    assert out.plan.answers == [] and out.plan.unresolved == []
