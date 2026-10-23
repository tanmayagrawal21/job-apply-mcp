"""ADR-0046: credential vault, consent-gated logins and account creation. Fake keyring only; nothing real."""
import asyncio
import json
import os
import pickle
import sqlite3
import sys
import types

import pytest

from job_apply_mcp import consent
from job_apply_mcp.accounts import login as login_mod
from job_apply_mcp.accounts.cli import cli
from job_apply_mcp.accounts.login import AccountManager, RunReport, Terms
from job_apply_mcp.accounts.password import ALPHABET, generate_password, meets_policy
from job_apply_mcp.accounts.store import AccountsDB
from job_apply_mcp.accounts.tools import status as vault_status
from job_apply_mcp.accounts.vault import Vault, VaultError, default_backend, normalize_host
from job_apply_mcp.config import Settings
from job_apply_mcp.core.secrets import MASK, Secret, redact

HOST = "acme.wd5.myworkdayjobs.com"
USER = "pat.doe@example.com"
PW = "correct-horse-test-pw"


class FakeKeyring:
    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}
        self.gets: list[tuple[str, str]] = []

    def get_password(self, service, username):
        self.gets.append((service, username))
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


class TTY:
    def __init__(self, tty=True):
        self._t = tty

    def isatty(self):
        return self._t


class FakeOps:
    """Scripted LoginOps. Records every call; captures what the page would receive."""

    def __init__(self, kind="login", results=("ok",), signup="created", captcha=False, terms=(), vault=None):
        self._kind, self._results, self._signup = kind, list(results), signup
        self.captcha = captcha
        self._terms = list(terms)
        self.calls: list[str] = []
        self.filled: dict[str, str] = {}
        self.accepted: list[str] = []
        self.vault = vault
        self.stored_before_fill: bool | None = None

    def url(self):
        return f"https://{HOST}/en-US/careers/login"

    async def captcha_visible(self):
        self.calls.append("captcha_visible")
        return self.captcha

    async def kind(self):
        return self._kind

    async def open_signin(self):
        self._kind = "login"
        return True

    async def open_signup(self):
        self._kind = "signup"
        return True

    async def fill_username(self, u):
        self.calls.append("fill_username")
        self.filled["user"] = u
        return True

    async def fill_password(self, secret, *, confirm=False):
        assert isinstance(secret, Secret)
        self.calls.append("fill_password")
        if self.vault is not None and self.stored_before_fill is None:
            self.stored_before_fill = self.vault.get(HOST, self.filled["user"]) == secret
        self.filled["confirm" if confirm else "password"] = secret.reveal()
        return True

    async def terms(self):
        return self._terms

    async def accept_terms(self, t):
        self.accepted.append(t.ref)
        return True

    async def submit(self, kind):
        self.calls.append(f"submit_{kind}")
        return True

    async def login_result(self):
        return self._results.pop(0) if self._results else "unknown"

    async def signup_result(self):
        return self._signup


@pytest.fixture
def cfg(tmp_path):
    s = Settings()
    s.paths.data_dir = str(tmp_path / "data")
    return s


@pytest.fixture
def kr():
    return FakeKeyring()


@pytest.fixture
def vault(cfg, kr):
    return Vault.from_settings(cfg, kr)


@pytest.fixture
def consent_on(monkeypatch):
    scopes = {"credentials"}
    monkeypatch.setattr(consent, "active", lambda settings, scope: scope in scopes)
    return scopes


@pytest.fixture(autouse=True)
def _no_verifier():
    login_mod.register_email_verifier(None)
    yield
    login_mod.register_email_verifier(None)


def mgr(cfg, vault, tmp_path):
    return AccountManager(cfg, vault, AccountsDB(tmp_path / "data"), RunReport())


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ vault


def test_vault_roundtrip_index_has_no_secret(vault, kr):
    vault.put(HOST, USER, Secret(PW))
    assert kr.items == {(f"job-apply-mcp-vault:{HOST}", USER): PW}
    assert vault.get(HOST, USER) == Secret(PW)
    assert vault.entries()[0]["host"] == HOST and vault.usernames(HOST) == [USER]
    raw = vault.index_path.read_text()
    assert PW not in raw                                       # all platforms: the index never holds a secret
    if os.name != "nt":   # POSIX mode bits; on Windows the file inherits the per-user %LOCALAPPDATA% ACL (ADR-0046)
        assert (vault.index_path.stat().st_mode & 0o077) == 0
    assert vault.remove(HOST) == [USER] and kr.items == {} and vault.entries() == []


def test_host_is_exact_no_parent_or_sibling_fallback(vault):
    vault.put(HOST, USER, Secret(PW))
    assert vault.usernames("other.wd5.myworkdayjobs.com") == []
    assert vault.usernames("wd5.myworkdayjobs.com") == []
    assert vault.usernames(f"https://{HOST.upper()}:443/path?q=1") == [USER]
    for bad in ("", "*.myworkdayjobs.com", "localhost", "a b.example.com"):
        with pytest.raises(VaultError):
            normalize_host(bad)


def test_no_keyring_extra_means_no_vault(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(VaultError, match="keyring"):
        default_backend()


@pytest.mark.parametrize("modname", ["keyring.backends.fail", "keyrings.alt.file"])
def test_insecure_keyring_backend_refused(monkeypatch, modname):
    cls = type("Keyring", (), {"__module__": modname})
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_keyring=lambda: cls()))
    with pytest.raises(VaultError, match="does not protect"):
        default_backend()


# ------------------------------------------------------------------ CLI


def test_cli_requires_tty(cfg, kr, capsys):
    assert cli(["list"], cfg, stdin=TTY(False), stdout=TTY(), backend=kr) == 3
    assert cli(["add", HOST, "-u", USER], cfg, stdin=TTY(), stdout=TTY(False), backend=kr,
               getpass_fn=lambda _: PW) == 3
    assert kr.items == {}


def test_cli_add_list_rm_never_prints_secret(cfg, kr, capsys):
    prompts = []
    assert cli(["add", f"https://{HOST}/login", "--username", USER], cfg, stdin=TTY(), stdout=TTY(), backend=kr,
               getpass_fn=lambda p: prompts.append(p) or PW) == 0
    assert len(prompts) == 2 and (f"job-apply-mcp-vault:{HOST}", USER) in kr.items
    assert cli(["list"], cfg, stdin=TTY(), stdout=TTY(), backend=kr) == 0
    assert cli(["rm", HOST], cfg, stdin=TTY(), stdout=TTY(), backend=kr) == 0
    out = capsys.readouterr()
    assert HOST in out.out and USER in out.out and PW not in out.out + out.err
    assert kr.items == {}


def test_cli_mismatched_password_stores_nothing(cfg, kr):
    answers = iter([PW, PW + "x"])
    assert cli(["add", HOST, "-u", USER], cfg, stdin=TTY(), stdout=TTY(), backend=kr,
               getpass_fn=lambda _: next(answers)) == 1
    assert kr.items == {}


# ------------------------------------------------------------------ password policy


def test_generated_passwords_meet_policy():
    seen = set()
    for n in (20, 24, 32, 64):
        for _ in range(250):
            pw = generate_password(n).reveal()
            assert len(pw) == n and meets_policy(pw) and set(pw) <= set(ALPHABET)
            seen.add(pw)
    assert len(seen) == 1000                      # no repeats: real randomness


@pytest.mark.parametrize("n", [8, 19, 65])
def test_password_length_bounds(n):
    with pytest.raises(ValueError):
        generate_password(n)


@pytest.mark.parametrize("pw", ["Aa2!" * 4, "aaaaaaaaaaaaaaaaaaaaaaa2!", "AAAAAABBBBBBCCCCCC22!!",
                                "Abcdefghijkmnopqrstuvw2", "Abcdefghijkmnopqrst2!\"", "!Abcdefghijkmnopqrst2",
                                "Abcdefghijkmnopqrsttt2!", "Abcdefghijkmno pqrst2!"])
def test_policy_rejects(pw):
    assert not meets_policy(pw)


# ------------------------------------------------------------------ config trust


def _cfg_layers(tmp_path, monkeypatch, user="", proj="", env=None, via_env=False):
    from test_approvals import _cfg
    return _cfg(tmp_path, monkeypatch, user=user, proj=proj, env=env, via_env=via_env)


def test_auto_create_default_off():
    assert Settings().accounts.auto_create is False


def test_auto_create_honoured_from_trusted_user_config(tmp_path, monkeypatch):
    s = _cfg_layers(tmp_path, monkeypatch, user="[accounts]\nauto_create = true\n")
    assert s.accounts.auto_create is True


@pytest.mark.parametrize("raw", ["true", '"true"', '"on"'])
def test_auto_create_ignored_from_project_file(tmp_path, monkeypatch, raw):
    s = _cfg_layers(tmp_path, monkeypatch, proj=f"[accounts]\nauto_create = {raw}\n")
    assert s.accounts.auto_create is False


@pytest.mark.parametrize("raw", ["true", "1", "on"])
def test_auto_create_ignored_from_env(tmp_path, monkeypatch, raw):
    s = _cfg_layers(tmp_path, monkeypatch, env={"JOB_APPLY__ACCOUNTS__AUTO_CREATE": raw})
    assert s.accounts.auto_create is False


def test_auto_create_ignored_from_untrusted_user_file(tmp_path, monkeypatch):
    s = _cfg_layers(tmp_path, monkeypatch, user="[accounts]\nauto_create = true\n", via_env=True)
    assert s.accounts.auto_create is False


def test_login_attempts_capped_at_two(tmp_path, monkeypatch):
    s = _cfg_layers(tmp_path, monkeypatch, user="[accounts]\nmax_login_attempts = 1\n",
                    env={"JOB_APPLY__ACCOUNTS__MAX_LOGIN_ATTEMPTS": "2"})
    assert s.accounts.max_login_attempts == 1
    with pytest.raises(ValueError):
        Settings.model_validate({"accounts": {"max_login_attempts": 3}})


# ------------------------------------------------------------------ consent gate


def test_consent_stub_is_closed(cfg):
    assert consent.active(cfg, "credentials") is False and consent.active(cfg, "mail") is False


def test_no_consent_nothing_is_used(cfg, vault, kr, tmp_path):
    vault.put(HOST, USER, Secret(PW))
    kr.gets.clear()
    cfg.accounts.auto_create = True
    m = mgr(cfg, vault, tmp_path)
    for kind in ("login", "signup"):
        ops = FakeOps(kind=kind)
        out = run(m.handle_wall(ops, f"https://{HOST}/login", email=USER, dry_run=False))
        assert out.status == "skipped" and ops.calls == [] and ops.filled == {}
    vault.remove(HOST)
    ops = FakeOps(kind="signup")
    out = run(m.handle_wall(ops, f"https://{HOST}/login", email=USER, dry_run=False))
    assert out.status == "skipped" and ops.calls == []
    assert kr.gets == [(f"job-apply-mcp-vault:{HOST}", USER)]      # only remove()'s own cleanup read
    assert m.db.list() == [] and kr.items == {}


def test_driver_wall_detection_is_consent_gated():
    import inspect

    from job_apply_mcp.apply import browser_playwright as bp
    src = inspect.getsource(bp.PlaywrightDriver._run)
    assert 'consent.active(self.s, "credentials")' in src


# ------------------------------------------------------------------ login


def test_login_fills_exact_host_secret(cfg, vault, consent_on, tmp_path):
    vault.put(HOST, USER, Secret(PW))
    vault.put("globex.wd1.myworkdayjobs.com", USER, Secret("other-tenant-test-pw"))
    ops = FakeOps()
    out = run(mgr(cfg, vault, tmp_path).handle_wall(ops, f"https://{HOST}/x"))
    assert out.status == "logged_in" and ops.filled == {"user": USER, "password": PW}
    assert PW not in repr(out) and PW not in out.detail()


@pytest.mark.parametrize(("res", "flag"), [("wrong_password", "wrong_password"), ("locked", "lockout")])
def test_wrong_password_or_lockout_stops_host(cfg, vault, consent_on, tmp_path, res, flag):
    vault.put(HOST, USER, Secret(PW))
    m = mgr(cfg, vault, tmp_path)
    out = run(m.handle_wall(FakeOps(results=[res]), HOST))
    assert out.status == "blocked" and m.report.compact() == {HOST: [flag]} and m.attempts(HOST) == 1
    again = FakeOps()
    assert run(m.handle_wall(again, HOST)).status == "blocked" and again.calls == []


def test_at_most_two_attempts_per_host(cfg, vault, consent_on, tmp_path):
    vault.put(HOST, USER, Secret(PW))
    m = mgr(cfg, vault, tmp_path)
    ops = FakeOps(results=["unknown", "unknown", "ok"])
    out = run(m.handle_wall(ops, HOST))
    assert out.status == "blocked" and ops.calls.count("submit_login") == 2 and m.attempts(HOST) == 2
    assert "login_failed" in m.report.compact()[HOST]


def test_visible_captcha_is_handoff_never_filled(cfg, vault, consent_on, tmp_path):
    vault.put(HOST, USER, Secret(PW))
    m = mgr(cfg, vault, tmp_path)
    ops = FakeOps(captcha=True)
    out = run(m.handle_wall(ops, HOST))
    assert out.status == "needs_human" and "CAPTCHA" in out.reason and ops.filled == {}
    assert m.report.compact() == {HOST: ["captcha"]}


def test_verification_code_after_login_is_human_only(cfg, vault, consent_on, tmp_path):
    vault.put(HOST, USER, Secret(PW))
    out = run(mgr(cfg, vault, tmp_path).handle_wall(FakeOps(results=["verification"]), HOST))
    assert out.status == "needs_human" and "verification code" in out.reason


def test_ambiguous_usernames_hand_off(cfg, vault, consent_on, tmp_path):
    vault.put(HOST, USER, Secret(PW))
    vault.put(HOST, "sam.roe@example.com", Secret("second-test-pw"))
    ops = FakeOps()
    assert run(mgr(cfg, vault, tmp_path).handle_wall(ops, HOST)).status == "needs_human" and ops.filled == {}
    ops = FakeOps()
    assert run(mgr(cfg, vault, tmp_path).handle_wall(ops, HOST, email=USER)).status == "logged_in"
    assert ops.filled["password"] == PW


# ------------------------------------------------------------------ account creation

REQ = Terms("0", "I agree to the Terms of Use and Privacy Policy", True, "https://acme.example.com/terms")
OPT = Terms("1", "Send me job alerts and newsletters", False)


def test_no_entry_and_auto_create_off_hands_off(cfg, vault, consent_on, tmp_path):
    ops = FakeOps(kind="signup")
    out = run(mgr(cfg, vault, tmp_path).handle_wall(ops, HOST, email=USER, dry_run=False))
    assert out.status == "needs_human" and "auto_create" in out.reason and ops.filled == {}


def test_dry_run_never_creates(cfg, vault, kr, consent_on, tmp_path):
    cfg.accounts.auto_create = True
    ops = FakeOps(kind="signup")
    out = run(mgr(cfg, vault, tmp_path).handle_wall(ops, HOST, email=USER, dry_run=True))
    assert out.status == "needs_human" and kr.items == {} and ops.filled == {}


def test_create_stores_keychain_first_and_db_has_no_password(cfg, vault, kr, consent_on, tmp_path):
    cfg.accounts.auto_create = True
    m = mgr(cfg, vault, tmp_path)
    ops = FakeOps(kind="signup", terms=[REQ, OPT], vault=vault)
    out = run(m.handle_wall(ops, f"https://{HOST}/login", email=USER, dry_run=False))
    assert out.status == "created" and ops.stored_before_fill is True
    pw = ops.filled["password"]
    assert ops.filled["confirm"] == pw and meets_policy(pw) and len(pw) >= 20
    assert kr.items[(f"job-apply-mcp-vault:{HOST}", USER)] == pw
    assert ops.accepted == ["0"]                                       # optional box left unticked
    row = m.db.get(HOST, USER)
    assert row["status"] == "active" and row["terms_url"] == REQ.url and row["created_at"]
    m.db.close()
    db_path = tmp_path / "data" / "accounts.db"
    cols = [r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(ats_accounts)")]
    assert not any("pass" in c or "secret" in c for c in cols)
    assert pw.encode() not in db_path.read_bytes()
    assert pw not in repr(out) and pw not in json.dumps(m.report.compact())


def test_create_once_per_host_per_run(cfg, vault, consent_on, tmp_path):
    cfg.accounts.auto_create = True
    m = mgr(cfg, vault, tmp_path)
    run(m.handle_wall(FakeOps(kind="signup", signup="exists"), HOST, email=USER, dry_run=False))
    ops = FakeOps(kind="signup")
    assert run(m.handle_wall(ops, HOST, email=USER, dry_run=False)).status == "blocked" and ops.filled == {}


def test_existing_account_rolls_back_vault_entry(cfg, vault, kr, consent_on, tmp_path):
    cfg.accounts.auto_create = True
    m = mgr(cfg, vault, tmp_path)
    out = run(m.handle_wall(FakeOps(kind="signup", signup="exists"), HOST, email=USER, dry_run=False))
    assert out.status == "needs_human" and kr.items == {} and m.db.get(HOST, USER)["status"] == "failed"


def test_email_verification_pending_without_mail_consent(cfg, vault, consent_on, tmp_path):
    cfg.accounts.auto_create = True
    called = []

    async def verifier(host, email):
        called.append(host)
        return True

    login_mod.register_email_verifier(verifier)
    m = mgr(cfg, vault, tmp_path)
    out = run(m.handle_wall(FakeOps(kind="signup", signup="verify_email"), HOST, email=USER, dry_run=False))
    assert out.status == "pending_verification" and called == []
    assert m.db.get(HOST, USER)["status"] == "pending_verification"


def test_email_verification_uses_mail_path_when_consented(cfg, vault, consent_on, tmp_path):
    cfg.accounts.auto_create = True
    consent_on.add("mail")

    async def verifier(host, email):
        return host == HOST and email == USER

    login_mod.register_email_verifier(verifier)
    m = mgr(cfg, vault, tmp_path)
    out = run(m.handle_wall(FakeOps(kind="signup", signup="verify_email"), HOST, email=USER, dry_run=False))
    assert out.status == "created" and m.db.get(HOST, USER)["status"] == "active"


@pytest.mark.parametrize("box", [
    Terms("0", "I confirm this application is my own work", True),                    # policy stop phrase
    Terms("0", "Subscribe me to marketing emails", True),                              # required but not terms
    Terms("0", "I agree to join the talent community and receive job alerts", True),   # optional-type text
])
def test_unsafe_required_checkbox_hands_off_before_storing(cfg, vault, kr, consent_on, tmp_path, box):
    cfg.accounts.auto_create = True
    ops = FakeOps(kind="signup", terms=[box])
    out = run(mgr(cfg, vault, tmp_path).handle_wall(ops, HOST, email=USER, dry_run=False))
    assert out.status == "needs_human" and kr.items == {} and ops.filled == {} and ops.accepted == []


def test_captcha_on_signup_rolls_back(cfg, vault, kr, consent_on, tmp_path):
    cfg.accounts.auto_create = True

    class LateCaptcha(FakeOps):
        async def captcha_visible(self):
            self.calls.append("captcha_visible")
            return "fill_password" in self.calls

    ops = LateCaptcha(kind="signup")
    out = run(mgr(cfg, vault, tmp_path).handle_wall(ops, HOST, email=USER, dry_run=False))
    assert out.status == "needs_human" and "submit_signup" not in ops.calls and kr.items == {}


# ------------------------------------------------------------------ MCP surface


def test_vault_status_has_no_secrets(cfg, vault, consent_on):
    vault.put(HOST, USER, Secret(PW))
    out = json.dumps(vault_status(cfg, vault))
    assert HOST in out and PW not in out and json.loads(out)["consent"] is True


def test_vault_status_is_the_only_vault_tool():
    from job_apply_mcp.server import mcp
    names = [t.name for t in asyncio.run(mcp.list_tools())]
    assert [n for n in names if "vault" in n or "credential" in n or "password" in n] == ["vault_status"]


# ------------------------------------------------------------------ property: secrets never leak

_SPECIAL = ['"', "\\", "{", "}", "%s", "%(x)s", "\n", "é", "😀", "<", "'", "$"]


def _random_secrets(n=60, seed=46):
    import random
    rnd = random.Random(seed)
    for i in range(n):
        core = "".join(rnd.choice(ALPHABET) for _ in range(rnd.randint(8, 40)))
        yield f"{core}{rnd.choice(_SPECIAL)}{i}" if i % 2 else core


class LeakyPage:
    """Fake Playwright page whose fill raises an error that ECHOES the value, like a careless driver might."""

    url = f"https://{HOST}/login"

    def __init__(self):
        self.received: list[str] = []

    def locator(self, sel):
        page = self
        confirm = "confirm" in sel or "verify" in sel

        class Loc:
            async def count(self):
                return 0 if confirm else 1

            def nth(self, i):
                return self

            async def is_visible(self):
                return True

            async def fill(self, value):
                if value == USER:
                    return
                page.received.append(value)
                raise RuntimeError(f"fill failed for value={value!r}")

        return Loc()


class LeakyKeyring(FakeKeyring):
    def set_password(self, service, username, password):
        raise RuntimeError(f"backend locked; could not store {password}")


def test_property_secrets_never_in_logs_output_or_exceptions(cfg, tmp_path, caplog, capsys, monkeypatch):
    import logging
    import traceback

    from job_apply_mcp.accounts.browser_ops import PlaywrightLoginOps
    from job_apply_mcp.core import output

    monkeypatch.setattr(consent, "active", lambda s, scope: True)
    cfg.accounts.auto_create = True
    caplog.set_level(logging.DEBUG)
    for k, secret in enumerate(_random_secrets()):
        sink: list[str] = []
        kr = FakeKeyring()
        v = Vault(tmp_path / f"d{k}", kr)
        host = f"t{k}.wd5.myworkdayjobs.com"
        v.put(host, USER, Secret(secret))
        sv = Secret(secret)
        sink += [repr(sv), str(sv), f"{sv}", f"{sv!r}", json.dumps({"s": sv}, default=str),
                 "{}".format(sv), "%s" % (sv,)]  # noqa: UP031, UP032 - every formatting path on purpose
        with pytest.raises(TypeError):
            pickle.dumps(sv)
        logging.getLogger("job_apply_mcp.test").warning("value %s / %r / raw %s", sv, sv, secret)
        try:
            raise RuntimeError(f"boom {secret}")
        except RuntimeError:
            logging.getLogger("job_apply_mcp.test").exception("caught")
        # login flow end to end, page raising errors that contain the value
        m = AccountManager(cfg, v, AccountsDB(tmp_path / f"d{k}"), RunReport())
        page = LeakyPage()
        ops = PlaywrightLoginOps(page, lambda: asyncio.sleep(0, result=False), settle_ms=0)
        out = run(m.handle_wall(ops, f"https://{host}/login", email=USER, dry_run=False))
        assert page.received == [secret]              # it did reach the page, and only the page
        sink += [repr(out), out.detail(), str(ops.last_error), json.dumps(m.report.compact())]
        # backend errors that echo the value
        try:
            Vault(tmp_path / f"e{k}", LeakyKeyring()).put(host, USER, Secret(secret))
        except VaultError as e:
            sink += [str(e), repr(e), "".join(traceback.format_exception(e))]
        # account creation through a backend that refuses
        m2 = AccountManager(cfg, Vault(tmp_path / f"f{k}", LeakyKeyring()), AccountsDB(tmp_path / f"f{k}"))
        out2 = run(m2.handle_wall(FakeOps(kind="signup"), f"https://n{k}.icims.com/", email=USER, dry_run=False))
        sink += [repr(out2)]
        # MCP output envelope
        async def tool(secret=secret, v=v):
            return json.dumps({"echo": secret, "status": vault_status(cfg, v)})
        tool.__name__ = "vault_status"
        sink.append(run(output.guarded(tool, lambda: 4000)()))

        async def bad(secret=secret):
            raise ValueError(f"oops {secret}")
        bad.__name__ = "vault_status"
        sink.append(run(output.guarded(bad, lambda: 4000)()))
        sink += [v.index_path.read_text(), redact(secret)]
        cap = capsys.readouterr()
        everything = "\n".join(sink) + caplog.text + cap.out + cap.err
        for form in {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]}:
            assert form not in everything, f"secret #{k} leaked"
        assert MASK in caplog.text
        caplog.clear()
