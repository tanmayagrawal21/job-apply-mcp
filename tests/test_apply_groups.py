"""Radio / checkbox GROUPS and server-rendered forms against LOCAL fixtures (ADR-0037): Ashby radio and checkbox
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
async def test_sms_consent_radio_is_never_answered_and_stops_the_run(drv, opt_in):
    base, posts = _serve(_consent_page(SMS_CONSENT))
    res = await drv.fill_and_submit(f"{base}/acme/job3/application", PHONE_FORM,
                                    {"q-phone": "555-0101", "pref:acknowledge_privacy_notices": opt_in}, {},
                                    dry_run=True)
    assert res.status == "incomplete", res.detail
    assert "text message" in res.detail
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


async def test_optional_unmarked_group_does_not_block(drv):
    base, _ = _serve(_gh_group("", "", ""))
    res = await drv.fill_and_submit(f"{base}/acme/jobs/7", ApplicationForm(ref="greenhouse:acme:7", fields=[GH_FIRST]),
                                    {"first_name": "Alex"}, {}, dry_run=True)
    assert res.status == "dry_run", res.detail


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
