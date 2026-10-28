"""QA issues #119 (a self-ID select the profile answers was committed to "I don't wish to answer": combined race /
ethnic-group wording and "veteran or active member" wording) and #120 (CC-305 disability options never matched by
exact equality; an unlabelled invalid field reported as "invalid field"). ADR-0070 amendment.

Invariant under test: when the profile holds a non-decline value for a self-ID topic, no decline option is ever
committed; an unmatched question stays blank and is reported. Fictional personas only; local fixtures only (browser
tests skip without a local Chrome; nothing touches the network).
"""
import json

import pytest
from test_apply_react_select import RS_JS, _serve
from test_eeo_selects_97_110 import REPORT, drv  # noqa: F401  # shared browser fixture

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.dom_rules import demographic_pick, disability_class, protected_only, self_id_label_ok
from job_apply_mcp.apply.mapper import DECLINE_MARKERS, map_fields, normalize
from job_apply_mcp.config import Settings
from job_apply_mcp.core.handoff import UNKNOWN_FACT, categorize
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField
from job_apply_mcp.profile import Contact, Demographics, Profile

D = "I don't wish to answer"
# Waymo-like wording (#119)
RACE_LABEL = "Please indicate your race / ethnic group (choose all that apply)"
VET_LABEL = "Please indicate if you are a veteran or active member of the United States Armed Forces."
RACE_OPTS = ["American Indian or Alaska Native", "Asian", "Black or African American", "Hispanic or Latino",
             "Middle Eastern or North African", "Native Hawaiian or Other Pacific Islander",
             "White (Not of Hispanic Origin)", D]
VET_OPTS = ["Yes, I am a veteran or active member", "No, I am not a veteran or active member", D]
# MongoDB-like CC-305 wording (#120)
DIS_OPTS = ["Yes, I have a disability, or have had one in the past",
            "No, I do not have a disability and have not had one in the past", D]
DIS_OPTS_OLD = ["Yes, I have a disability (or previously had a disability)", "No, I don't have a disability",
                "I do not want to answer"]


def _is_decline(o):
    return any(m in normalize(o) for m in DECLINE_MARKERS)


# ---------------------------------------------------------------- pure rules

def test_standard_long_wordings_are_the_plain_self_id_question():
    assert self_id_label_ok(RACE_LABEL, "race")
    assert self_id_label_ok(VET_LABEL, "veteran")
    assert self_id_label_ok("Are you a veteran or an active duty member of the US military?", "veteran")
    # the rewrites are per topic and closed: other questions that merely mention the topic still fail
    assert not self_id_label_ok(RACE_LABEL, "veteran")
    assert not self_id_label_ok("Is your company a veteran or active member owned business?", "veteran")
    assert not self_id_label_ok("Which ethnic group of customers have you served?", "race")


@pytest.mark.parametrize("served,want", [("No", VET_OPTS[1]), ("Yes", VET_OPTS[0]), ("", None)])
def test_is_veteran_answers_veteran_or_active_member(served, want):
    assert demographic_pick("veteran", ["I am not a protected veteran"], VET_OPTS, VET_LABEL, is_veteran=served) == want


def test_protected_veteran_status_alone_never_answers_plain_veteran_wording():
    # ADR-0063/0070: "not a PROTECTED veteran" is not "not a veteran"; without is_veteran nothing matches
    assert demographic_pick("veteran", ["I am not a protected veteran"], VET_OPTS, VET_LABEL) is None
    assert demographic_pick("veteran", ["I identify as one or more of the classifications of protected veteran"],
                            VET_OPTS, VET_LABEL) is None


def test_not_of_hispanic_origin_needs_the_owners_hispanic_answer():
    assert demographic_pick("race", ["White"], RACE_OPTS, RACE_LABEL, hispanic="No") == "White (Not of Hispanic Origin)"
    assert demographic_pick("race", ["White"], RACE_OPTS, RACE_LABEL, hispanic="") is None
    assert demographic_pick("race", ["White"], RACE_OPTS, RACE_LABEL, hispanic="Yes") is None
    assert demographic_pick("race", ["Black or African American"], RACE_OPTS, RACE_LABEL) == "Black or African American"


@pytest.mark.parametrize("text,want", [
    ("Yes, I have a disability, or have had one in the past", "yes"),
    ("Yes, I have a disability (or previously had a disability)", "yes"),
    ("I have a disability", "yes"),
    ("No, I do not have a disability and have not had one in the past", "no"),
    ("No, I don't have a disability", "no"),
    ("I don't have a disability", "no"),
    ("I don't wish to answer", None),
    ("I have a disability accommodation request", None),
    ("No, I do not need an accommodation", None),
    ("Yes", None),
])
def test_disability_answer_classes(text, want):
    assert disability_class(text) == want


@pytest.mark.parametrize("value,opts,want", [
    ("No, I do not have a disability", DIS_OPTS, DIS_OPTS[1]),
    ("No, I don't have a disability", DIS_OPTS, DIS_OPTS[1]),
    ("Yes, I have a disability (or previously had a disability)", DIS_OPTS, DIS_OPTS[0]),
    ("No, I do not have a disability and have not had one in the past", DIS_OPTS_OLD, DIS_OPTS_OLD[1]),
    ("Yes, I have a disability, or have had one in the past", DIS_OPTS_OLD, DIS_OPTS_OLD[0]),
    ("No", DIS_OPTS, DIS_OPTS[1]),                    # bare answer, on the status question only
    ("Something else entirely", DIS_OPTS, None),
])
def test_disability_status_matches_the_cc305_family(value, opts, want):
    assert demographic_pick("disability", [value], opts, "Disability Status") == want


def test_bare_disability_answer_needs_the_status_question():
    assert demographic_pick("disability", ["No"], DIS_OPTS, "Please indicate your disability status") is None


def test_protected_only():
    assert protected_only(["Please select", "I am a protected veteran", "I am not a protected veteran", D])
    assert not protected_only(VET_OPTS)
    assert not protected_only(["Select...", D])


# Regression (#119): a profile-answered question is never set to decline on the API-planned path either.
PERSONAS = [
    {"gender": "Non-binary", "race_ethnicity": "Native Hawaiian or Other Pacific Islander", "hispanic_latino": "Yes",
     "veteran_status": "I identify as one or more of the classifications of protected veteran",
     "disability_status": "Yes, I have a disability (or previously had a disability)"},
    {"gender": "Woman", "race_ethnicity": "Middle Eastern or North African", "hispanic_latino": "No",
     "veteran_status": "I am not a protected veteran", "disability_status": "No, I don't have a disability"},
]
UNMATCHED = [("Gender", ["Man", "Agender", "Prefer not to say"]),
             ("Race / Ethnicity", ["Asian", "White", "Decline to self-identify"]),
             ("Veteran Status", ["I am a veteran", "I don't wish to answer"]),
             ("Disability", ["Prefer not to say", "Other"]),
             ("Please indicate your race / heritage group of origin", ["Asian", "Decline to self-identify"])]


@pytest.mark.parametrize("demo", PERSONAS)
@pytest.mark.parametrize("label,opts", UNMATCHED)
def test_mapper_never_declines_a_profile_answered_topic(demo, label, opts):
    p = ProfileView(Profile(contact=Contact(first_name="Rin", last_name="Okafor"), demographics=Demographics(**demo)),
                    Settings())
    r = map_fields([FormField(key="x", label=label, kind=FieldKind.select, options=opts, required=True)], p)
    assert not any(_is_decline(str(a.value)) for a in r.answers), (label, r.answers)
    assert [f.key for f in r.unresolved] == ["x"]


def test_mapper_still_declines_when_the_owner_declines():
    p = ProfileView(Profile(contact=Contact(first_name="Rin"), demographics=Demographics()), Settings())
    r = map_fields([FormField(key="x", label="Gender", kind=FieldKind.select, options=["Man", "Woman", D])], p)
    assert [a.value for a in r.answers] == [D]


# ---------------------------------------------------------------- browser fixtures

def _survey(race_label=RACE_LABEL, vet_label=VET_LABEL, dis=DIS_OPTS):
    return ("""<html><body><form id=application-form>
<label for=first_name>First Name *</label><input id=first_name name=first_name required>
<div id=demographic-section class=demographic--container><h3>Globex Candidate Demographic Survey</h3>
<label for=5001>Please indicate your gender *</label><div id=5001_wrap></div>
<label for=5002>__RACE__ *</label><div id=5002_wrap></div>
<label for=5003>__VET__ *</label><div id=5003_wrap></div>
<label for=5004>Disability Status *</label><div id=5004_wrap></div></div>
<button type=submit>Submit application</button></form>
<script>__RS__
rs('5001', ['Male', 'Female', 'Non-Binary', __D__]); rs('5002', __RACEO__); rs('5003', __VETO__); rs('5004', __DISO__);
['5001', '5002', '5003', '5004'].forEach(i => document.getElementById(i).setAttribute('aria-required', 'true'));
__REPORT__
</script></body></html>""".replace("__RS__", RS_JS).replace("__REPORT__", REPORT).replace("__RACE__", race_label)
            .replace("__VET__", vet_label).replace("__D__", json.dumps(D)).replace("__RACEO__", json.dumps(RACE_OPTS))
            .replace("__VETO__", json.dumps(VET_OPTS)).replace("__DISO__", json.dumps(dis)))


FORM = ApplicationForm(ref="greenhouse:globex:61", fields=[
    FormField(key="first_name", label="First Name", kind=FieldKind.text, required=True)])
# a fictional persona, unrelated to any real profile
PERSONA = {"demographic:gender": "Female", "demographic:race": "Black or African American",
           "demographic:hispanic": "No", "demographic:veteran": "I am not a protected veteran",
           "demographic:is_veteran": "Yes", "demographic:disability": "No, I don't have a disability"}


async def _run(drv, html, answers):  # noqa: F811
    srv, posts = _serve(html)
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/globex/jobs/61", FORM,
                                    {"first_name": "Noor", **answers}, {}, dry_run=True)
    return res, json.loads([p for p in posts if p[0] == "/state"][-1][1])


async def test_waymo_like_survey_gets_the_profile_answers(drv):  # noqa: F811
    res, st = await _run(drv, _survey(), PERSONA)
    assert res.status == "dry_run" and not res.unresolved, (res.detail, res.unresolved)
    assert st["5001"] == "Female" and st["5002"] == "Black or African American"
    assert st["5003"] == "Yes, I am a veteran or active member"
    assert st["5004"] == "No, I do not have a disability and have not had one in the past"     # #120


async def test_unknown_served_status_is_never_declined(drv):  # noqa: F811
    # veteran_status is answered in protected wording, is_veteran unknown: no option corresponds -> blank + reported
    ans = {k: v for k, v in PERSONA.items() if k != "demographic:is_veteran"}
    res, st = await _run(drv, _survey(), ans)
    assert st["5003"] == "" and res.status == "incomplete", (st, res.detail)
    assert any(u.startswith(VET_LABEL) and "(unknown_fact:" in u for u in res.unresolved), res.unresolved
    assert categorize(res.status, res.detail, unresolved=res.unresolved) == UNKNOWN_FACT


async def test_unrecognised_wording_with_a_profile_answer_is_a_gap_not_a_decline(drv):  # noqa: F811
    lab = "Please indicate your race and national heritage group"
    res, st = await _run(drv, _survey(race_label=lab), PERSONA)
    assert st["5002"] == "" and res.status == "incomplete", (st, res.detail)
    assert any(u.startswith(lab) and "(tool_gap:" in u for u in res.unresolved), res.unresolved


async def test_no_profile_value_still_declines_a_required_select(drv):  # noqa: F811
    res, st = await _run(drv, _survey(), {})
    assert st["5001"] == st["5002"] == st["5003"] == st["5004"] == D, st
    assert res.status == "dry_run" and not res.unresolved, (res.detail, res.unresolved)


@pytest.mark.parametrize("value,want", [("Yes, I have a disability", DIS_OPTS[0]),
                                        ("No, I do not have a disability", DIS_OPTS[1]),
                                        ("Decline to self-identify", D)])
async def test_mongodb_like_disability_select(drv, value, want):  # noqa: F811
    res, st = await _run(drv, _survey(), {**PERSONA, "demographic:disability": value})
    assert st["5004"] == want and res.status == "dry_run", (st, res.detail)


async def test_native_invalid_field_is_named(drv):  # noqa: F811
    html = """<html><body><form id=application-form>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<div class=field><label>Portfolio passcode</label><div class=shell><input required aria-hidden="true" tabindex=-1>
</div></div><button type=submit>Submit application</button></form><script>__REPORT__</script></body></html>
""".replace("__REPORT__", REPORT)
    res, _ = await _run(drv, html, {})
    assert res.status == "incomplete" and "Portfolio passcode (native validation)" in res.detail, res.detail
