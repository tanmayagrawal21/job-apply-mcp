"""ADR-0059: live-run single-field blockers. Fictional profiles only (Alex Rivera, Flagstaff AZ; Initech)."""
import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import job_country, map_fields
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind as K
from job_apply_mcp.profile import Authorization, Contact, Preferences, Profile, Work

YN = ["Yes", "No"]


def view(region="AZ", country="United States", work=None, **auth):
    a = {"work_authorized": True, "requires_sponsorship": True} | auth
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city="Flagstaff", region=region,
                                               country=country),
                               work=[Work(company="Initech", title="Engineer", start="2024-01")] if work is None else work,
                               preferences=Preferences(willing_to_relocate=True, open_to_onsite=True),
                               authorization=Authorization.model_validate(a)), Settings())


def run(label, kind=K.boolean, options=None, p=None, job="US", company="Acme Robotics"):
    f = F("q", label, kind, options=list(options) if options else None)
    r = map_fields([f], p or view(), company=company, job_country=job)
    return {a.key: (a.value, a.source) for a in r.answers}.get("q")


REGION_Q = "Which state, province, or region do you currently live in?"


def test_region_of_residence_from_contact():
    assert run(REGION_Q, K.location) == ("Arizona", "rule:residence_region")
    assert run(REGION_Q, K.text) == ("Arizona", "rule:residence_region")
    assert run(REGION_Q, K.select, ["Alaska", "Arizona", "Arkansas"]) == ("Arizona", "rule:residence_region")
    assert run(REGION_Q, K.select, ["AK", "AZ", "AR"])[0] == "AZ"
    assert run(REGION_Q, K.text, p=view(region="Ontario", country="Canada"))[0] == "Ontario"
    assert run(REGION_Q, K.text, p=view(region="")) is None                  # missing fact: a profile gap


def test_hybrid_city_location_confirms_us():
    assert job_country("Hybrid - Denver", ["US"]) == "US"
    assert job_country("Hybrid - Springfield", ["US"]) is None


EXPLICIT = "Are you authorized to work lawfully in the United States for Acme Robotics?"
COMMENCE = ('Will you now or in the future require Acme Robotics to commence ("sponsor") an immigration case in order to '
            'employ you (for example, H-1B or other employment-based immigration case)? This is sometimes called '
            '"sponsorship" for an employment-based visa status.')


def test_explicit_country_question_on_an_unconfirmed_job():
    assert run(EXPLICIT, job=None) == (True, "rule:work_authorized")
    assert run(EXPLICIT, job="CA") is None                                # a confirmed OTHER country still blocks
    assert run("Will Acme Robotics sponsor a US work visa for you?", job=None) is None   # US as visa country only


def test_commence_sponsor_wording():
    assert run(COMMENCE) == (True, "rule:requires_sponsorship")
    assert run(COMMENCE, p=view(requires_sponsorship=False)) == (False, "rule:requires_sponsorship")
    assert run(COMMENCE, job=None) is None


def test_currently_an_employee_of_the_posting_company():
    q = "Are you currently an employee of a Acme Robotics agency?"
    assert run(q, options=YN) == (False, "rule:prior_employee")
    assert run(q, options=YN, p=view(work=[Work(company="Acme Robotics", title="Engineer", start="2022-01")])) == (
        True, "rule:prior_employee")
    assert run(q, options=YN, p=view(work=[])) == (False, "rule:prior_employee")   # not employed anywhere now
    two = [Work(company="Initech", title="Engineer", start="2024-01"), Work(company="Globex", title="TA", start="2024-02")]
    assert run(q, options=YN, p=view(work=two)) is None                     # ambiguous current employer


STATUS = ["Citizen", "Permanent Resident", "Visa Holder", "Require Sponsorship"]
STATUS_Q = ("Do you now, or will you in the future, require sponsorship for employment visa status to work legally "
            "for our company?")


def test_sponsorship_select_with_status_options():
    assert run(STATUS_Q, K.select, STATUS) == ("Require Sponsorship", "rule:requires_sponsorship")
    assert run(STATUS_Q, K.select, STATUS, p=view(requires_sponsorship=False)) is None   # status needs the owner
    assert run(STATUS_Q, K.select, STATUS, job=None) is None
    assert run("Are you a U.S. citizen or do you require sponsorship?", K.select, STATUS) is None


def test_example_with_generic_employment_based_words():
    q = ("Will you now or in the future require sponsorship for employment visa status (e.g., H-1B or other "
         "employment-based visa sponsorship) to work legally in the United States?")
    assert run(q, K.textarea) == ("Yes", "rule:requires_sponsorship")


@pytest.mark.parametrize("auth,req,want", [(True, False, "No"), (False, True, "Yes"), (True, True, None)])
def test_require_work_authorization_country_of_origin(auth, req, want):
    q = "Do you require work authorization to work in the country of origin for this position?"
    got = run(q, K.text, p=view(work_authorized=auth, requires_sponsorship=req))
    assert (got[0] if got else None) == want
    assert run(q, K.text, job=None) is None


def test_bare_visa_only_on_a_confirmed_home_country_job():
    assert run("Visa") == (True, "rule:requires_sponsorship")
    assert run("Visa", p=view(requires_sponsorship=False)) == (False, "rule:requires_sponsorship")
    assert run("Visa", job=None) is None
    assert run("Visa", job="CA") is None
