"""ADR-0035: optional submission caps (0 = no limit), trust rules, legacy params, status."""
import json

import pytest
from pydantic import ValidationError
from test_approvals import _cfg

from job_apply_mcp.config import Settings
from job_apply_mcp.control.agent import ControlAgent


async def _submit(a, ref, plat="lever"):
    return await a.check("submit", {"ref": ref, "platform": plat, "dry_run": False})


def agent(tmp_path, apply=None, quota=None):
    s = Settings.model_validate({"dry_run": False, "paths": {"data_dir": str(tmp_path)}, "apply": apply or {},
                                 "control": {"params": {"quota": quota} if quota else {}}})
    return ControlAgent(s)


async def test_defaults_unchanged(tmp_path):
    a = agent(tmp_path)
    assert a.daily_limit_status()["limit"] == {"day": 25, "platform": 10, "run": 10}


async def test_zero_disables_each_scope(tmp_path):
    a = agent(tmp_path, {"daily_limit": 2, "daily_limit_per_platform": 0, "run_limit": 0})
    assert [(await _submit(a, f"r{i}")).decision for i in range(3)] == ["pass", "pass", "block"]  # day cap only
    a = agent(tmp_path / "b", {"daily_limit": 0, "daily_limit_per_platform": 2, "run_limit": 0})
    assert [(await _submit(a, f"r{i}")).decision for i in range(3)] == ["pass", "pass", "block"]  # platform only
    assert (await _submit(a, "x", "ashby")).decision == "pass"
    a = agent(tmp_path / "c", {"daily_limit": 0, "daily_limit_per_platform": 0, "run_limit": 2})
    assert [(await _submit(a, f"r{i}", f"p{i}")).decision for i in range(3)] == ["pass", "pass", "block"]  # run only
    a = agent(tmp_path / "d", {"daily_limit": 0, "daily_limit_per_platform": 0, "run_limit": 0})
    for i in range(40):
        assert (await _submit(a, f"r{i}", f"p{i % 4}")).decision == "pass"
    lim = a.daily_limit_status()
    assert lim["limit"] == {"day": None, "platform": None, "run": None}
    assert lim["remaining"]["day"] is None and lim["used"]["day"] == 40


async def test_legacy_quota_params_win_and_zero_is_off(tmp_path):
    a = agent(tmp_path, {"daily_limit": 5}, {"per_day": 1})
    assert [(await _submit(a, f"r{i}")).decision for i in range(2)] == ["pass", "block"]
    a = agent(tmp_path / "z", {"daily_limit": 1, "run_limit": 1},
              {"per_day": 0, "per_run": 0, "per_platform_per_day": 0})
    for i in range(12):
        assert (await _submit(a, f"r{i}")).decision == "pass"
    with pytest.raises(ValidationError):
        Settings.model_validate({"apply": {"daily_limit": -1}})
    bad = agent(tmp_path / "n", quota={"per_day": -2})
    assert bad.config_error and "per_day" in bad.config_error


async def test_status_counts_respect_release_and_window(tmp_path):
    now = [1_000_000.0]
    s = Settings.model_validate({"dry_run": False, "paths": {"data_dir": str(tmp_path)},
                                 "apply": {"daily_limit": 5, "daily_limit_per_platform": 3, "run_limit": 4}})
    a = ControlAgent(s, clock=lambda: now[0])
    r1 = await _submit(a, "r1")
    await _submit(a, "r2")
    await _submit(a, "r3", "ashby")
    st = a.daily_limit_status()
    assert st["used"] == {"day": 3, "platform": {"ashby": 1, "lever": 2}, "run": 3}
    assert st["remaining"] == {"day": 2, "platform": {"ashby": 2, "lever": 1}, "run": 1}
    await a.release("r1", "fill failed", r1.event_id)                       # ADR-0032
    st = a.daily_limit_status()
    assert st["used"]["day"] == 2 and st["used"]["platform"]["lever"] == 1 and st["used"]["run"] == 2
    now[0] += 86400 + 5                                                      # outside the 24h window
    st = a.daily_limit_status()
    assert st["used"]["day"] == 0 and st["used"]["platform"] == {} and st["used"]["run"] == 2
    assert a.status()["daily_limit"] == st                                   # control_status carries the block


async def test_control_status_and_doctor(tmp_path):
    from job_apply_mcp import doctor
    s = Settings.model_validate({"paths": {"data_dir": str(tmp_path)}, "apply": {"daily_limit": 0}})
    out = json.loads(json.dumps(ControlAgent(s).status(), default=str))
    assert out["daily_limit"]["limit"]["day"] is None
    chk = doctor._daily_limit_check(s)
    assert chk.status == doctor.WARN and "no limit" in chk.detail
    assert doctor._daily_limit_check(Settings.model_validate({"paths": {"data_dir": str(tmp_path)}})).status == doctor.OK


def test_env_and_project_cannot_loosen_but_can_tighten(tmp_path, monkeypatch, caplog):
    loosen = "[apply]\ndaily_limit = 0\ndaily_limit_per_platform = 99\nrun_limit = 0\n" \
             "[control.params.quota]\nper_day = 0\nper_run = 50\n"
    with caplog.at_level("WARNING"):
        s = _cfg(tmp_path, monkeypatch, proj=loosen)
    assert (s.apply.daily_limit, s.apply.daily_limit_per_platform, s.apply.run_limit) == (25, 10, 10)
    assert s.control.params.get("quota", {}) == {} and "IGNORED" in caplog.text
    env = {"JOB_APPLY__APPLY__DAILY_LIMIT": "0", "JOB_APPLY__APPLY__RUN_LIMIT": "11",
           "JOB_APPLY__CONTROL__PARAMS__QUOTA__PER_PLATFORM_PER_DAY": "0"}
    s = _cfg(tmp_path, monkeypatch, env=env)
    assert (s.apply.daily_limit, s.apply.run_limit) == (25, 10) and s.control.params.get("quota", {}) == {}
    tight = "[apply]\ndaily_limit = 3\nrun_limit = 1\n[control.params.quota]\nper_day = 2\n"
    s = _cfg(tmp_path, monkeypatch, proj=tight)
    assert (s.apply.daily_limit, s.apply.run_limit) == (3, 1) and s.control.params["quota"] == {"per_day": 2}
    s = _cfg(tmp_path, monkeypatch, env={"JOB_APPLY__APPLY__DAILY_LIMIT": "7"})
    assert s.apply.daily_limit == 7


def test_trusted_user_config_may_loosen(tmp_path, monkeypatch):
    user = "[apply]\ndaily_limit = 0\ndaily_limit_per_platform = 40\n[control.params.quota]\nper_run = 0\n"
    s = _cfg(tmp_path, monkeypatch, user=user)
    assert (s.apply.daily_limit, s.apply.daily_limit_per_platform) == (0, 40)
    assert s.control.params["quota"] == {"per_run": 0}
    s = _cfg(tmp_path, monkeypatch, user=user, via_env=True)                 # JOB_APPLY_MCP_CONFIG is untrusted
    assert (s.apply.daily_limit, s.apply.daily_limit_per_platform) == (25, 10)
    assert s.control.params.get("quota", {}) == {}
    s = _cfg(tmp_path, monkeypatch, user=user, mode=0o666)                   # group/world-writable is untrusted
    assert s.apply.daily_limit == 25
    s = _cfg(tmp_path, monkeypatch, user=user, proj="[apply]\ndaily_limit = 5\n")
    assert s.apply.daily_limit == 5                                          # a project file may still tighten

