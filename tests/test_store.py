import asyncio

import pytest

from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import SubmitResult
from job_apply_mcp.services import Services
from job_apply_mcp.store.sqlite import SqliteStore, company_key, titles_similar


def res(ref, status="submitted", **kw):
    return SubmitResult(ref=ref, status=status, **kw)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "data")
    yield s
    s.close()


def test_keys_and_similarity():
    assert company_key("The Acme, Inc.") == company_key("ACME Inc") == "acme"
    assert titles_similar("Software Engineer, Backend", "Backend Software Engineer (Remote)")
    assert titles_similar("Data Analyst - R-12345", "Data Analyst")
    assert not titles_similar("Software Engineer I", "Software Engineer II")
    assert not titles_similar("Senior Data Analyst", "Data Analyst")
    assert not titles_similar("Data Analyst", "Product Designer")


async def test_wal_and_seen(store):
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not await store.seen("Acme", "Data Analyst")
    await store.record(res("g:acme:1", via="api", confirmation="c1"), company="Acme Inc", title="Data Analyst")
    assert await store.seen("acme", "Data Analyst (Remote)")
    assert not await store.seen("acme", "Senior Data Analyst")
    assert not await store.seen("other", "Data Analyst")
    assert not await store.seen("acme", "Data Analyst", within_days=0)


async def test_dry_run_does_not_block_and_promotion(store):
    await store.record(res("r1", "dry_run"), company="Acme", title="Analyst")
    assert not await store.seen("Acme", "Analyst")
    await store.record(res("r1", "submitted"), company="Acme", title="Analyst")
    assert await store.seen("Acme", "Analyst")
    kinds = [e["kind"] for e in await store.events("r1")]
    assert kinds == ["dry_run", "submitted"]
    assert (await store.get("r1"))["status"] == "submitted"


async def test_dedupe_window(store):
    await store.record(res("r1"), company="Acme", title="Analyst")
    store._db.execute("UPDATE applications SET created_at='2020-01-01T00:00:00+00:00'")
    assert not await store.seen("Acme", "Analyst", within_days=180)
    assert await store.seen("Acme", "Analyst", within_days=100000)


async def test_list_filters_pagination(store):
    for i in range(7):
        await store.record(res(f"r{i}", "submitted" if i % 2 else "needs_human"), company=f"Co{i % 2}",
                           title=f"Role {i}")
    p1 = await store.list(limit=3)
    assert len(p1["rows"]) == 3 and p1["next"] == 3 and p1["total"] == 7
    p3 = await store.list(limit=3, cursor=6)
    assert len(p3["rows"]) == 1 and p3["next"] is None
    assert (await store.list(status="submitted"))["total"] == 3
    assert (await store.list(company="co1"))["total"] == 3
    assert (await store.list(since="2999-01-01"))["total"] == 0


async def test_csv_roundtrip(store, tmp_path):
    await store.record(res("r1", via="api", confirmation="x"), company="Acme", title="Analyst, Data")
    await store.record(res("r2", "needs_human"), company="Beta", title="Engineer")
    out = tmp_path / "x.csv"
    assert await store.export_csv(out) == 2
    s2 = SqliteStore(tmp_path / "d2")
    try:
        assert await s2.import_csv(out) == 2
        assert await s2.import_csv(out) == 0          # idempotent
        assert await s2.seen("acme", "Data Analyst")
        assert (await s2.get("r1"))["confirmation"] == "x"
    finally:
        s2.close()


async def test_company_lock_prevents_double_apply(store):
    submitted = []

    async def worker(i):
        async with store.company_lock("Acme Inc"):
            if await store.seen("Acme", "Data Analyst"):
                return
            await asyncio.sleep(0.01)
            submitted.append(i)
            await store.record(res(f"r{i}"), company="Acme", title="Data Analyst")

    await asyncio.gather(*(worker(i) for i in range(8)))
    assert len(submitted) == 1
    assert store.company_lock("acme") is store.company_lock("ACME, Inc.")


async def test_services_list_applications(tmp_path):
    s = Settings(paths={"data_dir": str(tmp_path / "d")})
    svc = Services(s)
    await svc.store.record(res("r1"), company="Acme", title="Analyst")
    out = await svc.list_applications(status="", limit=10, cursor=0)
    assert out["total"] == 1 and out["rows"][0]["co"] == "Acme"
    svc.store.close()
