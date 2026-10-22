"""ADR-0037: typed trust-boundary guards (string values, user baseline) and signed grant use counters."""
import json

import pytest
from test_approvals import _cfg
from test_grants import mk

from job_apply_mcp.config import Settings
from job_apply_mcp.control.builtin import effective_limits
from job_apply_mcp.grants import GrantError

STRICT_CTL = "[control]\napply_enabled = false\ndev_mode = true\n"   # baseline where the risky value is the non-default

# (toml section or None, key, string value that would relax, user baseline, getter, expected safe value)
CASES = [
    (None, "dry_run", "false", "", lambda s: s.dry_run, True),
    ("apply", "auto_approve", "true", "", lambda s: s.apply.auto_approve, False),
    ("apply", "approval_mode", "agent", "", lambda s: s.apply.approval_mode, "human"),
    ("control", "apply_enabled", "true", STRICT_CTL, lambda s: s.control.apply_enabled, False),
    ("control", "dev_mode", "false", STRICT_CTL, lambda s: s.control.dev_mode, True),
    ("apply", "daily_limit", "0", "", lambda s: s.apply.daily_limit, 25),
    ("apply", "daily_limit_per_platform", "99", "", lambda s: s.apply.daily_limit_per_platform, 10),
    ("apply", "run_limit", "500", "", lambda s: s.apply.run_limit, 10),
    ("grants", "max_duration_s", "999999", "", lambda s: s.grants.max_duration_s, 43200),
    ("grants", "max_uses_cap", "5000", "", lambda s: s.grants.max_uses_cap, 1000),
    ("grants", "otp_window_min", "60", "", lambda s: s.grants.otp_window_min, 10),
    ("grants", "use_keyring", "false", "", lambda s: s.grants.use_keyring, True),
    ("browser", "captcha_handoff", "false", "", lambda s: s.browser.captcha_handoff, True),
]
IDS = [f"{c[0]}.{c[1]}" if c[0] else c[1] for c in CASES]


def _toml(sect, key, raw):
    line = f"{key} = {json.dumps(raw)}\n"           # a TOML *string*, e.g. daily_limit = "0"
    return line if sect is None else f"[{sect}]\n{line}"


@pytest.mark.parametrize(("sect", "key", "raw", "user", "get", "safe"), CASES, ids=IDS)
def test_string_value_in_project_file_cannot_relax(tmp_path, monkeypatch, caplog, sect, key, raw, user, get, safe):
    with caplog.at_level("WARNING"):
        s = _cfg(tmp_path, monkeypatch, user=user, proj=_toml(sect, key, raw))
    assert get(s) == safe and "IGNORED" in caplog.text


# pydantic also accepts "off"/"on"/"0"/"1" for bools; the env layer must be judged the same way
ENV_RAW = {"dry_run": "off", "auto_approve": "on", "apply_enabled": "1", "dev_mode": "no", "use_keyring": "0",
           "captcha_handoff": "f"}


@pytest.mark.parametrize(("sect", "key", "raw", "user", "get", "safe"), CASES, ids=IDS)
def test_string_value_in_env_cannot_relax(tmp_path, monkeypatch, sect, key, raw, user, get, safe):
    name = "JOB_APPLY__" + (f"{sect}__" if sect else "") + key.upper()
    s = _cfg(tmp_path, monkeypatch, user=user, env={name.upper(): ENV_RAW.get(key, raw)})
    assert get(s) == safe


@pytest.mark.parametrize(("key", "raw"), [("per_day", "0"), ("per_platform_per_day", "99"), ("per_run", "500")])
def test_string_legacy_quota_cannot_loosen(tmp_path, monkeypatch, key, raw):
    s = _cfg(tmp_path, monkeypatch, proj=f"[control.params.quota]\n{key} = {json.dumps(raw)}\n")
    assert s.control.params.get("quota", {}) == {}
    s = _cfg(tmp_path, monkeypatch, env={f"JOB_APPLY__CONTROL__PARAMS__QUOTA__{key.upper()}": raw})
    assert effective_limits(s.control.params.get("quota", {}), s) == {"day": 25, "platform": 10, "run": 10}


def test_user_tighter_value_cannot_be_loosened_back_to_default(tmp_path, monkeypatch, caplog):
    user = ("[apply]\ndaily_limit = 5\nrun_limit = 2\n[grants]\nmax_duration_s = 600\notp_window_min = 3\n"
            "[control.params.quota]\nper_platform_per_day = 4\n")
    proj = ("[apply]\ndaily_limit = 25\nrun_limit = \"10\"\n[grants]\nmax_duration_s = 43200\n"
            "[control.params.quota]\nper_platform_per_day = 10\n")
    with caplog.at_level("WARNING"):
        s = _cfg(tmp_path, monkeypatch, user=user, proj=proj)
    assert (s.apply.daily_limit, s.apply.run_limit, s.grants.max_duration_s) == (5, 2, 600)
    assert s.control.params["quota"] == {"per_platform_per_day": 4} and "IGNORED" in caplog.text
    env = {"JOB_APPLY__APPLY__DAILY_LIMIT": "25", "JOB_APPLY__GRANTS__OTP_WINDOW_MIN": "10",
           "JOB_APPLY__CONTROL__PARAMS__QUOTA__PER_PLATFORM_PER_DAY": "0"}
    s = _cfg(tmp_path, monkeypatch, user=user, env=env)
    assert (s.apply.daily_limit, s.grants.otp_window_min) == (5, 3)
    assert effective_limits(s.control.params["quota"], s)["platform"] == 4
    for k in env:
        monkeypatch.delenv(k)
    s = _cfg(tmp_path, monkeypatch, user=user, proj="[apply]\ndaily_limit = 3\n")
    assert s.apply.daily_limit == 3                                           # tightening further still works


def test_user_relaxed_value_is_the_baseline(tmp_path, monkeypatch):
    s = _cfg(tmp_path, monkeypatch, user="[apply]\ndaily_limit = 50\n", proj="[apply]\ndaily_limit = \"40\"\n")
    assert s.apply.daily_limit == 40                                          # below the user's 50: tighter
    s = _cfg(tmp_path, monkeypatch, user="[apply]\ndaily_limit = 50\n", proj="[apply]\ndaily_limit = 60\n")
    assert s.apply.daily_limit == 50


def test_use_keyring_only_disabled_by_trusted_user_config(tmp_path, monkeypatch):
    s = _cfg(tmp_path, monkeypatch, env={"JOB_APPLY__GRANTS__USE_KEYRING": "false"})
    assert s.grants.use_keyring is True
    monkeypatch.delenv("JOB_APPLY__GRANTS__USE_KEYRING")
    s = _cfg(tmp_path, monkeypatch, user="[grants]\nuse_keyring = false\n")
    assert s.grants.use_keyring is False
    s = _cfg(tmp_path, monkeypatch, user="[grants]\nuse_keyring = false\n", via_env=True)   # untrusted user file
    assert s.grants.use_keyring is True


def test_invalid_string_still_fails_validation(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="dry_run"):
        _cfg(tmp_path, monkeypatch, proj='dry_run = "maybe"\n')


def test_defaults_unchanged():
    s = Settings()
    assert s.dry_run and s.grants.use_keyring and s.browser.captcha_handoff and s.apply.daily_limit == 25


# ---- grants: signed use counter (audit truncation must not restore uses)
@pytest.mark.parametrize("cut", ["tail", "all", "delete"])
def test_audit_truncation_does_not_reset_uses(tmp_path, cut):
    st = mk(tmp_path)
    st.issue("approve_submissions", 600, max_uses=1)
    st.check("approve_submissions")
    lines = st._audit_file.read_text().splitlines()
    assert json.loads(lines[-1])["event"] == "use"
    if cut == "tail":
        st._audit_file.write_text("\n".join(lines[:-1]) + "\n")              # chain still verifies
        assert st._audit_lines()[1]
    elif cut == "all":
        st._audit_file.write_text("")
    else:
        st._audit_file.unlink()
    with pytest.raises(GrantError, match="max_uses"):
        st.check("approve_submissions")
    assert st.active() == []


def test_use_counter_is_signed(tmp_path):
    st = mk(tmp_path)
    g = st.issue("approve_submissions", 600, max_uses=2)
    st.check("approve_submissions")
    rows = json.loads(st._grants_file.read_text())
    assert rows[0]["used"] == 1 and st.uses(g.id) == 1
    rows[0]["used"] = 0                                                     # hand-edit without re-signing
    st._grants_file.write_text(json.dumps(rows))
    with pytest.raises(GrantError, match="tampered"):
        st.check("approve_submissions")


def test_record_without_counter_is_refused(tmp_path):
    st = mk(tmp_path)
    st.issue("approve_submissions", 600, max_uses=2)
    rows = json.loads(st._grants_file.read_text())
    del rows[0]["used"]
    st._grants_file.write_text(json.dumps(rows))
    with pytest.raises(GrantError, match="counter"):
        st.check("approve_submissions")


def test_unlimited_grant_still_counts(tmp_path):
    st = mk(tmp_path)
    g = st.issue("otp_assist", 600)
    for _ in range(3):
        st.check("otp_assist")
    assert st.uses(g.id) == 3 and st.active()[0][1] is None
