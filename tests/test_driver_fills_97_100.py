"""QA issues #97 (demographic survey + demographic-data consent), #98 (job-alert form filled instead of the
application form), #99 (board redirected to a company-hosted embed), #100 (race select that appears only after the
Hispanic/Latino answer) and the Lever pronouns group. ADR-0063. Fictional data; local fixtures only (browser tests
are skipped when no local Chrome is installed; nothing touches the network)."""
import json

import pytest
from test_apply_react_select import RS_JS, _serve

from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
from job_apply_mcp.apply.dom_rules import (
    demographic_pick,
    dom_demographic_key,
    greenhouse_offsite_embed,
    is_decline_value,
    is_demographic_consent,
    is_pronoun_label,
)
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField

# ---------------------------------------------------------------- pure rules


def test_offsite_redirect_reopens_the_embed():
    ref = "greenhouse:initech:123"
    assert greenhouse_offsite_embed("https://job-boards.greenhouse.io/initech/jobs/123",
                                    "https://careers.initech.example/jobs?gh_jid=123", ref) == \
        "https://job-boards.greenhouse.io/embed/job_app?for=initech&token=123"
    # no redirect, a deliberate company URL, a local fixture or a non-Greenhouse ref: unchanged
    assert greenhouse_offsite_embed("https://job-boards.greenhouse.io/initech/jobs/123",
                                    "https://job-boards.greenhouse.io/initech/jobs/123", ref) is None
    assert greenhouse_offsite_embed("https://careers.initech.example/x", "https://careers.initech.example/x", ref) is None
    assert greenhouse_offsite_embed("http://127.0.0.1:9/f", "http://127.0.0.1:9/f", ref) is None
    assert greenhouse_offsite_embed("https://job-boards.greenhouse.io/a/jobs/1", "https://a.example/", "lever:a:1") is None


@pytest.mark.parametrize("label,key", [
    ("I identify my gender as:", "gender"), ("I identify as:", None), ("Veteran Status:", "veteran"),
    ("I have a disability:", "disability"), ("Please identify your race", "race"),
    ("I identify my race/ethnicity as (please mark all that apply):", "race"), ("Are you Hispanic/Latino?", "hispanic"),
    ("Do you identify as transgender?", None), ("Race/Ethnicity (including Hispanic/Latino)", None),
    ("I identify my sexual orientation as:", "sexual_orientation"), ("Pronouns", "pronouns")])
def test_dom_demographic_key_needs_exactly_one_topic(label, key):
    assert dom_demographic_key(label) == key


def test_demographic_pick_closed_equivalences_only():
    race = ["Central Asian (inclusive of A, B)", "South Asian (inclusive of C, D)", "East Asian (inclusive of E)",
            "I don't wish to answer"]
    assert demographic_pick("race", ["South Asian", "Asian"], race) == "South Asian (inclusive of C, D)"
    assert demographic_pick("race", ["", "Asian"], race) is None                 # never a prefix / substring
    assert demographic_pick("race", ["Asian"], ["Asian Indian", "White"]) is None
    assert demographic_pick("gender", ["Male"], ["Man", "Woman", "Non-binary"]) == "Man"
    assert demographic_pick("gender", ["Woman"], ["Man", "Non-binary"]) is None   # never the other gender
    assert demographic_pick("race", ["Male"], ["Man"]) is None                    # synonyms are gender-only
    assert demographic_pick("disability", ["No, I do not have a disability"], ["Yes", "No"]) == "No"
    assert demographic_pick("disability", ["Yes, I have a disability"], ["Yes", "No"]) == "Yes"
    assert demographic_pick("veteran", ["No, I am not a veteran"], ["Yes", "No"]) is None   # yes/no lead: not veteran
    # "not a protected veteran" is not "not a veteran"
    assert demographic_pick("veteran", ["I am not a protected veteran"],
                            ["Yes, I am a veteran", "No, I am not a veteran"]) is None
    assert demographic_pick("gender", ["Decline to self-identify"], ["Man", "I don't wish to answer"]) is None
    assert is_decline_value("I don't wish to answer") and is_decline_value("Decline to self-identify")
    assert is_decline_value("") and not is_decline_value("He/him")


@pytest.mark.parametrize("label,ok", [
    (("By checking this box, I consent to Initech, Inc collecting, storing, and processing my responses to the "
      "demographic data surveys above."), True),
    ("I consent to the processing of my responses to the voluntary self-identification questions above.", True),
    ("I agree to Initech using my answers to the demographic survey below", True),
    ("By checking this box, I consent to Initech sharing my responses to the demographic data surveys above.", False),
    ("I consent to Initech and its partners processing my responses to the demographic survey above.", False),
    ("I consent to Initech processing my responses to the demographic survey above and to receive marketing.", False),
    ("I consent to a background check.", False),
    ("I consent to Initech processing my personal data in line with the privacy notice.", False),
])
def test_demographic_consent_template(label, ok):
    assert is_demographic_consent(label) is ok


def test_pronoun_label():
    assert is_pronoun_label("Pronouns") and is_pronoun_label("Preferred pronouns (optional)")
    assert is_pronoun_label("What are your pronouns?")
    assert not is_pronoun_label("Gender and pronouns") and not is_pronoun_label("Pronouns and accommodations")


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


def _state(posts):
    return json.loads([p for p in posts if p[0] == "/state"][-1][1])


REPORT = """function report() { const o = {};
  document.querySelectorAll('[id$=_wrap]').forEach(w => { o[w.id.slice(0, -5)] =
    [...w.querySelectorAll('.select__single-value,.select__multi-value__label')].map(x => x.textContent).join('|'); });
  document.querySelectorAll('input[type=checkbox]').forEach(c => { if (c.checked) o['checked:' + (c.id || c.value)] = true; });
  document.querySelectorAll('input[type=text], input:not([type])').forEach(i => { if (i.id && i.value) o['text:' + (i.form ? i.form.id : '') + ':' + i.id] = i.value; });
  fetch('/state', {method: 'POST', body: JSON.stringify(o)}); }
new MutationObserver(report).observe(document.body, {subtree: true, childList: true, attributes: true});
document.addEventListener('change', report); document.addEventListener('input', report); report();
document.querySelectorAll('form').forEach(f => f.onsubmit = e => e.preventDefault());"""

# #97: a custom demographic survey (Greenhouse "demographic_questions" shape, fictional company)
SURVEY_HTML = """<html><body><form id=application-form>
<label for=first_name>First Name *</label><input id=first_name name=first_name required>
<div id=demographic-section class=demographic--container><h3>Initech Demographic Questions</h3>
<label for=4001>I identify my gender as: *</label><div id=4001_wrap></div>
<label for=4002>I identify as: *</label><div id=4002_wrap></div>
<label for=4003>Veteran Status: *</label><div id=4003_wrap></div>
<label for=4004>I have a disability: *</label><div id=4004_wrap></div>
<label for=4005>I identify my race/ethnicity as (please mark all that apply): *</label><div id=4005_wrap></div>
<input type=checkbox required id=gdpr_demographic_data_consent_given_1 name=gdpr_demographic_data_consent_given value=1>
<label for=gdpr_demographic_data_consent_given_1>By checking this box, I consent to Initech, Inc collecting, storing,
and processing my responses to the demographic data surveys above.*</label></div>
<button type=submit>Submit application</button></form>
<script>__RS__
const D = "I don't wish to answer";
rs('4001', ['Man', 'Non-binary/third gender', 'Woman', D]); rs('4002', ['Cisgender', 'Transgender', D]);
rs('4003', __VET__); rs('4004', ['Yes', 'No', D]);
rs('4005', ['Central Asian (inclusive of A, B)', 'South Asian (inclusive of C, D)', 'White (origins in E)', D], {multi: true});
['4001', '4002', '4003', '4004', '4005'].forEach(i => document.getElementById(i).setAttribute('aria-required', 'true'));
__REPORT__
</script></body></html>""".replace("__RS__", RS_JS).replace("__REPORT__", REPORT)

PROFILE = {"demographic:gender": "Male", "demographic:race": "Asian", "demographic:race_detail": "South Asian",
           "demographic:veteran": "I am not a protected veteran",
           "demographic:disability": "No, I do not have a disability"}
FORM_SURVEY = ApplicationForm(ref="greenhouse:initech:41", fields=[
    FormField(key="first_name", label="First Name", kind=FieldKind.text, required=True)])


async def _survey(drv, vet_opts, answers):
    srv, posts = _serve(SURVEY_HTML.replace("__VET__", json.dumps(vet_opts)))
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/jobs/41", FORM_SURVEY,
                                    {"first_name": "Alex", **answers}, {}, dry_run=True)
    return res, _state(posts)


VET_OK = ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]


async def test_survey_filled_from_profile_and_consent_ticked_when_opted_in(drv):
    res, st = await _survey(drv, VET_OK, {**PROFILE, "pref:acknowledge_privacy_notices": True})
    assert res.status == "dry_run", res.detail
    assert st["4001"] == "Man" and st["4002"] == "I don't wish to answer"     # no profile key: required -> decline
    assert st["4003"] == "I am not a protected veteran" and st["4004"] == "No"
    assert st["4005"] == "South Asian (inclusive of C, D)"                   # race_detail wins over race
    assert st.get("checked:gdpr_demographic_data_consent_given_1") is True


async def test_consent_not_ticked_without_the_privacy_opt_in(drv):
    res, st = await _survey(drv, VET_OK, {**PROFILE, "pref:acknowledge_privacy_notices": False})
    assert res.status == "incomplete" and "I consent to Initech" in res.detail, (res.detail, st)
    assert st["4001"] == "Man" and not st.get("checked:gdpr_demographic_data_consent_given_1")


async def test_consent_not_ticked_when_the_profile_supplies_no_demographics(drv):
    res, st = await _survey(drv, VET_OK, {"pref:acknowledge_privacy_notices": True})
    assert res.status == "incomplete", res.detail
    assert st["4001"] == "I don't wish to answer"                              # required, nothing in the profile
    assert not st.get("checked:gdpr_demographic_data_consent_given_1")


async def test_veteran_wording_mismatch_stays_blank(drv):
    """"not a protected veteran" is never "No, I am not a veteran": the required select stays empty -> incomplete."""
    res, st = await _survey(drv, ["Yes, I am a veteran", "No, I am not a veteran", "I don't wish to answer"],
                            {**PROFILE, "pref:acknowledge_privacy_notices": True})
    assert res.status == "incomplete" and "Veteran Status" in res.detail, res.detail
    assert st["4003"] == ""


# #100: the race select only appears after "Are you Hispanic/Latino?" is answered
EEOC_HTML = """<html><body><form id=application-form>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<div class="eeoc__container body"><h2>Voluntary Self-Identification</h2>
<label for=gender>Gender</label><div id=gender_wrap></div>
<label for=hispanic_ethnicity>Are you Hispanic/Latino?</label><div id=hispanic_ethnicity_wrap></div>
<div id=race_row></div></div>
<button type=submit>Submit application</button></form>
<script>__RS__
rs('gender', ['Male', 'Female', 'Decline To Self Identify']);
rs('hispanic_ethnicity', ['Yes', 'No', 'Decline To Self Identify'], {onpick: t => { if (t !== 'No') return;
  document.getElementById('race_row').innerHTML = '<label for=race>Please identify your race</label><div id=race_wrap></div>';
  rs('race', ['American Indian or Alaskan Native', 'Asian', 'White', 'Decline To Self Identify']); }});
__REPORT__
</script></body></html>""".replace("__RS__", RS_JS).replace("__REPORT__", REPORT)


async def test_conditional_race_select_is_filled_on_rescan(drv):
    srv, posts = _serve(EEOC_HTML)
    form = ApplicationForm(ref="greenhouse:initech:42", fields=[FormField(key="first_name", label="First Name",
                                                                          kind=FieldKind.text)])
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/jobs/42", form,
                                    {"first_name": "Alex", "demographic:gender": "Male", "demographic:hispanic": "No",
                                     "demographic:race": "Asian"}, {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    st = _state(posts)
    assert st["gender"] == "Male" and st["hispanic_ethnicity"] == "No" and st["race"] == "Asian"


# #98: a job-alert sign-up form above the application form, sharing field ids and labels
TWO_FORMS_HTML = """<html><body>
<form id=job-alerts><h3>Job Alerts</h3>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<label for=last_name>Last Name</label><input id=last_name name=last_name>
<label for=alert_email>Email</label><input id=alert_email name=alert_email><button type=submit>Notify me</button></form>
<form id=application-form><h3>Apply</h3>
<label for=first_name>First Name (required)</label><input id=first_name name=first_name>
<label for=last_name>Last Name (required)</label><input id=last_name name=last_name>
<label for=email>Email (required)</label><input id=email name=email>
<label for=resume>Resume</label><input id=resume name=resume type=file>
<button type=submit>Submit your application</button></form>
<script>__REPORT__</script></body></html>""".replace("__REPORT__", REPORT)


async def test_fields_are_scoped_to_the_application_form(drv):
    srv, posts = _serve(TWO_FORMS_HTML)
    form = ApplicationForm(ref="greenhouse:initech:43", fields=[
        FormField(key="first_name", label="First Name", kind=FieldKind.text, required=True),
        FormField(key="last_name", label="Last Name", kind=FieldKind.text, required=True),
        FormField(key="email", label="Email", kind=FieldKind.text, required=True)])
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/jobs/43", form,
                                    {"first_name": "Alex", "last_name": "Rivera", "email": "alex@example.com"}, {},
                                    dry_run=True)
    assert res.status == "dry_run", res.detail
    st = _state(posts)
    assert st.get("text:application-form:first_name") == "Alex"
    assert st.get("text:application-form:last_name") == "Rivera"
    assert not any(k.startswith("text:job-alerts:") for k in st), st


# Lever pronouns: a checkbox list whose title is a sibling .application-label (no <label for>, no legend)
PRONOUNS_HTML = """<html><body><form id=application-form><ul>
<li class="application-question"><label><div class="application-label">Full name<span class="required">✱</span></div>
<div class="application-field"><input type="text" name="name" required></div></label></li>
<li class="application-question"><div class="application-label multiple-select">Pronouns</div>
<div class="application-field"><ul id="candidatePronounsCheckboxes">
<li><label><input type="checkbox" name="pronouns" value="He/him"><span>He/him</span></label></li>
<li><label><input type="checkbox" name="pronouns" value="She/her"><span>She/her</span></label></li>
<li><label><input type="checkbox" name="pronouns" value="They/them"><span>They/them</span></label></li>
<li><label><input type="checkbox" name="pronouns" value="Use name only"><span>Use name only</span></label></li>
<li><label><input type="checkbox" value="Custom" id="customPronounsOption"><span>Custom</span></label>
<input style="display:none" id="customPronounsTextField" type="text" name="pronouns"></li></ul></div></li>
</ul><button type=submit>Submit application</button></form>
<script>__REPORT__</script></body></html>""".replace("__REPORT__", REPORT)
FORM_LEVER = ApplicationForm(ref="lever:initech:abc", fields=[
    FormField(key="name", label="Full name", kind=FieldKind.text, required=True)])


@pytest.mark.parametrize("value,ticked", [("She/her", ["She/her"]), ("Decline to self-identify", []), (None, []),
                                          ("she", [])])
async def test_pronouns_only_from_the_profile(drv, value, ticked):
    srv, posts = _serve(PRONOUNS_HTML)
    ans = {"name": "Alex Rivera", **({"demographic:pronouns": value} if value else {})}
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/abc/apply", FORM_LEVER, ans, {},
                                    dry_run=True)
    assert res.status == "dry_run", res.detail                   # optional: an unanswered group never blocks
    st = _state(posts)
    assert sorted(k[len("checked:"):] for k in st if k.startswith("checked:")) == ticked
