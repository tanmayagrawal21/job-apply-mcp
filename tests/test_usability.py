"""Packaging, init wizard, doctor, paths and HTTP transport (ADR-0020). Offline."""
from __future__ import annotations

import json
import os
import tomllib

import httpx
import pytest

from job_apply_mcp import __version__, cli, doctor, httpserve, paths, setup_wizard
from job_apply_mcp.config import Settings, load_settings
from job_apply_mcp.profile import load_profile


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_APPLY_MCP_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path / "cfg" / "job-apply-mcp"


def _init(cfgdir, runtimes=(), **kw):
    lines: list[str] = []
    rc = setup_wizard.run_init(yes=True, config_dir=cfgdir, detect=lambda: list(runtimes), ram_gb=16,
                               out=lines.append, **kw)
    return rc, "\n".join(lines)


def _toml(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_version_matches_metadata():
    assert __version__ and __version__[0].isdigit()


def test_paths_posix_and_windows(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.default_config_dir_str() == "~/.config/job-apply-mcp"
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert paths.config_dir() == tmp_path / "Roaming" / "job-apply-mcp"
    assert paths.data_dir() == tmp_path / "Local" / "job-apply-mcp"


def test_init_creates_valid_files_with_dry_run(cfgdir, monkeypatch):
    rc, out = _init(cfgdir, contact={"first_name": "Sam", "last_name": "Tester"})
    assert rc == 0
    for name in ("config.toml", "profile.toml", "controls.toml"):
        _toml(cfgdir / name)
    monkeypatch.setenv("JOB_APPLY_MCP_CONFIG", str(cfgdir / "config.toml"))
    s = load_settings()
    assert s.dry_run is True and s.control.rules_file == str(cfgdir / "controls.toml")
    prof = load_profile(cfgdir / "profile.toml")
    assert prof.contact.first_name == "Sam" and prof.contact.email.endswith("example.com")
    assert '"job-apply"' in out and "mcpServers" in out


def test_init_never_overwrites_without_force(cfgdir):
    _init(cfgdir)
    (cfgdir / "profile.toml").write_text("# mine\n", encoding="utf-8")
    _, out = _init(cfgdir)
    assert (cfgdir / "profile.toml").read_text(encoding="utf-8") == "# mine\n" and "kept existing" in out
    _init(cfgdir, force=True)
    assert (cfgdir / "profile.toml.bak").read_text(encoding="utf-8") == "# mine\n"
    assert "# mine" not in (cfgdir / "profile.toml").read_text(encoding="utf-8")


def test_init_detects_ollama_and_suggests_pull(cfgdir):
    rt = setup_wizard.Runtime("ollama", "Ollama", "http://127.0.0.1:11434", ["qwen3:4b"])
    _, out = _init(cfgdir, runtimes=[rt])
    assert _toml(cfgdir / "config.toml")["llm"]["tiers"]["small"]["model"] == "qwen3:4b"
    assert "ollama pull gemma4:12b-it-qat" in out


def test_init_without_runtime_uses_client_tiers(cfgdir):
    _init(cfgdir)
    cfg = _toml(cfgdir / "config.toml")
    assert cfg["llm"]["tiers"]["small"]["provider"] == "client"
    Settings.model_validate(cfg)


def test_init_openai_compat_runtime(cfgdir):
    rt = setup_wizard.Runtime("openai_compat", "LM Studio", "http://127.0.0.1:1234/v1", ["some-model"])
    _init(cfgdir, runtimes=[rt])
    cfg = _toml(cfgdir / "config.toml")
    assert cfg["llm"]["providers"]["openai_compat"]["base_url"].endswith(":1234/v1")
    assert cfg["llm"]["tiers"]["small"]["model"] == "some-model"


def test_tier_defaults_by_ram():
    assert setup_wizard.tier_defaults(4).small == "qwen3:1.7b" and setup_wizard.tier_defaults(4).narrative is None
    assert setup_wizard.tier_defaults(12).narrative == "qwen2.5:7b"
    assert setup_wizard.tier_defaults(32).narrative.startswith("gemma")


def test_profile_values_are_escaped():
    text = setup_wizard.render_profile(
        (setup_wizard.examples_dir() / "profile.example.toml").read_text(encoding="utf-8"),
        {"first_name": 'Zoe "Z" \\ O\'Neil'})
    assert tomllib.loads(text)["contact"]["first_name"] == 'Zoe "Z" \\ O\'Neil'


def test_doctor_reports_and_fails_on_bad_config(cfgdir, monkeypatch, tmp_path):
    _init(cfgdir)
    monkeypatch.setenv("JOB_APPLY_MCP_CONFIG", str(cfgdir / "config.toml"))
    monkeypatch.setenv("JOB_APPLY__PATHS__DATA_DIR", str(tmp_path / "d"))
    lines: list[str] = []
    assert doctor.run_doctor(network=False, out=lines.append) == 0
    assert any("dry_run = true" in ln for ln in lines) and any("fictional example" in ln for ln in lines)
    (cfgdir / "config.toml").write_text("dry_run = 'maybe'\n", encoding="utf-8")
    lines.clear()
    assert doctor.run_doctor(network=False, out=lines.append) == 1
    assert any(ln.startswith("[FAIL] config") for ln in lines)


def test_doctor_unwritable_data_dir(cfgdir, monkeypatch, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setenv("JOB_APPLY__PATHS__DATA_DIR", str(f / "sub"))
    checks = {c.name: c for c in doctor.run_checks(network=False)}
    assert checks["data_dir"].status == "FAIL" and checks["data_dir"].fix


def test_cli_defaults_to_serve(monkeypatch):
    seen = {}
    monkeypatch.setattr("job_apply_mcp.server.mcp.run", lambda *a, **k: seen.setdefault("run", True))
    cli.main([])
    assert seen["run"]
    args = cli.build_parser().parse_args(["serve", "--transport", "http", "--port", "9000"])
    assert (args.host, args.port) == ("127.0.0.1", 9000)


def test_cli_flags_without_subcommand_mean_serve(monkeypatch):
    called = {}
    monkeypatch.setattr("job_apply_mcp.httpserve.run_http", lambda mcp, h, p: called.update(h=h, p=p))
    cli.main(["--transport", "http", "--port", "1234"])
    assert called == {"h": "127.0.0.1", "p": 1234}


def test_non_loopback_requires_token():
    for h in ("127.0.0.1", "::1", "localhost"):
        assert httpserve.is_loopback(h)
    for h in ("0.0.0.0", "", "10.0.0.5"):
        assert not httpserve.is_loopback(h)
    with pytest.raises(SystemExit):
        httpserve.check_bind("0.0.0.0", None)
    httpserve.check_bind("0.0.0.0", "tok")
    httpserve.check_bind("127.0.0.1", None)


async def test_bearer_middleware():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi"})

    app = httpserve.BearerAuth(inner, "s3cret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "s3cret"})).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer s3cret"})).text == "hi"
        assert (await c.get("/healthz")).json() == {"status": "ok"}
    open_app = httpserve.BearerAuth(inner, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=open_app), base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 200


async def test_real_app_rejects_without_token():
    from job_apply_mcp.server import mcp

    app = httpserve.build_app(mcp, "127.0.0.1", 8765, "tok")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as c:
        r = await c.post("/mcp", json={})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_client_snippet_is_json_serialisable():
    json.dumps(setup_wizard.client_snippet("uvx"))
    assert setup_wizard.client_snippet("python")["args"] == ["-m", "job_apply_mcp"]


def test_configuration_docs_are_current():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "gen_config_docs.py"
    spec = importlib.util.spec_from_file_location("gen_config_docs", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.OUT.read_text(encoding="utf-8") == mod.render(), "run: python scripts/gen_config_docs.py"


def test_tools_doc_covers_every_registered_tool():
    import asyncio
    from pathlib import Path

    from job_apply_mcp.server import mcp

    doc = (Path(__file__).resolve().parents[1] / "docs" / "tools.md").read_text(encoding="utf-8")
    names = [t.name for t in asyncio.run(mcp.list_tools())]
    assert names and [n for n in names if f"`{n}`" not in doc] == []


def _cfg_text(path, body="dry_run = true\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)


def test_xdg_dirs_honoured_everywhere_and_relative_ignored(monkeypatch, tmp_path):
    for p in ("linux", "win32"):
        monkeypatch.setattr(paths.sys, "platform", p)
        for k in ("CONFIG", "DATA", "CACHE"):
            monkeypatch.setenv(f"XDG_{k}_HOME", str(tmp_path / k))
        assert paths.config_dir() == tmp_path / "CONFIG" / "job-apply-mcp"
        assert paths.data_dir() == tmp_path / "DATA" / "job-apply-mcp"
        assert paths.cache_dir() == tmp_path / "CACHE" / "job-apply-mcp"
        assert paths.privacy_terms_file().parent == paths.config_dir()
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    assert paths.config_dir() == paths.Path.home() / ".config" / "job-apply-mcp"
    assert paths.cache_dir() == paths.Path.home() / ".cache" / "job-apply-mcp"


def test_server_and_doctor_read_the_xdg_config_not_home(monkeypatch, tmp_path):
    from job_apply_mcp import server
    home = tmp_path / "realhome"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("JOB_APPLY_MCP_CONFIG", raising=False)
    _cfg_text(home / ".config" / "job-apply-mcp" / "config.toml", "[policy]\ndedupe_days = 11\n")
    xdg = tmp_path / "xdg"
    _cfg_text(xdg / "job-apply-mcp" / "config.toml", "[policy]\ndedupe_days = 22\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setattr(server, "_svc", None)
    assert server.svc().settings.policy.dedupe_days == 22
    checks, s = doctor._config_checks()
    assert s is not None and s.policy.dedupe_days == 22
    assert any(str(xdg / "job-apply-mcp" / "config.toml") in c.detail for c in checks)
    assert not any(str(home) in c.detail for c in checks)
    monkeypatch.setattr(server, "_svc", None)


def test_trusted_config_is_the_fixed_path_not_xdg(monkeypatch, tmp_path):
    from job_apply_mcp import config
    fixed = paths.trusted_config_file()
    _cfg_text(fixed)
    assert fixed == paths._real_home() / ".config" / "job-apply-mcp" / "config.toml" or os.name == "nt"
    assert config._user_file_trusted(fixed) == (True, "")
    xdg = tmp_path / "repo" / ".x"                              # e.g. a repo's .mcp.json env block
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    f = xdg / "job-apply-mcp" / "config.toml"
    _cfg_text(f)
    assert paths.config_file() == f and paths.trusted_config_file() == fixed
    assert config._user_file_trusted(f)[0] is False
    monkeypatch.setenv("JOB_APPLY_MCP_CONFIG", str(f))
    assert config._user_file_trusted(f)[0] is False
    monkeypatch.delenv("JOB_APPLY_MCP_CONFIG")
    if os.name != "nt":                                         # POSIX mode bits only; Windows skips this check
        fixed.chmod(0o666)
        assert config._user_file_trusted(fixed)[0] is False     # group/world-writable


_RELAX = ('dry_run = false\n[apply]\napproval_mode = "agent"\nauto_approve = true\n'
          '[answers]\nautonomy = "infer"\n')


def test_env_set_xdg_config_cannot_relax_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("JOB_APPLY_MCP_CONFIG", raising=False)
    xdg = tmp_path / "repo" / ".x"
    _cfg_text(xdg / "job-apply-mcp" / "config.toml", _RELAX)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    s = load_settings()
    assert s.dry_run is True and s.apply.approval_mode != "agent" and not s.apply.auto_approve
    assert s.answers.autonomy != "infer"
    _cfg_text(paths.trusted_config_file(), _RELAX)              # the same text at the fixed path IS trusted
    monkeypatch.delenv("XDG_CONFIG_HOME")
    if os.name != "nt":
        assert load_settings().dry_run is False


def test_state_dir_ignores_xdg_data_home_unless_trusted_config_sets_it(monkeypatch, tmp_path):
    from job_apply_mcp.config import state_path
    monkeypatch.delenv("JOB_APPLY_MCP_CONFIG", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fresh"))
    s = Settings()
    s.paths.state_dir = None
    assert state_path(s) == paths.state_dir() and tmp_path / "fresh" not in state_path(s).parents
    assert paths.data_dir() == tmp_path / "fresh" / "job-apply-mcp"       # ordinary data still follows XDG
    custom = tmp_path / "state"
    _cfg_text(paths.trusted_config_file(), f'[paths]\nstate_dir = "{custom.as_posix()}"\n')
    if os.name != "nt":
        assert state_path(load_settings()) == custom
    xdg = tmp_path / "x"
    _cfg_text(xdg / "job-apply-mcp" / "config.toml", f'[paths]\nstate_dir = "{custom.as_posix()}"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))                       # untrusted file cannot move the state
    assert state_path(load_settings()) == paths.state_dir()


def test_fresh_xdg_data_home_does_not_reset_quota_usage(monkeypatch, tmp_path):
    from job_apply_mcp.control.agent import ControlAgent
    s = Settings()
    s.paths.state_dir = None
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d1"))
    a1 = ControlAgent(s)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d2"))
    a2 = ControlAgent(s)
    assert a1.audit_path == a2.audit_path == paths.state_dir() / "control_audit.jsonl"


def test_revoked_consent_stays_revoked_after_restoring_an_old_store(tmp_path):
    import shutil

    from job_apply_mcp.consent.store import ConsentStore
    from job_apply_mcp.grants.store import GrantError
    data = tmp_path / "state"
    st = ConsentStore(data, secret=b"k" * 32, hwm_file=tmp_path / "cfg" / "consent_uses.json")
    c = st.issue_consent(3600)
    shutil.copytree(data, tmp_path / "backup")
    assert st.revoke(c.id) == [c.id]
    shutil.rmtree(data)
    shutil.copytree(tmp_path / "backup", data)                  # restore the pre-revoke copy
    st2 = ConsentStore(data, secret=b"k" * 32, hwm_file=tmp_path / "cfg" / "consent_uses.json")
    assert st2.active() == []
    with pytest.raises(GrantError):
        st2.use("approve_submissions")


def test_server_info_version_is_the_package_version():
    from job_apply_mcp import server
    opts = server.mcp._mcp_server.create_initialization_options()
    assert opts.server_version == __version__


# ---- migration of the security-state stores into the fixed state dir (ADR-0054)
def _old_world(tmp_path, monkeypatch):
    """An install whose stores lived in a custom data dir; the new state dir (the fixed default) starts empty."""
    from job_apply_mcp import migrate
    migrate._done.clear()
    old = tmp_path / "olddata"
    s = Settings()
    s.paths.data_dir = str(old)
    s.paths.state_dir = None
    return s, old


def test_migration_keeps_submitted_posting_deduped(tmp_path, monkeypatch):
    from job_apply_mcp.apply.approvals import ApprovalBook
    from job_apply_mcp.config import state_path
    s, old = _old_world(tmp_path, monkeypatch)
    b = ApprovalBook(old)
    b.set_state("greenhouse:acme:1", "submitted", url="https://x.example/1")
    b.set_state("greenhouse:acme:2", "unknown", url="https://x.example/2")
    b._db.close()
    new = state_path(s)
    assert new != old
    nb = ApprovalBook(new)
    assert nb.state("greenhouse:acme:1")["state"] == "submitted"
    ok, st = nb.begin_submit("greenhouse:acme:1", None)
    assert ok is False and st == "submitted"               # still refused: no double submit
    assert nb.state("greenhouse:acme:2")["state"] == "unknown"
    assert ApprovalBook(old).state("greenhouse:acme:1")["state"] == "submitted"   # copied, not moved


def test_migration_union_most_restrictive_state_wins(tmp_path, monkeypatch):
    from job_apply_mcp.apply.approvals import ApprovalBook
    from job_apply_mcp.config import state_path
    s, old = _old_world(tmp_path, monkeypatch)
    ob = ApprovalBook(old)
    ob.set_state("a:b:1", "submitting", url="u")
    ob._db.close()
    nb0 = ApprovalBook(paths.state_dir())
    nb0.set_state("a:b:1", "planned", url="u")
    nb0.set_state("a:b:2", "approved", url="u2")
    nb0._db.close()
    nb = ApprovalBook(state_path(s))
    assert nb.state("a:b:1")["state"] == "submitting" and nb.state("a:b:2")["state"] == "approved"


def test_migration_carries_over_todays_quota(tmp_path, monkeypatch):
    import json
    import time

    from job_apply_mcp.config import state_path
    from job_apply_mcp.control.agent import ControlAgent
    s, old = _old_world(tmp_path, monkeypatch)
    old.mkdir()
    now = time.time()
    rows = [{"ts": round(now - 60 * i, 2), "a": "submit", "d": "pass", "ref": f"g:c:{i}", "plat": "greenhouse",
             "co": "c", "dry": False, "run": "r", "n": 1, "id": f"e{i}", "v": [], "w": []} for i in range(3)]
    (old / "control_audit.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    a = ControlAgent(s)
    assert a.audit_path.parent == state_path(s)
    assert len([e for e in a.state.events if e.action == "submit"]) == 3
    ControlAgent(s)._replay()                                            # idempotent: no double counting
    assert len((state_path(s) / "control_audit.jsonl").read_text().splitlines()) == 3


def test_migration_keeps_revoked_consent_revoked(tmp_path, monkeypatch):
    import shutil

    from job_apply_mcp.consent.store import ConsentStore
    _s, old = _old_world(tmp_path, monkeypatch)
    key = b"k" * 32
    st_old = ConsentStore(old, secret=key)
    c = st_old.issue_consent(3600)
    newdir = paths.state_dir()
    shutil.copytree(old, newdir)                                          # stale pre-revoke copy in the new place
    st_new = ConsentStore(newdir, secret=key)
    st_new.revoke(c.id)                                                   # tombstone, row gone
    shutil.copytree(old, tmp_path / "restore")                            # old dir still holds the signed row
    st_old2 = ConsentStore(old, secret=key)
    assert any(r.get("id") == c.id for r in st_old2._read_all())
    from job_apply_mcp import migrate
    migrate._merge_signed_rows(ConsentStore, old, newdir, secret=key)
    assert ConsentStore(newdir, secret=key).active() == []
    assert not any(r.get("id") == c.id for r in ConsentStore(newdir, secret=key)._read_all())


def test_migration_consent_conflict_takes_earliest_expiry_and_most_uses(tmp_path):
    import shutil

    from job_apply_mcp import migrate
    from job_apply_mcp.consent.store import ConsentStore
    key = b"k" * 32
    a, b = tmp_path / "a", tmp_path / "b"
    sa = ConsentStore(a, secret=key, hwm_file=tmp_path / "h.json")
    c = sa.issue_consent(7200, max_uses=5)
    shutil.copytree(a, b)
    sb = ConsentStore(b, secret=key, hwm_file=tmp_path / "h.json")
    sb._resign(c, used=3, expires=c.expires - 3600)
    migrate._merge_signed_rows(ConsentStore, b, a, secret=key, hwm_file=tmp_path / "h.json")
    row = next(r for r in ConsentStore(a, secret=key)._read_all() if r["id"] == c.id)
    assert row["used"] == 3 and row["expires"] == c.expires - 3600
    ConsentStore(a, secret=key)._verify_row(row)                          # re-signed validly


def test_migration_failure_blocks_submit_and_doctor_says_why(tmp_path, monkeypatch):
    import pytest

    from job_apply_mcp.config import state_path
    from job_apply_mcp.migrate import StateMigrationError
    s, old = _old_world(tmp_path, monkeypatch)
    old.mkdir()
    (old / "approvals.db").write_bytes(b"this is not a sqlite database" * 50)
    with pytest.raises(StateMigrationError):
        state_path(s)
    with pytest.raises(StateMigrationError):                              # stays failed: never silently starts empty
        state_path(s)
    chk = doctor._state_check(s)
    assert chk.status == doctor.FAIL and "REFUSED" in chk.detail


def test_migration_keeps_applications_deduped(tmp_path, monkeypatch):
    import asyncio

    from job_apply_mcp.config import state_path
    from job_apply_mcp.store.sqlite import SqliteStore
    s, old = _old_world(tmp_path, monkeypatch)
    old_store = SqliteStore(old)
    asyncio.run(old_store.record(type("R", (), {"ref": "greenhouse:acme:9", "company": "Acme", "title": "Engineer",
                                                "status": "submitted", "via": "t", "confirmation": None,
                                                "detail": ""})(),
                                company="Acme", title="Engineer"))
    old_store.close()
    new = state_path(s)
    assert new != old and (new / "applications.db").is_file()
    st = SqliteStore(new)
    assert asyncio.run(st.seen("Acme", "Engineer", ref="greenhouse:acme:9"))
