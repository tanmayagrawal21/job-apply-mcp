"""ADR-0030: generic mapper coverage (authorization, relocation/hybrid, referrals, years, degree year, languages).
Fictional profile only."""
import pytest
from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeStore, settings

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.derive import pick_languages, total_years, whole_years, years_option
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.mapper import canonical_key, map_fields, pick_auth_option
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.profile import Authorization, Education, Preferences, Profile, Work

YN = ["Yes", "No"]


def prof(**over) -> ProfileView:
    base = {
        "work": [Work(company="Initech", title="Engineer", start="2015-01", end="2018-12"),
                 Work(company="Hooli", title="Engineer", start="2018-01", end="2020-12")],   # overlap counted once -> 6.0y
        "education": [Education(school="Example State University", degree="BS", field="Computer Science",
                                start="2011-08", end="2014-05"),
                      Education(school="Springfield College", degree="MS", start="2020-08", end="2022-05")],
        "skill_years": {"Python": 5, "aws": 2.5},
        "languages": ["Python", "Go", "Rust", "Haskell"],
        "skills": ["Python"],
        "authorization": Authorization(work_authorized=True, requires_sponsorship=True, citizen=False),
        "preferences": Preferences(willing_to_relocate=True, open_to_hybrid=True),
    }
    base.update(over)
    return ProfileView(Profile(**base), Settings())


def run(f, p=None, company="Acme Robotics", job_in_us=False):
    r = map_fields([f], p or prof(), company=company, job_in_us=job_in_us)
    return ({a.key: a.value for a in r.answers}.get(f.key), [x.key for x in r.unresolved])


def sel(label, options=YN, key="q", **kw):
    return F(key, label, FieldKind.select, options=options, **kw)


# ---------------------------------------------------------------- work authorization / sponsorship
@pytest.mark.parametrize("label", [
    "Are you eligible to work in the US?", "Are you legally authorized to work in the United States?",
    "Do you have the right to work in the US?", "Are you permitted to work in the United States?"])
def test_authorized_yes(label):
    assert run(sel(label))[0] == "Yes"


@pytest.mark.parametrize("label", [
    "Do you now or will you in the future require immigration sponsorship",
    "Do you currently, or will you in the future, require visa sponsorship",
    "Will you now or in the future require sponsorship for employment visa status?",
    "Will you need visa sponsorship?",
    "Do you now or will you in the future require immigration sponsorship by the company for continued work-authorization?\n"])
def test_sponsorship_yes_never_flips(label):
    assert run(sel(label), job_in_us=True)[0] == "Yes"
    no = prof(authorization=Authorization(work_authorized=True, requires_sponsorship=False))
    assert run(sel(label), no, job_in_us=True)[0] == "No"
    assert run(sel(label))[0] is None          # ADR-0040: names no place and the job is not confirmed US


@pytest.mark.parametrize("label", [
    "Are you able to work in the US without sponsorship?",
    "Are you legally authorized to work in the United States without visa sponsorship?",
    "Can you work in the US and not require sponsorship?"])
def test_negated_sponsorship_flips_correctly(label):
    assert run(sel(label))[0] == "No"          # requires sponsorship -> cannot work without it
    ok = prof(authorization=Authorization(work_authorized=True, requires_sponsorship=False))
    assert run(sel(label), ok)[0] == "Yes"


def test_compound_two_part_question_is_unresolved():
    assert run(sel("Are you authorized to work in the US and will you require sponsorship?"))[0] is None


def test_sponsorship_unknown_stays_unresolved():
    p = prof(authorization=Authorization())
    assert run(sel("Will you require visa sponsorship?"), p) == (None, ["q"])


def test_authorization_never_falls_back_to_answers_patterns():
    # ADR-0050 (#70): flat [answers] patterns never answer authorization or sponsorship, even on a home-country job
    p = prof(authorization=Authorization(), answers={"eligible to work": "Yes", "require sponsorship": "Yes"})
    assert run(sel("Are you eligible to work in the US?"), p)[0] is None
    assert run(sel("Do you now or will you in the future require immigration sponsorship"), p, job_in_us=True)[0] is None


COUNTRY = ["I am a citizen of this country", "I have permission to work here but will require sponsorship",
           "I do not have permission to work here", "I have permission to work here without sponsorship"]


def test_country_where_you_live_variant_snaps_to_consistent_option():
    # ADR-0038: "the country where you live" may not be the US -> unresolved; the US form snaps to an option
    assert run(sel("Your authorization to work in the country where you live. Please select", COUNTRY))[0] is None
    assert run(sel("Can you work here and not require sponsorship?"))[0] is None
    f = sel("Your authorization to work in the United States. Please select", COUNTRY)
    # ADR-0040: an option naming a citizenship status makes it a legal-status question -> owner, never a rule guess
    assert run(f)[0] is None
    g = sel("Your authorization to work in the United States. Please select", COUNTRY[1:])
    assert run(g)[0] == COUNTRY[1]
    no_spons = prof(authorization=Authorization(work_authorized=True, requires_sponsorship=False))
    assert run(g, no_spons)[0] == COUNTRY[3]
    assert run(g, prof(authorization=Authorization(work_authorized=True)))[0] is None
    assert pick_auth_option(COUNTRY, True, True, True) in (None, COUNTRY[0], COUNTRY[1])  # citizen option only if citizen
    assert pick_auth_option([COUNTRY[0], COUNTRY[2]], True, True, False) is None


def test_us_citizen_never_yes_without_fact():
    assert run(sel("Are you a US citizen?"))[0] == "No"
    assert run(sel("Are you a US citizen?"), prof(authorization=Authorization(work_authorized=True)))[0] is None
    assert run(sel("Are you a US citizen or permanent resident?"))[0] is None


# ---------------------------------------------------------------- relocation / hybrid / onsite
@pytest.mark.parametrize("label", [
    "Are you open to working in a hybrid environment, 3 days a week in Chicago?",
    "Are you willing and able to commit to the hybrid policy if hired?",
    "Would you be comfortable with a hybrid schedule?"])
def test_hybrid_uses_preference(label):
    assert run(sel(label))[0] == "Yes"
    assert run(sel(label), prof(preferences=Preferences(open_to_hybrid=False)))[0] == "No"
    assert run(sel(label), prof(preferences=Preferences())) == (None, ["q"])


@pytest.mark.parametrize("label", [
    "If you are not located in Chicago or NY, are you open to relocating?",
    "Are you willing to relocate to New York?"])
def test_relocation_willingness(label):
    assert run(sel(label))[0] == "Yes"
    assert run(sel(label), prof(preferences=Preferences(willing_to_relocate=False)))[0] == "No"
    assert run(sel(label), prof(preferences=Preferences())) == (None, ["q"])


def test_relocation_support_is_not_willingness():
    f = sel("Will you require relocation support?")
    assert canonical_key(f.label) == "relocation_support"
    assert run(f) == (None, ["q"])                                     # willing != needs support
    assert run(f, prof(preferences=Preferences(needs_relocation_support=False)))[0] == "No"


def test_hybrid_arrangement_picker_is_not_a_yes_no_question():
    f = sel("What is your preferred hybrid or remote arrangement?", ["Hybrid", "Remote", "Onsite"])
    assert run(f)[0] is None


# ---------------------------------------------------------------- referral / know anyone
@pytest.mark.parametrize("label", [
    "Do you know anyone currently at Acme Robotics?", "Do you know someone who works at Acme Robotics?",
    "Were you referred by an employee?"])
def test_know_anyone_defaults_no(label):
    assert run(sel(label))[0] == "No"


def test_know_anyone_yes_only_with_listed_referral():
    p = prof(referrals={"Acme Robotics": "Sam Lee"})
    assert run(sel("Do you know anyone currently at Acme Robotics?"), p)[0] == "Yes"
    assert run(sel("Do you know anyone currently at Acme Robotics?"), p, company="Other Corp")[0] == "No"
    assert run(F("q", "Do you know anyone at Acme? If so, who?"), p)[0] == "Sam Lee"


def test_know_anyone_without_company_is_unresolved():
    assert run(sel("Do you know anyone currently at this company?"), company="")[0] is None


# ---------------------------------------------------------------- years of experience
def test_union_of_spans_and_floor():
    assert total_years([("2015-01", "2018-12"), ("2018-01", "2020-12")]) == 6.0
    assert total_years([("2015-01", "2015-12"), ("2017-01", "2017-06")]) == 1.5
    assert total_years([("", "")]) is None
    assert whole_years(5.99) == 5


@pytest.mark.parametrize("label", [
    "How many total years of professional software engineering experience do you have?",
    "Please input the total years of experience you have that are relevant to this role",
    "Total years of experience"])
def test_total_years_numeric(label):
    assert run(F("q", label, FieldKind.number))[0] == "6"
    assert run(F("q", label))[0] == "6"


def test_total_years_select_range_never_rounds_up():
    assert run(sel("Years of professional experience", ["0-2", "3-5", "6-9", "10+"]))[0] == "6-9"
    p = prof(work=[Work(company="Initech", title="E", start="2015-01", end="2019-11")])   # 4.9y -> floor 4
    assert run(sel("Years of professional experience", ["0-2", "3-5", "5-9", "10+"]), p)[0] == "3-5"
    assert years_option(4.9, ["0-2", "5+"]) is None


@pytest.mark.parametrize("label", [
    "Do you have at least 5 years of practical career experience?",
    "Do you have 5+ years of professional experience?",
    "Do you have at least 5 years of practical career experience in this field?\n"])
def test_at_least_n_compares_truthfully(label):
    assert run(sel(label))[0] == "Yes"
    assert run(sel(label.replace("5", "7")))[0] == "No"
    assert run(sel(label.replace("5", "6")))[0] == "Yes"           # exactly 6.0 >= 6
    short = prof(work=[Work(company="Initech", title="E", start="2018-01", end="2022-11")])   # 4.9y
    assert run(sel(label), short)[0] == "No"                       # 4.9 is not rounded up to 5


def test_per_technology_years_only_from_profile_facts():
    assert run(F("q", "Years of experience with Go/Python/Java/ or C++?", FieldKind.number))[0] == "5"
    assert run(F("q", "Years of experience GCP/AWS/ or Azure?", FieldKind.number))[0] == "2"
    assert run(F("q", "Years of experience with Kubernetes?", FieldKind.number)) == (None, ["q"])
    assert run(F("q", "Years of experience with Go/Java or C++?", FieldKind.number)) == (None, ["q"])
    assert run(sel("Do you have at least 3 years of Python experience?"))[0] == "Yes"
    assert run(sel("Do you have at least 6 years of Python experience?"))[0] == "No"
    assert run(sel("Do you have at least 3 years of Kubernetes experience?"))[0] is None


def test_years_without_any_experience_entries_is_unresolved():
    p = prof(work=[])
    assert run(F("q", "Total years of experience", FieldKind.number), p) == (None, ["q"])


# ---------------------------------------------------------------- most recent degree year
@pytest.mark.parametrize("label", ["In what year did you complete your most recent degree",
                                   "Year you received your highest degree"])
def test_degree_year_from_latest_education(label):
    assert run(F("q", label, FieldKind.number))[0] == "2022"
    assert run(sel(label, ["2020", "2021", "2022", "2023"]))[0] == "2022"
    assert run(sel(label, ["2019", "2023"])) == (None, ["q"])       # never a different year


# ---------------------------------------------------------------- languages
LANGS = ["Python", "Go", "Java", "C++", "Rust", "TypeScript"]


@pytest.mark.parametrize("label", ["Select the languages you are most proficient (production-grade)",
                                   "Which programming languages are you strongest in?"])
def test_language_multiselect_top_present_options(label):
    got = run(F("q", label, FieldKind.multiselect, options=LANGS))[0]
    assert got == ["Python", "Go", "Rust"]
    assert run(F("q", label, FieldKind.multiselect, options=["Java", "C++"])) == (None, ["q"])
    assert pick_languages(["golang", "cpp", "Zig"], LANGS, 3) == ["Go", "C++"]


def test_spoken_languages_not_answered():
    f = F("q", "Which languages are you fluent in (spoken)?", FieldKind.multiselect, options=["English", "Python"])
    assert run(f) == (None, ["q"])


# ---------------------------------------------------------------- unrelated stays unresolved; optional never blocks
def test_other_questions_stay_unresolved():
    assert run(F("q", "What is your favorite color?"))[1] == ["q"]
    assert run(F("q", "Describe your management philosophy", FieldKind.textarea))[1] == ["q"]


async def test_optional_unresolved_does_not_block_plan():
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("first_name", "First Name"),
        F("opt", "Anything else we should know?", FieldKind.select, required=False, options=["a", "b"]),
        F("opt2", "Years of experience with Kubernetes?", FieldKind.number, required=False),
        F("req", "Years of experience with Kubernetes?", FieldKind.number, required=True)])
    eng = ApplyEngine(settings(), resolve_adapter=lambda ref: FakeAdapter(form=form), profile=FakeProfile(),
                      router=None, gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    out = await eng.plan(POSTING.ref)
    assert out.plan.unresolved == ["req"]          # only the required field is reported/blocking


def test_semantic_keys_are_blocked_from_llm_guessing():
    from job_apply_mcp.apply.mapper import SEMANTIC_SENSITIVE
    assert {"requires_sponsorship", "work_authorized", "relocation_support", "open_to_hybrid",
            "knows_employee", "years_experience", "languages_known"} <= SEMANTIC_SENSITIVE
