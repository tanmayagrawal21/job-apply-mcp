"""Radio / checkbox GROUPS and server-rendered forms against LOCAL fixtures (ADR-0039): Ashby radio and checkbox
groups addressed by data-field-path, custom-drawn boxes whose real input is covered, DOM-only consent radios, the
required-group scan behind `incomplete`, and the hydration wait. Needs a local Chrome/Chromium; skipped otherwise.
Fictional data only."""
import http.server
import json
import threading

import pytest

from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField

REPORT_JS = """
function report() { fetch('/state', {method: 'POST', body: JSON.stringify(
  [...document.querySelectorAll('input[type=radio], input[type=checkbox]')].filter(e => e.checked).map(e => e.id || e.value))}); }
document.addEventListener('change', report, true); document.addEventListener('click', () => setTimeout(report, 50), true);
"""


def _serve(html):
    posts = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            posts.append((self.path, self.rfile.read(n).decode() if n else ""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", posts


def _checked(posts) -> list[str]:
    st = [p for p in posts if p[0] == "/state"]
    return json.loads(st[-1][1]) if st else []


@pytest.fixture
async def drv(tmp_path):
    s = Settings()
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.confirmation_wait_s = 2
    s.browser.hydrate_wait_s = 5
    d = PlaywrightDriver(s)
    try:
        await d._context()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no local browser: {e}")
    yield d
    await d.aclose()


# ---- Ashby: radio group (#44) and checkbox group (#45) addressed by data-field-path

def _ashby_opt(entry, i, typ, name, text):
    """An Ashby option: the real input sits under a drawn circle/box that intercepts pointer events."""
    oid = f"{entry}-labeled-{typ}-{i}"
    return (f'<div class=opt><span style="position:relative;display:inline-block;width:18px;height:18px">'
            f'<span style="position:absolute;inset:0;z-index:2;background:#fff;border:1px solid #333"></span>'
            f'<input type={typ} id="{oid}" name="{name}" style="position:absolute;inset:0;margin:0"></span>'
            f'<label for="{oid}">{text}</label></div>')


RELOC = ["I am currently located in Springfield", "I am willing to relocate", "I am not willing to relocate"]
OFFICES = ["Springfield", "Open to relo to Springfield", "Open to relo to Shelbyville", "Remote - US"]
ASHBY_HTML = f"""<html><body><div id=app>
<div data-field-path="_systemfield_name"><label for="_systemfield_name">Name</label>
<input id="_systemfield_name" name="_systemfield_name" required></div>
<div data-field-path="q-reloc"><fieldset class="radio-group">
<label class="_heading_ab12_5 _required_ab12_9 question-title" for="q-reloc">Are you located in Springfield, or willing to relocate?</label>
{''.join(_ashby_opt("e1_q-reloc", i, "radio", "e1_q-reloc", t) for i, t in enumerate(RELOC))}</fieldset></div>
<div data-field-path="q-office"><fieldset class="checkbox-group">
<label class="_required_ab12_9 question-title" for="q-office">Where are you located?</label>
{''.join(_ashby_opt("e1_q-office", i, "checkbox", t, t) for i, t in enumerate(OFFICES))}</fieldset></div>
<input type=file id="_systemfield_resume" style="display:none">
<button type=button>Submit Application</button></div>
<script>{REPORT_JS}</script></body></html>"""

ASHBY_FIELDS = [FormField(key="_systemfield_name", label="Name", kind=FieldKind.text, required=True),
                FormField(key="q-reloc", label="Are you located in Springfield, or willing to relocate?",
                          kind=FieldKind.select, options=RELOC, required=True),
                FormField(key="q-office", label="Where are you located?", kind=FieldKind.multiselect,
                          options=OFFICES, required=True)]


async def test_ashby_radio_and_checkbox_groups_are_ticked_and_verified(drv):
    base, posts = _serve(ASHBY_HTML)
    ans = {"_systemfield_name": "Alex Rivera", "q-reloc": "I am willing to relocate",
           "q-office": ["Open to relo to Springfield"]}
    res = await drv.fill_and_submit(f"{base}/acme/job1/application",
                                    ApplicationForm(ref="ashby:acme:job1", fields=ASHBY_FIELDS), ans, {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert sorted(_checked(posts)) == ["e1_q-office-labeled-checkbox-1", "e1_q-reloc-labeled-radio-1"]


async def test_ashby_required_radio_group_left_empty_is_incomplete(drv):
    base, _ = _serve(ASHBY_HTML)
    form = ApplicationForm(ref="ashby:acme:job2", fields=[ASHBY_FIELDS[0], ASHBY_FIELDS[2]])   # radio not in the plan
    res = await drv.fill_and_submit(f"{base}/acme/job2/application", form,
                                    {"_systemfield_name": "Alex Rivera", "q-office": ["Remote - US"]}, {}, dry_run=True)
    assert res.status == "incomplete", res.detail
    assert "willing to relocate" in res.detail and "Where are you located" not in res.detail


# ---- DOM-only consent radios (#45): mapped only for an opted-in, plain privacy-notice acknowledgement

SMS_CONSENT = """<div data-field-path="q-phone"><label for="q-phone">Phone</label>
<input id="q-phone" name="q-phone" type=tel required>
<div class="texting-consent-description"><p>Check Yes or No to indicate your agreement to receive text message updates
from Acme Corp regarding your job application.</p><p>View our privacy policy here: Privacy Policy</p></div>
<div class="_consentRadioGroup_ab12_13">
<label><input type=radio name=communicationConsent value=given><div><p><strong>Yes</strong> - I consent to receiving text messages</p></div></label>
<label><input type=radio name=communicationConsent value=notGiven><div><p><strong>No</strong> - I do not consent to receiving text messages</p></div></label>
</div></div>"""
PRIVACY_ACK = """<fieldset aria-required=true><legend>Applicant Privacy Notice acknowledgement</legend>
<label><input type=radio name=pn value=ack> I acknowledge</label>
<label><input type=radio name=pn value=nack> I do not acknowledge</label></fieldset>"""


def _consent_page(block):
    return (f"<html><body><div id=app>{block}<input type=file id=r style='display:none'>"
            f"<button type=button>Submit Application</button></div><script>{REPORT_JS}</script></body></html>")


PHONE_FORM = ApplicationForm(ref="ashby:acme:job3", fields=[
    FormField(key="q-phone", label="Phone", kind=FieldKind.text, required=True)])


@pytest.mark.parametrize("opt_in", [False, True])
async def test_multi_sentence_sms_consent_is_never_answered_and_stops_the_run(drv, opt_in):
    """A consent block with several sentences (Ashby-style) is not a whole-label opt-in template: fail closed."""
    base, posts = _serve(_consent_page(SMS_CONSENT))
    res = await drv.fill_and_submit(f"{base}/acme/job3/application", PHONE_FORM,
                                    {"q-phone": "555-0101", "pref:acknowledge_privacy_notices": opt_in}, {},
                                    dry_run=True)
    assert res.status == "incomplete" and "text message" in res.detail, res.detail
    assert _checked(posts) == []


@pytest.mark.parametrize("opt_in", [False, True])
async def test_privacy_notice_group_ticked_only_when_opted_in(drv, opt_in):
    base, posts = _serve(_consent_page(PRIVACY_ACK + '<label for="q-phone">Phone</label><input id="q-phone" type=tel>'))
    res = await drv.fill_and_submit(f"{base}/acme/job3/application", PHONE_FORM,
                                    {"q-phone": "555-0101", "pref:acknowledge_privacy_notices": opt_in}, {},
                                    dry_run=True)
    if opt_in:
        assert res.status == "dry_run", res.detail
        assert _checked(posts) == ["ack"]
    else:
        assert res.status == "incomplete" and "Privacy Notice" in res.detail, res.detail
        assert _checked(posts) == []


# ---- Greenhouse: required checkbox group must block "verified" (#46)

def _gh_group(attrs="", legend_mark='<span class="required">*</span>', input_attrs=""):
    boxes = "".join(
        f'<div class=checkbox__wrapper><div class=checkbox__input>'
        f'<input type=checkbox id="question_7[]_{i}" name="question_7[]" value="{i}" {input_attrs} '
        f'style="display:none">'
        f'<span style="display:inline-block;width:14px;height:14px;border:1px solid #333"></span></div>'
        f'<label for="question_7[]_{i}">{t}</label></div>'
        for i, t in enumerate(["Conference", "Job Board", "News Article"]))
    return (f'<html><body><form id=f><label for=first_name>First Name</label><input id=first_name name=first_name>'
            f'<fieldset class=checkbox id="question_7[]" {attrs}><legend>How did you hear about us? {legend_mark}</legend>'
            f'{boxes}</fieldset><button type=submit>Submit application</button></form>'
            f'<script>document.getElementById("f").onsubmit = e => e.preventDefault();{REPORT_JS}</script></body></html>')


GH_FIRST = FormField(key="first_name", label="First Name", kind=FieldKind.text)
GH_GROUP = FormField(key="question_7[]", label="How did you hear about us?", kind=FieldKind.multiselect,
                     options=["Conference", "Job Board", "News Article"], required=True)


@pytest.mark.parametrize("attrs,legend_mark,input_attrs", [
    ("aria-required=true", '<span class="required">*</span>', "required"),   # Greenhouse job-boards markup
    ("", "*", ""),                                                             # asterisk legend only
    ("", '<span class="required"></span>', ""),                               # required class only
])
async def test_unchecked_required_group_is_incomplete_not_dry_run(drv, attrs, legend_mark, input_attrs):
    base, _ = _serve(_gh_group(attrs, legend_mark, input_attrs))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/7", ApplicationForm(ref="greenhouse:acme:7", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", res.detail
    assert "How did you hear about us?" in res.detail


@pytest.mark.parametrize("api_optional", [True, False])
async def test_unmarked_group_blocks_unless_the_api_says_optional(drv, api_optional):
    """Inverted rule: no required markup at all, yet an empty group is a gap unless the API marks it required:false."""
    base, _ = _serve(_gh_group("", "", ""))
    fields = [GH_FIRST] + ([GH_GROUP.model_copy(update={"required": False})] if api_optional else [])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/7", ApplicationForm(ref="greenhouse:acme:7", fields=fields),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == ("dry_run" if api_optional else "incomplete"), res.detail


async def test_required_group_with_hidden_inputs_is_filled_and_verified(drv):
    base, posts = _serve(_gh_group("aria-required=true", '<span class="required">*</span>', "required"))
    form = ApplicationForm(ref="greenhouse:acme:7", fields=[GH_FIRST, GH_GROUP])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/7", form, {"first_name": "Alex", "question_7[]": ["Job Board"]},
                                    {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert _checked(posts) == ["question_7[]_1"]


async def test_group_click_that_never_registers_is_not_verified(drv):
    html = _gh_group("aria-required=true", '<span class="required">*</span>', "required onclick='return false'")
    base, posts = _serve(html)
    form = ApplicationForm(ref="greenhouse:acme:7", fields=[GH_FIRST, GH_GROUP])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/7", form, {"first_name": "Alex", "question_7[]": ["Job Board"]},
                                    {}, dry_run=True)
    assert res.status in ("needs_human", "incomplete") and res.status != "dry_run", res.detail
    assert _checked(posts) == []


# ---- server-rendered forms: no typing before hydration (relativity fill failures, #46 B)

SSR_HTML = """<html><body><form id=f><label for=first_name>First Name</label><input id=first_name name=first_name>
<button type=submit>Submit application</button></form>
<script>window.__remixContext = {}; let hydrated = false;
const i = document.getElementById('first_name');
i.addEventListener('input', () => { if (!hydrated) fetch('/early', {method: 'POST'}); });
setTimeout(() => { hydrated = true; i.value = ''; i['__reactProps$x1'] = {}; }, 1200);
document.getElementById('f').onsubmit = e => e.preventDefault();
</script></body></html>"""


@pytest.mark.parametrize("wait", [5.0, 0.0])
async def test_ssr_form_waits_for_hydration_before_typing(drv, wait):
    drv.b.hydrate_wait_s = wait
    base, posts = _serve(SSR_HTML)
    res = await drv.fill_and_submit(f"{base}/acme/jobs/8", ApplicationForm(ref="greenhouse:acme:8", fields=[
        FormField(key="first_name", label="First Name", kind=FieldKind.text, required=True)]),
        {"first_name": "Alex"}, {}, dry_run=True)
    early = [p for p in posts if p[0] == "/early"]
    if wait:
        assert res.status == "dry_run", res.detail
        assert not early
    else:                                       # the fixture really does punish early typing
        assert early


# ---- control review: a consent group next to the real group; unwrapped required sets; near-miss options

SMS_NEXT_HTML = """<html><body><div id=app>
<div data-field-path="q-contact"><fieldset class=consent><legend>I agree to receive text message updates</legend>
<label><input type=radio id=sms-yes name=smsConsent value=yes>Yes - I consent to receiving text messages</label>
<label><input type=radio id=sms-no name=smsConsent value=no>No - I do not consent</label></fieldset>
<fieldset class="radio-group"><label class="_required_ab12_9 question-title" for="q-contact">May we contact your references?</label>
<span><input type=radio id="e1_q-contact-labeled-radio-0" name="e1_q-contact"><label for="e1_q-contact-labeled-radio-0">Yes</label></span>
<span><input type=radio id="e1_q-contact-labeled-radio-1" name="e1_q-contact"><label for="e1_q-contact-labeled-radio-1">No</label></span>
</fieldset></div>
<input type=file id=r style="display:none"><button type=button>Submit Application</button></div>
<script>__REPORT__</script></body></html>""".replace("__REPORT__", REPORT_JS)


async def test_sms_group_next_to_the_real_group_is_never_ticked(drv):
    base, posts = _serve(SMS_NEXT_HTML)
    form = ApplicationForm(ref="ashby:acme:job9", fields=[
        FormField(key="q-contact", label="May we contact your references?", kind=FieldKind.select,
                  options=["Yes", "No"], required=True)])
    res = await drv.fill_and_submit(f"{base}/acme/job9/application", form, {"q-contact": "Yes"}, {}, dry_run=True)
    assert _checked(posts) == ["e1_q-contact-labeled-radio-0"]
    assert "sms-yes" not in _checked(posts)
    assert res.status == "dry_run", res.detail                    # the SMS opt-in is allowlisted: left empty, logged


@pytest.mark.parametrize("value,opts", [("Female", ["Male", "Non-binary"]), ("Woman", ["Man", "Non-binary"]),
                                        ("Asian", ["Asian Indian", "White"])])
async def test_choice_group_near_miss_is_never_ticked(drv, value, opts):
    boxes = "".join(f'<label><input type=radio id=o{i} name="q-eeo" value="{o}">{o}</label>' for i, o in enumerate(opts))
    html = (f"<html><body><form id=f><fieldset id=q-eeo aria-required=true><legend>Self-identification *</legend>"
            f"{boxes}</fieldset><button type=submit>Submit application</button></form>"
            f"<script>document.getElementById('f').onsubmit = e => e.preventDefault();{REPORT_JS}</script></body></html>")
    base, posts = _serve(html)
    form = ApplicationForm(ref="greenhouse:acme:9", fields=[
        FormField(key="q-eeo", label="Self-identification", kind=FieldKind.select, options=opts, required=True)])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/9", form, {"q-eeo": value}, {}, dry_run=True)
    assert _checked(posts) == []
    assert res.status != "dry_run", res.detail


@pytest.mark.parametrize("variant", ["asterisk", "required", "aria"])
async def test_unwrapped_required_radio_set_blocks_dry_run(drv, variant):
    mark = " *" if variant == "asterisk" else ""
    attr = {"asterisk": "", "required": "required", "aria": "aria-required=true"}[variant]
    html = (f"<html><body><form id=f><label for=first_name>First Name</label><input id=first_name name=first_name>"
            f"<div class=q><p class=question-text>Are you at least 18 years old?{mark}</p>"
            f"<label><input type=radio name=adult value=yes {attr}> Yes</label>"
            f"<label><input type=radio name=adult value=no {attr}> No</label></div>"
            f"<button type=submit>Submit application</button></form>"
            f"<script>document.getElementById('f').onsubmit = e => e.preventDefault();</script></body></html>")
    base, _ = _serve(html)
    res = await drv.fill_and_submit(f"{base}/acme/jobs/10", ApplicationForm(ref="greenhouse:acme:10", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", res.detail
    assert "18 years" in res.detail or "adult" in res.detail


async def test_unwrapped_optional_radio_set_does_not_block(drv):
    html = ("<html><body><form id=f><label for=first_name>First Name</label><input id=first_name name=first_name>"
            "<div class=q><p>Subscribe to our newsletter</p><label><input type=radio name=nl value=y> Yes</label>"
            "<label><input type=radio name=nl value=n> No</label></div>"
            "<button type=submit>Submit application</button></form>"
            "<script>document.getElementById('f').onsubmit = e => e.preventDefault();</script></body></html>")
    base, _ = _serve(html)
    res = await drv.fill_and_submit(f"{base}/acme/jobs/11", ApplicationForm(ref="greenhouse:acme:11", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "dry_run", res.detail


# ---- deny-by-default required sets outside fieldset / role=group (control review 2)

def _set_page(block):
    return ("<html><body><form id=f><label for=first_name>First Name</label><input id=first_name name=first_name>"
            f"{block}<button type=submit>Submit application</button></form>"
            "<script>document.getElementById('f').onsubmit = e => e.preventDefault();</script></body></html>")


RADIOS = '<label><input type=radio name=size value=s> Small</label><label><input type=radio name=size value=l> Large</label>'
SET_SHAPES = {
    "member-required": '<div><p>Shirt size</p><label><input type=radio name=size value=s required> Small</label>'
                       '<label><input type=radio name=size value=l> Large</label></div>',
    "member-aria": '<div><p>Shirt size</p><label><input type=radio name=size value=s aria-required=true> Small</label>'
                   '<label><input type=radio name=size value=l> Large</label></div>',
    "ancestor-aria": f'<div aria-required=true><div><p>Shirt size</p>{RADIOS}</div></div>',
    "ancestor-required": f'<div required><p>Shirt size</p>{RADIOS}</div>',
    "ancestor-data-required": f'<div data-required=true><p>Shirt size</p>{RADIOS}</div>',
    "plain-text-asterisk": f'<div><b>Shirt size *</b>{RADIOS}</div>',
    "asterisk-own-span": f'<div><div>Shirt size<span>*</span></div>{RADIOS}</div>',
    "required-word": f'<div><strong>Shirt size (required)</strong>{RADIOS}</div>',
    "unclear-no-text": f'<div>{RADIOS}</div>',
}


@pytest.mark.parametrize("shape", sorted(SET_SHAPES))
async def test_required_set_shapes_outside_fieldset_are_incomplete(drv, shape):
    base, _ = _serve(_set_page(SET_SHAPES[shape]))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/12", ApplicationForm(ref="greenhouse:acme:12", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", (shape, res.detail)


async def test_set_the_api_marks_required_is_incomplete(drv):
    base, _ = _serve(_set_page(f"<div><p>Shirt size</p>{RADIOS}</div>"))         # nothing on the page says required
    form = ApplicationForm(ref="greenhouse:acme:12", fields=[
        GH_FIRST, FormField(key="size", label="Shirt size", kind=FieldKind.select, options=["Small", "Large"],
                            required=True)])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/12", form, {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete" and "Shirt size" in res.detail, res.detail


@pytest.mark.parametrize("api_optional", [True, False])
async def test_unmarked_set_blocks_unless_the_api_says_optional(drv, api_optional):
    base, _ = _serve(_set_page(f"<div><p>Shirt size</p>{RADIOS}</div>"))
    fields = [GH_FIRST] + ([FormField(key="size", label="Shirt size", kind=FieldKind.select, options=["Small", "Large"],
                                      required=False)] if api_optional else [])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/12", ApplicationForm(ref="greenhouse:acme:12", fields=fields),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == ("dry_run" if api_optional else "incomplete"), res.detail


# ---- native self-ID read-back (control review 2)

NATIVE_EEO = """<html><body><form id=f><label for=first_name>First Name</label><input id=first_name name=first_name>
<label for=gender>Gender__MARK__</label><select id=gender __REQ__><option value="">Select...</option>
<option value=m>Male</option><option value=f>Female</option><option value=d>I don't wish to answer</option></select>
<button type=submit>Submit application</button></form>
<script>const g = document.getElementById('gender');
g.addEventListener('change', () => { if (g.value === 'f') g.value = 'm'; });     // a site that swaps the answer
const rep = () => fetch('/state', {method: 'POST', body: g.value}); rep();
['change', 'input'].forEach(t => g.addEventListener(t, () => setTimeout(rep, 0)));
document.getElementById('f').onsubmit = e => e.preventDefault();</script></body></html>"""


@pytest.mark.parametrize("required", [True, False])
async def test_native_self_id_mismatch_is_blanked(drv, required):
    html = NATIVE_EEO.replace("__MARK__", " *" if required else "").replace("__REQ__", "required" if required else "")
    base, posts = _serve(html)
    fields = [GH_FIRST] + ([] if required else [FormField(key="gender", label="Gender", kind=FieldKind.select,
                                                         options=["Male", "Female"], required=False)])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/13", ApplicationForm(ref="greenhouse:acme:13", fields=fields),
                                    {"first_name": "Alex", "demographic:gender": "Female"}, {}, dry_run=True)
    assert [p for p in posts if p[0] == "/state"][-1][1] == ""                  # never left showing "Male"
    assert res.status == ("incomplete" if required else "dry_run"), res.detail


# ---- phone country: the already-selected country is compared exactly (control review 2)

ITI_HTML = """<html><body><form id=f><label for=first_name>First Name</label><input id=first_name name=first_name>
<div class=iti><button type=button class=iti__selected-country title="United States Minor Outlying Islands: +1">+1</button>
<ul class=iti__country-list style="display:none">
<li class=iti__country><span class=iti__country-name>United States Minor Outlying Islands</span></li>
<li class=iti__country><span class=iti__country-name>United States</span></li>
<li class=iti__country><span class=iti__country-name>Canada</span></li></ul>
<label for=phone>Phone</label><input id=phone name=phone type=tel></div>
<button type=submit>Submit application</button></form>
<script>const b = document.querySelector('.iti__selected-country'), ul = document.querySelector('.iti__country-list');
b.onclick = () => { ul.style.display = 'block'; };
ul.querySelectorAll('li').forEach(li => li.onclick = () => { b.title = li.textContent.trim() + ': +1'; ul.style.display = 'none';
  fetch('/state', {method: 'POST', body: b.title}); });
document.getElementById('f').onsubmit = e => e.preventDefault();</script></body></html>"""


async def test_phone_country_near_miss_is_reselected_exactly(drv):
    base, posts = _serve(ITI_HTML)
    form = ApplicationForm(ref="greenhouse:acme:14", fields=[GH_FIRST, FormField(key="phone", label="Phone", kind=FieldKind.text)])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/14", form,
                                    {"first_name": "Alex", "phone": "555-0101", "contact:country": "United States"},
                                    {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert [p for p in posts if p[0] == "/state"][-1][1] == "United States: +1"


def test_same_country_is_exact():
    from job_apply_mcp.apply.browser_playwright import same_country
    assert same_country("United States", "United States: +1") and same_country("India", "India (+91)")
    assert not same_country("United States", "United States Minor Outlying Islands: +1")
    assert not same_country("India", "British Indian Ocean Territory +246")


# ---- control review 3: inverted rule. Every shape below has NO API data, so an empty choice group / set / native
# select is a gap whatever the markup says.

SHAPES_3 = {
    "legend-required-word": f"<fieldset><legend>Shirt size (Required)</legend>{RADIOS}</fieldset>",
    "ancestor-required-class": f'<div class="field required"><p>Shirt size</p>{RADIOS}</div>',
    "aria-describedby": ('<div><p>Shirt size</p><span id=size-help>Required</span>'
                         '<label><input type=radio name=size value=s aria-describedby=size-help> Small</label>'
                         '<label><input type=radio name=size value=l aria-describedby=size-help> Large</label></div>'),
    "css-only-asterisk": f"<style>.req::after{{content:' *'}}</style><div><p class=req>Shirt size</p>{RADIOS}</div>",
    "required-badge": f'<div><p>Shirt size <span class=badge>Required</span></p>{RADIOS}</div>',
    "leading-asterisk": f"<div><p>* Shirt size</p>{RADIOS}</div>",
    "no-markup-at-all": f"<div><p>Shirt size</p>{RADIOS}</div>",
    "native-select": ('<div><label for=size>Shirt size</label><select id=size name=size><option value="">Select...</option>'
                      '<option>Small</option><option>Large</option></select></div>'),
}


@pytest.mark.parametrize("shape", sorted(SHAPES_3))
async def test_any_empty_choice_without_api_optional_is_incomplete(drv, shape):
    base, _ = _serve(_set_page(SHAPES_3[shape]))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/15", ApplicationForm(ref="greenhouse:acme:15", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete" and "Shirt size" in res.detail, (shape, res.detail)


async def test_native_select_api_optional_does_not_block(drv):
    base, _ = _serve(_set_page(SHAPES_3["native-select"]))
    form = ApplicationForm(ref="greenhouse:acme:15", fields=[
        GH_FIRST, FormField(key="size", label="Shirt size", kind=FieldKind.select, options=["Small", "Large"], required=False)])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/15", form, {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "dry_run", res.detail


async def test_unnamed_checkboxes_are_one_set_per_field_container(drv):
    block = ('<div class=q><p>Skills</p><label><input type=checkbox> Python</label>'
             '<label><input type=checkbox> Rust</label><label><input type=checkbox> SQL</label></div>')
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/16", ApplicationForm(ref="greenhouse:acme:16", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", res.detail
    assert res.detail.count("Skills") == 1, res.detail                 # one gap for the field, not one per box


async def test_asterisk_heading_followed_by_unstarred_help_text(drv):
    block = f'<div><b>Shirt size *</b><p class=help>Pick the closest fit</p>{RADIOS}</div>'
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/17", ApplicationForm(ref="greenhouse:acme:17", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", res.detail
    assert "Shirt size" in res.detail and "closest fit" not in res.detail   # the question, not the help text


async def test_job_alert_widget_is_allowlisted_and_logged(drv, caplog):
    caplog.set_level("INFO")
    block = ('<div class=job-alert><p>Create a Job Alert</p>'
             '<label><input type=checkbox name=alert_opt> Send me job alerts for similar roles</label></div>')
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/18", ApplicationForm(ref="greenhouse:acme:18", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert "left empty on purpose" in caplog.text


VALIDITY_HTML = """<html><body>
<form id=alert><label for=alert_email>Email me job alerts</label><input id=alert_email type=email required></form>
<form id=f><label for=first_name>First Name</label><input id=first_name name=first_name>
<label for=zip>Postal code</label><input id=zip name=zip pattern="[0-9]{5}">
<button type=submit>Submit application</button></form>
<script>let calls = 0; document.getElementById('f').onsubmit = e => { e.preventDefault(); fetch('/submitted', {method: 'POST'}); };
HTMLFormElement.prototype.reportValidity = function () { fetch('/reported', {method: 'POST'}); return true; };
</script></body></html>"""


@pytest.mark.parametrize("zip_value,expect", [("abc", "incomplete"), ("62701", "dry_run")])
async def test_native_check_validity_on_the_application_form_only(drv, zip_value, expect):
    base, posts = _serve(VALIDITY_HTML)
    form = ApplicationForm(ref="greenhouse:acme:19", fields=[GH_FIRST, FormField(key="zip", label="Postal code", kind=FieldKind.text)])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/19", form, {"first_name": "Alex", "zip": zip_value}, {}, dry_run=True)
    assert res.status == expect, res.detail
    if expect == "incomplete":
        assert "Postal code (native validation)" in res.detail
    assert "alert" not in (res.detail or "").lower()                  # the separate job-alert form is never checked
    assert not [p for p in posts if p[0] in ("/submitted", "/reported")]   # never submit, never reportValidity


# ---- control review 4: the opt-in exemption is tight. Required markup / API required:true always wins; only a
# consent-intent phrase in the field's OWN label exempts; container text never does.

NOT_OPTINS = ["Do you have B2B marketing experience? *", "Have you built SMS gateways? *",
              "Are you open to future opportunities on other teams? *", "Marketing channel you manage *",
              "Have you led a product promotion launch? *", "Have you received a promotion in your current role? *",
              "Experience with text messaging APIs? *", "Have you built opt-in flows? *", "Have you written a newsletter? *",
              "Do you have experience with SMS gateways?", "Do you have B2B marketing experience?", "Newsletter?"]


@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize("label", NOT_OPTINS)
async def test_questions_that_mention_marketing_or_sms_are_never_exempt(drv, label, wrapped):
    block = (f"<fieldset><legend>{label}</legend>{RADIOS}</fieldset>" if wrapped else f"<div><p>{label}</p>{RADIOS}</div>")
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/20", ApplicationForm(ref="greenhouse:acme:20", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", (label, res.detail)


OPTIN = "I agree to receive text message updates"


@pytest.mark.parametrize("shape", ["member-required", "asterisk", "aria-group", "api-required"])
async def test_required_markup_or_api_beats_a_consent_intent_label(drv, shape):
    req = "required" if shape == "member-required" else ""
    mark = " *" if shape == "asterisk" else ""
    aria = "aria-required=true" if shape == "aria-group" else ""
    block = (f'<fieldset {aria}><legend>{OPTIN}{mark}</legend>'
             f'<label><input type=radio name=sms value=y {req}> Yes</label><label><input type=radio name=sms value=n> No</label></fieldset>')
    base, _ = _serve(_set_page(block))
    fields = [GH_FIRST] + ([FormField(key="sms", label=OPTIN, kind=FieldKind.boolean, required=True)]
                           if shape == "api-required" else [])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/21", ApplicationForm(ref="greenhouse:acme:21", fields=fields),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status != "dry_run", (shape, res.detail)


async def test_plain_consent_intent_label_is_exempt(drv, caplog):
    caplog.set_level("INFO")
    block = (f'<fieldset><legend>{OPTIN}</legend><label><input type=radio name=sms value=y> Yes</label>'
             f'<label><input type=radio name=sms value=n> No</label></fieldset>')
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/22", ApplicationForm(ref="greenhouse:acme:22", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert "left empty on purpose" in caplog.text


async def test_consent_words_in_container_or_help_text_do_not_exempt(drv):
    block = (f'<div class=q><p>Preferred contact method</p><small class=help>We may also send you job alerts and '
             f'text message updates.</small>{RADIOS}</div>')
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/23", ApplicationForm(ref="greenhouse:acme:23", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete" and "Preferred contact" in res.detail, res.detail


@pytest.mark.parametrize("variant", ["plain", "member-required", "api-required"])
async def test_marketing_experience_question_blocks_in_every_variant(drv, variant):
    req = "required" if variant == "member-required" else ""
    block = (f"<fieldset><legend>* Do you have B2B marketing experience?</legend>"
             f"<label><input type=radio name=b2b value=y {req}> Yes</label><label><input type=radio name=b2b value=n> No</label></fieldset>")
    base, _ = _serve(_set_page(block))
    fields = [GH_FIRST] + ([FormField(key="b2b", label="Do you have B2B marketing experience?", kind=FieldKind.boolean,
                                      required=True)] if variant == "api-required" else [])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/24", ApplicationForm(ref="greenhouse:acme:24", fields=fields),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status != "dry_run", (variant, res.detail)


async def test_marketing_option_under_which_team_blocks(drv):
    block = ('<fieldset><legend>Which team? *</legend><label><input type=radio name=team value=m> Marketing</label>'
             '<label><input type=radio name=team value=y> Yes</label></fieldset>')
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/25", ApplicationForm(ref="greenhouse:acme:25", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete" and "Which team" in res.detail, res.detail


async def test_required_native_select_mentioning_marketing_blocks(drv):
    block = ('<label for=chan>Marketing channel you manage *</label><select id=chan name=chan required>'
             '<option value="">Select...</option><option>Email</option><option>Social</option></select>')
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/26", ApplicationForm(ref="greenhouse:acme:26", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete" and "Marketing channel" in res.detail, res.detail


async def test_api_optional_key_matches_exactly(drv):
    """An optional API key `comments` must not exempt a DOM group named `extra_comments`."""
    block = '<div><p>Extra comments</p><label><input type=radio name=extra_comments value=a> A</label>' \
            '<label><input type=radio name=extra_comments value=b> B</label></div>'
    base, _ = _serve(_set_page(block))
    form = ApplicationForm(ref="greenhouse:acme:27", fields=[
        GH_FIRST, FormField(key="comments", label="Comments", kind=FieldKind.text, required=False)])
    res = await drv.fill_and_submit(f"{base}/acme/jobs/27", form, {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", res.detail


@pytest.mark.parametrize("label", [
    "Have you worked with Twilio and do you agree to receive SMS messages about this role",
    "Do you use Twilio? I agree to receive SMS messages",
    "Twilio experience; I agree to receive text message updates",
])
@pytest.mark.parametrize("wrapped", [True, False])
async def test_compound_question_with_consent_clause_is_never_exempt(drv, label, wrapped):
    """Control review 5: no free-text lead-in before the consent verb; a second clause disqualifies the exemption."""
    block = (f"<fieldset><legend>{label}</legend>{RADIOS}</fieldset>" if wrapped else f"<div><p>{label}</p>{RADIOS}</div>")
    base, _ = _serve(_set_page(block))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/28", ApplicationForm(ref="greenhouse:acme:28", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", (label, res.detail)



# ---- control review 6: the WHOLE label must be a short opt-in template

SECOND_CLAUSE_AFTER = [
    "I agree to receive text messages and certify that all my answers are true",
    "I agree to receive SMS updates and confirm I am authorized to work in the US",
    "I agree to receive SMS updates. Are you over 18?",
    "I consent to receive marketing emails or relocation information",
    "Create a job alert and confirm you are over 18",
    "We may contact me by text about my background check",
    "You agree to receive emails about arbitration of disputes",
]
PLAIN_OPTINS = [
    "I agree to receive SMS updates about my application",
    "Create a job alert for similar roles",
    "Please tick the box if you agree to receive updates",
    "I would like to receive marketing emails",
    "I agree to receive text message updates",
    "Create a Job Alert",
]


def _optin_block(label, kind):
    if kind == "radio":
        return (f"<fieldset><legend>{label}</legend><label><input type=radio name=o value=y> Yes</label>"
                f"<label><input type=radio name=o value=n> No</label></fieldset>")
    return f'<div class=optin><label><input type=checkbox name=o value=y> {label}</label></div>'


@pytest.mark.parametrize("kind", ["radio", "checkbox"])
@pytest.mark.parametrize("label", SECOND_CLAUSE_AFTER)
async def test_consent_with_a_second_clause_anywhere_is_incomplete(drv, label, kind):
    base, _ = _serve(_set_page(_optin_block(label, kind)))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/29", ApplicationForm(ref="greenhouse:acme:29", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "incomplete", (label, kind, res.detail)


@pytest.mark.parametrize("kind", ["radio", "checkbox"])
@pytest.mark.parametrize("label", PLAIN_OPTINS)
async def test_plain_whole_label_optins_stay_exempt(drv, label, kind):
    base, _ = _serve(_set_page(_optin_block(label, kind)))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/30", ApplicationForm(ref="greenhouse:acme:30", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "dry_run", (label, kind, res.detail)
