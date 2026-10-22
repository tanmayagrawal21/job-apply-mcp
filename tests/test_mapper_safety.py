"""ADR-0038: privacy-ack never answers sensitive questions; prior-employee is this company only; authorization is
US-scoped. Fictional profile only (Alex Rivera, Springfield)."""
import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import about_this_company, map_fields, privacy_ack_value, question_countries
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.profile import Authorization, Contact, Preferences, Profile, Work

YN = ["Yes", "No"]


def prof(ack=True, answers=None, **auth):
    a = {"work_authorized": True, "requires_sponsorship": False, "citizen": True} | auth
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city="Springfield",
                                               country="United States"),
                               work=[Work(company="Initech", title="Engineer", start="2020-01")],
                               authorization=Authorization(**a), answers=answers or {},
                               preferences=Preferences(acknowledge_privacy_notices=ack)), Settings())


def run(f, p=None, company="Acme Robotics"):
    r = map_fields([f], p or prof(), company=company)
    ans = {a.key: (a.value, a.source) for a in r.answers}.get(f.key)
    return ans, [x.key for x in r.unresolved]


def sel(label, options=YN):
    return F("q", label, FieldKind.select, options=options)


# ---------------------------------------------------------------- 1) privacy-ack vs sensitive questions
VETERAN = ["I am not a protected veteran", "Yes, I am a protected veteran", "I don't wish to answer"]
HISPANIC = ["Yes", "No", "Decline to self-identify"]


def test_sponsorship_question_mentioning_privacy_notice_is_a_sponsorship_question():
    label = ("Will you now or in the future require visa sponsorship? "
             "(See our Applicant Privacy Notice for how we use this data)")
    assert run(sel(label)) == (None, ["q"])          # extra wording -> never the ack path, never a guessed answer
    assert privacy_ack_value(sel(label)) is None
    assert run(sel("Will you now or in the future require visa sponsorship?"))[0] == ("No", "rule:requires_sponsorship")
    # unknown sponsorship fact: unresolved, never "Yes" from the ack path
    assert run(sel(label), prof(requires_sponsorship=None)) == (None, ["q"])


def test_veteran_question_mentioning_privacy_never_picks_a_veteran_option():
    label = "Protected veteran status (this information is covered by our Applicant Privacy Notice)"
    assert run(sel(label, VETERAN))[0] == ("I don't wish to answer", "rule:veteran")   # profile default: decline
    assert run(sel(label, VETERAN[:2]), prof(ack=True)) == (None, ["q"])
    assert privacy_ack_value(sel(label, VETERAN)) is None


def test_hispanic_question_mentioning_privacy_is_not_acknowledged():
    label = "Are you Hispanic or Latino? Please review our Privacy Notice before answering."
    assert run(sel(label, HISPANIC))[0] == ("Decline to self-identify", "rule:hispanic")
    assert run(sel(label, YN)) == (None, ["q"])
    ans, _ = run(F("q", label, FieldKind.boolean))
    assert ans is None or (ans[0] is not True and ans[1] == "rule:hispanic")


@pytest.mark.parametrize("label,options", [
    ("Do you have a disability? (Data handled per our Applicant Privacy Notice)",
     ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"]),
    ("Gender - see the Candidate Privacy Notice", ["Male", "Female", "Yes", "Decline to self-identify"]),
    ("Are you legally authorized to work in the United States? See our privacy policy", YN),
    ("I acknowledge the privacy notice. Are you authorized to work in the US?", YN),
])
def test_other_sensitive_labels_mentioning_privacy_never_take_the_ack_path(label, options):
    ans, _ = run(sel(label, options))
    assert ans is None or not ans[1].startswith("rule:privacy_ack")
    assert privacy_ack_value(sel(label, options)) is None
    assert privacy_ack_value(F("q", label, FieldKind.boolean)) is None


def test_ack_option_must_itself_be_an_acknowledgement():
    assert run(sel("Applicant Privacy Notice", ["Yes, I am a protected veteran", "No"])) == (None, ["q"])
    assert run(sel("Applicant Privacy Notice", ["Yes, I require sponsorship", "No"])) == (None, ["q"])
    assert run(sel("Applicant Privacy Notice", ["Yes, I have read it", "No"]))[0] == ("Yes, I have read it",
                                                                                        "rule:privacy_ack")
    assert run(sel("Applicant Privacy Notice", YN))[0] == ("Yes", "rule:privacy_ack")


# ---------------------------------------------------------------- 2) prior employee = this company only
@pytest.mark.parametrize("label", [
    "Have you ever worked for a U.S. federal government agency?",
    "Are you a current or former employee of the U.S. government?",
    "Have you previously worked for a competitor of Acme Robotics?",
    "Have you ever worked for another company that required a non-compete agreement?",
    "Have you previously been employed by any of our clients?",
])
def test_other_employer_questions_are_never_defaulted(label):
    assert not about_this_company(label, "Acme Robotics")
    assert run(sel(label)) == (None, ["q"])


def test_other_employer_question_uses_only_an_explicit_answer():
    p = prof(answers={"federal government agency": "No"})
    assert run(sel("Have you ever worked for a U.S. federal government agency?"), p)[0][0] == "No"


@pytest.mark.parametrize("label", ["Have you ever worked for Acme Robotics?", "Have you ever worked for Acme?",
                                   "Are you a current or former employee of this company?",
                                   "Have you previously worked for us?"])
def test_this_company_questions_still_answered(label):
    assert about_this_company(label, "Acme Robotics")
    assert run(sel(label))[0] == ("No", "rule:prior_employee")
    assert run(sel(label.replace("Acme Robotics", "Initech").replace("Acme", "Initech")),
               company="Initech LLC")[0] == ("Yes", "rule:prior_employee")


def test_a_different_named_employer_is_not_this_company():
    assert run(sel("Have you ever worked for Globex Corporation?")) == (None, ["q"])


# ---------------------------------------------------------------- 3) authorization is US-scoped
@pytest.mark.parametrize("label", ["Do you have the right to work in the UK?",
                                   "Are you legally authorized to work in Canada?",
                                   "Are you eligible to work in the European Union?",
                                   "Will you require visa sponsorship to work in the United Kingdom?",
                                   "Are you authorized to work in the US or Canada?"])
def test_non_us_authorization_is_unresolved_without_a_country_fact(label):
    assert run(sel(label)) == (None, ["q"])


def test_non_us_authorization_uses_a_country_specific_answer_only():
    p = prof(answers={"right to work in the uk": "No", "authorized to work": "Yes"})
    assert run(sel("Do you have the right to work in the UK?"), p)[0][0] == "No"
    assert run(sel("Are you legally authorized to work in Canada?"), p) == (None, ["q"])


@pytest.mark.parametrize("label,want", [("Are you legally authorized to work in the United States?", "Yes"),
                                        ("Are you authorized to work in the U.S.?", "Yes"),
                                        ("Are you legally authorized to work?", "Yes"),
                                        ("Will you require sponsorship for a US work visa?", "No")])
def test_us_or_unspecified_authorization_still_answered(label, want):
    assert run(sel(label))[0][0] == want


def test_question_countries():
    assert question_countries("Right to work in the U.K.?") == (False, ["united kingdom"])
    assert question_countries("Authorized to work in the USA?") == (True, [])
    assert question_countries("Are you based in Indiana?") == (False, [])


# ---------------------------------------------------------------- control-agent leaks (deny-by-default, ADR-0038)
ACK_OPTS = [YN, ["I acknowledge", "I do not acknowledge"]]
ACK_LEAKS = [
    "Privacy notice / H-1B status", "Privacy notice - OPT / CPT / STEM extension",
    "Privacy notice: will you need TN status",
    "Privacy notice - Are you legally able to work in the US without restriction?",
    "Privacy notice - Can you work in the US for any employer?",
    "Applicant privacy notice: are you a US person under export control (ITAR)?",
    "Privacy notice: VEVRAA", "Data privacy notice: Have you served in the armed forces?",
    "Privacy Notice - Do you identify as Black or African American?", "Privacy Notice - Are you Asian?",
    "Privacy notice - two or more races", "Privacy notice: Are you Spanish origin?",
    "I have read the privacy notice and I identify as a woman", "Privacy Notice - LGBTQ+",
    "Privacy notice - national origin", "Privacy notice: religion", "Privacy notice: marital status",
    "Privacy notice: date of birth", "Privacy notice: security clearance",
    # adversarial paraphrases
    "Privacy Notice (Pacific Islander)", "Applicant Privacy Notice - native language", "Privacy policy: pregnant?",
    "I acknowledge the privacy policy. Do you hold an active clearance?", "Privacy notice — union membership",
    "Data processing notice: Are you over 40?", "Privacy notice: I identify as non-binary",
    "Privacy Notice: Yes, I am a person with a disability", "Candidate privacy notice: F-1 student",
    "Privacy notice: Are you a protected individual?", "Privacy notice: country of birth",
]


@pytest.mark.parametrize("label", ACK_LEAKS)
@pytest.mark.parametrize("options", ACK_OPTS)
def test_privacy_ack_leaks_are_closed(label, options):
    ans, _ = run(sel(label, options))
    assert ans is None or ans[1] != "rule:privacy_ack", ans
    assert privacy_ack_value(sel(label, options)) is None
    assert privacy_ack_value(F("q", label, FieldKind.boolean)) is None


@pytest.mark.parametrize("label", ["Applicant Privacy Acknowledgement", "Privacy Notice",
                                   "I acknowledge that I have read and understand the Applicant Privacy Notice",
                                   "Please confirm you have reviewed our Candidate Data Processing Notice"])
def test_plain_privacy_notices_still_acknowledged(label):
    assert run(sel(label, ["I acknowledge", "I do not acknowledge"]))[0] == ("I acknowledge", "rule:privacy_ack")


def test_ack_option_must_be_plain_acknowledgement_wording():
    assert run(sel("Privacy Notice", ["Yes, I acknowledge and I am Asian", "No"])) == (None, ["q"])
    assert run(sel("Privacy Notice", ["I acknowledge (US person)", "No"])) == (None, ["q"])


@pytest.mark.parametrize("label", [
    "Have you ever worked for Acme's parent company?", "Have you ever worked for a subsidiary of Acme Robotics?",
    "Have you ever worked for Acme Robotics' auditor (Deloitte)?", "Have you ever worked for an affiliate of ours?",
    "Are you a former employee of Acme Robotics' joint venture partner Wayne Enterprises?",
    "Have you ever worked for a company here in Springfield?",
    "Have you ever worked for Acme Robotics or any of its subsidiaries?",
    "Have you previously worked for Acme Robotics as a contractor or consultant?",
    "Have you ever worked for Acme Robotics or a staffing agency placed there?",
    "Were you previously employed by Acme Robotics' predecessor?",
    "Have you ever worked for a company acquired by Acme Robotics?",
    "Have you previously worked for the Acme Robotics Foundation?",
])
def test_prior_employee_other_relationships_unresolved(label):
    assert not about_this_company(label, "Acme Robotics")
    assert run(sel(label)) == (None, ["q"])


def test_prior_employee_configured_alias():
    assert about_this_company("Have you ever worked for Roadrunner Labs?", "Acme Robotics", ["Roadrunner Labs"])
    assert not about_this_company("Have you ever worked for Roadrunner Labs?", "Acme Robotics")


@pytest.mark.parametrize("label", [
    "Are you legally authorized to work in London?", "Are you authorized to work in Toronto?",
    "Do you have the right to work in Berlin?", "Are you eligible to work in Bangalore?",
    "Are you authorized to work in Dubai?", "Are you legally authorized to work in Saudi Arabia?",
    "Are you authorized to work in Qatar?", "Are you eligible to work in Sri Lanka?",
    "Do you have the right to work in Bulgaria?", "Are you authorized to work in Croatia?",
    "Are you authorized to work in Lithuania?", "Are you authorized to work in Latvia?",
    "Are you authorized to work in Slovakia?", "Are you authorized to work in Morocco?",
    "Are you authorized to work in Ghana?", "Are you authorized to work in Bangladesh?",
    "Are you authorized to work in Uruguay?", "Are you authorized to work in Iceland?",
    "Are you authorized to work in Slovenia?", "Are you authorized to work in Serbia?",
    "Are you authorized to work in Cyprus?", "Are you authorized to work in Malta?",
    "Are you authorized to work in Nepal?", "Are you authorized to work in Ecuador?",
    "Are you authorized to work in Venezuela?", "Are you authorized to work in Guatemala?",
    "Are you authorized to work in Panama?", "Are you authorized to work in Jamaica?",
    "Do you require a visa to work in Ontario?", "Do you require a visa to work in the country where this role is based?",
    "Are you authorized to work in the country where this job is located?", "Are you authorized to work in Quebec?",
    "Are you authorized to work in the EMEA region?", "Are you authorized to work in New South Wales?",
    "Will you require sponsorship to work in our Springfield office?", "Are you authorized to work in Puerto Rico?",
    "Are you authorized to work in the US and the Philippines?", "Are you authorized to work here?",
    "Are you authorized to work locally?", "Are you eligible to work in this jurisdiction?",
])
def test_authorization_any_non_us_place_unresolved(label):
    assert run(sel(label)) == (None, ["q"])
    assert run(sel(label), prof(requires_sponsorship=True)) == (None, ["q"])


@pytest.mark.parametrize("label,want", [("Are you legally authorized to work in America?", "Yes"),
                                        ("Are you authorized to work in the USA for any employer?", "Yes"),
                                        ("Will you now or in the future require H-1B sponsorship?", "No")])
def test_authorization_us_wording_still_answered(label, want):
    assert run(sel(label))[0][0] == want


def test_engine_passes_configured_company_aliases():
    from types import SimpleNamespace

    from job_apply_mcp.apply.engine import ApplyEngine
    s = Settings()
    s.dedupe.company_aliases = {"Roadrunner Labs": "Acme Robotics", "Globex": "Initech"}
    assert ApplyEngine._company_aliases(SimpleNamespace(s=s), "Acme Robotics Inc") == ["Roadrunner Labs"]


# ---------------------------------------------------------------- qualifier words in the company name (ADR-0038)
QUALIFIER_LABELS = {
    "State Street": ["Have you ever worked for a state agency?", "Have you ever worked for the state?",
                     "Have you ever worked for State Street?", "Have you ever been employed by a state government?",
                     "Are you a former employee of any state?", "Have you previously worked for State?"],
    "Agency Inc": ["Have you ever worked for an agency?", "Have you ever worked for Agency Inc?",
                   "Have you ever worked for a federal agency?", "Were you previously employed by a staffing agency?",
                   "Are you a current or former employee of Agency?"],
    "Competitor Labs": ["Have you ever worked for a competitor?", "Have you ever worked for Competitor Labs?",
                        "Have you previously been employed by a competitor of ours?",
                        "Are you a former employee of a competitor?", "Have you ever worked for Competitor?"],
}


@pytest.mark.parametrize("company,label", [(c, x) for c, xs in QUALIFIER_LABELS.items() for x in xs])
def test_company_name_with_qualifier_word_is_never_answered(company, label):
    assert not about_this_company(label, company)
    assert run(sel(label), company=company) == (None, ["q"])


@pytest.mark.parametrize("label", [
    "Have you ever worked for Acme Robotics in any capacity?", "Have you worked with Acme Robotics' partners?",
    "Have you ever worked for Acme Robotics or a related entity?", "Have you ever applied to Acme Robotics?",
    "Have you ever interviewed with Acme Robotics?", "Do you know anyone who has worked for Acme Robotics?",
    "Has a family member ever worked for Acme Robotics?", "Have you ever worked for Acme Robotics in Springfield?",
    "Have you worked for Acme Robotics within the last 12 months?",
])
def test_prior_employee_requires_a_plain_direct_employment_question(label):
    assert not about_this_company(label, "Acme Robotics")


@pytest.mark.parametrize("label", ["Have you ever been employed by Acme Robotics?",
                                   "Have you previously worked at Acme Robotics?",
                                   "Have you ever worked for Acme Robotics before?",
                                   "Were you previously employed at Acme Robotics?",
                                   "Are you a former employee of Acme Robotics?"])
def test_direct_employment_paraphrases_still_answered(label):
    assert about_this_company(label, "Acme Robotics")
    assert run(sel(label))[0] == ("No", "rule:prior_employee")


@pytest.mark.parametrize("company", ["Army Labs", "Military Robotics", "Navy Pier Foods", "Air Force Analytics",
                                     "Marine Systems Co", "Marines Outfitters", "Defense Dynamics", "Defence Works",
                                     "Coast Guard Supply", "National Guard Partners", "Veterans Health Tech",
                                     "Veteran Movers"])
@pytest.mark.parametrize("label", ["Have you ever worked for Army?", "Have you ever worked for Military?",
                                   "Have you ever worked for the military?", "Have you ever served in the armed forces?",
                                   "Have you previously been employed by the Department of Defense?",
                                   "Have you ever worked for the Navy?", "Are you a former employee of the Air Force?"])
def test_military_qualifier_company_names_are_never_answered(company, label):
    assert not about_this_company(label, company)
    assert run(sel(label), company=company) == (None, ["q"])


def test_military_qualifier_alias_is_never_answered():
    assert not about_this_company("Have you ever worked for Acme Robotics?", "Acme Robotics", ["Acme Defense"])
    assert about_this_company("Have you ever worked for Acme Robotics?", "Acme Robotics", ["Roadrunner Labs"])
