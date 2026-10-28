"""Issue #125 (ADR-0073 amendment): a closed "Do you acknowledge that this is a hybrid / onsite role (based) in
<known places> ..." template. Yes only when the schedule fact is true AND the profile city / region is a named place
or willing_to_relocate is true; anything else is unresolved (never No). Fictional profile (Springfield, IL); the
places are generic public ones."""
import pytest
from test_auth_wordings_121 import run, unresolved, view

# the posting's wording, shape-for-shape (places, "and/or", the office-days tail)
CHIME_LIKE = ("Do you acknowledge that this is a hybrid role based in San Francisco, New York and/or Chicago and you "
              "will be required to come into the office four days a week?")
LOCS = ["Chicago, IL, USA", "New York, NY, USA", "San Francisco, CA, USA"]

ALL_YES = {"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": True}
ALL_NO = {"open_to_onsite": False, "open_to_hybrid": False, "willing_to_relocate": False}
HYBRID_NO_MOVE = {"open_to_onsite": False, "open_to_hybrid": True, "willing_to_relocate": False}
MOVE_NO_HYBRID = {"open_to_onsite": True, "open_to_hybrid": False, "willing_to_relocate": True}
MIXED = [ALL_NO, HYBRID_NO_MOVE, MOVE_NO_HYBRID]

YES_LABELS = [
    CHIME_LIKE,
    "Do you acknowledge that this is a hybrid role based in Chicago?",
    "Do you understand that this is a hybrid position in San Francisco or New York?",
    "Do you acknowledge this is a hybrid role in Chicago with 3 days a week in office? (Yes/No)",
    ("Do you acknowledge that this is a hybrid role based in New York and you will be required to come into our "
     "office 2 days a week?"),
]


@pytest.mark.parametrize("label", YES_LABELS)
def test_acknowledgement_answers_yes_with_hybrid_and_relocation(label):
    assert run(label, view(prefs=ALL_YES), locations=LOCS)[0] == "Yes"


def test_chime_like_wording_answers_yes():
    v, r = run(CHIME_LIKE, view(prefs=ALL_YES), locations=LOCS)
    assert v == "Yes" and not r.unresolved and "q" not in r.reasons


def test_profile_city_among_the_places_needs_no_relocation():
    label = "Do you acknowledge that this is a hybrid role based in Springfield or Chicago?"
    assert run(label, view(prefs=HYBRID_NO_MOVE))[0] == "Yes"
    # region (state code IL) by its name
    assert run("Do you acknowledge that this is a hybrid role based in Illinois?", view(prefs=HYBRID_NO_MOVE))[0] == "Yes"


@pytest.mark.parametrize("prefs", MIXED)
@pytest.mark.parametrize("label", YES_LABELS)
def test_no_and_mixed_profiles_stay_unresolved_never_no(label, prefs):
    why = unresolved(label, view(prefs=prefs), locations=LOCS)
    assert why["category"] == "unknown_fact"
    assert "open_to_hybrid" in why["reason"] and "willing_to_relocate" in why["reason"]


def test_hybrid_false_in_own_city_stays_unresolved():
    label = "Do you acknowledge that this is a hybrid role based in Springfield?"
    unresolved(label, view(prefs={"open_to_hybrid": False, "willing_to_relocate": False}))


def test_missing_facts_are_recorded_as_gaps():
    v, r = run(CHIME_LIKE, view(prefs={}), locations=LOCS)
    facts = {g["fact"] for g in r.profile_gaps}
    assert v is None and {"preferences.open_to_hybrid", "preferences.willing_to_relocate"} <= facts


def test_onsite_acknowledgement_reads_open_to_onsite():
    label = "Do you acknowledge that this is an onsite role in Chicago?"
    assert run(label, view(prefs=MOVE_NO_HYBRID), locations=LOCS)[0] == "Yes"
    why = unresolved(label, view(prefs={**ALL_YES, "open_to_onsite": False}), locations=LOCS)
    assert "open_to_onsite" in why["reason"]
    # "five days a week" on a hybrid acknowledgement is on site (schedule_fact)
    five = ("Do you acknowledge that this is a hybrid role based in Chicago and you will be required to come into the "
            "office five days a week?")
    assert run(five, view(prefs=MOVE_NO_HYBRID), locations=LOCS)[0] == "Yes"
    unresolved(five, view(prefs=HYBRID_NO_MOVE | {"willing_to_relocate": True}), locations=LOCS)


def test_range_to_five_days_stays_with_the_owner():
    label = ("Do you acknowledge that this is a hybrid role based in Chicago and you will be required to come into "
             "the office 4 or 5 days a week?")
    assert "owner" in unresolved(label, view(prefs=ALL_YES), locations=LOCS)["reason"]


@pytest.mark.parametrize("prefs", [ALL_YES, *MIXED])
@pytest.mark.parametrize("label", [
    # negated / aversive
    "Do you not acknowledge that this is a hybrid role based in Chicago?",
    "Do you acknowledge that this is not a hybrid role based in Chicago?",
    "Do you acknowledge that this is a hybrid role based in Chicago and you will never come into the office?",
    "Do you acknowledge that this is a hybrid role based in Chicago, which you would rather avoid?",
    "Do you acknowledge that this is a hybrid role based in Chicago and you mind coming into the office?",
    # compound
    "Do you acknowledge that this is a hybrid role based in Chicago and that the salary is fixed?",
    "Do you acknowledge that this is a hybrid role based in Chicago and agree to a background check?",
    "Do you acknowledge that this is a hybrid role based in Chicago and you will be required to travel monthly?",
    "If you are not local to Chicago, do you acknowledge that this is a hybrid role based in Chicago?",
    # unknown place / reference
    "Do you acknowledge that this is a hybrid role based in Gotham?",
    "Do you acknowledge that this is a hybrid role based in Chicago or Gotham?",
    "Do you acknowledge that this is a hybrid role based in the area?",
    "Do you acknowledge that this is a hybrid role based anywhere?",
])
def test_negated_compound_and_unknown_place_stay_unresolved(label, prefs):
    unresolved(label, view(prefs=prefs), locations=LOCS)


def test_our_office_tail_does_not_take_the_office_rule():
    # "our office" would otherwise route to the ADR-0055 office rule, which can answer No
    label = ("Do you acknowledge that this is a hybrid role based in New York and you will be required to come into "
             "our office 2 days a week?")
    unresolved(label, view(prefs=ALL_NO), locations=LOCS)


def test_deterministic():
    assert {run(CHIME_LIKE, view(prefs=ALL_YES), locations=LOCS)[0] for _ in range(5)} == {"Yes"}
