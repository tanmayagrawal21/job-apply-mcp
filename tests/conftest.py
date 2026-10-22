import os

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


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
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
