import os
from pathlib import Path

import pytest

from job_apply_mcp.platforms import HttpClient


def pytest_collection_modifyitems(config, items):
    if os.environ.get("JAM_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test; set JAM_LIVE=1")
    for it in items:
        if "live" in it.keywords:
            it.add_marker(skip)


@pytest.fixture
async def http(tmp_path):
    c = HttpClient(cache_dir=tmp_path / "cache", ttl_s=900, retries=2, backoff_s=0)
    yield c
    await c.aclose()


def _snapshot(root: Path) -> dict[str, int]:
    """Path -> mtime_ns of everything under root (names/mtimes only; file contents are never read)."""
    out: dict[str, int] = {}
    if root.exists():
        for p in [root, *root.rglob("*")]:
            try:
                out[str(p)] = p.stat().st_mtime_ns
            except OSError:
                pass
    return out


@pytest.fixture(scope="session", autouse=True)
def _real_data_dir_untouched():
    """Fail the session if any test created or modified anything under the developer's real data dir."""
    from job_apply_mcp import paths
    real = paths.data_dir()
    before = _snapshot(real)
    yield
    assert _snapshot(real) == before, f"test suite touched the real data dir {real}"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """The suite never reads the developer's real config, env overrides or data (HOME, XDG_*, APPDATA,
    LOCALAPPDATA, USERPROFILE, JOB_APPLY__*, JOB_APPLY_MCP_CONFIG all point at / are cleared for tmp_path)."""
    home = tmp_path / "home"
    home.mkdir()
    for k in list(os.environ):
        if k.startswith("JOB_APPLY__") or k == "JOB_APPLY_MCP_CONFIG":
            monkeypatch.delenv(k)
    for k, v in {"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / ".config",
                 "XDG_DATA_HOME": home / ".local" / "share", "APPDATA": home / "AppData" / "Roaming",
                 "LOCALAPPDATA": home / "AppData" / "Local"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.chdir(tmp_path)  # a stray ./job-apply-mcp.toml in the cwd must not leak in either
    yield home


@pytest.fixture(autouse=True)
def _isolated_data_dir(_isolated_env, tmp_path, monkeypatch):
    """No test may touch the real ~/.local/share/job-apply-mcp (audit log, store, profile, evidence):
    every Settings() built during a test points its data and browser-profile dirs at tmp_path."""
    from job_apply_mcp import paths
    from job_apply_mcp.config import Settings
    orig = Settings.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        default_data = paths.default_data_dir_str()
        if self.paths.data_dir.startswith("~") or self.paths.data_dir == default_data:
            self.paths.data_dir = str(tmp_path / "data")
        if self.browser.user_data_dir.startswith("~") or self.browser.user_data_dir == default_data + "/browser-profile":
            self.browser.user_data_dir = str(tmp_path / "profile")

    monkeypatch.setattr(Settings, "__init__", init)
