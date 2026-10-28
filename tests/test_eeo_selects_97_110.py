"""QA issues #97 (Greenhouse demographic survey: veteran select and demographic-data consent left empty) and #110
(Lever EEO selects left at "Select ..." while the dry run said "filled and verified"). ADR-0070. Fictional values
only; local fixtures only (browser tests are skipped when no local Chrome is installed; nothing touches the network).
"""
import json

import pytest
from apply_fakes import FakeProfile
from test_apply_react_select import RS_JS, _serve

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
from job_apply_mcp.apply.dom_rules import demographic_pick, is_demographic_consent
from job_apply_mcp.apply.engine import _privacy_opt_in
from job_apply_mcp.config import Settings
from job_apply_mcp.core.handoff import UNKNOWN_FACT, categorize
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField
from job_apply_mcp.profile import Contact, Demographics, Preferences, Profile

# ---------------------------------------------------------------- pure rules

LEVER_RACE = ["Hispanic or Latino", "White (Not Hispanic or Latino)", "Asian (Not Hispanic or Latino)",
              "Two or More Races (Not Hispanic or Latino)", "Decline to self-identify"]


def test_not_hispanic_parenthetical_needs_the_owners_hispanic_answer():
    assert demographic_pick("race", ["Asian"], LEVER_RACE, "Race", hispanic="No") == "Asian (Not Hispanic or Latino)"
    assert demographic_pick("race", ["Asian"], LEVER_RACE, "Race", hispanic="No, I am not Hispanic or Latino") \
        == "Asian (Not Hispanic or Latino)"
    assert demographic_pick("race", ["Asian"], LEVER_RACE, "Race") is None                    # hispanic unknown
    assert demographic_pick("race", ["Asian"], LEVER_RACE, "Race", hispanic="Yes") is None    # contradicts the option
    assert demographic_pick("race", ["Asian"], LEVER_RACE, "Race", hispanic="Decline to self-identify") is None
    # a finer race_detail still wins when the form offers it
    gh = ["East Asian (inclusive of A)", "South Asian (inclusive of B, C)", "White (origins in D)"]
    assert demographic_pick("race", ["South Asian", "Asian"], gh, "Race") == "South Asian (inclusive of B, C)"


def test_contractions_fold():
    opts = ["Yes, I have a disability (or previously had a disability)", "No, I don't have a disability",
            "I don't wish to answer"]
    assert demographic_pick("disability", ["No, I do not have a disability"], opts) == "No, I don't have a disability"
    assert demographic_pick("disability", ["No, I don't have a disability"],
                            ["Yes", "No, I do not have a disability"]) == "No, I do not have a disability"


@pytest.mark.parametrize("opts,served,want", [
    (["I am a veteran", "I am not a veteran", "Decline to self-identify"], "No", "I am not a veteran"),
    (["Yes, I am a veteran", "No, I am not a veteran", "I don't wish to answer"], "No", "No, I am not a veteran"),
    (["Yes, I am a veteran", "No, I am not a veteran", "I don't wish to answer"], "Yes", "Yes, I am a veteran"),
    (["I am a protected veteran", "I am not a protected veteran"], "No", "I am not a protected veteran"),  # implied
    (["I am a protected veteran", "I am not a protected veteran"], "Yes", None),   # served says nothing on protected
    (["I am a veteran", "I am not a veteran"], "", None),                          # unknown: never derived
    (["I am a veteran", "I am not a veteran"], "Decline to self-identify", None),
])
def test_is_veteran_answers_only_the_plain_wording(opts, served, want):
    assert demographic_pick("veteran", [], opts, "Veteran status", is_veteran=served) == want


def test_veteran_status_exact_match_still_wins():
    opts = ["I identify as one or more of the classifications of protected veteran", "I am not a protected veteran",
            "No, I am not a veteran"]
    assert demographic_pick("veteran", ["I am not a protected veteran"], opts, "Veteran Status",
                            is_veteran="No") == "I am not a protected veteran"


@pytest.mark.parametrize("label", [
    ("By checking this box, I consent to Initech processing my responses to the demographic survey above and agree "
     "to the non-compete policy."),
    "I consent to Initech using my responses to the demographic survey above for marketing and SMS messages.",
    "I certify that I am a U.S. citizen and consent to Initech processing my responses to the demographic survey.",
    "I consent to Initech using AI to process my responses to the demographic survey above.",
    "I consent to a criminal background check.",
])
def test_other_certifications_are_never_the_demographic_consent(label):
    assert not is_demographic_consent(label)


def _view(flag, **demo):
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera"),
                               preferences=Preferences(acknowledge_privacy_notices=flag),
                               demographics=Demographics(**demo)), Settings())


@pytest.mark.parametrize("flag,want", [(True, True), (False, False), (None, False)])
def test_privacy_opt_in_reads_the_real_profile_fact(flag, want):
    """#97 root cause: the profile adapter renders a structured True as "Yes", so `is True` never opted in."""
    assert _privacy_opt_in(_view(flag)) is want


def test_privacy_opt_in_ignores_free_text(monkeypatch):
    monkeypatch.setitem(FakeProfile.VALUES, "acknowledge_privacy_notices", "yes please")
    assert _privacy_opt_in(FakeProfile()) is False


def test_is_veteran_is_a_profile_fact():
    assert _view(None, is_veteran="No").lookup("is_veteran") == "No"
    assert _view(None).lookup("is_veteran") in (None, "")


def test_dom_gaps_are_unknown_facts():
    assert categorize("incomplete", "required fields still empty", unresolved=["Veteran Status (unknown_fact: x)"]) \
        == UNKNOWN_FACT


# ---------------------------------------------------------------- browser fixtures


@pytest.fixture
async def drv(tmp_path):
    s = Settings()
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.confirmation_wait_s = 2
    s.browser.hydrate_wait_s = 1
    d = PlaywrightDriver(s)
    try:
        await d._context()
    except Exception as e:  # noqa: BLE001  # no browser available
        pytest.skip(f"no local browser: {e}")
    yield d
    await d.aclose()


REPORT = """function report() { const o = {};
  document.querySelectorAll('[id$=_wrap]').forEach(w => { o[w.id.slice(0, -5)] =
    [...w.querySelectorAll('.select__single-value,.select__multi-value__label')].map(x => x.textContent).join('|'); });
  document.querySelectorAll('select').forEach(s => { o['sel:' + (s.id || s.name)] = s.value ? s.selectedOptions[0].text : ''; });
  document.querySelectorAll('input[type=checkbox]').forEach(c => { if (c.checked) o['checked:' + (c.id || c.value)] = true; });
  fetch('/state', {method: 'POST', body: JSON.stringify(o)}); }
new MutationObserver(report).observe(document.body, {subtree: true, childList: true, attributes: true});
document.addEventListener('change', report); document.addEventListener('input', report); report();
document.querySelectorAll('form').forEach(f => f.onsubmit = e => e.preventDefault());"""


def _state(posts):
    return json.loads([p for p in posts if p[0] == "/state"][-1][1])


# #110: Lever's U.S. EEO block. Native selects addressed only by name, each wrapped in a <label> whose title is a
# .application-label div (no id, no <label for>).
def _lever_select(name, title, opts):
    o = "".join(f'<option value="{t}">{t}</option>' for t in opts)
    return (f'<div class="application-question"><label><div class="application-label">{title}</div>'
            f'<div class="application-field"><div class="application-dropdown"><select name="{name}">'
            f'<option value="">Select ...</option>{o}</select></div></div></label></div>')


LEVER_HTML = ("""<html><body><form id=application-form><ul>
<li class="application-question"><label><div class="application-label">Full name<span class="required">✱</span></div>
<div class="application-field"><input type="text" name="name" required></div></label></li></ul>
<div class="section eeo-section page-centered application-form" data-qa="eeo-section">
<h4>U.S. Equal Employment Opportunity information (Completion is voluntary)</h4>"""
              + _lever_select("eeo[gender]", "Gender", ["Male", "Female", "Decline to self-identify"])
              + _lever_select("eeo[race]", "Race", LEVER_RACE)
              + _lever_select("eeo[veteran]", "Veteran status",
                              ["I am a veteran", "I am not a veteran", "Decline to self-identify"])
              + """</div><button type=submit>Submit application</button></form><script>__REPORT__</script>
</body></html>""").replace("__REPORT__", REPORT)
FORM_LEVER = ApplicationForm(ref="lever:initech:abc", fields=[
    FormField(key="name", label="Full name", kind=FieldKind.text, required=True)])
LEVER_PROFILE = {"demographic:gender": "Female", "demographic:race": "Asian", "demographic:hispanic": "No",
                 "demographic:veteran": "I am not a protected veteran"}


async def _lever(drv, answers):
    srv, posts = _serve(LEVER_HTML)
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/abc/apply", FORM_LEVER,
                                    {"name": "Alex Rivera", **answers}, {}, dry_run=True)
    return res, _state(posts)


async def test_lever_eeo_selects_filled_from_profile(drv):
    res, st = await _lever(drv, {**LEVER_PROFILE, "demographic:is_veteran": "No"})
    assert res.status == "dry_run", res.detail
    assert st["sel:eeo[gender]"] == "Female" and st["sel:eeo[race]"] == "Asian (Not Hispanic or Latino)"
    assert st["sel:eeo[veteran]"] == "I am not a veteran"
    assert not res.unresolved and "NOT verified" not in res.detail


async def test_lever_placeholder_select_is_never_reported_verified(drv):
    """No is_veteran: "not a protected veteran" does not say "not a veteran", so the select stays at its
    placeholder. The dry run says so and lists it in unresolved as an unknown_fact."""
    res, st = await _lever(drv, LEVER_PROFILE)
    assert st["sel:eeo[veteran]"] == "" and st["sel:eeo[gender]"] == "Female"
    assert res.status == "dry_run" and "NOT verified" in res.detail and "Veteran status" in res.detail, res.detail
    assert len(res.unresolved) == 1 and res.unresolved[0].startswith("Veteran status (unknown_fact:")
    assert "is_veteran" in res.unresolved[0]


async def test_lever_race_without_the_hispanic_answer_stays_blank_and_reported(drv):
    ans = {k: v for k, v in LEVER_PROFILE.items() if k != "demographic:hispanic"}
    res, st = await _lever(drv, {**ans, "demographic:is_veteran": "No"})
    assert st["sel:eeo[race]"] == "" and res.status == "dry_run"
    assert [u.split(" (", 1)[0] for u in res.unresolved] == ["Race"], res.unresolved


async def test_lever_profile_declines_and_blank_profile_report_nothing(drv):
    res, st = await _lever(drv, {})
    assert res.status == "dry_run" and not res.unresolved and st["sel:eeo[gender]"] == ""   # optional, no value
    res, st = await _lever(drv, {"demographic:gender": "Decline to self-identify"})
    assert st["sel:eeo[gender]"] == "Decline to self-identify" and not res.unresolved


# #97: Greenhouse custom demographic survey (react-select), fictional company, with the plain veteran wording
SURVEY_HTML = """<html><body><form id=application-form>
<label for=first_name>First Name *</label><input id=first_name name=first_name required>
<div id=demographic-section class=demographic--container><h3>Initech Demographic Questions</h3>
<label for=4001>I identify my gender as: *</label><div id=4001_wrap></div>
<label for=4003>Veteran Status: *</label><div id=4003_wrap></div>
<label for=4005>I identify my race/ethnicity as (please mark all that apply): *</label><div id=4005_wrap></div>
<input type=checkbox required id=gdpr_demographic_data_consent_given_1 name=gdpr_demographic_data_consent_given value=1>
<label for=gdpr_demographic_data_consent_given_1>By checking this box, I consent to Initech, Inc collecting, storing,
and processing my responses to the demographic data surveys above.*</label></div>
<button type=submit>Submit application</button></form>
<script>__RS__
const D = "I don't wish to answer";
rs('4001', ['Man', 'Woman', D]);
rs('4003', ['Yes, I am a veteran', 'No, I am not a veteran', 'I identify as one or more protected veteran classifications', D]);
rs('4005', ['South Asian (inclusive of A, B)', 'White (origins in C)', D], {multi: true});
['4001', '4003', '4005'].forEach(i => document.getElementById(i).setAttribute('aria-required', 'true'));
__REPORT__
</script></body></html>""".replace("__RS__", RS_JS).replace("__REPORT__", REPORT)
GH_PROFILE = {"demographic:gender": "Female", "demographic:race": "Asian", "demographic:race_detail": "South Asian",
              "demographic:veteran": "I am not a protected veteran", "pref:acknowledge_privacy_notices": True}
FORM_GH = ApplicationForm(ref="greenhouse:initech:51", fields=[
    FormField(key="first_name", label="First Name", kind=FieldKind.text, required=True)])


async def _gh(drv, html, answers):
    srv, posts = _serve(html)
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/jobs/51", FORM_GH,
                                    {"first_name": "Alex", **answers}, {}, dry_run=True)
    return res, _state(posts)


async def test_greenhouse_survey_complete_with_is_veteran(drv):
    res, st = await _gh(drv, SURVEY_HTML, {**GH_PROFILE, "demographic:is_veteran": "No"})
    assert res.status == "dry_run", res.detail
    assert st["4001"] == "Woman" and st["4003"] == "No, I am not a veteran"
    assert st["4005"] == "South Asian (inclusive of A, B)"
    assert st.get("checked:gdpr_demographic_data_consent_given_1") is True and not res.unresolved


async def test_greenhouse_required_veteran_gap_is_incomplete_and_unresolved(drv):
    res, st = await _gh(drv, SURVEY_HTML, GH_PROFILE)
    assert res.status == "incomplete" and "Veteran Status" in res.detail, res.detail
    assert st["4003"] == ""
    assert any(u.startswith("Veteran Status") and "(unknown_fact:" in u for u in res.unresolved), res.unresolved
    assert categorize(res.status, res.detail, unresolved=res.unresolved) == UNKNOWN_FACT


# Greenhouse EEOC block with NATIVE selects (older boards), standard EEOC wording
NATIVE_HTML = """<html><body><form id=application-form>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<div class="eeoc__container"><h2>Voluntary Self-Identification</h2>
<label for=gender>Gender</label><select id=gender><option value="">Please select</option>
<option>Male</option><option>Female</option><option>Decline To Self Identify</option></select>
<label for=race>Race</label><select id=race><option value="">Please select</option>
<option>White (Not Hispanic or Latino)</option><option>Asian (Not Hispanic or Latino)</option>
<option>Decline To Self Identify</option></select>
<label for=veteran_status>Veteran Status</label><select id=veteran_status><option value="">Please select</option>
<option>I am a protected veteran</option><option>I am not a protected veteran</option>
<option>I don't wish to answer</option></select>
<label for=disability_status>Disability Status</label><select id=disability_status>
<option value="">Please select</option><option>Yes, I have a disability (or previously had a disability)</option>
<option>No, I don't have a disability</option><option>I don't wish to answer</option></select>
</div><button type=submit>Submit application</button></form><script>__REPORT__</script></body></html>
""".replace("__REPORT__", REPORT)
FORM_NATIVE = ApplicationForm(ref="greenhouse:initech:52", fields=[
    FormField(key="first_name", label="First Name", kind=FieldKind.text)])


async def test_greenhouse_native_eeoc_selects(drv):
    srv, posts = _serve(NATIVE_HTML)
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/jobs/52", FORM_NATIVE,
                                    {"first_name": "Alex", "demographic:gender": "Female", "demographic:race": "Asian",
                                     "demographic:hispanic": "No",
                                     "demographic:veteran": "I am not a protected veteran",
                                     "demographic:disability": "No, I do not have a disability"}, {}, dry_run=True)
    st = _state(posts)
    assert res.status == "dry_run" and not res.unresolved, (res.detail, res.unresolved)
    assert st["sel:gender"] == "Female" and st["sel:race"] == "Asian (Not Hispanic or Latino)"
    assert st["sel:veteran_status"] == "I am not a protected veteran"
    assert st["sel:disability_status"] == "No, I don't have a disability"
