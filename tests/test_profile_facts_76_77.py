"""ADR-0053 (#76, #77): authorization on confirmed home-country jobs, and employer / prior-employment / residence /
preference questions answered from profile facts only. Fictional profiles only (Alex Rivera, Springfield)."""
import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import canonical_key, map_fields
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.platforms.greenhouse import GreenhouseAdapter
from job_apply_mcp.profile import Authorization, Contact, Preferences, Profile, Work

YN = ["Yes", "No"]
FLAT = {"now or in the future": "Yes", "require sponsorship": "Yes", "authorized to work": "No"}
CURRENT = [Work(company="Initech", title="Software Engineer", start="2023-01"),
           Work(company="Globex", title="Intern", start="2021-05", end="2021-08")]


def view(country="United States", region="IL", work=None, prefs=None, answers=None, **auth):
    a = {"work_authorized": True, "requires_sponsorship": False} | auth
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city="Springfield",
                                               region=region, country=country),
                               work=CURRENT if work is None else work, preferences=prefs or Preferences(),
                               authorization=Authorization.model_validate(a), answers=dict(FLAT) | (answers or {})),
                       Settings())


def run(label, kind=FieldKind.boolean, options=YN, p=None, job="US", company="Acme Robotics"):
    f = F("q", label, kind, options=list(options) if options else None)
    r = map_fields([f], p or view(), company=company, job_country=job)
    run.gaps = [g.get("fact") for g in r.profile_gaps]
    return {a.key: (a.value, a.source) for a in r.answers}.get("q")


# ---------------------------------------------------------------- #76 authorization on a confirmed US job
AUTH_SEL = ["Yes, I am currently legally authorized to work in the country where the job is located.",
            "No, I am not currently legally authorized to work in the country where the job is located."]
SPONS_SEL = ["Yes, I will require immigration sponsorship now to legally work in the country where the job is located.",
             ("Yes, I will require immigration sponsorship in the future to legally work in the country where the job is "
              "located."),
             ("No, I do not and will not require immigration sponsorship to legally work in the country where the job is "
              "located.")]
AUTH_Q = "Are you legally authorized to work in the country where the job is located?"
SPONS_Q = ("Will you now or in the future require company sponsorship to retain or extend your work authorization in "
           "the country where the job is located?")


def test_job_located_select_questions_answered_on_confirmed_us_job():
    assert run(AUTH_Q, FieldKind.select, AUTH_SEL) == (AUTH_SEL[0], "rule:work_authorized")
    assert run(SPONS_Q, FieldKind.select, SPONS_SEL)[0] == SPONS_SEL[2]


def test_sponsorship_now_vs_future_is_never_guessed():
    assert run(SPONS_Q, FieldKind.select, SPONS_SEL, p=view(requires_sponsorship=True)) is None


@pytest.mark.parametrize("job", ["CA", None])
def test_job_located_questions_stay_unresolved_off_home_country(job):
    assert run(AUTH_Q, FieldKind.select, AUTH_SEL, job=job) is None
    assert run(SPONS_Q, FieldKind.select, SPONS_SEL, job=job) is None


@pytest.mark.parametrize("label", ["Will you require Acme Robotics to sponsor you for a work permit now or in the future?",
                                   "Will Acme Robotics need to sponsor you to work in the US?"])
def test_company_name_is_not_a_place_on_a_confirmed_us_job(label):
    assert run(label) == (False, "rule:requires_sponsorship")
    assert run(label, job=None) is None
    assert run(label, job="CA") is None


def test_us_work_visa_wording_still_needs_a_confirmed_us_job():   # ADR-0040 safeguard unchanged
    assert run("Will you require Acme Robotics to sponsor you for a US work permit now or in the future?",
               job=None) is None


def test_misspelled_sponsorship_is_recognised_and_never_flat():
    label = "Do you, now or in the future, require sponorship to work in the country you currently reside in?"
    assert canonical_key(label) == "requires_sponsorship"
    assert run(label, job=None) is None                    # the flat "now or in the future" answer never applies
    assert run("Do you require sponorship?", job="US") == (False, "rule:requires_sponsorship")
    assert run("Do you require sponorship?", job="CA") is None


def test_no_home_country_means_no_answer():
    assert run(AUTH_Q, FieldKind.select, AUTH_SEL, p=view(country="")) is None


def test_greenhouse_office_named_after_a_country_confirms_it():
    gh = GreenhouseAdapter.__new__(GreenhouseAdapter)
    assert gh._countries({"offices": [{"name": "United States", "location": None}]}) == ["US"]
    assert gh._countries({"offices": [{"name": "US-PERM", "location": None}]}) == ["UNCONFIRMED"]
    assert gh._countries({"offices": [{"name": "NYC", "location": ""}]}) == ["UNCONFIRMED"]
    assert gh._countries({"offices": [{"name": "Canada", "location": "Seattle, WA"}]}) == ["UNCONFIRMED"]


# ---------------------------------------------------------------- #77 employer and title
@pytest.mark.parametrize("label,want", [("Who is your current or previous employer?", "Initech"),
                                        ("What is your current or previous job title?", "Software Engineer"),
                                        ("Current company", "Initech"), ("What is your current job title?",
                                                                         "Software Engineer")])
def test_current_employer_and_title_from_work(label, want):
    assert run(label, FieldKind.text, None) == (want, f"rule:{canonical_key(label)}")


def test_recent_employer_falls_back_to_latest_ended_entry_but_current_does_not():
    past = [Work(company="Globex", title="Intern", start="2021-05", end="2021-08"),
            Work(company="Hooli", title="Analyst", start="2022-01", end="2023-06")]
    assert run("Who is your current or previous employer?", FieldKind.text, None, p=view(work=past))[0] == "Hooli"
    assert run("Current company", FieldKind.text, None, p=view(work=past)) is None
    assert run.gaps == ["work.current"]
    assert run("Who is your current or previous employer?", FieldKind.text, None, p=view(work=[])) is None
    assert run.gaps == ["work"]


# ---------------------------------------------------------------- #77 prior employment
SPOT = ["No", "Yes - Intern", "Yes - Full Time Employment"]


def test_previously_employed_multiselect_from_work_history():
    q = "Have you ever been previously employed by Acme Robotics?"
    assert run(q, FieldKind.multiselect, SPOT) == (["No"], "rule:prior_employee")
    worked = [*CURRENT, Work(company="Acme Robotics", title="Engineer", start="2019-01", end="2020-01")]
    assert run(q, FieldKind.multiselect, SPOT, p=view(work=worked)) is None     # which "Yes" is not guessed
    assert run(q, p=view(work=worked)) == (True, "rule:prior_employee")


def test_affiliate_form_is_yes_only_when_the_company_is_in_work_history():
    q = "Have you ever been employed by Acme Robotics or an Acme Robotics affiliate?"
    worked = [Work(company="Acme Robotics", title="Engineer", start="2019-01", end="2020-01")]
    assert run(q, p=view(work=worked)) == (True, "rule:prior_employee")
    assert run(q) is None                                  # "No" would also deny unknown affiliates


@pytest.mark.parametrize("label", ["Have you worked with Acme Robotics?", "Have you ever worked with Acme?"])
def test_worked_with_posting_company_is_yes_only_never_no(label):   # control: "with" may be a vendor relationship
    assert run(label) is None
    assert run(label.replace("Acme Robotics", "Initech").replace("Acme", "Initech"), company="Initech") == \
        (True, "rule:prior_employee")
    assert canonical_key("Have you worked with Python?") is None


@pytest.mark.parametrize("label", ["Have you ever worked for Acme Robotics?", "Have you worked at Acme Robotics?",
                                   "Have you ever been employed by Acme Robotics?",
                                   "Have you been previously employed by Acme Robotics?"])
def test_worked_for_at_or_employed_by_is_deterministic(label):
    assert run(label) == (False, "rule:prior_employee")
    worked = [Work(company="Acme Robotics", title="Engineer", start="2019-01", end="2020-01")]
    assert run(label, p=view(work=worked)) == (True, "rule:prior_employee")


# ---------------------------------------------------------------- "now"-only sponsorship (control)
NOW_Q = ["Do you currently require visa sponsorship?", "Do you require sponsorship now?",
         "Do you require sponsorship at this time?"]


@pytest.mark.parametrize("label", NOW_Q)
def test_now_only_sponsorship_needs_the_now_fact(label):
    p = view(requires_sponsorship=True)
    assert run(label, p=p) is None
    assert run.gaps == ["requires_sponsorship_now"]
    assert run(label, p=view(requires_sponsorship=True, requires_sponsorship_now=False)) == \
        (False, "rule:requires_sponsorship")
    assert run(label, p=view(requires_sponsorship=True, requires_sponsorship_now=True)) == \
        (True, "rule:requires_sponsorship")
    assert run(label, p=view(requires_sponsorship=False)) == (False, "rule:requires_sponsorship")   # never: no now
    assert run(label, p=view(requires_sponsorship=True, requires_sponsorship_now=False), job="CA") is None


def test_now_or_future_keeps_requires_sponsorship():
    q = "Will you now or in the future require visa sponsorship?"
    assert run(q, p=view(requires_sponsorship=True)) == (True, "rule:requires_sponsorship")
    assert run(q, p=view(requires_sponsorship=True, requires_sponsorship_now=False)) == (True, "rule:requires_sponsorship")


def test_per_country_now_fact():
    p = view(requires_sponsorship=True, CA={"work_authorized": True, "requires_sponsorship": True,
                                           "requires_sponsorship_now": False})
    assert run("Do you currently require sponsorship to work in Canada?", p=p, job="CA") == \
        (False, "rule:requires_sponsorship")


def test_prior_employment_without_any_work_history_is_a_gap():
    assert run("Have you ever worked for Acme Robotics?", p=view(work=[])) is None
    assert run.gaps == ["work"]


# ---------------------------------------------------------------- #77 residence and location
COUNTRIES = ["Canada", "India", "UK", "US", "Other"]


@pytest.mark.parametrize("label", ["Please select the country where you currently reside.", "Country of residence",
                                   "In which country do you currently live?"])
def test_country_of_residence_from_contact(label):
    assert run(label, FieldKind.select, COUNTRIES) == ("US", "rule:country")
    assert run(label, FieldKind.select, COUNTRIES, p=view(country="Canada", region="ON"))[0] == "Canada"
    assert run(label, FieldKind.multiselect, COUNTRIES)[0] == ["US"]
    assert run(label, FieldKind.select, COUNTRIES, p=view(country="")) is None
    assert run.gaps == ["contact.country"]


def test_city_and_state_of_residence():
    q = "If located in the US, in what city and state do you reside?"
    assert run(q, FieldKind.text, None) == ("Springfield, IL", "rule:residence_city_state")
    assert run(q, FieldKind.text, None, p=view(country="Canada", region="ON")) is None
    assert run(q, FieldKind.text, None, p=view(region="")) is None
    assert run.gaps == ["contact.region"]


@pytest.mark.parametrize("label,want", [("Are you located in Canada?", False), ("Are you based in Illinois?", True),
                                        ("Do you live in Texas?", False), ("Are you located in Springfield, IL?", True),
                                        ("Do you live within 50 miles of Springfield, Illinois?", True),
                                        ("Are you located in Springfield, USA?", True)])
def test_located_in_place_when_computable(label, want):
    assert run(label) == (want, f"rule:{canonical_key(label)}")


@pytest.mark.parametrize("label", ["Are you located in Springfield?", "Do you live within 25 km of Springfield?",
                                   "Are you located in Springfield, MO?", "Are you located in Springfield, Canada?",
                                   "Are you located in Chicago?", "Are you located in New York?",
                                   "Do you live within 50 miles of Chicago, IL?", "Are you based in the Bay Area?",
                                   "Are you located in Canada or Mexico?"])
def test_located_in_place_not_computable_stays_unresolved(label):
    assert run(label) is None


def test_located_in_a_us_state_from_outside_the_us_is_no():
    assert run("Are you based in Illinois?", p=view(country="Canada", region="ON")) == (False, "rule:located_in_place")


# ---------------------------------------------------------------- #77 preferences
@pytest.mark.parametrize("label,field", [("Are you willing to relocate?", "willing_to_relocate"),
                                         ("Are you open to a hybrid schedule?", "open_to_hybrid"),
                                         ("Are you comfortable working onsite 3 days a week?", "open_to_onsite")])
def test_preferences_only_when_configured(label, field):
    assert run(label, p=view(prefs=Preferences(**{field: True}))) == (True, f"rule:{field}")
    assert run(label, p=view(prefs=Preferences(**{field: False})))[0] is False
    assert run(label) is None
    assert run.gaps == [f"preferences.{field}"]


# ---------------------------------------------------------------- precedence over the autonomy layer (ADR-0052)
@pytest.mark.parametrize("level", ["safe_default", "infer"])
@pytest.mark.parametrize("label", ["Have you worked with Acme Robotics?", "Have you ever been employed by Acme Robotics?",
                                   "Do you have experience with Acme Robotics?",
                                   "Are you located in Chicago?", "Do you live within 25 miles of Chicago, IL?"])
async def test_fact_rule_fields_are_never_assumed_or_inferred(level, label):
    from apply_fakes import POSTING, FakeAdapter, FakeGates, FakeProfile, FakeRouter, FakeStore, settings

    from job_apply_mcp.apply.engine import ApplyEngine
    from job_apply_mcp.apply.mapper import fact_rule_field
    from job_apply_mcp.core.models import ApplicationForm
    f = F("q_fact", label, FieldKind.select, options=YN)

    class One(FakeAdapter):
        async def get_form(self, ref):
            return ApplicationForm(ref=ref, submit_via=["api"], fields=[f])
    router = FakeRouter(short={"q_fact": {"value": "Yes", "confidence": 0.99, "rationale": "fictional"}})
    e = ApplyEngine(settings(answers__autonomy=level), resolve_adapter=lambda ref: One(), profile=FakeProfile(),
                    router=router, gates=FakeGates(), store=FakeStore())
    out = await e.plan(POSTING.ref)
    if fact_rule_field(f, POSTING.company):
        assert "q_fact" in out.plan.unresolved and not out.plan.answers     # no work history / not computable
    else:                                                                    # not a fact question: unaffected
        assert all(not a.source.startswith("rule:") for a in out.plan.answers)
    assert fact_rule_field(F("q", "Have you worked with Acme Robotics?"), "Acme Robotics")


def test_worked_with_salesforce_without_history_stays_unresolved():   # reviewer: blocking case
    assert run("Have you worked with Salesforce?", company="Salesforce") is None
    worked = [Work(company="Salesforce", title="Engineer", start="2019-01", end="2020-01")]
    assert run("Have you worked with Salesforce?", company="Salesforce", p=view(work=worked)) == \
        (True, "rule:prior_employee")
