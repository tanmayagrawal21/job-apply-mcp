"""ADR-0048: consent window. TTY-only minting, expiry/revoke, scope enforcement, per-run binding, unattended
approvals, unattended email codes (newest message from the right sender only; the code never leaks), and "no
window means no unattended action". All people, companies and codes are fictional."""
import asyncio
import json
import logging
import pickle
import random
import string
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from apply_fakes import POSTING, FakeAdapter, FakeGates, FakeProfile, FakeStore, settings
from test_apply_engine import _full_router
from test_verification import EXTRA, CodeDriver

from job_apply_mcp import consent
from job_apply_mcp.apply.approvals import ApprovalBook
from job_apply_mcp.apply.browser_playwright import captcha_attempt_ok
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.config import Settings, load_settings
from job_apply_mcp.consent import otp as cotp
from job_apply_mcp.consent.cli import cli
from job_apply_mcp.consent.store import CONSENT_SCOPES, ConsentStore, run_context
from job_apply_mcp.core.models import SubmitResult
from job_apply_mcp.grants import GrantError
from job_apply_mcp.ingest.confirmations import Message
from job_apply_mcp.ingest.sources import ImapSource

SECRET = b"c" * 32
DOMAIN = "greenhouse.io"
AR = (f"mx.example.com; dkim=pass (2048-bit key) header.d={DOMAIN} header.s=s1; spf=pass smtp.mailfrom={DOMAIN}",)


class TTY:
    def __init__(self, tty=True):
        self._t = tty

    def isatty(self):
        return self._t


class Clock:
    def __init__(self):
        self.t = 2_000_000.0

    def __call__(self):
        return self.t


def mk(tmp_path, clock=None, run_id=None):
    return ConsentStore(tmp_path / "d", SECRET, clock=clock or time.time, user="alice", run_id=run_id,
                        hwm_file=tmp_path / "hwm.json")


@pytest.fixture
def cfg(tmp_path):
    s = Settings()
    s.paths.data_dir = str(tmp_path / "data")
    s.grants.use_keyring = False
    s.verification.trusted_authserv_ids = ["mx.example.com"]
    return s


def phrase(span):
    return lambda _: f"I consent to unattended applying for {span}"


# ------------------------------------------------------------------ minting is human-only (TTY + exact phrase)

def test_consent_refuses_without_tty(cfg, tmp_path, capsys):
    st = mk(tmp_path)
    for i, o in ((TTY(False), TTY(True)), (TTY(True), TTY(False)), (TTY(False), TTY(False))):
        for argv in (["--for", "24h"], ["--per-run"], ["--for", "1h", "--max", "3"]):
            assert cli(argv, cfg, stdin=i, stdout=o, input_fn=phrase("1d00h"), store=st) == 3
    assert "human-only" in capsys.readouterr().out and st.status() == []


def test_consent_needs_exact_phrase_and_shows_every_scope(cfg, tmp_path, capsys):
    st = mk(tmp_path)
    assert cli(["--for", "24h"], cfg, stdin=TTY(), stdout=TTY(), input_fn=lambda _: "yes", store=st) == 1
    assert st.status() == []
    assert cli(["--for", "24h", "--max", "5"], cfg, stdin=TTY(), stdout=TTY(), input_fn=phrase("24h00m"),
               store=st) == 0
    out = capsys.readouterr().out
    assert all(s in out for s in CONSENT_SCOPES) and "visible CAPTCHA" in out and "--revoke" in out
    (row,) = st.status()
    assert row["scopes"] == sorted(CONSENT_SCOPES) and row["submissions_left"] == 5 and row["mode"] == "window"


def test_scope_subset_and_unknown_scope(cfg, tmp_path, capsys):
    st = mk(tmp_path)
    assert cli(["--for", "2h", "--scopes", "otp_unattended,captcha_attempt"], cfg, stdin=TTY(), stdout=TTY(),
               input_fn=phrase("2h00m"), store=st) == 0
    assert st.status()[0]["scopes"] == ["captcha_attempt", "otp_unattended"]
    assert cli(["--for", "2h", "--scopes", "solve_captcha"], cfg, stdin=TTY(), stdout=TTY(),
               input_fn=phrase("2h00m"), store=st) == 2
    assert "unknown scope" in capsys.readouterr().out


def test_duration_and_max_caps(cfg, tmp_path, capsys):
    st = mk(tmp_path)
    assert cli(["--for", "8d"], cfg, stdin=TTY(), stdout=TTY(), input_fn=phrase("x"), store=st) == 2
    assert cli(["--for", "1h", "--max", "0"], cfg, stdin=TTY(), stdout=TTY(), input_fn=phrase("x"), store=st) == 2
    assert cli(["--for", "7d"], cfg, stdin=TTY(), stdout=TTY(), input_fn=phrase("168h00m"), store=st) == 0
    assert st.status() and "7d" not in capsys.readouterr().out.split("Covers:")[0][-5:]


def test_caps_not_relaxable_from_env(monkeypatch):
    monkeypatch.setenv("JOB_APPLY__GRANTS__CONSENT_MAX_DURATION_S", "99999999")
    monkeypatch.setenv("JOB_APPLY__GRANTS__CONSENT_PER_RUN_MAX_S", "99999999")
    g = load_settings().grants
    assert g.consent_max_duration_s == 7 * 86400 and g.consent_per_run_max_s == 12 * 3600


def test_no_mcp_tool_can_mint_and_status_is_read_only(cfg):
    from job_apply_mcp import server
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {n for n in names if "consent" in n} == {"consent_status"}
    assert not any(n.startswith(("grant_", "issue", "mint")) for n in names)
    out = json.loads(asyncio.run(server.mcp._tool_manager._tools["consent_status"].fn()))
    assert "active" in out and set(out["scopes"]) == set(CONSENT_SCOPES)


def test_status_and_active_write_nothing(cfg, tmp_path):
    st = mk(tmp_path)
    assert st.status() == [] and consent.status(cfg)["active"] == []
    assert consent.active(cfg, "approve_submissions") is None
    assert not (Path(cfg.paths.data_dir) / "grants").exists()
    st.issue_consent(600)
    before = st._audit_file.read_text()
    st.use("approve_submissions", consume=False)
    with pytest.raises(ValueError):
        st.use("nope", consume=False)
    mk(tmp_path, run_id="other").use("credentials", consume=False)
    assert st._audit_file.read_text() == before                 # read-only probe: no use, no audit line


# ------------------------------------------------------------------ expiry, revoke, scopes, tamper, max uses

def test_expiry(tmp_path):
    c = Clock()
    st = mk(tmp_path, c)
    st.issue_consent(60)
    assert st.use("approve_submissions").id
    c.t += 61
    with pytest.raises(GrantError, match="expired"):
        st.use("approve_submissions")
    assert st.status() == []


def test_revoke_via_cli(cfg, tmp_path, capsys):
    st = mk(tmp_path)
    a, b = st.issue_consent(600), st.issue_consent(600, mode="per_run")
    assert cli(["--status"], cfg, store=st) == 0 and a.id in capsys.readouterr().out
    assert cli(["--revoke", a.id], cfg, store=st) == 0
    assert [r["id"] for r in st.status()] == [b.id]
    assert cli(["--revoke"], cfg, store=st) == 0 and st.status() == []
    with pytest.raises(GrantError, match="no active grant"):
        st.use("otp_unattended")
    assert "revoked" in [e["event"] for e in st.audit_rows()]


def test_scopes_enforced(tmp_path):
    st = mk(tmp_path)
    st.issue_consent(600, scopes=["otp_unattended"])
    assert st.use("otp_unattended").scopes == ["otp_unattended"]
    for s in ("approve_submissions", "captcha_attempt", "credentials"):
        with pytest.raises(GrantError, match="no active grant"):
            st.use(s)
    with pytest.raises(ValueError):
        st.use("solve_captcha")
    with pytest.raises(ValueError):
        st.issue_consent(600, scopes=["otp_unattended", "make_accounts"])


def test_tampered_scopes_or_expiry_rejected(tmp_path):
    st = mk(tmp_path)
    st.issue_consent(600, scopes=["otp_unattended"])
    rows = json.loads(st._grants_file.read_text())
    rows[0]["scopes"] = sorted(CONSENT_SCOPES)                 # widen the scopes by hand
    st._grants_file.write_text(json.dumps(rows))
    with pytest.raises(GrantError, match="tampered"):
        st.use("approve_submissions")
    rows[0]["scopes"], rows[0]["expires"] = ["otp_unattended"], rows[0]["expires"] + 10**7
    st._grants_file.write_text(json.dumps(rows))
    with pytest.raises(GrantError, match="tampered"):
        st.use("otp_unattended")
    other = ConsentStore(tmp_path / "d", b"z" * 32, user="alice", hwm_file=tmp_path / "hwm.json")
    with pytest.raises(GrantError):
        other.use("otp_unattended")


def test_grants_and_consents_do_not_mix(tmp_path):
    from job_apply_mcp.grants import GrantStore
    g = GrantStore(tmp_path / "d", SECRET, user="alice", hwm_file=tmp_path / "g.json")
    g.issue("approve_submissions", 600)
    st = mk(tmp_path)
    with pytest.raises(GrantError):
        st.use("approve_submissions")
    st.issue_consent(600)
    assert len(g.active()) == 1 and len(st.status()) == 1


def test_max_counts_submissions_only_and_survives_rollback(tmp_path):
    st = mk(tmp_path)
    c = st.issue_consent(600, max_uses=2)
    snapshot = st._grants_file.read_text()
    st.use("approve_submissions", detail="ref=a")
    st.use("otp_unattended")
    st.use("captcha_attempt")                                   # unmetered: never counted
    assert st.status()[0]["submissions_left"] == 1
    st._grants_file.write_text(snapshot)                        # roll back the signed counter
    st.use("approve_submissions", detail="ref=b")               # max(record, audit, hwm) = 1 -> still allowed
    with pytest.raises(GrantError, match="max_uses"):
        st.use("approve_submissions", detail="ref=c")
    assert st.use("otp_unattended").id == c.id                  # the last submission's code step still works
    ev = [(e["event"], e["scope"]) for e in st.audit_rows()]
    assert ev.count(("use", "approve_submissions")) == 2 and ("act", "otp_unattended") in ev


def test_per_run_binds_to_first_run_and_ends(tmp_path):
    st = mk(tmp_path)
    c = st.issue_consent(3600, mode="per_run")
    with run_context("run-A"):
        assert st.use("approve_submissions").run == "run-A"
        assert st.use("otp_unattended").id == c.id
    with run_context("run-B"), pytest.raises(GrantError, match="another run"):
        st.use("approve_submissions")
    with run_context("run-B"):
        assert st.status() == []                                 # not this run's consent
    with run_context("run-A"):
        assert st.end_run() == [c.id]
        with pytest.raises(GrantError, match="expired"):
            st.use("approve_submissions")


def test_per_run_binding_is_signed(tmp_path):
    st = mk(tmp_path)
    st.issue_consent(3600, mode="per_run")
    with run_context("run-A"):
        st.use("approve_submissions")
    rows = json.loads(st._grants_file.read_text())
    rows[0]["run"] = "run-B"
    st._grants_file.write_text(json.dumps(rows))
    with run_context("run-B"), pytest.raises(GrantError, match="tampered"):
        st.use("approve_submissions")


# ------------------------------------------------------------------ captcha: hidden widgets only

def test_captcha_attempt_only_for_passive_widgets():
    called = []

    def yes():
        called.append(1)
        return True

    passive = {"visible": False, "unsafe": False}
    assert captcha_attempt_ok("handoff", ["recaptcha-badge"], passive, yes) and called
    assert not captcha_attempt_ok("handoff", ["recaptcha-badge"], passive, lambda: False)
    called.clear()
    for st in ({"visible": True, "unsafe": False}, {"visible": False, "unsafe": True}, {}):
        assert not captcha_attempt_ok("handoff", ["hcaptcha"], st, yes)
        assert not captcha_attempt_ok("attempt", ["hcaptcha"], st, yes)
    assert not captcha_attempt_ok("handoff", [], passive, yes)   # no widget (e.g. Ashby): stays a handoff
    assert called == []                                          # visible/unsafe never even consults the consent


def test_consent_captcha_requires_scope(cfg):
    from job_apply_mcp.apply.browser_playwright import _consent_captcha
    st = consent.store_for(cfg)
    assert _consent_captcha(cfg, "r") is False
    st.issue_consent(600, scopes=["otp_unattended"])
    assert _consent_captcha(cfg, "r") is False
    st.issue_consent(600, scopes=["captcha_attempt"])
    assert _consent_captcha(cfg, "r") is True


# ------------------------------------------------------------------ engine: approvals and unattended codes

class AutoCodeDriver(CodeDriver):
    def __init__(self, good):
        super().__init__(good=good)
        self.entered: list[str] = []

    async def provide_code(self, pid, code):
        self.entered.append(code)
        return await super().provide_code(pid, code)

    async def abandon_pending(self, pid, why):
        if pid not in self.opened:
            return None
        ref, _, _ = self.opened.pop(pid)
        return SubmitResult(ref=ref, status="handoff", via="browser", detail=why)


class Mail:
    def __init__(self, msgs):
        self.msgs, self.fetches = msgs, 0

    def fetch(self, since):
        self.fetches += 1
        return [m for m in self.msgs if m.date and m.date >= since]


def engine(tmp_path, s, drv):
    book = ApprovalBook(tmp_path / "book")
    return ApplyEngine(s, resolve_adapter=lambda r: FakeAdapter(submit_status="needs_browser"), profile=FakeProfile(),
                       router=_full_router(), gates=FakeGates(), store=FakeStore(), approvals=book,
                       driver_factory=lambda n: drv), book


def esettings(tmp_path, mode="human"):
    s = settings(writing__max_rounds=1)
    s.apply.approval_mode = mode
    s.paths.data_dir = str(tmp_path / "data")
    s.grants.use_keyring = False
    s.grants.otp_unattended_wait_s = 0
    s.verification.trusted_authserv_ids = ["mx.example.com"]
    return s


async def _submit(e):
    out = await e.plan(POSTING.ref)
    return await e.submit(POSTING.ref, EXTRA, out.approval_id, dry_run=False)


def _mail(code, now=None):
    now = now or datetime.now(UTC)
    return [Message(f"Hiring <no-reply@{DOMAIN}>", "Your security code", now - timedelta(minutes=30),
                    "Your code is 11112222"),                                   # too old
            Message("Jobs <alerts@other-board.example.com>", "Security code", now + timedelta(seconds=5),
                    "code: 33334444"),                                          # wrong sender, newest overall
            Message(f"Hiring <no-reply@{DOMAIN}>", "Security code", now - timedelta(seconds=20),
                    "Your code is 55556666", auth_results=AR),                   # right sender, older
            Message(f"Hiring <no-reply@us.{DOMAIN}>", "Security code", now,
                    f"Copy and paste this code into the security code field: {code}", auth_results=AR)]   # newest


async def test_no_window_human_mode_still_waits_for_a_human(tmp_path):
    s = esettings(tmp_path)
    e, _ = engine(tmp_path, s, AutoCodeDriver("Zx81Qa7P"))
    r = await _submit(e)
    assert r.status == "needs_human" and "awaiting human approval" in r.detail


async def test_window_auto_approves_and_logs_the_consent_id(tmp_path):
    s = esettings(tmp_path)
    c = consent.store_for(s).issue_consent(600, scopes=["approve_submissions"])
    drv = AutoCodeDriver("Zx81Qa7P")
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert r.status == "awaiting_code" and drv.entered == []    # no otp_unattended scope: today's behaviour
    ev = [(x["event"], x["actor"]) for x in book.audit_rows(POSTING.ref)]
    assert ("approved", f"consent:{c.id}") in ev
    assert any(x["event"] == "use" and "ref=" in x["detail"] for x in consent.store_for(s).audit_rows())


async def test_no_window_means_no_mail_read_and_no_code_entered(tmp_path, monkeypatch):
    s = esettings(tmp_path, mode="agent")
    mail = Mail(_mail("Zx81Qa7P"))
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: mail)
    drv = AutoCodeDriver("Zx81Qa7P")
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert r.status == "awaiting_code" and r.pending_id == "pend1"
    assert mail.fetches == 0 and drv.entered == [] and book.state(POSTING.ref)["state"] == "awaiting_code"


async def test_unattended_code_uses_newest_message_from_the_expected_sender(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    code = "Zx81Qa7P"
    s = esettings(tmp_path)
    c = consent.store_for(s).issue_consent(600)
    mail = Mail(_mail(code))
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: mail)
    drv = AutoCodeDriver(code)
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert r.status == "submitted" and drv.entered == [code] and drv.submits == 1 and drv.clicks == 1, r.detail
    assert book.state(POSTING.ref)["state"] == "submitted"
    ev = [(x["event"], x["actor"]) for x in book.audit_rows(POSTING.ref)]
    assert ("code_provided", f"consent:{c.id}") in ev and ("approved", f"consent:{c.id}") in ev
    _assert_absent(code, tmp_path, caplog, r, book)


async def test_no_mail_in_time_is_unknown_and_never_retried(tmp_path, monkeypatch):
    s = esettings(tmp_path)
    consent.store_for(s).issue_consent(600)
    old = [m for m in _mail("Zx81Qa7P") if "11112222" in m.snippet or "other-board" in m.sender]
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: Mail(old))
    drv = AutoCodeDriver("Zx81Qa7P")
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert r.status == "roadblock" and "unknown" in r.detail and "never retried" in r.detail
    assert drv.entered == [] and book.state(POSTING.ref)["state"] == "unknown"
    again = await _submit(e)
    assert again.status == "needs_human" and drv.submits == 1    # blocking: no second click, no second read


async def test_code_that_does_not_fit_the_page_is_never_typed(tmp_path, monkeypatch):
    s = esettings(tmp_path)
    consent.store_for(s).issue_consent(600)
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: Mail(_mail("123456")))
    drv = AutoCodeDriver("Zx81Qa7P")                             # page expects 8 characters
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert r.status == "roadblock" and drv.entered == [] and book.state(POSTING.ref)["state"] == "unknown"
    assert "123456" not in json.dumps(r.model_dump())


async def test_no_mail_source_configured_is_unknown(tmp_path):
    s = esettings(tmp_path)
    consent.store_for(s).issue_consent(600)
    drv = AutoCodeDriver("Zx81Qa7P")
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)                                         # default ingest.source="client": not readable
    assert r.status == "roadblock" and book.state(POSTING.ref)["state"] == "unknown" and drv.entered == []


# ------------------------------------------------------------------ only the newest message is read

class FakeImap:
    def __init__(self, raw_by_id):
        self.raw, self.fetched, self.searched = raw_by_id, [], []

    def select(self, folder, readonly=False):
        assert readonly
        return "OK", [b""]

    def search(self, charset, *crit):
        self.searched.append(crit)
        return "OK", [b" ".join(self.raw)]

    def fetch(self, i, what):
        assert "PEEK" in what
        self.fetched.append(i)
        return "OK", [(b"x", self.raw[i])]

    def logout(self):
        pass


def _eml(sender, minutes_ago, body):
    d = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return f"From: {sender}\r\nSubject: Security code\r\nDate: {d}\r\nAuthentication-Results: {AR[0]}\r\n\r\n{body}\r\n".encode()


def test_imap_reads_only_the_newest_message_from_the_sender():
    from job_apply_mcp.config import ImapCfg
    raw = {b"3": _eml(f"a@{DOMAIN}", 4, "code 11112222"), b"12": _eml(f"a@{DOMAIN}", 1, "code 77778888"),
           b"9": _eml(f"a@{DOMAIN}", 2, "code 55556666")}
    conn = FakeImap(raw)
    src = ImapSource(ImapCfg(), connect=lambda: conn)
    m = cotp.latest_from(src, DOMAIN, datetime.now(UTC) - timedelta(minutes=10))
    assert conn.fetched == [b"12"] and "77778888" in m.snippet
    assert any(DOMAIN in c for c in conn.searched[0])            # the server-side search is FROM the domain


def test_newest_message_too_old_is_not_replaced_by_an_older_one():
    from job_apply_mcp.config import ImapCfg
    conn = FakeImap({b"5": _eml(f"a@{DOMAIN}", 30, "code 11112222"), b"2": _eml(f"a@{DOMAIN}", 40, "code 2")})
    src = ImapSource(ImapCfg(), connect=lambda: conn)
    assert cotp.latest_from(src, DOMAIN, datetime.now(UTC) - timedelta(minutes=10)) is None
    assert conn.fetched == [b"5"]


async def test_waits_for_a_newer_message_when_the_newest_has_no_code(cfg):
    cfg.grants.otp_unattended_wait_s, cfg.grants.otp_poll_s = 60, 5
    now = datetime.now(UTC)
    mail = Mail([Message(f"x@{DOMAIN}", "We received your application", now, "Thanks for applying", AR)])
    ticks = {"t": 0.0}

    async def sleep(s):
        ticks["t"] += s
        mail.msgs.append(Message(f"x@{DOMAIN}", "Security code", now + timedelta(seconds=1), "code 90817263", AR))

    res = await cotp.fetch_code(cfg, sender_domain=DOMAIN, clicked_at=now, source=mail, sleep=sleep,
                                monotonic=lambda: ticks["t"])
    assert res.status == "found" and res.code.reveal() == "90817263" and mail.fetches == 2


async def test_mail_before_the_click_is_ignored(cfg):
    now = datetime.now(UTC)
    mail = Mail([Message(f"x@{DOMAIN}", "Security code", now - timedelta(minutes=3), "code 90817263", AR)])
    cfg.grants.otp_unattended_wait_s = 0
    res = await cotp.fetch_code(cfg, sender_domain=DOMAIN, clicked_at=now, source=mail)
    assert res.status == "no_message" and res.code is None


# ------------------------------------------------------------------ property: the code never leaks

def _assert_absent(code, tmp_path, caplog, *objs):
    blob = caplog.text + "".join(json.dumps(o.model_dump()) if hasattr(o, "model_dump") else repr(o) for o in objs)
    blob += "".join(p.read_bytes().decode("latin-1") for p in Path(tmp_path).rglob("*") if p.is_file())
    for o in objs:
        if isinstance(o, ApprovalBook):
            blob += json.dumps(o.audit_rows(POSTING.ref)) + json.dumps(o.state(POSTING.ref))
    assert code not in blob


def _rand_code(rng):
    body = [rng.choice(string.ascii_letters + string.digits) for _ in range(7)]
    body.insert(rng.randrange(8), rng.choice(string.digits))       # mailed codes carry at least one digit
    return "".join(body)


def test_extract_code_from_typical_mails():
    ex = cotp.extract_code
    assert ex("Security code", "Copy and paste this code into the security code field: Zx81Qa7P") == "Zx81Qa7P"
    assert ex("Your verification code", "Your code is 482913. It expires in 10 minutes.") == "482913"
    assert ex("Thanks for applying", "We received your application on 2026-10-22.") is None


def test_secret_code_never_renders():
    rng = random.Random(48)
    for _ in range(200):
        code = _rand_code(rng)
        sc = cotp.SecretCode(code)
        res = cotp.FetchResult("found", code=sc, detail="x")
        rendered = [str(sc), repr(sc), f"{sc}", f"{sc!r}", f"{sc:>20}", f"{sc} {sc!r}", repr(res), str(res)]
        assert all(code not in r for r in rendered)
        with pytest.raises(TypeError):
            pickle.dumps(sc)
        with pytest.raises(TypeError):
            json.dumps({"c": sc})
        assert sc.reveal() == code


@pytest.mark.parametrize("seed", range(12))
async def test_property_code_absent_from_logs_output_and_exceptions(tmp_path, monkeypatch, caplog, seed):
    rng = random.Random(seed)
    caplog.set_level(logging.DEBUG)
    code = _rand_code(rng)
    good = code if rng.random() < 0.6 else _rand_code(rng)       # sometimes the page rejects the code
    s = esettings(tmp_path, mode=rng.choice(["human", "agent"]))
    consent.store_for(s).issue_consent(600, max_uses=rng.choice([None, 1, 3]))
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: Mail(_mail(code)))
    drv = AutoCodeDriver(good)
    explode = rng.random() < 0.3
    if explode:                                                  # driver failure whose message quotes the code
        async def boom(pid, c):
            drv.entered.append(c)
            raise RuntimeError(f"typing failed for {c}")
        drv.provide_code = boom
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert drv.entered == [code]
    assert r.status in (("roadblock",) if explode else ("submitted", "handoff"))
    if r.status != "submitted":
        assert book.state(POSTING.ref)["state"] == "unknown"
    _assert_absent(code, tmp_path, caplog, r, book)


def test_cli_entry_passes_leading_options_through(monkeypatch, capsys):
    from job_apply_mcp import cli as top
    with pytest.raises(SystemExit) as ex:
        top.main(["consent", "--for", "1h"])                    # pytest's stdin is not a TTY
    assert ex.value.code == 3 and "human-only" in capsys.readouterr().out
    with pytest.raises(SystemExit) as ex:
        top.main(["consent", "--status"])
    assert ex.value.code == 0 and "no active consent" in capsys.readouterr().out


# ------------------------------------------------------------------ review round 1: general-purpose, fail-closed

@pytest.mark.parametrize(("subject", "body", "want"), [
    ("Ihr Bestätigungscode", "Ihr Bestätigungscode lautet: 4K7P92QX", "4K7P92QX"),
    ("認証コード", "認証コード：8H3K2M9Q を入力してください", "8H3K2M9Q"),
    ("验证码", "您的验证码是739182，10分钟内有效", "739182"),
    ("Código de verificación", "Tu código de verificación es 58Q2XK", "58Q2XK"),
    ("Code de vérification", "Votre code : 3J9K7Q", "3J9K7Q"),
    ("인증번호", "인증번호 [624913]", "624913"),
    ("Thanks for applying", "We received your application on 2026-10-22. Shipping opinion 2026.", None),
])
def test_extract_code_is_not_english_only(subject, body, want):
    assert cotp.extract_code(subject, body) == want


def test_imap_dates_do_not_depend_on_the_locale(monkeypatch):
    import locale

    from job_apply_mcp.ingest.sources import build_search, imap_date
    d = datetime(2026, 3, 5, tzinfo=UTC)
    for loc in ("de_DE.UTF-8", "fr_FR.UTF-8", "ja_JP.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, loc)
        except locale.Error:
            continue
        try:
            assert imap_date(d) == "05-Mar-2026" and build_search(d, [DOMAIN], [])[:2] == ["SINCE", "05-Mar-2026"]
        finally:
            locale.setlocale(locale.LC_TIME, "C")
    assert imap_date(d) == "05-Mar-2026"


def test_unattended_codes_are_serialized_per_sender_domain(tmp_path):
    s = esettings(tmp_path)
    e, _ = engine(tmp_path, s, AutoCodeDriver("Zx81Qa7P"))
    assert not isinstance(e._otp_guard(POSTING.ref, "greenhouse", False), asyncio.Lock)   # no window
    consent.store_for(s).issue_consent(600, scopes=["approve_submissions"])
    assert not isinstance(e._otp_guard(POSTING.ref, "greenhouse", False), asyncio.Lock)   # no otp scope
    consent.store_for(s).issue_consent(600, scopes=["otp_unattended"])
    a = e._otp_guard(POSTING.ref, "greenhouse", False)
    assert isinstance(a, asyncio.Lock) and a is e._otp_guard("greenhouse:other:2", "greenhouse", False)
    assert not isinstance(e._otp_guard(POSTING.ref, "greenhouse", True), asyncio.Lock)    # dry run
    assert not isinstance(e._otp_guard("custom:x:1", "custom", False), asyncio.Lock)       # no sender domain


async def test_second_submission_on_same_sender_waits_for_the_first_code(tmp_path, monkeypatch):
    s = esettings(tmp_path)
    consent.store_for(s).issue_consent(600, scopes=["otp_unattended"])
    e, _ = engine(tmp_path, s, AutoCodeDriver("Zx81Qa7P"))
    lock = e._otp_guard(POSTING.ref, "greenhouse", False)
    order = []

    async def holder():
        async with lock:
            order.append("first-click")
            await asyncio.sleep(0.05)
            order.append("first-code")

    async def second():
        await asyncio.sleep(0.01)
        async with e._otp_guard("greenhouse:other:2", "greenhouse", False):
            order.append("second-click")

    await asyncio.gather(holder(), second())
    assert order == ["first-click", "first-code", "second-click"]


async def test_code_never_reaches_tool_output_for_the_llm(tmp_path, monkeypatch):
    from job_apply_mcp import server
    code = "Q7m2Kx9P"
    s = esettings(tmp_path)
    consent.store_for(s).issue_consent(600)
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: Mail(_mail(code)))
    e, book = engine(tmp_path, s, AutoCodeDriver(code))
    r = await _submit(e)
    assert r.status == "submitted"
    tool_text = server._out(r.model_dump()) + server._out({"rows": [r.model_dump()]})
    tool_text += json.dumps(consent.status(s)) + json.dumps(book.audit_rows(POSTING.ref))
    assert code not in tool_text and "Q7m2" not in tool_text and "Kx9P" not in tool_text


def test_extract_code_fallback_is_unambiguous_or_nothing():
    assert cotp.extract_code("Mensaje", "Gebruik 739182 om verder te gaan") == "739182"
    assert cotp.extract_code("Mensaje", "Referentie 739182, dossier 551203") is None


# ------------------------------------------------------------------ review round 2: sender authentication, config trust

def _forged(code, auth=()):
    now = datetime.now(UTC)
    return [*_mail("Zx81Qa7P", now - timedelta(seconds=10)),
            Message(f"Hiring <no-reply@{DOMAIN}>", "Security code", now + timedelta(seconds=2),
                    f"Your security code: {code}", auth_results=tuple(auth))]


@pytest.mark.parametrize("auth", [
    (),                                                                                  # no Authentication-Results
    (f"mx.example.com; dkim=fail header.d={DOMAIN}; spf=fail; dmarc=fail header.from={DOMAIN}",),
    ("mx.example.com; dkim=pass header.d=evil.example.net; dmarc=none",),                 # signed by someone else
    (f"mx.example.com; dkim=pass header.d={DOMAIN}.evil.example.net",),                   # lookalike signer
    ("mx.example.com; spf=pass smtp.mailfrom=evil.example.net", AR[0]),                  # forged A-R below the top
])
async def test_forged_from_never_supplies_a_code(tmp_path, monkeypatch, auth):
    s = esettings(tmp_path)
    consent.store_for(s).issue_consent(600)
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: Mail(_forged("Fq4Rg8Ht", auth)))
    drv = AutoCodeDriver("Fq4Rg8Ht")
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert r.status == "roadblock" and "not DKIM/DMARC-verified" in r.detail
    assert drv.entered == [] and book.state(POSTING.ref)["state"] == "unknown"     # forged code never typed, and
    assert (await _submit(e)).status == "needs_human" and drv.submits == 1           # the real one is not waited for


@pytest.mark.parametrize(("hdr", "ok"), [
    (f"mx.example.com; dkim=pass header.d=mail.{DOMAIN}", True),
    (f"mx.example.com; dkim=pass header.i=@{DOMAIN}", True),
    (f"mx.example.com; dkim=fail header.d={DOMAIN}; dmarc=pass (p=reject) header.from={DOMAIN}", True),
    (f"mx.example.com; dkim=pass (comment; with semicolon) header.d={DOMAIN}", True),
    (f"mx.example.com; dmarc=pass header.from=not{DOMAIN}", False),
    (f"mx.example.com; dkim=neutral header.d={DOMAIN}", False),
    ("mx.example.com; none", False),
])
def test_authentication_results_parsing(hdr, ok):
    m = Message(f"a@{DOMAIN}", "s", None, "", auth_results=(hdr,))
    assert cotp.authenticated(m, DOMAIN, ["mx.example.com"]) is ok


def test_trusted_authserv_ids_pick_the_receiving_servers_header():
    forged_top = "attacker.example.net; dkim=pass header.d=" + DOMAIN
    m = Message(f"a@{DOMAIN}", "s", None, "", auth_results=(forged_top, "mx.example.com; dkim=fail header.d=x"))
    assert cotp.authenticated(m, DOMAIN) is False                    # empty list: nothing is trusted
    assert cotp.authenticated(m, DOMAIN, ["mx.example.com"]) is False
    forged_as_ours = Message(f"a@{DOMAIN}", "s", None, "", auth_results=(f"mx.example.com; dkim=pass header.d={DOMAIN}",))
    assert cotp.authenticated(forged_as_ours, DOMAIN, []) is False   # even a perfect-looking header
    good = Message(f"a@{DOMAIN}", "s", None, "", auth_results=("other; none", f"mx.example.com; dkim=pass header.d={DOMAIN}"))
    assert cotp.authenticated(good, DOMAIN, ["MX.example.com"]) is True


def test_client_supplied_messages_never_carry_authentication():
    m = Message.from_dict({"sender": f"a@{DOMAIN}", "subject": "code", "snippet": "123456",
                           "auth_results": [AR[0]], "Authentication-Results": AR[0]})
    assert m.auth_results == () and not cotp.authenticated(m, DOMAIN, ["mx.example.com"])


def test_raw_mail_parses_authentication_results():
    from job_apply_mcp.ingest.sources import message_from_bytes
    m = message_from_bytes(_eml(f"a@{DOMAIN}", 1, "code 77778888"))
    assert m.auth_results == AR and cotp.authenticated(m, DOMAIN, ["mx.example.com"])


def _write_user_config(text):
    from job_apply_mcp import paths
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    f.chmod(0o600)
    return f


@pytest.mark.parametrize(("env", "val"), [
    ("JOB_APPLY__VERIFICATION__SENDER_DOMAINS__GREENHOUSE", "gmail.com"),
    ("JOB_APPLY__INGEST__SOURCE", "mbox"),
    ("JOB_APPLY__INGEST__PATH", "/tmp/forged-mail"),
    ("JOB_APPLY__INGEST__IMAP__HOST", "imap.evil.example.net"),
])
def test_mail_settings_from_env_disable_unattended_codes(monkeypatch, env, val):
    assert load_settings()._otp_mail_trusted is True
    monkeypatch.setenv(env, val)
    assert load_settings()._otp_mail_trusted is False


@pytest.mark.parametrize("toml", ['[verification.sender_domains]\ngreenhouse = "gmail.com"\n',
                                  '[verification]\ntrusted_authserv_ids = ["attacker.example.net"]\n',
                                  '[ingest]\nsource = "mbox"\npath = "/tmp/forged-mail"\n'])
def test_mail_settings_from_project_file_disable_unattended_codes(tmp_path, toml):
    (tmp_path / "job-apply-mcp.toml").write_text(toml)
    assert load_settings(tmp_path)._otp_mail_trusted is False


def test_mail_settings_from_trusted_user_config_are_honoured(tmp_path):
    _write_user_config('[ingest]\nsource = "imap"\n[ingest.imap]\nhost = "imap.example.com"\n'
                       '[verification]\ntrusted_authserv_ids = ["mx.example.com"]\n')
    assert load_settings(tmp_path)._otp_mail_trusted is True
    (tmp_path / "job-apply-mcp.toml").write_text('[ingest.imap]\nhost = "imap.evil.example.net"\n')
    assert load_settings(tmp_path)._otp_mail_trusted is False


async def test_untrusted_mail_settings_mean_no_mail_read(tmp_path, monkeypatch):
    s = esettings(tmp_path)
    s._otp_mail_trusted = False
    consent.store_for(s).issue_consent(600)
    mail = Mail(_mail("Zx81Qa7P"))
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: mail)
    drv = AutoCodeDriver("Zx81Qa7P")
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert r.status == "roadblock" and "trusted user config" in r.detail
    assert mail.fetches == 0 and drv.entered == [] and book.state(POSTING.ref)["state"] == "unknown"


def test_mail_scope_alias_for_sibling_modules(cfg):
    assert consent.active(cfg, "mail") is None
    consent.store_for(cfg).issue_consent(600, scopes=["otp_unattended"])
    assert consent.active(cfg, "mail") is not None and consent.use(cfg, "mail", "ref=x") is not None
    with pytest.raises(ValueError):
        consent.active(cfg, "mailbox")



# ------------------------------------------------------------------ review round 3: no trusted authserv id -> off

async def test_forged_header_with_empty_trusted_list_is_refused(tmp_path, monkeypatch):
    s = esettings(tmp_path)
    s.verification.trusted_authserv_ids = []
    consent.store_for(s).issue_consent(600)
    forged = (f"mx.example.com; dkim=pass header.d={DOMAIN}; dmarc=pass header.from={DOMAIN}",)
    mail = Mail(_forged("Fq4Rg8Ht", forged))
    monkeypatch.setattr("job_apply_mcp.ingest.sources.make_source", lambda *a, **k: mail)
    drv = AutoCodeDriver("Fq4Rg8Ht")
    e, book = engine(tmp_path, s, drv)
    r = await _submit(e)
    assert r.status == "awaiting_code" and r.pending_id == "pend1"          # held for the human path
    assert "otp: needs_human" in r.detail and "trusted_authserv_ids" in r.detail
    assert mail.fetches == 0 and drv.entered == [] and book.state(POSTING.ref)["state"] == "awaiting_code"
    assert "Fq4Rg8Ht" not in json.dumps(r.model_dump())
    res = await cotp.fetch_code(s, sender_domain=DOMAIN, clicked_at=datetime.now(UTC) - timedelta(seconds=30),
                                source=mail)
    assert res.status == "unauthenticated" and res.code is None


def test_status_explains_how_to_enable_unattended_codes(cfg):
    assert "otp_unattended" not in consent.status(cfg)
    cfg.verification.trusted_authserv_ids = []
    st = consent.status(cfg)
    assert st["otp_unattended"].startswith("off:") and "trusted_authserv_ids" in st["otp_unattended"]


def test_trusted_authserv_ids_cannot_come_from_a_project_file(tmp_path):
    (tmp_path / "job-apply-mcp.toml").write_text('[verification]\ntrusted_authserv_ids = ["mx.example.com"]\n')
    assert load_settings(tmp_path)._otp_mail_trusted is False


# ------------------------------------------------------------------ integration with the vault (ADR-0046)

def test_vault_scopes_follow_a_real_consent_window(cfg):
    assert not consent.active(cfg, "credentials") and not consent.active(cfg, "mail")
    consent.store_for(cfg).issue_consent(600, scopes=["credentials"])
    assert consent.active(cfg, "credentials") and not consent.active(cfg, "mail")
    consent.store_for(cfg).issue_consent(600, scopes=["otp_unattended"])
    assert consent.active(cfg, "mail")
    assert set(consent.SCOPES) >= {"credentials", "mail"}


def test_vault_status_serializes_with_an_open_window(cfg):
    from job_apply_mcp.accounts import tools as vtools
    consent.store_for(cfg).issue_consent(600, scopes=["credentials"])

    class NoVault:
        def entries(self):
            return []
    out = vtools.status(cfg, vault=NoVault())
    assert out["consent"] is True and json.loads(json.dumps(out))["consent"] is True


def test_the_adr_0046_stub_module_is_gone():
    import job_apply_mcp.consent as c
    assert Path(c.__file__).name == "__init__.py" and callable(c.use) and callable(c.status)


@pytest.mark.parametrize("scopes", [None, ["credentials"], ["otp_unattended", "approve_submissions"]])
def test_confirmation_lists_each_granted_scope_and_its_real_effect(cfg, tmp_path, capsys, scopes):
    st = mk(tmp_path)
    argv = ["--for", "24h"] + (["--scopes", *scopes] if scopes else [])
    shown = []
    assert cli(argv, cfg, stdin=TTY(), stdout=TTY(), store=st,
               input_fn=lambda p: shown.append(capsys.readouterr().out) or "no") == 1   # printed BEFORE the prompt
    text = shown[0]
    granted = sorted(scopes or CONSENT_SCOPES)
    assert f"Scopes granted ({len(granted)}): {', '.join(granted)}" in text
    effects = {"credentials": "Sign in with vault passwords and create accounts with generated passwords on job sites",
               "otp_unattended": "Read verification codes from your mail and enter them",
               "approve_submissions": "approved automatically", "captcha_attempt": "visible CAPTCHA"}
    for s in granted:
        assert f"  {s}\n      unlocks: {CONSENT_SCOPES[s]}" in text and effects[s] in text
    for s in set(CONSENT_SCOPES) - set(granted):
        assert f"  {s}\n" not in text and s in text.split("Not granted:")[1]
    assert "Nothing uses this scope" not in text and st.status() == []


def test_scope_descriptions_are_accurate():
    assert "Nothing uses" not in " ".join(CONSENT_SCOPES.values())
    assert "generated passwords" in CONSENT_SCOPES["credentials"] and "`mail`" in CONSENT_SCOPES["otp_unattended"]
