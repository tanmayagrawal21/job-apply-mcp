"""ADR-0040: country-general authorization. Per-country profile facts, job_country() for any country, data-driven
location matching. Fictional profile only (Alex Rivera, Springfield)."""
import pytest
from apply_fakes import F, FakeProfile

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import job_country, location_in_country, location_is_us, map_fields, never_llm
from job_apply_mcp.apply.places import ambiguous_codes, country_iso
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.profile import Authorization, Contact, Demographics, Profile

YN = ["Yes", "No"]


def view(**auth):
    return ProfileView(Profile(contact=Contact(first_name="Alex", city="Springfield"),
                               authorization=Authorization.model_validate(auth)), Settings())


def ans(label, p, job=None, options=YN):
    r = map_fields([F("q", label, FieldKind.select, options=list(options))], p, job_country=job)
    return {a.key: a.value for a in r.answers}.get("q"), r.profile_gaps


# ---------------------------------------------------------------- locations, any country
@pytest.mark.parametrize("country,yes,no", [
    ("CA", ["Toronto, ON", "Toronto, Ontario, Canada", "Montreal, QC", "Remote - Canada", "Remote (Canada)",
            "Vancouver, British Columbia", "Saskatoon, SK", "Calgary, AB", "Vancouver, BC", "Victoria, BC"],
     ["Toronto, CA", "Vancouver, WA", "Regina", "Halifax, NS, USA", "Ottawa, ON (also London)",
      "London, ON", "Remote", "Toronto, ON; New York, NY", "Remote - US"]),
    ("GB", ["London, UK", "London, England", "Manchester, United Kingdom", "Edinburgh, Scotland", "Remote - UK",
            "Cardiff, Wales"],
     ["London", "London, ON", "Cambridge, MA", "London, UK (also Paris)", "Remote", "Dublin, Ireland"]),
    ("IN", ["Bengaluru, Karnataka, India", "Bengaluru, KA", "Mumbai, MH", "Pune, Maharashtra", "Hyderabad, TG",
            "Remote - India", "New Delhi, Delhi"],
     ["Bengaluru, IN", "Chennai", "Kochi, KL, USA", "Nagpur, MH", "Remote", "Mumbai, India / Dubai"]),
    ("AU", ["Sydney, New South Wales", "Melbourne, Victoria", "Brisbane, QLD", "Perth, Western Australia",
            "Adelaide, SA", "Remote - Australia", "Hobart, TAS", "Perth, WA", "Melbourne, VIC", "Sydney, NSW"],
     ["Sydney, NSW, USA", "Darwin", "Remote", "Seattle, WA", "Perth, WA, US"]),
])
def test_location_in_country(country, yes, no):
    for loc in yes:
        assert location_in_country(loc, country), (country, loc)
    for loc in no:
        assert not location_in_country(loc, country), (country, loc)


def test_us_alias_and_ambiguity_data():
    assert location_is_us("Seattle, WA") and not location_is_us("Perth, WA") and not location_is_us("Toronto, CA")
    assert "wa" in ambiguous_codes("US") and "wa" in ambiguous_codes("AU") and "on" not in ambiguous_codes("CA")
    assert "sa" in ambiguous_codes("AU") and "ka" not in ambiguous_codes("IN")
    assert country_iso("United States") == "US" and country_iso("Canada") == "CA" and country_iso("UK") == "GB"
    assert country_iso("CA", allow_codes=False) is None and country_iso("Atlantis") is None


@pytest.mark.parametrize("loc", ["Remote (US) + Remote (UK)", "Remote (US) and Remote (UK)", "Remote (US) Remote (UK)",
                                 "Remote (US) + Toronto", "Remote (US) + Remote (Canada)", "New York (US) + London (UK)",
                                 "New York, NY (also London)", "New York, NY (London possible)"])
def test_parentheticals_and_joined_places_never_confirm(loc):
    assert job_country(loc, ["US"]) is None


def test_job_country_any_country():
    assert job_country("Toronto, ON", ["CA"]) == "CA"
    assert job_country("London, UK", ["GB"]) == "GB"
    assert job_country("Bengaluru, Karnataka", ["IN"], ["Mumbai, MH"]) == "IN"
    assert job_country("Sydney, NSW", ["AU"]) == "AU"
    assert job_country("New York, NY (HQ)", ["US"]) == "US"
    for loc, cs in [("Toronto, ON", ["US"]), ("Toronto, ON", []), ("Toronto, ON", ["CA", "US"]),
                    ("Toronto, ON", ["UNCONFIRMED"]), ("London, UK", ["GB"] * 1 + ["UNCONFIRMED"]),
                    ("Remote - EMEA", ["GB"]), ("Toronto, ON", ["XX"])]:
        assert job_country(loc, cs) is None, (loc, cs)


# ---------------------------------------------------------------- per-country authorization facts
def test_flat_keys_are_the_home_country():
    p = view(work_authorized=True, requires_sponsorship=False)            # old config: home_country defaults to US
    assert ans("Will you require sponsorship?", p, "US")[0] == "No"
    assert ans("Are you authorized to work in the United States?", p)[0] == "Yes"
    assert ans("Will you require sponsorship?", p, "CA") == (None, [{"country": "CA", "fact": "requires_sponsorship",
                                                                    "key": "q"}])
    q = view(home_country="CA", work_authorized=True, requires_sponsorship=False)
    assert ans("Will you require sponsorship?", q, "CA")[0] == "No"
    assert ans("Will you require sponsorship?", q, "US")[0] is None


def test_per_country_tables_answer_their_own_country():
    p = view(US={"work_authorized": True, "requires_sponsorship": True},
             CA={"work_authorized": True, "requires_sponsorship": False}, GB={"work_authorized": False})
    assert ans("Will you require sponsorship?", p, "US")[0] == "Yes"
    assert ans("Will you require sponsorship?", p, "CA")[0] == "No"
    assert ans("Are you authorized to work?", p, "GB")[0] == "No"
    # a question naming a country uses that country's facts, whatever the job's country
    assert ans("Are you legally authorized to work in Canada?", p, "US")[0] == "Yes"
    assert ans("Do you require sponsorship to work in Canada?", p)[0] == "No"
    assert ans("Do you have the right to work in the UK?", p, "US")[0] == "No"
    # unknown country facts: unresolved and recorded as a profile gap
    got, gaps = ans("Will you require sponsorship?", p, "AU")
    assert got is None and gaps == [{"country": "AU", "fact": "requires_sponsorship", "key": "q"}]
    got, gaps = ans("Do you need sponsorship to work in Germany?", p, "US")
    assert got is None and gaps[0]["country"] == "DE"


def test_country_visa_lexicons():
    p = view(US={"work_authorized": True, "requires_sponsorship": True},
             CA={"work_authorized": True, "requires_sponsorship": True})
    assert ans("Will you require sponsorship (e.g., H-1B, TN)?", p, "US")[0] == "Yes"
    assert ans("Will you require sponsorship (e.g., LMIA, PGWP)?", p, "CA")[0] == "Yes"
    assert ans("Will you require sponsorship (e.g., H-1B, TN)?", p, "CA")[0] is None    # US visas on a Canadian job
    assert ans("Will you require sponsorship (e.g., LMIA)?", p, "US")[0] is None
    assert ans("Will you require sponsorship (e.g., H-1B)?", p)[0] is None                # job country unknown


def test_place_less_and_contradictions_stay_unresolved():
    p = view(US={"work_authorized": True, "requires_sponsorship": False},
             CA={"work_authorized": True, "requires_sponsorship": False})
    assert ans("Will you require sponsorship?", p)[0] is None
    assert ans("Are you authorized to work in the US or Canada?", p, "US")[0] is None
    assert ans("Are you authorized to work in the EU?", p, "US")[0] is None
    assert ans("Are you authorized to work in this country?", p, "CA")[0] == "Yes"
    assert ans("Are you authorized to work in this country?", p)[0] is None
    assert ans("Are you a U.S. person under ITAR?", p, "US")[0] is None                  # legal: never answered


def test_fake_profiles_keep_working():
    class P(FakeProfile):
        VALUES = {"work_authorized": "Yes", "requires_sponsorship": "No"}   # noqa: RUF012
    r = map_fields([F("q", "Will you require sponsorship?", FieldKind.select, options=YN)], P(), job_in_us=True)
    assert [a.value for a in r.answers] == ["No"]


def test_never_llm_control_round_two():
    for label in ["Have you ever been charged with a crime?", "Any pending charges?", "Do you have a DUI?",
                  "Are you willing to undergo a background screen?", "Are you a Canadian PR?",
                  "Do you have a Schengen permit?", "Do you have indefinite leave to remain?", "Do you need a Blue Card?",
                  "Are you legally able to start work immediately?", "Are you fine working from Austin?"]:
        assert never_llm(F("k", label, FieldKind.text, max_length=80)), label


async def test_engine_plan_lists_profile_gaps():
    from apply_fakes import POSTING, FakeAdapter, FakeGates, FakeStore, settings

    from job_apply_mcp.apply.engine import ApplyEngine
    from job_apply_mcp.core.models import ApplicationForm
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("q", "Do you require sponsorship to work in Canada?", FieldKind.select, options=YN)])
    e = ApplyEngine(settings(), resolve_adapter=lambda ref: FakeAdapter(form=form), profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    out = await e.plan(POSTING.ref)
    assert out.compact()["profile_gaps"] == [{"country": "CA", "fact": "requires_sponsorship", "key": "q"}]
    assert "q" in out.plan.unresolved


def test_us_citizen_comes_from_us_facts():
    p = view(home_country="CA", citizen=True)                       # Canadian citizen, home Canada
    r = map_fields([F("q", "Are you a US citizen?", FieldKind.select, options=YN)], p)
    assert "Yes" not in [a.value for a in r.answers]
    q = view(home_country="CA", citizen=True, US={"citizen": True})
    r = map_fields([F("q", "Are you a US citizen?", FieldKind.select, options=YN)], q)
    assert [a.value for a in r.answers] == ["Yes"]
    assert view(citizen=True).lookup("us_citizen") == "Yes"          # home US: the flat key still counts


def test_short_llm_allowlist():
    from apply_fakes import FakeGates, FakeStore, settings

    from job_apply_mcp.apply.engine import ApplyEngine
    from job_apply_mcp.apply.mapper import llm_safe_short
    e = ApplyEngine(settings(), resolve_adapter=lambda ref: None, profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    blocked = ["Have you been convicted of a misdemeanor?", "Do you have any unspent police cautions?",
               "Do you have pre-settled status?", "Do you hold a Stamp 4?", "Can you work CET hours?",
               "Do you have a valid driving licence?", "Nickname", "Pick one", "What is your favourite colour?"]
    for label in blocked:
        f = F("k", label, FieldKind.text, max_length=80)
        assert not llm_safe_short(f), label
    for label in ["Any misdemeanors involving software?", "Have you been cautioned by police for software piracy?",
                  "Do you have pre-settled status to use our platform?", "Are you an Irish Stamp 4 holder with Python skills?",
                  "Can you work CET hours on our platform?", "Do you have a driving licence and tools?",
                  "Have you misused company tools resulting in dismissal?", "Were you fired for software misuse?",
                  "Have you been terminated from a software job?", "Any disciplinary action related to coding?",
                  "Do you have a pending lawsuit about software?", "Have you ever been bankrupt? (skills)",
                  "Do you consent to a coding test?", "How many years of experience with Rust?",
                  "Link to your portfolio", "When can you start?"]:
        assert not llm_safe_short(F("k", label, FieldKind.text, max_length=80)), label
    for label in ["Favorite build tool", "What is your notice period?", "Which programming languages do you know?",
                  "What tools do you use?", "GitHub URL", "How did you hear about us?", "Earliest start date"]:
        assert llm_safe_short(F("k", label, FieldKind.text, max_length=80)), label
    assert e._is_short(F("k", "Favorite build tool", FieldKind.text, max_length=80))


async def test_country_gaps_are_recorded_with_their_country(tmp_path):
    from apply_fakes import POSTING, FakeAdapter, FakeGates, FakeStore, settings

    from job_apply_mcp.apply.engine import ApplyEngine
    from job_apply_mcp.core.models import ApplicationForm
    from job_apply_mcp.persona.gaps import ProfileDB
    form = ApplicationForm(ref=POSTING.ref, fields=[
        F("q", "Do you require sponsorship to work in Canada?", FieldKind.select, options=YN, required=True)])
    e = ApplyEngine(settings(), resolve_adapter=lambda ref: FakeAdapter(form=form), profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    e.gaps = ProfileDB(tmp_path)
    await e.plan(POSTING.ref)
    assert e.gaps.gap("authorization.CA.requires_sponsorship") is not None
    assert e.gaps.gap("requires_sponsorship") is None          # never the unscoped home-country key


async def test_unconfirmed_country_answer_never_reaches_a_us_job(tmp_path):
    """Control repro (8c75416f): an answer typed for an unconfirmed-country posting must not answer a US job."""
    from apply_fakes import POSTING, FakeAdapter, FakeGates, FakeStore, settings

    from job_apply_mcp.apply.engine import ApplyEngine
    from job_apply_mcp.core.models import ApplicationForm
    from job_apply_mcp.persona import PersonaPolicy, ProfileDB
    db = ProfileDB(tmp_path)
    pv = ProfileView(Profile(contact=Contact(first_name="Alex", city="Springfield")), Settings())   # no auth facts
    pv.persona = PersonaPolicy(pv.p.persona, db, pv.resume_projects)
    labels = ["Will you require sponsorship?", "Are you authorized to work in the country where this job is located?"]
    form = ApplicationForm(ref=POSTING.ref, fields=[F(f"q{i}", lab, FieldKind.select, options=YN, required=True)
                                                    for i, lab in enumerate(labels)])
    posting = POSTING.model_copy(update={"location": "Remote (US) + Remote (UK)", "countries": ["US"]})
    e = ApplyEngine(settings(), resolve_adapter=lambda ref: FakeAdapter(form=form, posting=posting), profile=pv,
                    router=None, gates=FakeGates(), store=FakeStore(), driver_factory=lambda n: None)
    e.persona, e.gaps = pv.persona, db
    out = await e.plan(POSTING.ref)
    assert set(out.plan.unresolved) >= {"q0", "q1"} and not out.profile_gaps
    asked = [q["key"] for q in db.questions(limit=50, include_skipped=True)]
    assert not [k for k in asked if "sponsor" in k or "authori" in k], asked          # no reusable key at all
    for k in asked:                                                                    # the owner answers everything
        db.set_answer(k, "No", origin="user_written")
    us = map_fields([F("q", "Will you require sponsorship?", FieldKind.select, options=YN)], pv, job_country="US",
                    persona=pv.persona)
    assert us.answers == []


def test_free_text_gender_only_required_and_only_gender_if_required():
    def run(required, **demo):
        p = ProfileView(Profile(contact=Contact(first_name="Alex"), demographics=Demographics(**demo)), Settings())
        r = map_fields([F("g", "Gender", FieldKind.text, required=required)], p)
        return [a.value for a in r.answers]
    assert run(False, gender="Male") == [] and run(True, gender="Male") == []           # never from `gender`
    assert run(False, gender_if_required="Male") == []                                  # optional: blank
    assert run(True, gender_if_required="Male") == ["Male"]                             # required: gender_if_required
    assert run(True, gender_if_required="Decline to self-identify") == []
    for label in ["Race", "Veteran status", "Disability status", "Pronouns"]:
        p = ProfileView(Profile(demographics=Demographics(race_ethnicity="Asian", veteran_status="No",
                                                          disability_status="No", pronouns="she/her")), Settings())
        assert map_fields([F("x", label, FieldKind.text, required=True)], p).answers == [], label
