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


# --- identity-first dedupe (ADR-0010 amendment) ---
from job_apply_mcp.store.sqlite import canonical_url


def test_canonical_url_strips_tracking():
    a = canonical_url("https://WWW.Jobs.example.com/acme/123/?utm_source=x&gh_src=y&gh_jid=9#apply")
    assert a == canonical_url("https://jobs.example.com/acme/123?gh_jid=9&ref=abc") == "jobs.example.com/acme/123?gh_jid=9"
    assert canonical_url("https://jobs.example.com/acme/123?gh_jid=10") != a
    assert canonical_url("not a url") is None


async def test_distinct_requisitions_same_title_not_merged(store):
    await store.record(res("greenhouse:acme:1"), company="Acme", title="Software Engineer",
                       url="https://jobs.example.com/acme/1?utm_source=a")
    assert await store.seen_why("Acme", "Software Engineer", ref="greenhouse:acme:2",
                                url="https://jobs.example.com/acme/2") is None
    assert (await store.seen_why("Acme", "Software Engineer", ref="greenhouse:acme:1")) == "req_id:greenhouse:acme:1"
    # same job re-listed under a different ref but the same canonical URL
    assert (await store.seen_why("Acme", "SWE", ref="lever:acme:zzz",
                                 url="https://jobs.example.com/acme/1?ref=feed")).startswith("url:")


async def test_title_fallback_modes(store):
    await store.record(res("mail:abc"), company="Acme", title="Data Analyst")   # no stable id
    assert (await store.seen_why("Acme", "Data Analyst", ref="greenhouse:acme:5")).startswith("title~")
    assert (await store.seen_why("Acme", "Data Analyst")).startswith("title~")              # nothing known: fallback
    await store.record(res("greenhouse:acme:1"), company="Beta", title="Data Analyst")
    assert await store.seen("Beta", "Data Analyst", ref="greenhouse:beta:2", mode="title") is True
    assert await store.seen("Beta", "Data Analyst", ref="greenhouse:beta:2") is False


# --- ADR-0010 amendment 2: fallback against id-less rows, aliases, fuzzy titles ---


async def test_imported_row_blocks_new_posting_with_id_identical_title(store):
    # imported history: no requisition id, no url
    await store.record(res("import:hist:1"), company="Quillfeather Games", title="Senior Gameplay Engineer")
    why = await store.seen_why("Quillfeather Games", "Senior Gameplay Engineer", ref="greenhouse:quillfeather:771",
                               url="https://boards.example.com/quillfeather/771")
    assert why and why.startswith("title~1.00") and "legacy-row" in why


async def test_parent_brand_alias_blocks_when_configured():
    st = SqliteStore(":memory:", company_aliases={"Quillfeather": "Quillfeather Interactive Entertainment Holdings Board"})
    await st.record(res("import:hist:2"), company="Quillfeather Interactive Entertainment Holdings Board",
                    title="Technical Artist")
    why = await st.seen_why("Quillfeather", "Technical Artist", ref="lever:quillfeather:abc")
    assert why and "company=alias" in why
    plain = SqliteStore(":memory:")
    await plain.record(res("import:hist:2"), company="Quillfeather Interactive Entertainment Holdings Board",
                       title="Technical Artist")
    assert await plain.seen_why("Quillfeather", "Technical Artist", ref="lever:quillfeather:abc") is None


async def test_slug_and_suffix_normalization(store):
    assert company_key("Mossgrove Technologies Global Inc") == "mossgrove"
    await store.record(res("mail:x"), company="Mossgrove Labs Group", title="Data Engineer")
    # board slug 'mossgrove-labs' / 'mossgrovelabs' resolves to the same identity as the display name
    assert await store.seen("mossgrovelabs", "Data Engineer", ref="greenhouse:mossgrovelabs:9")
    assert await store.seen("mossgrove-labs", "Data Engineer", ref="greenhouse:mossgrovelabs:9")


async def test_fuzzy_title_fallback_and_threshold(store):
    await store.record(res("mail:y"), company="Thistledown", title="Backend Software Engineer")
    assert await store.seen("Thistledown", "Software Engineer, Backend (Remote)", ref="lever:thistledown:1")
    assert not await store.seen("Thistledown", "Senior Backend Software Engineer", ref="lever:thistledown:2")
    strict = SqliteStore(":memory:", title_threshold=0.95)
    await strict.record(res("mail:y"), company="Thistledown", title="Backend Software Engineer")
    assert not await strict.seen("Thistledown", "Backend Software Engineer Platform", ref="lever:thistledown:3")


async def test_two_requisitions_with_ids_never_merged_even_with_alias():
    st = SqliteStore(":memory:", company_aliases={"brandco": "parentco"})
    await st.record(res("greenhouse:parentco:1"), company="ParentCo", title="Level Designer")
    assert await st.seen_why("BrandCo", "Level Designer", ref="greenhouse:brandco:2") is None
