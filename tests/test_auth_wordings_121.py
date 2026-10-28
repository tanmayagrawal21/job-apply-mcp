"""ADR-0073 (#121): authorization / sponsorship / relocation wordings answered deterministically from the structured
[authorization] and [preferences] facts, and a category + non-empty reason for every one that stays unresolved.
Fictional profile and companies only (Alex Rivera, Springfield; Northwind, Quillon, Brightpath)."""
import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import company_names, job_country, map_fields
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.profile import Authorization, Contact, CountryAuthorization, Preferences, Profile

YN = ["Yes", "No"]


def view(auth=None, prefs=None, countries=None):
    a = {"home_country": "US", "work_authorized": True, "requires_sponsorship": True, **(auth or {})}
    a = {k: v for k, v in a.items() if v is not None}
    return ProfileView(Profile(
        contact=Contact(first_name="Alex", last_name="Rivera", city="Springfield", region="IL", country="US"),
        authorization=Authorization(**a, countries={k: CountryAuthorization(**v) for k, v in (countries or {}).items()}),
        preferences=Preferences(**(prefs or {}))), Settings())


SPONSOR = view()                                                    # authorized, needs sponsorship
NO_SPONSOR = view({"requires_sponsorship": False})                  # authorized, no sponsorship
UNKNOWN = view({"requires_sponsorship": None})                      # sponsorship fact missing


def run(label, p, *, options=YN, kind=FieldKind.select, jc="US", company="Northwind", locations=()):
    f = F("q", label, kind, options=options)
    r = map_fields([f], p, company=company, job_country=jc, job_locations=list(locations))
    got = {a.key: a.value for a in r.answers}
    return got.get("q"), r


def unresolved(label, p, **kw):
    v, r = run(label, p, **kw)
    assert v is None and [f.key for f in r.unresolved] == ["q"], (label, v)
    why = r.reasons["q"]
    assert why["category"] in ("unknown_fact", "tool_gap") and why["reason"].strip()
    return why


# ---------------------------------------------------------------- the issue's wordings, generalized
ANY_KIND = ("Will you, now or in the future, require an employment visa or sponsorship of any kind in the country "
            "where this job is located?")
VISA_STATUS = "Will you require sponsorship for employment visa status now or in the future?"
WORK_PERMIT = "Will you require Northwind to sponsor you for a US work permit now or in the future?"
JOB_PLACE_AUTH = "Are you legally authorized to work in the country where this position is located?"
EITHER = "Do you require sponsorship to work in Canada or the United States?"
LAWFULLY = "Are you authorized to work lawfully in the United States for Quillon?"


@pytest.mark.parametrize("label", [ANY_KIND, VISA_STATUS, WORK_PERMIT,
                                   "Will you now or in the future require visa sponsorship for employment at Northwind?"])
def test_sponsorship_wordings_follow_requires_sponsorship(label):
    assert run(label, SPONSOR)[0] == "Yes"
    assert run(label, NO_SPONSOR)[0] == "No"
    assert unresolved(label, UNKNOWN)["category"] == "unknown_fact"


def test_any_kind_wording_as_a_boolean():
    assert run(ANY_KIND, SPONSOR, options=[], kind=FieldKind.boolean)[0] is True
    assert run(ANY_KIND, NO_SPONSOR, options=[], kind=FieldKind.boolean)[0] is False


def test_us_work_permit_still_needs_a_confirmed_us_job():
    # ADR-0053/0062: the US named only as the permit's country is not the place of work; the reason says so
    why = unresolved(WORK_PERMIT, SPONSOR, jc=None)
    assert why["category"] == "unknown_fact" and "permit" in why["reason"]
    unresolved("Will Northwind need to sponsor your U.S. work authorization?", NO_SPONSOR, jc=None)
    unresolved(WORK_PERMIT, SPONSOR, jc="CA")                       # a job confirmed elsewhere


def test_lowercase_us_is_a_pronoun_not_the_country():
    # "for us work" (lower case) never names the United States
    why = unresolved("Will you require us to sponsor you for us work now or in the future?", SPONSOR, jc=None)
    assert "names no country" in why["reason"]


@pytest.mark.parametrize("jc, want", [("US", "Yes")])
def test_job_place_authorization_on_a_confirmed_home_country_job(jc, want):
    assert run(JOB_PLACE_AUTH, SPONSOR, jc=jc)[0] == want
    assert run(JOB_PLACE_AUTH, view({"work_authorized": False}), jc=jc)[0] == "No"


def test_job_place_authorization_without_a_confirmed_job_country_is_an_unknown_fact():
    # the same wording at a posting whose country is not confirmed (e.g. "Remote - USA" with a coded office)
    why = unresolved(JOB_PLACE_AUTH, SPONSOR, jc=None)
    assert why["category"] == "unknown_fact" and "job's country" in why["reason"]
    why = unresolved(VISA_STATUS, SPONSOR, jc=None)
    assert "names no country" in why["reason"]


def test_job_place_authorization_abroad_needs_that_countrys_facts():
    why = unresolved(JOB_PLACE_AUTH, SPONSOR, jc="DE")
    assert why["category"] == "unknown_fact" and "authorization.DE" in why["reason"]
    p = view(countries={"DE": {"work_authorized": True}})
    assert run(JOB_PLACE_AUTH, p, jc="DE")[0] == "Yes"


def test_either_country_sponsorship():
    opts = ["Yes", "No "]                                           # a trailing space in the option text
    assert run(EITHER, SPONSOR, options=opts)[0] == "Yes"           # required in one of them
    both_no = view({"requires_sponsorship": False}, countries={"CA": {"requires_sponsorship": False}})
    assert run(EITHER, both_no, options=opts)[0] == "No "           # required in neither
    why = unresolved(EITHER, NO_SPONSOR, options=opts)              # CA unknown, US no: not decidable
    assert "authorization.CA" in why["reason"]
    _, r = run(EITHER, NO_SPONSOR, options=opts)
    assert {"country": "CA", "fact": "requires_sponsorship", "key": "q"} in r.profile_gaps


@pytest.mark.parametrize("label", [
    "Do you require sponsorship to work in Canada and the United States?",           # "and": both, not either
    "Are you authorized to work in Canada or the United States?",                    # authorization: either or both
    "Do you currently require sponsorship to work in Canada or the United States?",  # now only
    "Do you require sponsorship to work in Canada or the United States without relocating?",
])
def test_either_country_rule_is_narrow(label):
    both = view(countries={"CA": {"requires_sponsorship": True, "work_authorized": True}})
    unresolved(label, both)


def test_authorized_lawfully_in_the_united_states_for_a_board_token_company():
    assert run(LAWFULLY, SPONSOR, jc=None, company="getquillon")[0] == "Yes"
    assert run(LAWFULLY, view({"work_authorized": False}), jc=None, company="getquillon")[0] == "No"
    # an unrelated company word stays a non-vocabulary word
    unresolved("Are you authorized to work lawfully in the United States for Brightpath?", SPONSOR, jc=None,
               company="getquillon")


def test_company_names_from_a_board_token():
    assert "quillon" in company_names(LAWFULLY, "getquillon")
    assert company_names("authorized to work in Denver for us?", "acmedenver") == ["acmedenver"]   # a place word
    assert company_names("authorized to work for Quill?", "getquillon") == ["getquillon"]          # under half
    assert company_names("for Brightpath", "Brightpath Labs", ["BPL"]) == ["Brightpath Labs", "BPL"]
    # control: state, country, region and generic words are never taken from a board token
    assert company_names("Are you authorized to work in Texas?", "gettexas") == ["gettexas"]
    assert company_names("Can you work in our office?", "acmeoffice") == ["acmeoffice"]
    assert company_names("authorized to work in Canada?", "joincanada") == ["joincanada"]
    assert company_names("authorized to work in New Jersey?", "jerseyco") == ["jerseyco"]


@pytest.mark.parametrize("label, board", [
    ("Will you require sponsorship to work outside the United States?", "outsideinc"),
    ("Will you require sponsorship to work outside?", "outsideinc"),
    ("Will you require sponsorship to work abroad?", "goabroad"),
    ("Will you require sponsorship to work overseas?", "overseasjobs"),
    ("Will you require sponsorship to work Abroad or for abroad clients?", "goabroad"),   # one lower-case use
])
def test_scope_words_are_never_the_company_word(label, board):
    # reviewer: stripping "outside" / "abroad" would answer a non-home question with the home-country fact
    unresolved(label, NO_SPONSOR, company=board)
    assert board == company_names(label, board)[0] and len(company_names(label, board)) == 1


def test_company_word_must_be_capitalized():
    assert company_names("authorized to work for quillon?", "getquillon") == ["getquillon"]
    assert "quillon" in company_names("authorized to work for Quillon?", "getquillon")


# ---------------------------------------------------------------- negation and now / future variants
@pytest.mark.parametrize("label, sponsor, no_sponsor", [
    ("Are you authorized to work in the United States without sponsorship?", "No", "Yes"),
    ("Are you legally authorized to work in the US without the need for visa sponsorship now or in the future?",
     "No", "Yes"),
    ("Do you not require visa sponsorship to work in the United States?", "No", "Yes"),
    ("Will you now or in the future require sponsorship to work in the US?", "Yes", "No"),
    ("Will you, now or in the future, require sponsorship to work in the United States?", "Yes", "No"),
])
def test_negation_and_future_variants(label, sponsor, no_sponsor):
    assert run(label, SPONSOR, jc=None)[0] == sponsor
    assert run(label, NO_SPONSOR, jc=None)[0] == no_sponsor


def test_now_only_needs_the_now_fact():
    label = "Do you currently require visa sponsorship to work in the United States?"
    why = unresolved(label, SPONSOR, jc=None)
    assert "requires_sponsorship_now" in why["reason"]
    assert run(label, view({"requires_sponsorship_now": False}), jc=None)[0] == "No"
    assert run(label, NO_SPONSOR, jc=None)[0] == "No"               # never in the future means not now either


# ---------------------------------------------------------------- never beyond what the profile states
@pytest.mark.parametrize("label", [
    "Are you authorized to work in the US? (U.S. persons as defined under ITAR/EAR)",
    "Are you a U.S. citizen or permanent resident authorized to work in the US?",
    "Are you eligible to work in the US and able to access export-controlled technology?",
])
def test_export_control_and_citizenship_wordings_stay_with_the_owner(label):
    assert unresolved(label, SPONSOR, jc="US")["category"] == "unknown_fact"


@pytest.mark.parametrize("label", [
    "I certify that I am a US citizen and will provide proof of citizenship.",
    "Please confirm the visa type you will hold on your start date.",
    "Will you require sponsorship for an H-1B transfer or cap-exempt petition with Northwind's affiliate in Canada?",
])
def test_specific_fact_certifications_are_not_answered(label):
    unresolved(label, SPONSOR, options=[], kind=FieldKind.text)


@pytest.mark.parametrize("label", ["Are you not a US citizen?", "Are you a US citizen or not?",
                                   "Are you a US citizen or a dual citizen?"])
def test_citizen_fact_never_answers_a_negated_or_compound_question(label):
    for c in (True, False):
        assert run(label, view({"citizen": c}))[0] is None


def test_unmatched_us_wording_is_a_tool_gap_with_a_reason():
    why = unresolved("Are you authorized to work in the United States on a cap-exempt basis at our lab?", SPONSOR)
    assert why["category"] == "tool_gap" and "not matched" in why["reason"]


# ---------------------------------------------------------------- relocation / on-site from [preferences]
HQ = "Can you work from our Springfield HQ (4 days a week)?"
BAY = "Are you currently based in the Riverside Area, or ready to relocate?"


def test_office_hq_days_question_from_preferences():
    p = view(prefs={"open_to_hybrid": True, "willing_to_relocate": True})
    assert run(HQ, p, options=[], kind=FieldKind.text, locations=["Riverside, OR"])[0] == "Yes"
    assert run(HQ, p, locations=["Riverside, OR"])[0] == "Yes"
    assert run(HQ, view(prefs={"open_to_hybrid": False}), locations=["Riverside, OR"])[0] == "No"
    why = unresolved(HQ, view(), locations=["Riverside, OR"])
    assert "preferences.open_to_hybrid" in why["reason"]


@pytest.mark.parametrize("label", [
    "Can you not work from our office?",
    "Can you not work from our Springfield HQ (4 days a week)?",
    "Would you rather not work from our Riverside office?",
    "Would you be unable to work from our Springfield HQ?",
    "Would you never work from our office?",
    "Would you prefer not to work from our office?",
    "Are you not willing to work on site at our Riverside HQ?",
    "Are you unable to work on site at our Riverside office 5 days a week?",
])
def test_office_question_with_a_negation_is_not_answered(label):
    # reviewer: the free slot before "work" never absorbs a negation (wrong polarity)
    p = view(prefs={"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": True})
    v, _ = run(label, p, locations=["Riverside, OR"])
    assert v is None


AVERSE = ["mind", "dislike", "hate", "resent", "object to", "resist", "oppose", "hesitate to", "struggle to",
          "be against", "hardly", "barely", "only", "nowhere", "neither", "not", "never", "rather not"]


@pytest.mark.parametrize("word", AVERSE)
@pytest.mark.parametrize("form", ["Would you {w} working from our office?", "Would you {w} work from our Riverside HQ?",
                                  "Can you {w} work from our Riverside office (4 days a week)?"])
def test_office_slot_is_a_closed_list(word, form):
    p = view(prefs={"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": True})
    assert run(form.format(w=word), p, locations=["Riverside, OR"])[0] is None


@pytest.mark.parametrize("label", ["Are you not able to work onsite?", "Are you unable to work on site?",
                                   "Would you mind working onsite?", "Would you hate working in office?",
                                   "Are you not open to a hybrid schedule?", "Are you not willing to relocate?",
                                   "Would you hesitate to relocate?"])
def test_negated_preference_questions_stay_with_the_owner(label):
    # control: ADR-0055 open-wording aliases flipped polarity ("not able to work onsite" -> Yes)
    p = view(prefs={"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": True})
    why = unresolved(label, p)
    assert why["category"] == "unknown_fact"


@pytest.mark.parametrize("adverb", ["regularly", "also", "ever", "possibly", "please", "consistently",
                                    "occasionally", "sometimes", "comfortably", "reliably"])
def test_whitelisted_adverbs_still_answer(adverb):
    p = view(prefs={"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": True})
    assert run(f"Can you {adverb} work from our Riverside HQ?", p, locations=["Riverside, OR"])[0] == "Yes"
    assert run("Are you able to work onsite?", p)[0] == "Yes"


def test_leading_residence_condition_is_not_a_negation():
    p = view(prefs={"willing_to_relocate": True})
    assert run("If you are not local to the area, are you willing to relocate?", p)[0] == "Yes"
    unresolved("If you are not local to the area, are you not willing to relocate?", p)


def test_scope_word_company_name_is_never_stripped():
    # control: a company literally named "Outside" must not strip its own name from "work Outside the US"
    assert company_names("Will you require sponsorship to work Outside the United States?", "Outside Inc") == []
    unresolved("Will you require sponsorship to work Outside the United States?", NO_SPONSOR, company="Outside Inc")
    unresolved("Will you require sponsorship to work Outside?", NO_SPONSOR, company="outside")


def test_office_question_slot_still_takes_plain_words():
    p = view(prefs={"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": True})
    assert run("Could you regularly work from our Riverside HQ?", p, locations=["Riverside, OR"])[0] == "Yes"


def test_based_or_ready_to_relocate_from_preferences():
    assert run(BAY, view(prefs={"willing_to_relocate": True}), options=[], kind=FieldKind.text, locations=["Riverside, CA"])[0] == "Yes"
    why = unresolved(BAY, view(), options=[], kind=FieldKind.text, locations=["Riverside, CA"])
    assert "willing_to_relocate" in why["reason"]


# ---------------------------------------------------------------- job country: a shortened location entry
def test_shortened_location_beside_its_full_entry_confirms_the_country():
    assert job_country("Springfield, MA", ["US"], ["Springfield, MA, United States"]) == "US"
    assert job_country("Springfield, MA", ["US"], []) is None                    # alone, "MA" stays ambiguous
    assert job_country("Springfield, MA", ["US"], ["Shelbyville, MA, United States"]) is None
    # control: a one-part location never confirms through a longer entry
    assert job_country("Springfield", ["US"], ["Springfield, IL, United States"]) is None
    assert job_country("Paris", ["US"], ["Paris, Texas, United States"]) is None
    assert job_country("Paris, Texas", ["US"], ["Paris, Texas, United States"]) == "US"


# ---------------------------------------------------------------- determinism (the "resolves on another posting" note)
def test_resolution_depends_only_on_the_posting_not_on_batch_order():
    p, fields = SPONSOR, [F("a", JOB_PLACE_AUTH, FieldKind.select, options=YN),
                          F("b", VISA_STATUS, FieldKind.select, options=YN)]
    first = map_fields(fields, p, company="Northwind", job_country=None)
    map_fields(fields, p, company="Brightpath", job_country="US")            # another posting in the same batch
    again = map_fields(fields, p, company="Northwind", job_country=None)
    assert [f.key for f in first.unresolved] == [f.key for f in again.unresolved] == ["a", "b"]
    assert first.reasons == again.reasons
    confirmed = map_fields(fields, p, company="Northwind", job_country="US")
    assert {a.key: a.value for a in confirmed.answers} == {"a": "Yes", "b": "Yes"}


# ---------------------------------------------------------------- the plan's client task carries the reason
async def test_plan_client_task_has_reason_and_category():
    from apply_fakes import POSTING, FakeAdapter, FakeGates, FakeStore, settings

    from job_apply_mcp.apply.engine import ApplyEngine
    from job_apply_mcp.core.models import ApplicationForm
    f = F("q_auth", VISA_STATUS, FieldKind.select, options=YN)
    g = F("q_other", "What is your favourite pastry?", FieldKind.text)

    class One(FakeAdapter):
        async def get_form(self, ref):
            return ApplicationForm(ref=ref, submit_via=["api"], fields=[f, g])
    e = ApplyEngine(settings(answers__autonomy="strict"), resolve_adapter=lambda ref: One(), profile=SPONSOR,
                    router=None, gates=FakeGates(), store=FakeStore())
    out = await e.plan(POSTING.ref)                          # POSTING has no confirmed country
    tasks = {t["key"]: t for t in out.client_tasks}
    assert tasks["q_auth"]["category"] == "unknown_fact" and "country" in tasks["q_auth"]["reason"]
    assert tasks["q_other"]["category"] == "unknown_fact" and tasks["q_other"]["reason"]


# ---------------------------------------------------------------- closed positive preference templates (control)
PREFS = {"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": True}


@pytest.mark.parametrize("label", [
    "If you are not local and do not want to relocate, are you willing to relocate?",
    "If you are not local to the area and would not move, are you willing to relocate?",
    "If you are not based in Riverside but do not plan to move, are you open to relocating?",
    "Are you averse to relocating?", "Are you unhappy to relocate?", "Are you uncomfortable working onsite?",
    "Are you uneasy about a hybrid schedule?", "Do you dread working in office?", "Are you hostile to relocating?",
    "Are you disinclined to relocate?", "Would you decline to work onsite?", "Would you rule out relocating?",
    "Is relocating a deal-breaker for you?", "Are you willing to relocate, or not?",
    "Are you willing to relocate if we do not pay for it?", "Are you willing to relocate to the moon eventually?",
])
def test_preference_wording_outside_the_templates_is_unresolved(label):
    why = unresolved(label, view(prefs=PREFS))
    assert why["category"] == "unknown_fact" and why["reason"]


@pytest.mark.parametrize("label, want", [
    ("Willing to relocate?", "Yes"), ("Willing to relocate? (Yes/No)", "Yes"), ("Are you able to relocate?", "Yes"),
    ("Are you willing to relocate for this role?", "Yes"), ("Are you willing to relocate to New York?", "Yes"),
    ("If you are not local to the area, are you willing to relocate?", "Yes"),
    ("If you are not located in Chicago or NY, are you open to relocating?", "Yes"),
    ("Are you currently based in the Riverside Area, or ready to relocate?", "Yes"),
    ("Are you open to a hybrid schedule?", "Yes"), ("Would you be comfortable with a hybrid schedule?", "Yes"),
    ("Are you open to working in a hybrid environment, 3 days a week in Chicago?", "Yes"),
    ("Are you willing and able to commit to the hybrid policy if hired?", "Yes"),
    ("Are you comfortable working onsite 3 days a week?", "Yes"),
    ("Are you open to working out of our Torrance (LA) Office 5 Days a Week?", "Yes"),
    ("Will you be able to work from our office in Union Square, NYC?", "Yes"),
    ("Can you work from our Riverside HQ (4 days a week)?", "Yes"),
])
def test_common_positive_preference_wordings_still_answer(label, want):
    locs = ["Riverside, OR", "Union Square, New York City"]
    assert run(label, view(prefs=PREFS), locations=locs)[0] == want


def test_based_or_ready_to_relocate_only_decides_yes():
    label = "Are you currently based in the Riverside Area, or ready to relocate?"
    assert run(label, view(prefs={"willing_to_relocate": False}))[0] is None   # may still be based there


# ---------------------------------------------------------------- control round: whole-label backstop, known places
NO_PREFS = {"open_to_onsite": False, "open_to_hybrid": False, "willing_to_relocate": False}
LOCS = ["Riverside, OR"]


@pytest.mark.parametrize("prefs", [PREFS, NO_PREFS])
@pytest.mark.parametrize("label", [
    "If you are not local to New York And Unwilling, are you willing to relocate?",
    "Would you quit over a hybrid schedule?", "Is a hybrid schedule a showstopper for you?",
    "Would you walk away from an onsite role?", "Is relocating a deal killer?",
    "Would you turn down a hybrid role?", "Do you prefer remote over hybrid?",
    "Would you tolerate working onsite?", "Are you willing to relocate to Over My Dead Body?",
    "Are you willing to relocate to Anywhere But New York?", "Are you willing to relocate to Impossible?",
    "Are you willing to relocate to New York Sadly?", "Can you work from our Impossible HQ?",
    "If you are not local to Nowhere Special, are you willing to relocate?",
])
def test_control_examples_stay_unresolved(label, prefs):
    assert run(label, view(prefs=prefs), locations=LOCS)[0] is None


@pytest.mark.parametrize("label", [
    "Are you willing to relocate to New York?", "Are you willing to relocate to the Bay Area?",
    "Are you willing to relocate to Riverside?", "Are you willing to relocate to Springfield?",   # profile city
    "If you are not located in Chicago or NY, are you open to relocating?",
    "If you are not local to the area, are you willing to relocate?",
    "Can you work from our San Francisco HQ (4 days a week)?", "Can you work from our Riverside office?",
])
def test_known_places_answer_with_yes_and_no_profiles(label):
    assert run(label, view(prefs=PREFS), locations=LOCS)[0] == "Yes"
    assert run(label, view(prefs=NO_PREFS), locations=LOCS)[0] == "No"


def test_unknown_place_without_the_posting_location_is_unresolved():
    assert run("Are you willing to relocate to Riverside?", view(prefs=PREFS), locations=[])[0] is None
    assert run("Are you willing to relocate to OR?", view(prefs=PREFS), locations=[])[0] == "Yes"   # state code
    assert run("Are you willing to relocate to or?", view(prefs=PREFS), locations=[])[0] is None
