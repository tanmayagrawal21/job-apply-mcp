"""QA issues #98 / #99 (ADR-0069): a company-hosted careers page that embeds the Greenhouse application form in an
iframe (#grnhse_app, often inserted lazily by embed.js) next to its own job-alert sign-up form. The driver must open
the embedded form and never fill the job-alert box; a page holding none of the posting's fields is never filled and
never reported as a successful dry run. Fictional company and data; local fixtures only (browser tests are skipped
when no local Chrome is installed; nothing touches the network)."""
import http.server
import json
import threading
import time

import pytest

from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
from job_apply_mcp.apply.dom_rules import app_form_missing, greenhouse_frame_target
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField

# ---------------------------------------------------------------- pure rules


def test_frame_target_accepts_greenhouse_or_same_origin_only():
    page = "https://careers.initech.example/jobs/ml-engineer?gh_jid=55"
    gh = "https://job-boards.greenhouse.io/embed/job_app?for=initech&token=55&b=https%3A%2F%2Fcareers.initech.example"
    assert greenhouse_frame_target(page, gh) == gh
    assert greenhouse_frame_target(page, "https://boards.greenhouse.io/embed/job_app?token=55") == \
        "https://boards.greenhouse.io/embed/job_app?token=55"
    assert greenhouse_frame_target("http://127.0.0.1:9/careers", "/embed/job_app?for=initech&token=55") == \
        "http://127.0.0.1:9/embed/job_app?for=initech&token=55"
    # a third-party host, a non-http scheme, an empty src or the page itself: never followed
    assert greenhouse_frame_target(page, "https://forms.other-vendor.example/apply?id=55") is None
    assert greenhouse_frame_target(page, "https://greenhouse.io.evil.example/embed/job_app") is None
    assert greenhouse_frame_target(page, "about:blank") is None
    assert greenhouse_frame_target(page, "javascript:void(0)") is None
    assert greenhouse_frame_target(page, "") is None
    assert greenhouse_frame_target(page, page + "#apply") is None


AT = "@"   # kept apart so userinfo URLs below are not mistaken for e-mail addresses
P = "https://careers.initech.example/careers?gh_jid=55"
GOOD = "https://job-boards.greenhouse.io/embed/job_app?for=initech&token=55"


@pytest.mark.parametrize("bad", [
    "https://evil.example\\@job-boards.greenhouse.io/embed/job_app?for=initech&token=55",   # WHATWG: host evil.example
    "https://evil.example\\.greenhouse.io/embed/job_app?token=55",
    "https:\\\\evil.example/embed/job_app?token=55",
    "https://u" + AT + "job-boards.greenhouse.io/embed/job_app?token=55",                  # userinfo
    "https://evil.example:pw" + AT + "job-boards.greenhouse.io/embed/job_app?token=55",
    "https://evil.example%40job-boards.greenhouse.io/embed/job_app?token=55",             # percent-encoded '@'
    "https://evil.example%5C" + AT + "job-boards.greenhouse.io/embed/job_app?token=55",            # percent-encoded '\\'
    "https://evil.example%2f.greenhouse.io/embed/job_app?token=55",                       # percent-encoded '/'
    "https://job-boards.greenhouse.io%2eevil.example/embed/job_app?token=55",
    "https://job-boards.greenhouse.io\t.evil.example/embed/job_app?token=55",            # tab (WHATWG strips)
    "https://job-boards.greenhouse.io\n.evil.example/embed/job_app?token=55",            # newline
    "https://job-boards.greenhouse.io\r/embed/job_app?token=55",
    "https://job-boards.greenhouse.io /embed/job_app?token=55",                           # inner space
    "https://job-boards.greenhouse.io\x00/embed/job_app?token=55",
    "https://job-boards.greenhouse.\u0131o/embed/job_app?token=55",                      # non-ASCII (IDNA)
])
def test_frame_target_refuses_anything_the_browser_could_parse_differently(bad):
    assert greenhouse_frame_target(P, bad) is None
    assert greenhouse_frame_target(P, bad, resolved=bad) is None


def test_frame_target_uses_and_checks_the_browser_resolution():
    assert greenhouse_frame_target(P, GOOD, resolved=GOOD) == GOOD
    # the browser's resolution names another host than urllib's reading: refused
    assert greenhouse_frame_target(P, GOOD, resolved="https://evil.example/embed/job_app?token=55") is None
    assert greenhouse_frame_target(P, GOOD, resolved="http://job-boards.greenhouse.io/embed/job_app?token=55") is None
    assert greenhouse_frame_target(P, GOOD, resolved="") is None                  # browser could not resolve it
    rel = greenhouse_frame_target("http://127.0.0.1:9/careers", "/embed/job_app?token=55",
                                  resolved="http://127.0.0.1:9/embed/job_app?token=55")
    assert rel == "http://127.0.0.1:9/embed/job_app?token=55"


def test_frame_target_must_be_the_refs_job():
    assert greenhouse_frame_target(P, GOOD, job_id="55") == GOOD
    assert greenhouse_frame_target(P, GOOD.replace("token=55", "token=56"), job_id="55") is None
    assert greenhouse_frame_target(P, "https://job-boards.greenhouse.io/embed/job_app?for=initech", job_id="55") is None
    assert greenhouse_frame_target(P, "https://job-boards.greenhouse.io/embed/job_app?gh_jid=55", job_id="55")
    assert greenhouse_frame_target(P, "https://job-boards.greenhouse.io/embed/job_app?for=initech") is not None


def test_app_form_missing_needs_two_keys_and_zero_hits():
    assert app_form_missing(["first_name", "last_name", "email"], 0)
    assert not app_form_missing(["first_name", "last_name", "email"], 1)
    assert not app_form_missing(["first_name"], 0)            # too little to judge
    assert not app_form_missing([], 0)


# ---------------------------------------------------------------- browser fixtures

REPORT = """function report() { const o = {};
  document.querySelectorAll('input[type=text], input:not([type])').forEach(i => { if (i.id && i.value)
    o['text:' + (i.form ? i.form.id : '') + ':' + i.id] = i.value; });
  fetch('/state' + location.pathname, {method: 'POST', body: JSON.stringify(o)}); }
document.addEventListener('change', report); document.addEventListener('input', report); report();
document.querySelectorAll('form').forEach(f => f.onsubmit = e => e.preventDefault());"""

# Company careers page: a job-alert sign-up (same visible labels as the application form) and the Greenhouse
# container that embed.js fills with an iframe after a delay.
OUTER = """<html><body><h1>Initech Careers: Machine Learning Engineer</h1>
<form id=job-alerts><h3>Job Alerts</h3>
<label for=ja_first>First Name</label><input id=ja_first name=ja_first>
<label for=ja_last>Last Name</label><input id=ja_last name=ja_last>
<label for=ja_email>Email</label><input id=ja_email name=ja_email><button type=submit>Sign up</button></form>
<div id=grnhse_app></div>
<script>__REPORT__
__INSERT__</script></body></html>""".replace("__REPORT__", REPORT)
LAZY_IFRAME = """setTimeout(() => { const f = document.createElement('iframe'); f.id = 'grnhse_iframe';
  f.src = '/embed/job_app?for=initech&token=55'; document.getElementById('grnhse_app').appendChild(f); }, 1200);"""

EMBED = """<html><body><form id=application-form><h3>Apply for this job</h3>
<label for=first_name>First Name *</label><input id=first_name name=first_name required>
<label for=last_name>Last Name *</label><input id=last_name name=last_name required>
<label for=email>Email *</label><input id=email name=email required>
<button type=submit>Submit application</button></form>
<script>__REPORT__</script></body></html>""".replace("__REPORT__", REPORT)


def _serve(pages: dict[str, str]):
    posts: list[tuple[str, str]] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path.split("?")[0])
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((body or "not found").encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            posts.append((self.path, self.rfile.read(n).decode() if n else ""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, posts


def _filled(posts, path: str) -> dict:
    """Every non-empty text value any report from `path` ever saw (union over reports)."""
    out: dict = {}
    for p, body in posts:
        if p == "/state" + path:
            out.update(json.loads(body))
    return out


@pytest.fixture
async def drv(tmp_path):
    s = Settings()
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.confirmation_wait_s = 2
    s.browser.hydrate_wait_s = 1
    s.browser.timeout_s = 4
    s.browser.evidence = False
    s.browser.user_data_dir = str(tmp_path / "profile")
    d = PlaywrightDriver(s)
    try:
        await d._context()
    except Exception as e:  # noqa: BLE001  # no browser available
        pytest.skip(f"no local browser: {e}")
    yield d
    await d.aclose()


FORM = ApplicationForm(ref="greenhouse:initech:55", fields=[
    FormField(key="first_name", label="First Name", kind=FieldKind.text, required=True),
    FormField(key="last_name", label="Last Name", kind=FieldKind.text, required=True),
    FormField(key="email", label="Email", kind=FieldKind.text, required=True)])
ANS = {"first_name": "Alex", "last_name": "Rivera", "email": "alex@example.com"}


async def test_lazy_embed_iframe_is_opened_and_job_alert_box_untouched(drv):
    """#98/#99: the iframe appears after the page loaded; the application form inside it is filled."""
    srv, posts = _serve({"/careers/ml-engineer": OUTER.replace("__INSERT__", LAZY_IFRAME), "/embed/job_app": EMBED})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/careers/ml-engineer", FORM, ANS, {},
                                    dry_run=True)
    assert res.status == "dry_run", res.detail
    emb = _filled(posts, "/embed/job_app")
    assert emb.get("text:application-form:first_name") == "Alex"
    assert emb.get("text:application-form:last_name") == "Rivera"
    assert emb.get("text:application-form:email") == "alex@example.com"
    assert not _filled(posts, "/careers/ml-engineer"), "the job-alert box must never be filled"


async def test_unreached_embed_is_not_filled_and_not_a_successful_dry_run(drv):
    """The embed never arrives: the wait is bounded, the job-alert box is not filled, and the dry run says so."""
    srv, posts = _serve({"/careers/ml-engineer": OUTER.replace("__INSERT__", "")})
    t0 = time.monotonic()
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/careers/ml-engineer", FORM, ANS, {},
                                    dry_run=True)
    assert time.monotonic() - t0 < 30
    assert res.status == "needs_human" and "application form not found" in res.detail, res.detail
    assert not _filled(posts, "/careers/ml-engineer")


async def test_failing_probe_fails_closed(drv, monkeypatch):
    """If the application-form probe cannot run, nothing is filled (needs_human), never a fill on a guess."""
    import job_apply_mcp.apply.browser_playwright as bp
    monkeypatch.setattr(bp, "_APPKEYS_JS", "() => { throw new Error('probe broken'); }")
    srv, posts = _serve({"/embed/job_app": EMBED})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/embed/job_app?for=initech&token=55", FORM,
                                    ANS, {}, dry_run=True)
    assert res.status == "needs_human" and "application form not found" in res.detail, res.detail
    assert not _filled(posts, "/embed/job_app")


async def test_embed_of_another_job_is_not_followed(drv):
    other = LAZY_IFRAME.replace("token=55", "token=77")
    srv, posts = _serve({"/careers/ml-engineer": OUTER.replace("__INSERT__", other), "/embed/job_app": EMBED})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/careers/ml-engineer", FORM, ANS, {},
                                    dry_run=True)
    assert res.status == "needs_human" and "application form not found" in res.detail, res.detail
    assert not _filled(posts, "/embed/job_app") and not _filled(posts, "/careers/ml-engineer")


async def test_foreign_iframe_in_the_embed_slot_is_never_followed(drv):
    srv, posts = _serve({"/careers/ml-engineer": OUTER.replace(
        "__INSERT__", "document.getElementById('grnhse_app').innerHTML = '<iframe src=\"about:blank\"></iframe>';")})
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/careers/ml-engineer", FORM, ANS, {},
                                    dry_run=True)
    assert res.status == "needs_human" and "application form not found" in res.detail, res.detail
    assert not _filled(posts, "/careers/ml-engineer")
