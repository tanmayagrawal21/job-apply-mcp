"""Driver behaviour against LOCAL HTML fixtures (no real sites): confirmation detection, pre-submit
validation, boolean/upload checks, evidence capture. Needs a local Chrome/Chromium; skipped otherwise."""
import http.server
import json
import threading
from dataclasses import replace

import pytest

from job_apply_mcp.apply.browser_playwright import (
    ATS,
    PlaywrightDriver,
    decide_outcome,
    evidence_dir,
    is_submit_request,
    network_verdict,
    trim,
)
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField

FORM_HTML = """<html><body><div id=root><form id=f>
<label for="_systemfield_name">Full name</label><input id="_systemfield_name" name="_systemfield_name">
<label for="email">Email</label><input id="email" name="email">
<div role=group aria-label="Authorized to work?"><button type=button aria-pressed=false id=yes>Yes</button>
<button type=button aria-pressed=false id=no>No</button></div>
<label for=resume>Resume</label><input type=file id=resume name=resume>
<span id=chip></span>
<div id=err></div>
<button type=submit id=sub>Submit Application</button></form></div>
<script>
document.querySelectorAll('[aria-pressed]').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('[aria-pressed]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));});
document.getElementById('resume').onchange=e=>{setTimeout(()=>{document.getElementById('chip').textContent=e.target.files[0].name;},__CHIPDELAY__)};
__EXTRA__
document.getElementById('f').onsubmit=async ev=>{ev.preventDefault();
  const r=await fetch('/api/non-user-graphql?op=ApiSubmitApplicationForm',{method:'POST',body:'{}'});
  const j=await r.text(); __ONRESP__ };
</script></body></html>"""


class Srv:
    def __init__(self, page_html, api_body, api_status=200):
        self.posts = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(page_html.encode())

            def do_POST(self):
                outer.posts += 1
                self.send_response(api_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(api_body.encode())

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/acme/application"


def page(extra="", onresp="", chip_delay=100):
    return (FORM_HTML.replace("__EXTRA__", extra).replace("__ONRESP__", onresp)
            .replace("__CHIPDELAY__", str(chip_delay)))


FORM = ApplicationForm(ref="ashby:acme:job1", fields=[
    FormField(key="_systemfield_name", label="Full name", kind=FieldKind.text, required=True),
    FormField(key="email", label="Email", kind=FieldKind.text),
    FormField(key="auth", label="Authorized to work?", kind=FieldKind.boolean),
    FormField(key="resume", label="Resume", kind=FieldKind.file)])
ANS = {"_systemfield_name": "Alex Rivera", "email": "alex.rivera@example.com", "auth": True}
GOOD = '{"data":{"submitApplicationForm":{"__typename":"FormSubmitSuccess"}}}'


@pytest.fixture
async def drv(tmp_path):
    s = Settings()
    assert str(tmp_path) in s.paths.data_dir        # conftest isolation
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.confirmation_wait_s = 3
    s.browser.upload_settle_s = 1
    s.browser.captcha_handoff = False       # click-path fixtures; the handoff test turns it on
    d = PlaywrightDriver(s)
    try:
        await d._context()
    except Exception as e:  # noqa: BLE001  # no browser available
        pytest.skip(f"no local browser: {e}")
    yield d
    await d.aclose()


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "alex_resume.pdf"
    p.write_bytes(b"%PDF-1.4")
    return {"resume": str(p)}


def _evidence(drv, ref="ashby:acme:job1"):
    return evidence_dir(drv.s.paths.data_dir, ref)


# ---- pure helpers

def test_network_verdict_and_outcome():
    a = ATS["ashby"]
    ok = [{"submit": True, "status": 200, "body": GOOD}]
    bad = [{"submit": True, "status": 200, "body": '{"errors":[{"message":"Invalid email"}]}'}]
    assert network_verdict(a, ok) == "success" and network_verdict(a, bad) == "failure"
    assert network_verdict(a, [{"submit": True, "status": 500, "body": ""}]) == "failure"
    assert network_verdict(a, [{"submit": False, "status": 200}]) is None
    # network beats text; failure beats a stale success-looking page
    assert decide_outcome(a, "https://x/y", "", True, ok) == "submitted"
    assert decide_outcome(a, "https://x/y", "Thank you for applying", False, bad) == "failed"
    assert decide_outcome(a, "https://x/y", "Your application was successfully submitted", False, []) == "submitted"
    assert decide_outcome(a, "https://x/y", "Your application was successfully submitted", True, []) is None
    g = ATS["greenhouse"]
    assert decide_outcome(g, "https://b/acme/jobs/1/confirmation", "", True, []) == "submitted"
    assert decide_outcome(ATS["lever"], "https://jobs.lever.co/acme/1/thanks", "", True, []) == "submitted"
    assert is_submit_request(a, "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitApplicationForm", "post")
    assert not is_submit_request(a, "https://jobs.ashbyhq.com/api/non-user-graphql?op=Other", "POST")
    assert trim("a  b" * 100, 20).endswith("[truncated]")


def test_evidence_dir_is_under_data_dir(tmp_path):
    d = evidence_dir(tmp_path, "ashby:acme:1")
    assert d.parent == tmp_path / "evidence" and len(d.name) == 12


# ---- local browser

async def test_network_success_with_form_still_shown_and_evidence(drv, resume):
    srv = Srv(page(), GOOD)
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=False)
    assert res.status == "submitted", res.detail
    ev = _evidence(drv)
    assert list(ev.glob("submitted-*.png")) and srv.posts == 1
    meta = json.loads(next(ev.glob("submitted-*.json")).read_text())
    assert meta["network"][0]["submit"] and "FormSubmitSuccess" in meta["network"][0]["body"]
    assert "Alex" not in res.detail           # detail references the folder, not values


async def test_text_confirmation_requires_form_gone(drv, resume):
    on = "document.getElementById('root').innerHTML='<h2>Your application was successfully submitted</h2>';"
    srv = Srv(page(onresp=on), "{}")                      # no network signal -> text fallback
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=False)
    assert res.status == "submitted", res.detail


async def test_no_confirmation_is_needs_human_with_evidence(drv, resume):
    srv = Srv(page(), "{}")
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=False)
    assert res.status == "needs_human" and "no success confirmation" in res.detail and "evidence=" in res.detail
    assert list(_evidence(drv).glob("no-confirmation-*.png")) and srv.posts == 1


async def test_server_error_response_reported(drv, resume):
    srv = Srv(page(), '{"errors":[{"message":"Invalid email"}]}')
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=False)
    assert res.status == "needs_human" and "rejected" in res.detail
    meta = json.loads(next(_evidence(drv).glob("failed-*.json")).read_text())
    assert "Invalid email" in meta["network"][0]["body"]


async def test_presubmit_validation_blocks_click(drv, resume):
    extra = ("document.getElementById('email').setAttribute('aria-invalid','true');"
             "document.getElementById('err').innerHTML='<div role=alert>Email looks wrong</div>';")
    srv = Srv(page(extra=extra), GOOD)
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=False)
    assert res.status == "needs_human" and "validation errors before submit" in res.detail
    assert "invalid:email" in res.detail and srv.posts == 0
    assert list(_evidence(drv).glob("presubmit-errors-*.json"))


async def test_dry_run_reads_back_and_saves_evidence(drv, resume):
    srv = Srv(page(), GOOD)
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert list(_evidence(drv).glob("dry-run-*.png")) and srv.posts == 0


async def test_boolean_not_registering_click_fails_readback(drv, resume):
    broken = "document.querySelectorAll('[aria-pressed]').forEach(b=>{b.onclick=null;b.setAttribute('onclick','return false')});"
    srv = Srv(page(extra=broken), GOOD)
    # replace handlers after the originals ran: buttons never become pressed
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=True)
    assert res.status == "needs_human" and "auth" in res.detail


async def test_upload_chip_never_appearing_blocks_submit(drv, resume):
    srv = Srv(page(chip_delay=999999), GOOD)
    drv.b.upload_settle_s = 0.5
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=False)
    assert res.status == "needs_human" and "resume" in res.detail and srv.posts == 0



GH_HTML = """<html><body><button id=top>Apply</button><form id=f onsubmit="event.preventDefault();
fetch('/acme/jobs/1',{method:'POST',body:'x'}).then(()=>{location='/acme/jobs/1/confirmation'})">
<label for=first_name>First Name</label><input id=first_name name=first_name>
<button type=button disabled>Submit</button><button type=submit>Submit application</button></form>
<p id=ok></p><script>if(location.pathname.endsWith('/confirmation'))document.body.innerHTML='<h1>Thank you for applying</h1>';
</script></body></html>"""

GH_FORM = ApplicationForm(ref="greenhouse:acme:1", fields=[
    FormField(key="first_name", label="First Name", kind=FieldKind.text)])


async def test_greenhouse_disambiguates_submit_button(drv):
    srv = Srv(GH_HTML, "{}")
    url = srv.url.replace("/application", "/jobs/1")
    res = await drv.fill_and_submit(url, GH_FORM, {"first_name": "Alex"}, {}, dry_run=False)
    assert res.status == "submitted", res.detail
    assert srv.posts == 1


async def test_still_refuses_two_real_submit_buttons(drv):
    html = GH_HTML.replace("<button type=button disabled>Submit</button>", "<button type=submit>Submit</button>")
    srv = Srv(html, "{}")
    url = srv.url.replace("/application", "/jobs/1")
    res = await drv.fill_and_submit(url, GH_FORM, {"first_name": "Alex"}, {}, dry_run=False)
    assert res.status == "needs_human" and "refusing to guess" in res.detail and srv.posts == 0


REACT_LIKE = """<html><body><form id=f><label for=nm>Full name</label><input id=nm name=nm>
<div id=err></div><button type=submit>Submit Application</button></form><script>
let state=''; // controlled input: only input after real key presses updates state
let keyed=false; const nm=document.getElementById('nm');
nm.addEventListener('keydown',e=>{ if(e.isTrusted) keyed=true; });
nm.addEventListener('input',e=>{ if(keyed) state=e.target.value; });
document.getElementById('f').onsubmit=async ev=>{ev.preventDefault();
  if(!state){document.getElementById('err').innerHTML='<div role=alert>Missing entry for required field: Full name</div>';return;}
  await fetch('/api/non-user-graphql?op=ApiSubmitApplicationForm',{method:'POST',body:'{}'});
  document.body.innerHTML='<h1>Your application was successfully submitted</h1>';};
</script></body></html>"""
RFORM = ApplicationForm(ref="ashby:acme:react", fields=[FormField(key="nm", label="Full name", kind=FieldKind.text)])


async def test_react_controlled_input_needs_real_typing_on_ashby(drv):
    srv = Srv(REACT_LIKE, "{}")
    ok = await drv.fill_and_submit(srv.url, RFORM, {"nm": "Alex Rivera"}, {}, dry_run=False)
    assert ok.status == "submitted", ok.detail
    # the same page filled programmatically (fill) is rejected and reported as a failure with the banner text
    drv.b.confirmation_wait_s = 2
    ATS["ashby"], saved = replace(ATS["ashby"], typing="fill"), ATS["ashby"]
    try:
        bad = await drv.fill_and_submit(srv.url, RFORM, {"nm": "Alex Rivera"}, {}, dry_run=False)
    finally:
        ATS["ashby"] = saved
    assert bad.status == "needs_human" and "FAILED" in bad.detail and "Missing entry" in bad.detail


async def test_captcha_page_hands_off_without_click(drv, resume):
    extra = "var sc=document.createElement('div');sc.className='grecaptcha-badge';document.body.appendChild(sc);"
    drv.b.captcha_handoff = True
    for name, h in (("handoff", False), ("needs_human", True)):
        srv = Srv(page(extra=extra), GOOD)
        form = FORM.model_copy(update={"ref": "ashby:acme:cap" + name})
        drv.b.headless = h                      # live context stays as launched; the flag drives the decision
        res = await drv.fill_and_submit(srv.url, form, ANS, resume, dry_run=False)
        assert srv.posts == 0, "must never click submit on a captcha page"
        assert res.status == name and "evidence=" in res.detail, res.detail
        if name == "handoff":
            assert len(drv._ctx.pages) >= 1 and "human must click" in res.detail
    drv.b.headless = True


CARDS_HTML = """<html><body><form id=f>
<label>Why us?<textarea name="cards[u1][field0]"></textarea></label>
<label><input type=radio name="cards[u1][field1]" value="Yes"><span>Yes</span></label>
<label><input type=radio name="cards[u1][field1]" value="No"><span>No</span></label>
<label><input type=checkbox name="cards[u1][field2]" value="LinkedIn"><span>LinkedIn</span></label>
<label><input type=checkbox name="cards[u1][field2]" value="Referral"><span>Referral</span></label>
<label><input type=checkbox name="cards[u1][field2]" value="Other"><span>Other</span></label>
<select name="cards[u1][field3]"><option value="">-</option><option>Yes</option><option>No</option></select>
</form></body></html>"""


async def test_lever_card_radio_checkbox_select_text_filled_by_name(drv):
    srv = Srv(CARDS_HTML, GOOD)
    form = ApplicationForm(ref="lever:acme:job1", fields=[
        FormField(key="cards[u1][field0]", label="Why us?", kind=FieldKind.textarea),
        FormField(key="cards[u1][field1]", label="Relocate?", kind=FieldKind.select, options=["Yes", "No"]),
        FormField(key="cards[u1][field2]", label="Heard", kind=FieldKind.multiselect,
                  options=["LinkedIn", "Referral", "Other"]),
        FormField(key="cards[u1][field3]", label="Sponsor?", kind=FieldKind.select, options=["Yes", "No"])])
    ans = {"cards[u1][field0]": "Because.", "cards[u1][field1]": "No",
           "cards[u1][field2]": ["LinkedIn", "Other"], "cards[u1][field3]": "Yes"}
    res = await drv.fill_and_submit(srv.url, form, ans, {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert srv.posts == 0
