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
