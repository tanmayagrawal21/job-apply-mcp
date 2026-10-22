"""ADR-0026: human-provided verification codes. Pure helpers, engine state machine (fake driver), local-fixture
browser flow (skipped without a local browser) and the 'code never leaks' checks. All data is fictional."""
import http.server
import json
import logging
import threading
from pathlib import Path

import pytest
from apply_fakes import POSTING, FakeAdapter, FakeGates, FakeProfile, FakeStore, settings
from test_apply_engine import _full_router

from job_apply_mcp.apply.approvals import ApprovalBook
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.verification import CodeConstraints, constraints_from, normalize_code, validate_code
from job_apply_mcp.config import Settings, VerificationCfg
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField, SubmitResult

CODE = "Ab3dE9xZ"            # fictional 8-char code
EXTRA = {"q_why": "Because Acme Robotics builds warehouse fleets."}


# ------------------------------------------------------------------ pure helpers

def test_validate_code_format_only():
    split8 = CodeConstraints(length=8, min_len=8, max_len=8, split=8)
    assert validate_code(CODE, split8) is None
    assert validate_code("Ab3d-E9xZ", split8) is None and validate_code(" Ab3d E9xZ ", split8) is None
    assert "8-character" in validate_code("123", split8)
    assert "letters and digits" in validate_code("Ab3d!E9x", split8)
    num = CodeConstraints(length=6, digits_only=True)
    assert validate_code("123456", num) is None and "numeric" in validate_code("12345a", num)
    free = CodeConstraints(min_len=4, max_len=10)
    assert validate_code("1234", free) is None and validate_code("12", free) and validate_code("1" * 11, free)
    assert validate_code("", free) == "code is empty"
    assert validate_code("abc123", CodeConstraints(pattern="[0-9]{6}")) is not None
    assert normalize_code("12 34-56") == "123456"


def test_error_messages_never_echo_the_code():
    for c in (CodeConstraints(length=4), CodeConstraints(digits_only=True), CodeConstraints(pattern="[0-9]{6}")):
        assert "Zq9Zq9Zq9" not in (validate_code("Zq9Zq9Zq9!", c) or "")


def test_constraints_from_inputs():
    cfg = VerificationCfg()
    split = [{"i": i, "maxlength": 1, "inputmode": "", "type": "text", "pattern": None} for i in range(8)]
    c = constraints_from(split, cfg)
    assert c and c.split == 8 and c.length == 8
    one = [{"i": 0, "maxlength": 6, "inputmode": "numeric", "type": "text", "pattern": None}]
    c = constraints_from(one, cfg)
    assert c and c.length == 6 and c.digits_only and c.split == 0
    assert constraints_from([{"i": 0, "maxlength": None, "type": "text"}] * 2, cfg) is None   # ambiguous
    assert constraints_from([], cfg) is None


def test_config_defaults_are_safe():
    v = Settings().verification
    assert v.enabled and v.code_ttl_s == 600 and v.max_attempts == 1 and v.notify is False


# ------------------------------------------------------------------ engine state machine (fake driver)

class CodeDriver:
    """Stands in for PlaywrightDriver's verification surface; the code only ever lives in arguments."""
    name = "fake"

    def __init__(self, good=CODE, ttl=600):
        self.good, self.now, self.ttl = good, 0.0, ttl
        self.opened: dict[str, tuple[str, float, int]] = {}
        self.clicks = 0
        self.submits = 0
        self.expired_ids: list[str] = []

    async def fill_and_submit(self, apply_url, form, answers, files, *, dry_run=True):
        if dry_run:
            return SubmitResult(ref=form.ref, status="dry_run", via="browser")
        self.submits += 1
        self.opened["pend1"] = (form.ref, self.now + self.ttl, 1)
        return SubmitResult(ref=form.ref, status="awaiting_code", via="browser", pending_id="pend1",
                            detail="the site sent a verification code to the applicant")

    def pending_info(self, pid):
        if pid not in self.opened:
            return None
        ref, exp, _ = self.opened[pid]
        return {"pending_id": pid, "ref": ref, "expires_in_s": int(exp - self.now)}

    def pending_check(self, pid, code):
        return validate_code(code, CodeConstraints(length=8, min_len=8, max_len=8))

    async def provide_code(self, pid, code):
        ref, exp, _ = self.opened[pid]
        if exp <= self.now:
            self.opened.pop(pid)
            return SubmitResult(ref=ref, status="handoff", via="browser", detail="code expired")
        self.clicks += 1
        self.opened.pop(pid)
        ok = normalize_code(code) == self.good
        return SubmitResult(ref=ref, status="submitted" if ok else "handoff", via="browser",
                            detail=None if ok else "the entered code was not accepted; attempts exhausted")

    async def expire_pending(self):
        out = []
        for pid, (ref, exp, _) in list(self.opened.items()):
            if exp <= self.now:
                self.opened.pop(pid)
                out.append(SubmitResult(ref=ref, status="handoff", via="browser", detail="window expired"))
        return out


def make(tmp_path, drv=None):
    drv = drv or CodeDriver()
    book = ApprovalBook(tmp_path)
    e = ApplyEngine(settings(writing__max_rounds=1), resolve_adapter=lambda r: FakeAdapter(submit_status="needs_browser"),
                    profile=FakeProfile(), router=_full_router(), gates=FakeGates(), store=FakeStore(),
                    approvals=book, driver_factory=lambda n: drv)
    return e, drv, book


async def _awaiting(e):
    out = await e.plan(POSTING.ref)
    r = await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)
    return out, r


async def test_awaiting_code_state_and_never_resubmitted(tmp_path):
    e, drv, book = make(tmp_path)
    _, r = await _awaiting(e)
    assert r.status == "awaiting_code" and r.pending_id == "pend1"
    assert book.state(POSTING.ref)["state"] == "awaiting_code" and not e.store.records
    out2 = await e.plan(POSTING.ref)                              # a second submit must not click again
    r2 = await e.submit(POSTING.ref, EXTRA, out2.approval_id, dry_run=False)
    assert r2.status == "needs_human" and "provide_verification_code" in r2.detail and drv.submits == 1


async def test_provide_valid_code_confirms_and_records(tmp_path):
    e, drv, book = make(tmp_path)
    await _awaiting(e)
    r = await e.provide_verification_code("pend1", CODE)
    assert r.status == "submitted" and book.state(POSTING.ref)["state"] == "submitted"
    assert [x[0].status for x in e.store.records] == ["submitted"] and drv.submits == 1 and drv.clicks == 1


async def test_wrong_format_rejected_without_consuming_anything(tmp_path):
    e, drv, book = make(tmp_path)
    await _awaiting(e)
    r = await e.provide_verification_code("pend1", "123")
    assert r.status == "awaiting_code" and "invalid code format" in r.detail and "123" not in r.detail.replace("8", "")
    assert drv.clicks == 0 and book.state(POSTING.ref)["state"] == "awaiting_code"
    assert (await e.provide_verification_code("pend1", CODE)).status == "submitted"      # still usable


async def test_unknown_pending_id(tmp_path):
    e, drv, _ = make(tmp_path)
    r = await e.provide_verification_code("nope", CODE)
    assert r.status == "error" and "unknown or expired" in r.detail and drv.clicks == 0


async def test_wrong_code_is_one_attempt_then_handoff_and_blocking(tmp_path):
    e, drv, book = make(tmp_path)
    await _awaiting(e)
    r = await e.provide_verification_code("pend1", "ZZZZ9999")
    assert r.status == "handoff" and r.detail.startswith("handoff:")
    assert book.state(POSTING.ref)["state"] == "unknown"          # blocking: never auto-resubmitted
    again = await e.provide_verification_code("pend1", CODE)
    assert again.status == "error" and drv.clicks == 1
    out = await e.plan(POSTING.ref)
    assert (await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)).status == "needs_human"
    assert drv.submits == 1


async def test_expiry_becomes_handoff(tmp_path):
    e, drv, book = make(tmp_path)
    await _awaiting(e)
    drv.now = 601
    r = await e.provide_verification_code("pend1", CODE)          # sweep runs first
    assert r.status == "error" and drv.clicks == 0
    st = book.state(POSTING.ref)
    assert st["state"] == "unknown" and "handoff" in st["detail"]
    assert "code_expired" in [x["event"] for x in book.audit_rows(POSTING.ref)]


async def test_code_never_in_audit_db_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    e, _, book = make(tmp_path)
    await _awaiting(e)
    await e.provide_verification_code("pend1", CODE)
    blob = json.dumps(book.audit_rows(POSTING.ref)) + json.dumps(book.state(POSTING.ref)) + caplog.text
    blob += "".join(p.read_bytes().decode("latin-1") for p in Path(tmp_path).rglob("*") if p.is_file())
    assert CODE not in blob and normalize_code(CODE) not in blob


async def test_elicitation_path(monkeypatch):
    from job_apply_mcp import server

    class Res:
        def __init__(self, action, code=""):
            self.action = action
            self.data = type("D", (), {"code": code})()

    seen = []

    class Svc:
        async def provide_verification_code(self, pid, code):
            seen.append(pid)
            return {"status": "submitted"}

    class Ctx:
        def __init__(self, res=None, boom=False):
            self.res, self.boom = res, boom

        async def elicit(self, message, schema):
            assert "never stored" in message and CODE not in message
            if self.boom:
                raise RuntimeError("client has no elicitation")
            return self.res

    monkeypatch.setattr(server, "svc", lambda: Svc())
    row = {"status": "awaiting_code", "pending_id": "p9"}
    assert (await server._elicit_code(Ctx(Res("accept", CODE)), row))["status"] == "submitted" and seen == ["p9"]
    assert await server._elicit_code(Ctx(Res("decline")), row) == row
    assert await server._elicit_code(Ctx(boom=True), row) == row and seen == ["p9"]


# ------------------------------------------------------------------ local fixture pages + real browser

PAGE = """<html><body><div id=root><form id=f>
<label for=fullname>Full name</label><input id=fullname name=fullname>
<label for=email>Email</label><input id=email name=email>
<div id=gate hidden>Enter the 8-character security code we sent to your email.__INPUTS__</div>
<button type=submit id=sub>Submit application</button></form></div>
<script>
let phase = 0;
const inputs = () => [...document.querySelectorAll('#gate input')];
document.getElementById('f').onsubmit = async ev => { ev.preventDefault();
  if (phase === 0) {
    await fetch('/submit', {method: 'POST', body: '{}'}); phase = 1;
    document.getElementById('gate').hidden = false; return; }
  const code = inputs().map(i => i.value).join('');
  const r = await (await fetch('/verify', {method: 'POST', body: code})).json();
  if (r.ok) { history.pushState({}, '', '/acme/jobs/1/confirmation');
    document.getElementById('root').innerHTML = '<h2>Thank you for applying</h2>'; }
  else { document.getElementById('gate').insertAdjacentHTML('beforeend', '<p>That code is incorrect</p>'); }
};
document.addEventListener('input', e => { const l = inputs(); const i = l.indexOf(e.target);
  if (l.length > 1 && e.target.value && i < l.length - 1) l[i + 1].focus(); });
</script></body></html>"""
SPLIT = "".join(f'<input id="security-input-{i}" maxlength=1 aria-label="Character {i + 1}">' for i in range(8))
SINGLE = '<input name="security_code" maxlength=8 aria-label="Security code">'


class Srv:
    def __init__(self, inputs):
        self.submits, self.verifies = 0, []
        outer = self
        html = PAGE.replace("__INPUTS__", inputs)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if self.path == "/submit":
                    outer.submits += 1
                    out = {"need_code": True}
                else:
                    outer.verifies.append(body)
                    out = {"ok": body == CODE}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(out).encode())

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/acme/jobs/1"


FORM = ApplicationForm(ref="greenhouse:acme:1", fields=[
    FormField(key="fullname", label="Full name", kind=FieldKind.text, required=True),
    FormField(key="email", label="Email", kind=FieldKind.text)])
ANS = {"fullname": "Alex Rivera", "email": "alex.rivera@example.com"}


@pytest.fixture
async def drv(tmp_path):
    pytest.importorskip("playwright")
    from job_apply_mcp.apply.browser_playwright import PlaywrightDriver
    s = Settings()
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.confirmation_wait_s = 3
    s.verification.typing_delay_ms = 5
    d = PlaywrightDriver(s)
    try:
        await d._context()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no local browser: {e}")
    yield d
    await d.aclose()


async def _blob(drv, res_list, caplog):
    from job_apply_mcp.apply.browser_playwright import evidence_dir
    ev = evidence_dir(drv.s.paths.data_dir, FORM.ref)
    files = "".join(p.read_text() for p in ev.glob("*.json")) if ev.exists() else ""
    return files + caplog.text + "".join(r.model_dump_json() for r in res_list)


@pytest.mark.parametrize("inputs", [SPLIT, SINGLE], ids=["split-inputs", "single-input"])
async def test_browser_prompt_then_code_then_confirmed(drv, inputs, caplog):
    caplog.set_level(logging.DEBUG)
    srv = Srv(inputs)
    r1 = await drv.fill_and_submit(srv.url, FORM, ANS, {}, dry_run=False)
    assert r1.status == "awaiting_code" and r1.pending_id and srv.submits == 1
    assert len(drv._ctx.pages) >= 1 and drv.pending_info(r1.pending_id)["attempts_left"] == 1   # page kept open
    bad = await drv.provide_code(r1.pending_id, "123")                      # wrong format: not typed, not clicked
    assert bad.status == "awaiting_code" and "invalid code format" in bad.detail and srv.verifies == []
    ok = await drv.provide_code(r1.pending_id, CODE[:4] + "-" + CODE[4:])   # hyphen/space tolerated
    assert ok.status == "submitted", ok.detail
    assert srv.verifies == [CODE] and srv.submits == 1                      # one verify click, never resubmitted
    assert drv.pending_info(r1.pending_id) is None and await drv.provide_code(r1.pending_id, CODE) is None
    blob = await _blob(drv, [r1, bad, ok], caplog)
    assert CODE not in blob and normalize_code(CODE) not in blob


async def test_browser_wrong_code_single_attempt_then_handoff_inputs_blank(drv, caplog):
    caplog.set_level(logging.DEBUG)
    srv = Srv(SPLIT)
    r1 = await drv.fill_and_submit(srv.url, FORM, ANS, {}, dry_run=False)
    r2 = await drv.provide_code(r1.pending_id, "Zz9Zz9Zz")
    assert r2.status == "handoff" and "NOT retried" in r2.detail and srv.verifies == ["Zz9Zz9Zz"] and srv.submits == 1
    page = drv._ctx.pages[-1]
    assert await page.evaluate("[...document.querySelectorAll('#gate input')].every(i => i.value === '')")
    assert "Zz9Zz9Zz" not in await _blob(drv, [r1, r2], caplog)
    assert await drv.provide_code(r1.pending_id, CODE) is None and srv.verifies == ["Zz9Zz9Zz"]


async def test_browser_expiry_hands_off_with_evidence_and_never_types(drv):
    from job_apply_mcp.apply.browser_playwright import evidence_dir
    srv = Srv(SPLIT)
    r1 = await drv.fill_and_submit(srv.url, FORM, ANS, {}, dry_run=False)
    now = [1000.0]
    drv.codes.clock = lambda: now[0]
    drv.codes.pending[r1.pending_id].expires = 1000.0 + 10
    now[0] += 11
    r2 = await drv.provide_code(r1.pending_id, CODE)
    assert r2.status == "handoff" and "expired" in r2.detail and "evidence=" in r2.detail
    assert srv.verifies == [] and srv.submits == 1
    assert list(evidence_dir(drv.s.paths.data_dir, FORM.ref).glob("code-handoff-*.png"))


async def test_browser_sweep_expires_all_pending(drv):
    srv = Srv(SINGLE)
    r1 = await drv.fill_and_submit(srv.url, FORM, ANS, {}, dry_run=False)
    drv.codes.pending[r1.pending_id].expires = 0
    out = await drv.expire_pending()
    assert [o.status for o in out] == ["handoff"] and drv.pending_list() == []


async def test_browser_disabled_keeps_old_roadblock(drv):
    drv.s.verification.enabled = False
    srv = Srv(SPLIT)
    r = await drv.fill_and_submit(srv.url, FORM, ANS, {}, dry_run=False)
    assert r.status == "roadblock" and "verification" in r.detail


async def test_desktop_notify_is_fixed_text_and_best_effort(monkeypatch):
    from job_apply_mcp.apply import verification as v
    calls = []

    class P:
        async def wait(self):
            return 0

    async def fake_exec(*argv, **kw):
        calls.append(argv)
        return P()

    monkeypatch.setattr(v.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(v.shutil, "which", lambda n: "/usr/bin/notify-send")
    await v.desktop_notify("A verification code is needed")
    assert calls and "A verification code is needed" in calls[0]

    async def boom(*a, **k):
        raise OSError("no binary")

    monkeypatch.setattr(v.asyncio, "create_subprocess_exec", boom)
    await v.desktop_notify("x")                       # never raises


# ------------------------------------------------------------------ otp_assist (ADR-0027) first, human typing second

def _assist_env(monkeypatch, status, caplog=None):
    from job_apply_mcp import server
    from job_apply_mcp.grants.otp import OtpResult

    seen, asked = [], []

    class Svc:
        async def provide_verification_code(self, pid, code):
            seen.append((pid, code))
            return {"status": "submitted"}

    class Res:
        action, data = "accept", type("D", (), {"code": "Typed8Ch"})()

    class Ctx:
        async def elicit(self, message, schema):
            asked.append(message)
            return Res()

    async def fake_assist(settings, *, sender_domain, elicit, **kw):
        asked.append(("assist", sender_domain))
        return OtpResult(status, code=CODE if status == "confirmed" else None, masked="••••xZ")

    monkeypatch.setattr(server, "svc", lambda: Svc())
    monkeypatch.setattr(server, "assist_code", fake_assist)
    return server, Ctx(), seen, asked


ROW = {"status": "awaiting_code", "pending_id": "p1", "ref": "greenhouse:acme:1"}


async def test_assist_confirmed_enters_granted_code_without_asking_to_type(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    server, ctx, seen, asked = _assist_env(monkeypatch, "confirmed")
    out = await server._elicit_code(ctx, dict(ROW))
    assert out["status"] == "submitted" and seen == [("p1", CODE)]
    assert asked == [("assist", "greenhouse.io")] and CODE not in caplog.text


@pytest.mark.parametrize("status", ["declined", "fallback", "no_grant", "no_message", "no_code", "error"])
async def test_assist_other_statuses_fall_back_to_human_typing(monkeypatch, status):
    server, ctx, seen, asked = _assist_env(monkeypatch, status)
    out = await server._elicit_code(ctx, dict(ROW))
    assert out["status"] == "submitted" and seen == [("p1", "Typed8Ch")]       # only the human-typed code is used
    assert CODE not in json.dumps(seen) and "••••xZ" in asked[1] and "not used" in asked[1]


async def test_assist_disabled_or_unknown_ats_skips_mail_path(monkeypatch):
    server, ctx, seen, asked = _assist_env(monkeypatch, "confirmed")
    out = await server._elicit_code(ctx, {**ROW, "ref": "workday:acme:1"})        # no sender domain known
    assert seen == [("p1", "Typed8Ch")] and not any(a[0] == "assist" for a in asked if isinstance(a, tuple))
    assert out["status"] == "submitted"


async def test_real_assist_without_grant_reports_no_grant():
    from job_apply_mcp.grants.otp import assist_code
    res = await assist_code(Settings(), sender_domain="greenhouse.io", elicit=None)
    assert res.status == "no_grant" and res.code is None
