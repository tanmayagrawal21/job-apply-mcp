import pytest

from job_apply_mcp.platforms import HttpClient


@pytest.fixture
async def http(tmp_path):
    c = HttpClient(cache_dir=tmp_path / "cache", ttl_s=900, retries=2, backoff_s=0)
    yield c
    await c.aclose()
