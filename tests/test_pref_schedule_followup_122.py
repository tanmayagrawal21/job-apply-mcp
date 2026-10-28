"""PR #122 review follow-up (ADR-0073 amendment): five office days a week is on site, not hybrid; a conditional
"If you are not local to <place>, ..." office question needs willing_to_relocate too. Fictional profile; the
places are generic public ones."""
import pytest
from test_auth_wordings_121 import run, unresolved, view

LOCS = ["Riverside, OR"]
ALL_YES = {"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": True}
ALL_NO = {"open_to_onsite": False, "open_to_hybrid": False, "willing_to_relocate": False}
HYBRID_ONLY = {"open_to_onsite": False, "open_to_hybrid": True, "willing_to_relocate": True}
ONSITE_ONLY = {"open_to_onsite": True, "open_to_hybrid": False, "willing_to_relocate": True}


# ---------------------------------------------------------------- (a) hybrid "5 days a week" is on site
@pytest.mark.parametrize("label", [
    "Are you open to a hybrid schedule 5 days a week?",
    "Are you comfortable with a hybrid schedule five days a week?",
    "Can you work hybrid 5 days a week in Riverside?",
])
def test_hybrid_five_days_reads_open_to_onsite(label):
    assert run(label, view(prefs=ALL_YES), locations=LOCS)[0] == "Yes"
    assert run(label, view(prefs=ALL_NO), locations=LOCS)[0] == "No"
    assert run(label, view(prefs=HYBRID_ONLY), locations=LOCS)[0] == "No"     # was "Yes" from open_to_hybrid
    assert run(label, view(prefs=ONSITE_ONLY), locations=LOCS)[0] == "Yes"


def test_plain_hybrid_and_onsite_are_unchanged():
    assert run("Are you open to a hybrid schedule?", view(prefs=HYBRID_ONLY))[0] == "Yes"
    assert run("Are you open to a hybrid schedule 3 days a week?", view(prefs=HYBRID_ONLY))[0] == "Yes"
    assert run("Are you able to work onsite?", view(prefs=HYBRID_ONLY))[0] == "No"
    assert run("Are you able to work onsite 5 days a week?", view(prefs=ONSITE_ONLY))[0] == "Yes"


@pytest.mark.parametrize("prefs", [ALL_YES, ALL_NO, HYBRID_ONLY])
@pytest.mark.parametrize("label", [
    "Are you open to a hybrid schedule 4 or 5 days a week?",
    "Are you open to a hybrid schedule 3 to 5 days a week?",
    "Are you open to a hybrid role full time?",
])
def test_ambiguous_schedules_stay_with_the_owner(label, prefs):
    why = unresolved(label, view(prefs=prefs))
    assert why["category"] == "unknown_fact" and "owner" in why["reason"]


# ---------------------------------------------------------------- (b) "If you are not local ..., onsite?"
COND = [
    "If you are not local to Riverside, are you comfortable working onsite?",
    "If you are not local to the area, are you able to work onsite?",
    "If you are not based in Chicago, are you open to a hybrid schedule?",
]


@pytest.mark.parametrize("label", COND)
def test_conditional_office_question_needs_relocation_for_yes(label):
    assert run(label, view(prefs=ALL_YES), locations=LOCS)[0] == "Yes"


@pytest.mark.parametrize("prefs", [
    ALL_NO,
    {"open_to_onsite": True, "open_to_hybrid": True, "willing_to_relocate": False},    # office yes, relocation no
    {"open_to_onsite": False, "open_to_hybrid": False, "willing_to_relocate": True},   # relocation yes, office no
])
@pytest.mark.parametrize("label", COND)
def test_conditional_office_question_otherwise_unresolved(label, prefs):
    why = unresolved(label, view(prefs=prefs), locations=LOCS)
    assert why["category"] == "unknown_fact" and "willing_to_relocate" in why["reason"]


def test_conditional_office_question_with_missing_relocation_fact_records_a_gap():
    label = COND[0]
    v, r = run(label, view(prefs={"open_to_onsite": True}), locations=LOCS)
    assert v is None and any(g["fact"] == "preferences.willing_to_relocate" for g in r.profile_gaps)


def test_conditional_relocation_question_is_unchanged():
    label = "If you are not local to the area, are you willing to relocate?"
    assert run(label, view(prefs=ALL_YES), locations=LOCS)[0] == "Yes"
    assert run(label, view(prefs=ALL_NO), locations=LOCS)[0] == "No"
