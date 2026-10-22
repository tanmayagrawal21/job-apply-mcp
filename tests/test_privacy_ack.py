"""ADR-0034: opt-in acknowledgement of applicant privacy notices. Fictional profile only."""
import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import map_fields
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.profile import Contact, Preferences, Profile

ACK = ["I acknowledge", "I do not acknowledge"]


def prof(flag):
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city="Springfield"),
                               preferences=Preferences(acknowledge_privacy_notices=flag)), Settings())


def run(f, flag=True, **kw):
    r = map_fields([f], prof(flag), company="Acme Robotics", **kw)
    return r.answers[0].value if r.answers else None


def sel(label, options=ACK):
    return F("q", label, FieldKind.select, options=options)


POSITIVE = ["Applicant Privacy Acknowledgement", "Privacy Notice", "Job Applicant Data Privacy Notice",
            "By submitting my application, I acknowledge that I have read the privacy policy",
            "Recruitment Privacy Notice",
            "Please acknowledge that you have read and agree to our Privacy Policy"]


def test_default_is_off():
    assert Preferences().acknowledge_privacy_notices is None


@pytest.mark.parametrize("label", POSITIVE)
def test_acknowledged_only_when_true(label):
    assert run(sel(label)) == "I acknowledge"
    assert run(F("q", label, FieldKind.boolean)) is True
    assert run(sel(label), flag=None) is None
    assert run(sel(label), flag=False) is None
    assert run(F("q", label, FieldKind.boolean), flag=None) is None


def test_yes_option_and_text_box():
    assert run(sel("Privacy Notice", ["Yes", "No"])) == "Yes"
    assert run(F("q", "Privacy Notice", FieldKind.text)) is None      # never typed into free text
    assert run(sel("Privacy Notice", ["Acknowledge", "Agree"])) is None   # ambiguous


EXCLUDED = [
    "I personally completed this application and acknowledge the privacy notice",          # attestation
    "Privacy notice: I did not use AI to complete this application",                      # no-AI
    "I certify the information is true and accurate; I acknowledge the privacy policy",    # truthfulness
    "I acknowledge the privacy notice and consent to a background check",                  # background
    "I acknowledge the privacy policy and consent to a drug test",                         # drug test
    "I acknowledge the privacy notice and agree to binding arbitration",                   # arbitration
    "I acknowledge the privacy notice and agree to receive marketing emails",              # marketing
    "Privacy policy acknowledgement and SMS text messages opt-in",                         # SMS
    "Join our talent community and acknowledge the privacy notice",                        # talent community
    "I acknowledge the privacy policy and the terms of employment",                        # terms of employment
    "Privacy notice and use of artificial intelligence in hiring",                         # AI use
]


@pytest.mark.parametrize("label", EXCLUDED)
def test_exclusions_stay_unresolved_even_when_true(label):
    assert run(sel(label)) is None, label
    assert run(F("q", label, FieldKind.boolean)) is None, label


def test_marketing_optin_stays_no():
    assert run(sel("Sign up for our newsletter", ["Yes", "No"])) == "No"


def test_policy_stop_phrases_are_respected():
    label = "Privacy notice: I confirm this is entirely my handiwork"
    assert run(sel(label)) == "I acknowledge"
    assert run(sel(label), stop_phrases=["my handiwork"]) is None


def test_options_with_excluded_wording_are_refused():
    assert run(sel("Privacy Notice", ["I acknowledge and consent to a background check", "No"])) is None
