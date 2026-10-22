"""ADR-0027: human-signed time-limited grants, enforcement, otp_assist."""
import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from job_apply_mcp.config import Settings
from job_apply_mcp.grants import GrantError, GrantStore
from job_apply_mcp.grants import otp as otp_mod
from job_apply_mcp.grants.cli import cli
from job_apply_mcp.grants.store import parse_duration
from job_apply_mcp.ingest.confirmations import Message

SECRET = b"k" * 32


class TTY:
    def __init__(self, tty=True):
        self._t = tty

    def isatty(self):
        return self._t


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def cfg(tmp_path):
    s = Settings()
    s.paths.data_dir = str(tmp_path / "data")
    s.grants.use_keyring = False
    return s


def mk(tmp_path, clock=None, user="alice"):
    return GrantStore(tmp_path / "d", SECRET, clock=clock or time.time, user=user)


def test_parse_duration():
    assert parse_duration("90s") == 90 and parse_duration("1h30m") == 5400 and parse_duration("15") == 900
    with pytest.raises(ValueError):
        parse_duration("soon")


# ---- CLI / TTY
def test_grant_refuses_without_tty(cfg, tmp_path, capsys):
    st = mk(tmp_path)
    for i, o in ((TTY(False), TTY(True)), (TTY(True), TTY(False))):
        rc = cli(["approve_submissions", "--for", "1h"], cfg, stdin=i, stdout=o, input_fn=lambda _: "x", store=st)
        assert rc == 3
    assert "human-only" in capsys.readouterr().out and st.active() == []


def test_grant_requires_exact_phrase_and_shows_scope(cfg, tmp_path, capsys):
    st = mk(tmp_path)
    assert cli(["approve_submissions", "--for", "1h"], cfg, stdin=TTY(), stdout=TTY(),
               input_fn=lambda _: "yes", store=st) == 1
    assert st.active() == []
    prompts = []

    def ok(prompt):
        prompts.append(prompt)
        return "I authorize approve_submissions for 1h00m"
    assert cli(["approve_submissions", "--for", "1h", "--max-uses", "2"], cfg, stdin=TTY(), stdout=TTY(),
               input_fn=ok, store=st) == 0
    out = capsys.readouterr().out
    assert "APPROVE application submissions" in out and "Max uses:  2" in out
    ((g, left),) = st.active()
    assert g.scope == "approve_submissions" and left == 2


def test_duration_cap(cfg, tmp_path, capsys):
    rc = cli(["otp_assist", "--for", "13h"], cfg, stdin=TTY(), stdout=TTY(), input_fn=lambda _: "", store=mk(tmp_path))
    assert rc == 2 and "12h" in capsys.readouterr().out


def test_cap_not_relaxable_from_env(monkeypatch, tmp_path):
    from job_apply_mcp.config import load_settings
    monkeypatch.setenv("JOB_APPLY__GRANTS__MAX_DURATION_S", "999999")
    assert load_settings().grants.max_duration_s == 43200
    monkeypatch.setenv("JOB_APPLY__GRANTS__MAX_DURATION_S", "60")
    assert load_settings().grants.max_duration_s == 60


def test_list_and_revoke(cfg, tmp_path, capsys):
    st = mk(tmp_path)
    a, b = st.issue("otp_assist", 600), st.issue("approve_submissions", 600)
    assert cli(["--list"], cfg, store=st) == 0 and a.id in capsys.readouterr().out
    assert cli(["--revoke", a.id], cfg, store=st) == 0
    assert [g.id for g, _ in st.active()] == [b.id]
    cli(["--revoke"], cfg, store=st)
    assert st.active() == []


# ---- enforcement
def test_tamper_signature_rejected(tmp_path):
    st = mk(tmp_path)
    g = st.issue("otp_assist", 60)
    rows = json.loads(st._grants_file.read_text())
    rows[0]["expires"] += 99999                                # extend the window by hand
    st._grants_file.write_text(json.dumps(rows))
    with pytest.raises(GrantError, match="tampered"):
        st.check("otp_assist")
    assert st.active() == []
    rows[0]["expires"] -= 99999
    rows[0]["scope"] = "approve_submissions"                   # change scope
    st._grants_file.write_text(json.dumps(rows))
    with pytest.raises(GrantError):
        st.check("approve_submissions")
    assert g.id


def test_forged_with_wrong_secret_rejected(tmp_path):
    mk(tmp_path).issue("otp_assist", 60)
    other = GrantStore(tmp_path / "d", b"z" * 32, user="alice")
    with pytest.raises(GrantError):
        other.check("otp_assist")


def test_expiry(tmp_path):
    c = Clock()
    st = mk(tmp_path, c)
    st.issue("otp_assist", 60)
    st.check("otp_assist")
    c.t += 61
    with pytest.raises(GrantError, match="expired"):
        st.check("otp_assist")


def test_max_uses_and_audit(tmp_path):
    st = mk(tmp_path)
    g = st.issue("approve_submissions", 600, max_uses=2)
    st.check("approve_submissions", detail="ref=a")
    st.check("approve_submissions", detail="ref=b")
    with pytest.raises(GrantError, match="max_uses"):
        st.check("approve_submissions")
    ev = [e["event"] for e in st.audit_rows()]
    assert ev.count("use") == 2 and "denied" in ev and "issued" in ev
    assert all(e["grant"] == g.id for e in st.audit_rows() if e["event"] == "use")


def test_audit_tamper_fails_closed_for_limited_grants(tmp_path):
    st = mk(tmp_path)
    st.issue("approve_submissions", 600, max_uses=1)
    st.check("approve_submissions")
    st.issue("otp_assist", 60)                                  # so the use line is not the log tail
    lines = st._audit_file.read_text().splitlines()
    st._audit_file.write_text("\n".join(lines[:1] + lines[2:]) + "\n")        # drop the "use" line mid-chain
    with pytest.raises(GrantError):
        st.check("approve_submissions")


def test_wrong_os_user(tmp_path):
    mk(tmp_path, user="alice").issue("otp_assist", 60)
    with pytest.raises(GrantError, match="OS user"):
        mk(tmp_path, user="mallory").check("otp_assist")


def test_wrong_scope_and_none(tmp_path):
    st = mk(tmp_path)
    with pytest.raises(GrantError):
        st.check("otp_assist")
    st.issue("approve_submissions", 60)
    with pytest.raises(GrantError):
        st.check("otp_assist")


def test_secret_file_fallback_is_0600(tmp_path, monkeypatch):
    import os

    from job_apply_mcp.grants.store import load_secret
    f = tmp_path / "cfg" / "grant.key"
    s1 = load_secret(use_keyring=False, key_file=f)
    assert load_secret(use_keyring=False, key_file=f) == s1 and len(s1) == 32
    if os.name != "nt":
        assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_mcp_has_no_grant_mutation_tools():
    from job_apply_mcp.server import mcp
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "grants_status" in names
    assert not {n for n in names if "grant" in n} - {"grants_status"}


def test_status_is_read_only_and_hides_sig(cfg):
    from job_apply_mcp.grants import tools
    st = GrantStore(cfg.paths.data_dir, use_keyring=False)
    st.issue("otp_assist", 600)
    out = tools.status(cfg)
    assert out["active"][0]["scope"] == "otp_assist" and "sig" not in json.dumps(out)


# ---- engine integration
async def test_engine_uses_grant_to_approve(cfg, tmp_path):
    from job_apply_mcp.grants import try_use
    assert try_use(cfg, "approve_submissions") is None
    GrantStore(cfg.paths.data_dir, use_keyring=False).issue("approve_submissions", 600, max_uses=1)
    g = try_use(cfg, "approve_submissions", "ref=x")
    assert g is not None and try_use(cfg, "approve_submissions") is None


# ---- otp_assist
class Src:
    def __init__(self, msgs):
        self.msgs = msgs

    def fetch(self, since):
        return self.msgs


NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def msg(minutes_ago, sender="no-reply@mail.example-ats.com", body="Your verification code is 482912.", subj="Code"):
    return Message(sender=sender, subject=subj, date=NOW - timedelta(minutes=minutes_ago), snippet=body)


@pytest.fixture
def granted(cfg):
    GrantStore(cfg.paths.data_dir, use_keyring=False).issue("otp_assist", 600)
    return cfg


async def test_otp_requires_grant(cfg):
    asked = []

    async def elicit(m):
        asked.append(m)
        return True
    r = await otp_mod.assist_code(cfg, sender_domain="example-ats.com", elicit=elicit, source=Src([msg(1)]), now=NOW)
    assert r.status == "no_grant" and r.code is None and not asked


async def test_otp_needs_confirmation_and_masks(granted):
    seen = []

    async def yes(m):
        seen.append(m)
        return True
    r = await otp_mod.assist_code(granted, sender_domain="example-ats.com", elicit=yes, source=Src([msg(1)]), now=NOW)
    assert r.status == "confirmed" and r.code == "482912"
    assert "••••12" in seen[0] and "482912" not in seen[0] and "submit?" in seen[0]
    assert "482912" not in repr(r)


async def test_otp_declined_returns_no_code(granted):
    async def no(m):
        return False
    r = await otp_mod.assist_code(granted, sender_domain="example-ats.com", elicit=no, source=Src([msg(1)]), now=NOW)
    assert r.status == "declined" and r.code is None


async def test_otp_no_elicitation_falls_back_masked_only(granted):
    r = await otp_mod.assist_code(granted, sender_domain="example-ats.com", elicit=None, source=Src([msg(1)]), now=NOW)
    assert r.status == "fallback" and r.code is None and r.masked == "••••12"


async def test_otp_only_latest_message_domain_and_window(granted):
    old_other = msg(2, body="code 111111")
    newer = msg(1, body="code 222222")
    spoof = msg(0, sender="x@example-ats.com.evil.test", body="code 333333")
    stale = msg(30, body="code 444444")
    got = []

    async def yes(m):
        got.append(m)
        return True
    r = await otp_mod.assist_code(granted, sender_domain="example-ats.com", elicit=yes,
                                  source=Src([old_other, newer, spoof, stale]), now=NOW)
    assert r.code == "222222"
    r = await otp_mod.assist_code(granted, sender_domain="example-ats.com", elicit=yes, source=Src([stale]), now=NOW)
    assert r.status == "no_message"


async def test_otp_code_never_logged(granted, caplog):
    caplog.set_level("DEBUG")

    async def yes(m):
        return True
    await otp_mod.assist_code(granted, sender_domain="example-ats.com", elicit=yes, source=Src([msg(1)]), now=NOW)
    st = GrantStore(granted.paths.data_dir, use_keyring=False)
    blob = st._audit_file.read_text() + caplog.text + json.dumps(st.audit_rows())
    assert "482912" not in blob and "otp_confirmed" in blob


async def test_otp_grant_use_limited(granted):
    st = GrantStore(granted.paths.data_dir, use_keyring=False)
    st.revoke("all")
    st.issue("otp_assist", 600, max_uses=1)

    async def yes(m):
        return True
    r1 = await otp_mod.assist_code(granted, sender_domain="example-ats.com", elicit=yes, source=Src([msg(1)]), now=NOW)
    r2 = await otp_mod.assist_code(granted, sender_domain="example-ats.com", elicit=yes, source=Src([msg(1)]), now=NOW)
    assert r1.status == "confirmed" and r2.status == "no_grant"
