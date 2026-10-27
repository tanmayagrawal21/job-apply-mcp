"""Per-ATS post-submit outcome detection and the Ashby upload wait (ADR-0066). Pure classifier tests plus local
fixtures that imitate the shipped ATS behaviour (Greenhouse security-code fallback, Ashby "updating" toast and score
rejection). Fictional data; nothing touches the network; browser tests skip without a local Chrome."""
import http.server
import threading

import pytest

from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
from job_apply_mcp.apply.outcomes import classify
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField

# ---------------------------------------------------------------- pure classifier


def _rec(body, status=200):
    return {"url": "https://example.test/x", "status": status, "submit": True, "body": body[:50], "_cls": body}


@pytest.mark.parametrize("body,status,kind", [
    ('{"code":"captcha-failed","security_code_recipient":"a***@example.com"}', 428, "code_requested"),
    ('{"code":"captcha-failed"}', 428, "captcha_rejected"),
    ('{"code":"captcha-retry"}', 428, "captcha_rejected"),
    ('{"code":"invalid-attributes","message":"email"}', 422, "validation_error"),
    ('{"code":"invalid-security-code","message":"Invalid"}', 422, "code_requested"),
    ('{}', 200, "success"),
    ('{"code":"something-new"}', 500, None),
])
def test_greenhouse_network(body, status, kind):
    got = classify("greenhouse", [_rec(body, status)], {})
    assert (got[0] if got else None) == kind


def test_greenhouse_dom_and_since():
    assert classify("greenhouse", [], {"gh_code": True})[0] == "code_requested"
    assert classify("greenhouse", [], {"gh_code": True}, after_code=True) is None   # the old prompt lingers
    msg = "Please complete the reCAPTCHA and resubmit your application."
    assert classify("greenhouse", [], {"gh_submit_error": msg})[0] == "captcha_rejected"
    old = _rec('{"code":"captcha-failed","security_code_recipient":"x"}', 428)
    assert classify("greenhouse", [old], {}, since=1) is None                      # an earlier click's response


@pytest.mark.parametrize("body,kind", [
    ('{"errors":[{"message":"x","extensions":{"ashbyErrorType":"RECAPTCHA_SCORE_BELOW_THRESHOLD"}}],"data":null}',
     "captcha_rejected"),
    (('{"data":{"submitApplicationFormAction":{"applicationFormResult":{"__typename":"FormSubmitSuccess"},'
     '"messages":{"blockMessageForCandidateHtml":null}}}}'), "success"),
    (('{"data":{"submitApplicationFormAction":{"applicationFormResult":{"__typename":"FormSubmitSuccess"},'
     '"messages":{"blockMessageForCandidateHtml":"<p>You already applied</p>"}}}}'), "blocked"),
    (('{"data":{"submitApplicationFormAction":{"applicationFormResult":{"__typename":"FormRender",'
     '"errorMessages":["Email is required"]}}}}'), "validation_error"),
])
def test_ashby_network(body, kind):
    assert classify("ashby", [_rec(body)], {})[0] == kind


def test_ashby_dom():
    assert classify("ashby", [], {"updating_toast": True})[0] == "not_sent"
    assert classify("ashby", [_rec("{}")], {"updating_toast": True}) is None      # something WAS sent: never not_sent
    assert classify("ashby", [], {"ashby_failure": "We couldn't submit your application Try these steps"})[0] == \
        "captcha_rejected"
    assert classify("ashby", [], {"captcha_toast": True})[0] == "captcha_rejected"
    assert classify("ashby", [], {"ashby_success": True})[0] == "success"
    assert classify("ashby", [], {"corrections": True})[0] == "validation_error"
    assert classify("ashby", [], {"ashby_failure": "Something went wrong - sorry."}) is None   # generic: unknown


def test_lever_dom():
    assert classify("lever", [], {"lever_success": "Application submitted!"})[0] == "success"
    assert classify("lever", [_rec("")], {"lever_error": "Please verify you are human"})[0] == "captcha_rejected"
    assert classify("lever", [_rec("")], {"lever_error": "Email is invalid"})[0] == "validation_error"
    assert classify("lever", [], {"lever_error": "Email is invalid"}) is None     # no POST yet: native validation


# ---------------------------------------------------------------- browser fixtures


def _serve(pages: dict[str, str], posts: dict[str, tuple[int, str]]):
    log: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(pages.get(self.path.split("?")[0], "").encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            log.append(self.path)
            code, body = next(((c, b) for k, (c, b) in posts.items() if k in self.path), (200, "{}"))
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, log


@pytest.fixture
async def drv(tmp_path):
    s = Settings()
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.captcha_handoff = False
    s.browser.hydrate_wait_s = 1
    s.browser.confirmation_wait_s = 6
    s.browser.upload_settle_s = 1
    d = PlaywrightDriver(s)
    try:
        await d._context()
    except Exception as e:  # noqa: BLE001  # no browser available
        pytest.skip(f"no local browser: {e}")
    yield d
    await d.aclose()


FORM = ApplicationForm(ref="greenhouse:initech:7", fields=[
    FormField(key="first_name", label="First Name", kind=FieldKind.text, required=True)])

# Greenhouse: POST to .../jobs/7; on captcha-failed with a recipient the page shows the email code step
GH_HTML = """<html><body><form id=application-form>
<label for=first_name>First Name</label><input id=first_name name=first_name required>
<p id=submit-error></p><div id=code></div>
<button type=submit>Submit application</button></form>
<script>
document.getElementById('application-form').onsubmit = async e => { e.preventDefault();
  const r = await fetch('/initech/jobs/7', {method: 'POST', body: '{}'}); const b = await r.json();
  if (r.ok) { location.assign('/initech/jobs/7/confirmation'); return; }
  if (b.code === 'captcha-failed' && b.security_code_recipient) {
    document.getElementById('code').innerHTML = '<fieldset id=email-verification><p>A verification code was sent to '
      + b.security_code_recipient + '. To submit your application, enter the 8-character code.</p>'
      + [0,1,2,3,4,5,6,7].map(i => '<input id=security-input-' + i + ' maxlength=1>').join('') + '</fieldset>';
  } else if (b.code === 'captcha-failed') {
    document.getElementById('submit-error').textContent = 'Please complete the reCAPTCHA and resubmit your application.';
  } };
</script></body></html>"""


async def test_greenhouse_low_score_routes_to_awaiting_code(drv):
    srv, log = _serve({"/initech/jobs/7": GH_HTML},
                      {"/jobs/7": (428, '{"code":"captcha-failed","security_code_recipient":"a***@example.com"}')})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/jobs/7", FORM,
                                    {"first_name": "Alex"}, {}, dry_run=False)
    assert res.status == "awaiting_code" and res.pending_id, res.detail
    assert log == ["/initech/jobs/7"]                                      # exactly one submit


async def test_greenhouse_captcha_failed_without_code_is_terminal(drv):
    srv, log = _serve({"/initech/jobs/7": GH_HTML}, {"/jobs/7": (428, '{"code":"captcha-failed"}')})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/jobs/7", FORM,
                                    {"first_name": "Alex"}, {}, dry_run=False)
    assert res.status == "captcha_rejected" and "not retried" in res.detail, res.detail
    assert log == ["/initech/jobs/7"]


# Ashby: while `busy`, Submit only shows the "updating" toast and sends nothing; the upload finishes after 1.2 s
ASHBY_HTML = """<html><body><div id=root><form id=application-form>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<button type=submit>Submit Application</button></form></div><div id=toast></div>
<script>
let busy = true; setTimeout(() => { busy = false; document.getElementById('toast').textContent = ''; }, 1200);
document.getElementById('application-form').onsubmit = async e => { e.preventDefault();
  if (busy) { document.getElementById('toast').textContent =
    "We're updating your application (e.g. uploading files), please try again when they're finished."; return; }
  const r = await fetch('/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction', {method: 'POST', body: '{}'});
  const b = await r.json();
  if (b.errors) { document.getElementById('root').innerHTML = '<div class="ashby-application-form-failure-container" '
      + 'role=alert><h2>We couldn\\'t submit your application</h2><p>Try these steps</p></div>'; return; }
  document.getElementById('root').innerHTML = '<div class="ashby-application-form-success-container" role=status>'
    + '<h2>Success</h2><p>Your application was successfully submitted.</p></div>'; };
</script></body></html>"""
AFORM = ApplicationForm(ref="ashby:initech:9", fields=[FormField(key="first_name", label="First Name",
                                                                 kind=FieldKind.text)])
OK = ('{"data":{"submitApplicationFormAction":{"applicationFormResult":{"__typename":"FormSubmitSuccess"},'
      '"messages":{"blockMessageForCandidateHtml":null}}}}')
LOW = '{"errors":[{"message":"low","extensions":{"ashbyErrorType":"RECAPTCHA_SCORE_BELOW_THRESHOLD"}}],"data":null}'


async def test_ashby_updating_toast_is_not_sent_and_clicked_once_more(drv):
    srv, log = _serve({"/initech/9/application": ASHBY_HTML}, {"ApiSubmit": (200, OK)})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/9/application", AFORM,
                                    {"first_name": "Alex"}, {}, dry_run=False)
    assert res.status == "submitted", res.detail
    assert log == ["/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction"]   # one request in total


async def test_ashby_score_rejection_is_terminal_and_not_retried(drv):
    html = ASHBY_HTML.replace("let busy = true", "let busy = false")
    srv, log = _serve({"/initech/9/application": html}, {"ApiSubmit": (200, LOW)})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/9/application", AFORM,
                                    {"first_name": "Alex"}, {}, dry_run=False)
    assert res.status == "captcha_rejected" and "RECAPTCHA_SCORE_BELOW_THRESHOLD" in res.detail, res.detail
    assert len(log) == 1


def test_records_body_for_classification_is_not_in_evidence():
    rec = {"url": "u", "status": 200, "submit": True, "body": "short", "_cls": "long secret body"}
    assert {k: v for k, v in rec.items() if not k.startswith("_")} == {"url": "u", "status": 200, "submit": True,
                                                                         "body": "short"}
