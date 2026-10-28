"""Per-ATS post-submit outcome detection and the Ashby upload wait (ADR-0066). Pure classifier tests plus local
fixtures that imitate the shipped ATS behaviour (Greenhouse security-code fallback, Ashby "updating" toast and score
rejection). Fictional data; nothing touches the network; browser tests skip without a local Chrome."""
import http.server
import threading
import time

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
    ('{"code":"invalid-attachment","message":"resume"}', 400, "validation_error"),
    # ADR-0068 amendment: the same shape on any other status is not a verdict -> undecided (unknown)
    ('{"code":"invalid-attributes","message":"email"}', 500, None),
    ('{"code":"invalid-attributes","message":"email"}', 502, None),
    ('{"code":"invalid-attachment","message":"resume"}', 403, None),
    ('{"code":"invalid-attributes","message":"email"}', 408, None),
    ('{"code":"invalid-attributes","message":"email"}', 200, None),
    ('{"code":"invalid-attributes","message":"email"}', 0, None),
    ('Internal Server Error: invalid-attributes', 500, None),
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
    assert classify("ashby", [], {"updating_toast": True}, requested=False)[0] == "not_sent"
    assert classify("ashby", [], {"updating_toast": True}) is None                # a request was (or may be) issued
    assert classify("ashby", [_rec("{}")], {"updating_toast": True}, requested=False) is None   # a response exists
    assert classify("ashby", [], {"ashby_failure": "We couldn't submit your application Try these steps"})[0] == \
        "captcha_rejected"
    assert classify("ashby", [], {"captcha_toast": True})[0] == "captcha_rejected"
    assert classify("ashby", [], {"ashby_success": True})[0] == "success"
    assert classify("ashby", [], {"corrections": True}, requested=False)[0] == "validation_error"   # nothing sent
    # ADR-0068 amendment: the banner beside a sent request without a verdict is not a verdict -> undecided (unknown)
    assert classify("ashby", [], {"corrections": True}) is None                   # issued (or maybe), no response
    assert classify("ashby", [_rec("{}")], {"corrections": True}, requested=False) is None
    assert classify("ashby", [_rec("Bad Gateway")], {"corrections": True}) is None
    gql = '{"data":null,"errors":[{"message":"Invalid email"}],"errorMessages":["Invalid email"]}'
    assert classify("ashby", [_rec(gql)], {"corrections": True})[0] == "validation_error"   # Ashby's own verdict
    assert classify("ashby", [], {"ashby_failure": "Something went wrong - sorry."}) is None   # generic: unknown


def test_lever_dom():
    assert classify("lever", [], {"lever_success": "Application submitted!"})[0] == "success"
    assert classify("lever", [_rec("")], {"lever_error": "Please verify you are human"})[0] == "captcha_rejected"
    assert classify("lever", [_rec("")], {"lever_error": "Email is invalid"})[0] == "validation_error"
    assert classify("lever", [], {"lever_error": "Email is invalid"}) is None     # no POST yet: native validation


# ---------------------------------------------------------------- browser fixtures


def _serve(pages: dict[str, str], posts: dict[str, tuple[int, str]], *, delay: float = 0.0, drop: bool = False):
    """Fixture server. POSTs are logged when RECEIVED. For submit POSTs ("ApiSubmit" / "/jobs/"), `delay` holds the
    response open (an in-flight request) and `drop` sends headers and then cuts the body (the request reached the server, then failed)."""
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
            submit = "ApiSubmit" in self.path or "/jobs/" in self.path
            if submit and drop:                       # headers sent, body cut short: the request reached us, then failed
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "500")
                self.end_headers()
                self.wfile.write(b'{"da')
                self.wfile.flush()
                self.close_connection = True
                return
            if submit and delay:
                time.sleep(delay)
            code, body = next(((c, b) for k, (c, b) in posts.items() if k in self.path), (200, "{}"))
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, log


@pytest.fixture
async def drv(tmp_path):
    s = Settings()
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.captcha_handoff = False
    s.browser.hydrate_wait_s = 1
    s.browser.confirmation_wait_s = 15
    s.browser.upload_settle_s = 1
    s.paths.data_dir = str(tmp_path / "data")
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
ASHBY_SRC = """<html><body><div id=root><form id=application-form>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<button type=submit>Submit Application</button></form></div><div id=toast></div>
<script>
let busy = true, refused = 0;
const TOAST = "We're updating your application (e.g. uploading files), please try again when they're finished.";
document.getElementById('application-form').onsubmit = async e => { e.preventDefault();
  if (__SENDS_WHILE_BUSY__ && busy) { document.getElementById('toast').textContent = TOAST;
    fetch('/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction', {method: 'POST', body: '{}'}).catch(() => {});
    return; }
  if (busy) { refused++; document.getElementById('toast').textContent = TOAST;
    setTimeout(() => { busy = false; document.getElementById('toast').textContent = ''; }, 800); return; }
  const r = await fetch('/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction', {method: 'POST', body: '{}'});
  const b = await r.json();
  if (b.errors) { document.getElementById('root').innerHTML = '<div class="ashby-application-form-failure-container" '
      + 'role=alert><h2>We couldn\\'t submit your application</h2><p>Try these steps</p></div>'; return; }
  document.getElementById('root').innerHTML = '<div class="ashby-application-form-success-container" role=status>'
    + '<h2>Success</h2><p>Your application was successfully submitted.</p></div>'; };
</script></body></html>"""
ASHBY_HTML = ASHBY_SRC.replace("__SENDS_WHILE_BUSY__", "false")
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
    assert log == ["/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction"]   # exactly one re-click, one request


async def test_ashby_score_rejection_is_terminal_and_not_retried(drv):
    html = ASHBY_HTML.replace("let busy = true,", "let busy = false,")
    srv, log = _serve({"/initech/9/application": html}, {"ApiSubmit": (200, LOW)})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/9/application", AFORM,
                                    {"first_name": "Alex"}, {}, dry_run=False)
    assert res.status == "captcha_rejected" and "RECAPTCHA_SCORE_BELOW_THRESHOLD" in res.detail, res.detail
    assert len(log) == 1


def test_records_body_for_classification_is_not_in_evidence():
    rec = {"url": "u", "status": 200, "submit": True, "body": "short", "_cls": "long secret body"}
    assert {k: v for k, v in rec.items() if not k.startswith("_")} == {"url": "u", "status": 200, "submit": True,
                                                                         "body": "short"}


# ---------------------------------------------------------------- control review on #107: never re-click once sent


def _sends_html(noise: int = 0) -> str:
    """The page shows the "updating" toast AND issues the submit request (in flight / failing per the server).
    `noise` non-submit POSTs fire first (Ashby GraphQL chatter that once exhausted the 30-record cap)."""
    src = ASHBY_SRC.replace("__SENDS_WHILE_BUSY__", "true")
    if noise:
        src = src.replace("let busy = true,", f"for (let i = 0; i < {noise}; i++) fetch('/telemetry?i=' + i, "
                                              "{method: 'POST', body: '{}'}); let busy = true,")
    return src


def _submits(log):
    return [p for p in log if "ApiSubmit" in p]


@pytest.mark.parametrize(("delay", "drop"), [(20.0, False), (0.0, True)])
async def test_toast_with_a_submit_request_in_flight_or_failed_is_never_reclicked(drv, delay, drop):
    srv, log = _serve({"/initech/9/application": _sends_html()}, {"ApiSubmit": (200, OK)}, delay=delay, drop=drop)
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/9/application", AFORM,
                                    {"first_name": "Alex"}, {}, dry_run=False)
    assert len(_submits(log)) == 1, log                                    # no second click
    assert res.status == "roadblock", res.detail                           # engine: unknown, blocking


async def test_many_non_submit_posts_do_not_hide_the_submit_request(drv):
    srv, log = _serve({"/initech/9/application": _sends_html(noise=35)}, {"ApiSubmit": (200, OK)})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/initech/9/application", AFORM,
                                    {"first_name": "Alex"}, {}, dry_run=False)
    assert len(_submits(log)) == 1, log
    assert res.status == "submitted", res.detail                           # the response is still classified


# ---------------------------------------------------------------- Greenhouse emailed code: otp_unattended gating


@pytest.mark.parametrize("case", ["window_trusted", "window_no_trusted_ids", "window_without_scope", "no_window"])
async def test_greenhouse_code_completes_unattended_only_in_a_window_with_trusted_ids(tmp_path, monkeypatch, case):
    """The driver's Greenhouse code step is `awaiting_code` (tested above); the engine completes it from mail ONLY
    with the otp_unattended scope in an active window AND verification.trusted_authserv_ids set (ADR-0048).
    Otherwise it is handed to the human: no mail read, nothing typed."""
    from test_consent import AutoCodeDriver, Mail, _mail, _submit, engine, esettings

    from job_apply_mcp import consent

    code = "Zx81Qa7P"                                   # 8 characters, the Greenhouse security-code shape
    mail = Mail(_mail(code))
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: mail)
    s = esettings(tmp_path, mode="agent" if case == "no_window" else "human")
    if case == "window_no_trusted_ids":
        s.verification.trusted_authserv_ids = []
    if case == "window_without_scope":
        consent.store_for(s).issue_consent(600, scopes=["approve_submissions"])
    elif case != "no_window":
        consent.store_for(s).issue_consent(600)
    drv = AutoCodeDriver(code)
    e, _ = engine(tmp_path, s, drv)
    r = await _submit(e)
    if case == "window_trusted":
        assert r.status == "submitted" and drv.entered == [code] and drv.submits == 1, r.detail
    else:
        assert r.status == "awaiting_code" and drv.entered == [] and mail.fetches == 0, (case, r.status, r.detail)
        assert drv.submits == 1
