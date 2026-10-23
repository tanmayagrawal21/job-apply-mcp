"""ADR-0055 (#85, #86, #87, #89): residence-coupled hybrid options, name and education facts, prior employment "in the
past in a ... capacity", a sponsorship preamble, U.S.-person status, and the short-fact review exemption.
Fictional profiles only (Alex Rivera, Springfield IL / Flagstaff AZ); labels follow the real forms in the issues."""
import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.engine import _short_fact
from job_apply_mcp.apply.mapper import canonical_key, fact_rule_field, map_fields, residence_match
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.profile import Authorization, Contact, Education, Preferences, Profile, Work

YN = ["Yes", "No"]
WORK = [Work(company="Initech", title="Software Engineer", start="2024-06"),
        Work(company="Globex", title="Software Engineering Intern", start="2023-05", end="2023-08")]
EDU = [Education(school="Springfield State University", degree="PhD", field="Marine Biology", end="2019-12"),
       Education(school="Shelbyville College", degree="BS", field="Geology", end="2017-12")]
PREFS = Preferences(willing_to_relocate=True, open_to_hybrid=True, open_to_onsite=True)


def view(city="Springfield", region="IL", country="United States", prefs=PREFS, work=None, edu=None,
         settings=None, **auth):
    a = {"work_authorized": True, "requires_sponsorship": True} | auth
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city=city, region=region,
                                               country=country),
                               work=WORK if work is None else work, education=EDU if edu is None else edu,
                               preferences=prefs, authorization=Authorization.model_validate(a)),
                       settings or Settings())


def run(label, kind=FieldKind.boolean, options=YN, p=None, job="US", company="Acme Robotics", locs=()):
    f = F("q", label, kind, options=list(options) if options else None)
    r = map_fields([f], p or view(), company=company, job_country=job, job_locations=list(locs))
    run.gaps = [g.get("fact") for g in r.profile_gaps]
    run.unresolved = r.unresolved
    return {a.key: (a.value, a.source) for a in r.answers}.get("q")


# ---------------------------------------------------------------- #85 residence coupled with hybrid / relocation
HYB_Q = ("This role is tied to the office location listed in the job posting. Team members are expected to work from "
         "the office 3 days per week as part of Acme's hybrid work model. Are you currently based in the listed "
         "location and able to work in person 3 days per week?")
HYB_OPTS = ["Yes, I’m based in this location and able to work from the office 3 days per week",
            "No, I’m not based in this location but willing to relocate",
            "No, I’m only able to work remotely", "Other (optional context)"]


@pytest.mark.parametrize("locs,job", [(["San Francisco"], None), (["New York"], "US"), (["Austin, TX"], "US"),
                                      (["London, UK"], "GB")])
def test_owner_elsewhere_picks_relocate_never_based_here(locs, job):
    p = view(city="Flagstaff", region="AZ")
    assert run(HYB_Q, FieldKind.select, HYB_OPTS, p=p, locs=locs, job=job) == (HYB_OPTS[1], "rule:residence_here")


def test_owner_in_the_posting_city_picks_based_here():
    assert run(HYB_Q, FieldKind.select, HYB_OPTS, locs=["Springfield, IL"]) == (HYB_OPTS[0], "rule:residence_here")
    p = view(city="San Francisco", region="CA")
    assert run(HYB_Q, FieldKind.select, HYB_OPTS, p=p, locs=["San Francisco"])[0] == HYB_OPTS[0]


@pytest.mark.parametrize("locs", [["Chicago, IL"], ["Springfield"], [], ["Remote"], ["Peoria, Illinois"]])
def test_residence_not_computable_stays_unresolved(locs):
    # same state, another city (may be nearby), a bare unknown city, no location: never "based here", never "not"
    assert run(HYB_Q, FieldKind.select, HYB_OPTS, locs=locs, job="US") is None
    assert run.unresolved[0].options == HYB_OPTS               # the owner sees every option


def test_relocate_option_needs_willing_to_relocate_true():
    for prefs in (Preferences(open_to_hybrid=True), Preferences(willing_to_relocate=False, open_to_hybrid=True)):
        p = view(city="Flagstaff", region="AZ", prefs=prefs)
        assert run(HYB_Q, FieldKind.select, HYB_OPTS, p=p, locs=["New York"]) is None


def test_based_here_needs_the_hybrid_preference():
    p = view(prefs=Preferences(willing_to_relocate=True, open_to_hybrid=False, open_to_onsite=False))
    # never "based here and able to work from the office"; the remote-only option is the truthful one
    assert run(HYB_Q, FieldKind.select, HYB_OPTS, p=p, locs=["Springfield, IL"]) == (HYB_OPTS[2], "rule:open_to_hybrid")


def test_open_to_hybrid_alone_never_claims_residence():   # the #85 wrong answer
    p = view(city="Flagstaff", region="AZ", prefs=Preferences(open_to_hybrid=True))
    assert run(HYB_Q, FieldKind.select, HYB_OPTS, p=p, locs=["San Francisco"]) is None
    assert fact_rule_field(F("q", HYB_Q, FieldKind.select, options=HYB_OPTS))     # never assumed or inferred


def test_yes_no_label_coupling():
    q = "Are you based in the listed location and able to work in person 3 days per week?"
    assert run(q, locs=["Springfield, IL"]) == (True, "rule:residence_here")
    assert run(q, p=view(city="Flagstaff", region="AZ"), locs=["New York"]) == (False, "rule:residence_here")
    assert run(q, locs=["Chicago, IL"]) is None
    either = ("In office attendance is an essential function for this role. Do you currently reside within commuting "
              "distance or are you open to relocation to the local Acme office(s) designated in this post?")
    assert run(either, locs=["GB-London"], job="GB") == (True, "rule:residence_here")
    no_move = view(prefs=Preferences(willing_to_relocate=False))
    assert run(either, p=no_move, locs=["Chicago, IL"]) is None           # might live within commuting distance
    assert run(either, p=no_move, locs=["GB-London"], job="GB") == (False, "rule:residence_here")


def test_conditional_local_wording_is_a_relocation_question():
    assert run("If you are not local to the area, are you willing to relocate?") == (True, "rule:willing_to_relocate")


def test_open_to_relocating_option_without_a_residence_claim_still_maps():
    opts = ["I am local to the Springfield area", "I am open to relocating and coming into the office 3 days a week",
            "I am not able to relocate"]
    got = run("Are you able to relocate?", FieldKind.select, opts, locs=["Chicago, IL"])
    assert got == (opts[1], "rule:willing_to_relocate")


def test_residence_match_helper():
    p = view(city="Flagstaff", region="AZ")
    assert residence_match(p, ["Flagstaff, AZ"]) is True
    assert residence_match(p, ["Flagstaff, Arizona, United States"]) is True
    assert residence_match(p, ["San Francisco, CA", "New York, NY"]) is False
    assert residence_match(p, ["Phoenix, AZ"]) is None
    assert residence_match(p, ["San Francisco, CA", "Flagstaff, AZ"]) is True
    assert residence_match(p, ["San Francisco, CA", "Remote"]) is None
    assert residence_match(view(region=""), ["Austin, TX"]) is None        # no region: never decided


# ---------------------------------------------------------------- #86 names, degree type, school, internship
def test_legal_name_and_preferred_last_name():
    assert run("Legal First and Last Name", FieldKind.text, None) == ("Alex Rivera", "rule:full_name")
    assert run("Preferred Last Name", FieldKind.text, None) == ("Rivera", "rule:last_name")
    assert run("Preferred First Name", FieldKind.text, None) == ("Alex", "rule:preferred_name")


def test_degree_type_is_the_highest_degree():
    opts = ["Undergraduate/Bachelor's", "Associate's", "PhD"]
    assert run("Degree Type", FieldKind.multiselect, opts) == (["PhD"], "rule:degree_type")
    assert run("Degree Type", FieldKind.select, opts) == ("PhD", "rule:degree_type")
    assert run("What is your highest level of education?", FieldKind.text, None) == ("PhD", "rule:degree_type")
    bs_only = view(edu=[EDU[1]])
    assert run("Degree Type", FieldKind.select, opts, p=bs_only)[0] == "Undergraduate/Bachelor's"
    assert run("Degree Type", FieldKind.select, opts, p=view(edu=[])) is None
    assert "education" in run.gaps
    odd = view(edu=[Education(school="X", degree="Certificate in Welding", end="2020-05"), EDU[0]])
    assert run("Degree Type", FieldKind.select, opts, p=odd) is None        # an unknown level may be higher


def test_school_attended_and_most_recent_school():
    assert run("University or School Attended", FieldKind.text, None)[0] == "Springfield State University"
    assert run("What is the most recent school you attended?", FieldKind.text, None) == (
        "Springfield State University", "rule:recent_school")


def test_most_recent_degree_obtained_skips_a_future_degree():
    opts = ["High School", "Associate's Degree", "Bachelor's Degree", "Doctor of Philosophy (Ph.D.)", "Other"]
    q = "What is the most recent degree you obtained?"
    assert run(q, FieldKind.select, opts) == ("Doctor of Philosophy (Ph.D.)", "rule:recent_degree")
    future = view(edu=[Education(school="S", degree="PhD", end="2999-05"), EDU[1]])
    assert run(q, FieldKind.select, opts, p=future)[0] == "Bachelor's Degree"
    assert run("Most recent degree", FieldKind.select, opts, p=future)[0] == "Doctor of Philosophy (Ph.D.)"


def test_last_internship_only_from_work():
    assert run("Where was your last internship?", FieldKind.text, None) == ("Globex", "rule:last_internship")
    assert run("Where was your last internship?", FieldKind.text, None, p=view(work=WORK[:1])) is None
    assert "work.internship" in run.gaps


def test_start_quarter_is_not_answered():
    opts = ["Q1: January 2027 - March 2027", "Q2: April 2027 - June 2027"]
    assert run("Please indicate which quarter you would be able to start work for this position.",
               FieldKind.multiselect, opts) is None


# ---------------------------------------------------------------- #87 most recently worked, prior employment
def test_where_most_recently_worked():
    assert run("Where have you most recently worked?", FieldKind.text, None) == ("Initech", "rule:recent_company")


@pytest.mark.parametrize("label", [
    "Have you worked at Acme Robotics in the past in a Full-time, Part-time, contractor or Intern capacity?",
    "Have you ever been employed by Acme Robotics?"])
def test_prior_employment_booleans(label):
    assert run(label) == (False, "rule:prior_employee")
    assert run(label.replace("Acme Robotics", "Globex"), company="Globex") == (True, "rule:prior_employee")
    assert run(label, p=view(work=[])) is None                                 # no work history: a gap, not a No


def test_prior_employment_other_capacity_wording_stays_with_owner():
    assert run("Have you worked at Acme Robotics in the past as a sales partner?") is None
    assert run("Have you ever worked for Acme Robotics in any capacity?") is None            # ADR-0038 safety test
    assert run("Have you ever been employed by Acme Robotics or an Acme Robotics affiliate?") is None   # ADR-0053


# ---------------------------------------------------------------- #89 sponsorship preamble (#57 family)
LAMBDA_Q = ("Acme Robotics may agree to sponsor individuals to obtain work authorization (such as H-1B, TN or O-1 visa "
            "status). Will you now or in the future require sponsorship from Acme Robotics to maintain your work "
            "authorization in the U.S.?")


def test_sponsorship_preamble_on_confirmed_home_country_job():
    assert run(LAMBDA_Q) == (True, "rule:work_authorized")
    assert run(LAMBDA_Q, p=view(requires_sponsorship=False)) == (False, "rule:work_authorized")


@pytest.mark.parametrize("job", [None, "CA"])
def test_sponsorship_preamble_off_home_country_stays_unresolved(job):
    assert run(LAMBDA_Q, job=job) is None


def test_other_preambles_are_not_dropped():
    q = ("Acme Robotics sponsors UK Skilled Worker visas (such as Tier 2). Will you now or in the future require "
         "sponsorship from Acme Robotics to maintain your work authorization in the U.S.?")
    assert run(q) is None


# ---------------------------------------------------------------- U.S. person (ITAR/EAR)
USP_Q = ("A “U.S. person” is a citizen, legal permanent resident, or legal temporary resident (i.e., a refugee or "
         "asylee) of the United States. Which of the following best describes your “U.S. person” status?")
USP_OPTS = ["I am a U.S. person", "I am a citizen of Cuba, Iran, North Korea, or Syria AND I am NOT a U.S. person",
            "None of the above; I am a citizen of a different country"]


def test_us_person_from_authorization_fact():
    assert canonical_key(USP_Q) == "us_person"
    yes = view(us_person=True)
    assert run(USP_Q, FieldKind.select, USP_OPTS, p=yes) == (USP_OPTS[0], "rule:us_person")
    assert run("Are you a U.S. person as defined by ITAR/EAR?", p=yes) == (True, "rule:us_person")
    no = view(us_person=False, citizenship="Canada")
    assert run(USP_Q, FieldKind.select, USP_OPTS, p=no) == (USP_OPTS[2], "rule:us_person")
    assert run("Are you a U.S. person as defined by ITAR/EAR?", p=no) == (False, "rule:us_person")


def test_us_person_missing_or_ambiguous_stays_unresolved():
    assert run(USP_Q, FieldKind.select, USP_OPTS) is None
    assert "authorization.us_person" in run.gaps
    assert run(USP_Q, FieldKind.select, USP_OPTS, p=view(us_person=False)) is None          # citizenship unknown
    korea = view(us_person=False, citizenship="South Korea")
    assert run(USP_Q, FieldKind.select, USP_OPTS, p=korea) is None      # an option names a "Korea": the owner decides
    q = "Are you authorized to work in the US? (U.S. persons as defined under ITAR/EAR)"
    assert run(q, p=view(us_person=True)) is None                          # a work-authorization question too


def test_us_person_policy_fallback_only_when_explicit():
    s = Settings.model_validate({"policy": {"us_person": True}})
    assert run("Are you a U.S. person as defined by ITAR/EAR?", p=view(settings=s)) == (True, "rule:us_person")
    assert run("Are you a U.S. person as defined by ITAR/EAR?", p=view()) is None   # default False is not a fact
    s2 = Settings.model_validate({"policy": {"us_person": True}})
    assert run("Are you a U.S. person as defined by ITAR/EAR?",
               p=view(settings=s2, us_person=False, citizenship="Canada")) == (False, "rule:us_person")


# ---------------------------------------------------------------- #87 short facts skip the prose review gate
@pytest.mark.parametrize("label,value,want", [
    ("What is the most recent school you attended?", "Springfield State University", True),
    ("Who is your current or previous employer?", "Initech", True),
    ("What is your current or previous job title?", "Software Engineer", True),
    ("Legal First and Last Name", "Alex Rivera", True),
    ("What is the most recent school you attended?", "Springfield State. I loved it there and learned a lot.", False),
    ("Why do you want to work here?", "Initech", False),
])
def test_short_fact_exemption(label, value, want):
    assert _short_fact(F("q", label, FieldKind.text), value) is want


# ---------------------------------------------------------------- [demographics].race_detail (owner request)
def _race(opts, **demo):
    from job_apply_mcp.profile import Demographics
    p = ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera"), demographics=Demographics(**demo)),
                    Settings())
    f = F("r", "Race / Ethnicity", FieldKind.select, options=opts)
    r = map_fields([f], p)
    return {a.key: (a.value, a.source) for a in r.answers}.get("r")


EEO1 = ["American Indian or Alaska Native", "Black or African American", "White", "Two or more races", "Decline to self-identify"]
DETAIL = ["English", "Irish", "Scottish", "Black or African American", "Decline to self-identify"]


def test_race_detail_picks_the_matching_option():
    assert _race(DETAIL, race_ethnicity="White", race_detail="Irish") == ("Irish", "rule:race")


def test_race_detail_falls_back_to_the_eeo1_option():
    assert _race(EEO1, race_ethnicity="White", race_detail="Irish") == ("White", "rule:race")
    assert _race(EEO1, race_ethnicity="White") == ("White", "rule:race")


def test_race_detail_never_picks_a_broader_or_narrower_option():
    # no matching detail option and no plain "White" option: decline, never "English" / "Scottish"
    assert _race(DETAIL, race_ethnicity="White", race_detail="Welsh")[0] == "Decline to self-identify"
    assert _race(["Northern Irish", "Scottish", "Decline to self-identify"], race_ethnicity="White",
                 race_detail="Irish")[0] == "Decline to self-identify"


def test_declined_race_wins_over_detail():
    assert _race(DETAIL, race_detail="Irish")[0] == "Decline to self-identify"


def test_race_detail_stays_eeo_isolated():
    from job_apply_mcp.apply.mapper import eeo_class
    assert eeo_class("Race / Ethnicity") == (True, "race")          # the EEO path runs before every other rule
    other = F("q", "Which region do you prefer?", FieldKind.select, options=DETAIL)
    p = ProfileView(Profile(demographics={"race_ethnicity": "White", "race_detail": "Irish"}), Settings())
    assert map_fields([other], p).answers == []                      # never used outside a race question
