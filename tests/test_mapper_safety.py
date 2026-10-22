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
    ans, _ = run(sel(label))
    assert ans == ("No", "rule:requires_sponsorship")
    assert privacy_ack_value(sel(label)) is None
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
