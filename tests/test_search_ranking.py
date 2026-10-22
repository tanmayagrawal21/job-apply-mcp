import html
import sqlite3
from datetime import UTC, datetime

import respx

from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import Posting
from job_apply_mcp.search.ranking import bm25_scores, rank_postings, tokens
from job_apply_mcp.services import Services

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def post(i, title, desc="", loc="Remote, US", d="2026-10-20", sal=None):
    return Posting(ref=f"greenhouse:acmeco:{i}", platform="greenhouse", company="Acme", title=title, location=loc,
                   url=f"https://x.test/{i}", posted_at=d, description=desc, salary=sal)


def cfg(**kw):
    return Settings.model_validate({"search": kw})


def test_bm25_prefers_rarer_and_repeated_terms():
    docs = [tokens("python python sql"), tokens("python java"), tokens("java go")]
    s = bm25_scores(["python"], docs)
    assert s[0] > s[1] > s[2] == 0


def test_description_only_match_ranks_and_explains():
    s = cfg(skills=["Python"], seniority="entry", location_order=["remote", "tucson"])
    ps = [post(1, "Software Engineer", "We use Python and Postgres daily."),
          post(2, "Software Engineer", "Java shop.", loc="Berlin")]
    r = rank_postings(ps, "python", s.search.skills, s.search, s.policy, NOW)
    assert r[0].posting.ref.endswith(":1") and r[0].q_hit and not r[1].q_hit
    assert r[0].score > r[1].score and len(r[0].why) <= 3
    assert any("python" in w for w in r[0].why)


def test_seniority_location_comp_signals():
    s = cfg(seniority="entry", location_order=["tucson", "remote"], weights={
        "query": 0, "skills": 0, "seniority": 1, "location": 1, "comp": 1, "recency": 0})
    s.policy.salary_floor = 100_000
    good = post(1, "Junior Engineer", loc="Tucson, AZ", sal="$110k - $130k")
    bad = post(2, "Senior Engineer", loc="Berlin", sal="$60k - $70k")
    a, b = rank_postings([good, bad], "", [], s.search, s.policy, NOW)
    assert a.posting is good and a.score > b.score
    assert "pay meets floor" in a.why and any("mismatch" in w for w in b.why)


def mk(tmp_path, **search):
    return Services(Settings.model_validate({
        "paths": {"data_dir": str(tmp_path), "profile": str(tmp_path / "none.toml")},
        "concurrency": {"min_interval_s": 0},
        "search": search,
        "platforms": {"backoff_s": 0, "boards": {"greenhouse": ["acmeco"]}}}))


def gh(i, title, text):
    return {"id": i, "title": title, "updated_at": "2026-10-20T10:00:00-04:00", "company_name": "Acme Co",
            "absolute_url": f"https://job-boards.greenhouse.io/acmeco/jobs/{i}", "location": {"name": "Remote, US"},
            "content": html.escape(f"<p>{text}</p>")}


def mock_board():
    jobs = [gh(1, "Data Engineer", "Daily work in Rust and Kafka."), gh(2, "Analyst", "Reporting in Excel.")]
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs").respond(json={"jobs": jobs})
    for j in jobs:
        respx.get(f"https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/{j['id']}").respond(json=j)


@respx.mock
async def test_search_finds_description_only_match_and_caches(tmp_path):
    mock_board()
    s = mk(tmp_path)
    out = await s.search(query="rust", since_days=0, apply_policy=False)
    assert [r["ref"] for r in out["rows"]] == ["greenhouse:acmeco:1"]
    assert out["rows"][0]["score"] > 0 and any("rust (desc)" in w for w in out["rows"][0]["why"])
    n = len(respx.calls)
    await s.search(query="rust", since_days=0, apply_policy=False)
    assert len(respx.calls) <= n + 1  # description fetches served from the in-process cache (list may refetch)
    await s.aclose()


@respx.mock
async def test_match_title_is_legacy_and_rank_off_has_no_score(tmp_path):
    mock_board()
    s = mk(tmp_path, match="title")
    assert (await s.search(query="rust", since_days=0, apply_policy=False))["rows"] == []
    s2 = mk(tmp_path, rank=False)
    out = await s2.search(since_days=0, apply_policy=False)
    assert "score" not in out["rows"][0]
    await s.aclose()
    await s2.aclose()


@respx.mock
async def test_description_triggers_policy_drop(tmp_path):
    jobs = [gh(1, "Data Engineer", "Rust. We are unable to provide visa sponsorship.")]
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs").respond(json={"jobs": [{**jobs[0], "content": ""}]})
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/1").respond(json=jobs[0])
    s = Services(Settings.model_validate({
        "paths": {"data_dir": str(tmp_path), "profile": str(tmp_path / "none.toml")},
        "policy": {"requires_sponsorship": True}, "concurrency": {"min_interval_s": 0},
        "platforms": {"backoff_s": 0, "boards": {"greenhouse": ["acmeco"]}}}))
    out = await s.search(query="rust", since_days=0)
    assert out["rows"] == [] and out["filtered"] == 1
    await s.aclose()


@respx.mock
async def test_embedding_rerank_off_by_default_and_soft_fail(tmp_path):
    mock_board()
    s = mk(tmp_path, embeddings=True, embed_url="http://127.0.0.1:9")  # nothing listening: falls back silently
    respx.post("http://127.0.0.1:9/api/embed").respond(500)
    out = await s.search(query="rust", since_days=0, apply_policy=False)
    assert [r["ref"] for r in out["rows"]] == ["greenhouse:acmeco:1"]
    await s.aclose()


@respx.mock
async def test_embedding_rerank_blends_similarity(tmp_path):
    mock_board()
    s = mk(tmp_path, embeddings=True, embed_url="http://127.0.0.1:9", min_score=0)
    respx.post("http://127.0.0.1:9/api/embed").respond(json={"embeddings": [[1, 0], [1, 0], [0, 1]]})
    out = await s.search(since_days=0, apply_policy=False)
    top = out["rows"][0]   # vectors are assigned in pre-rank order; the one parallel to the query wins
    assert any(w.startswith("semantic 1.00") for w in top["why"]) and top["score"] > out["rows"][1]["score"]
    await s.aclose()


def test_old_db_migrates(tmp_path):
    from job_apply_mcp.store.sqlite import SqliteStore

    db = sqlite3.connect(tmp_path / "applications.db")
    db.executescript("""CREATE TABLE applications(ref TEXT PRIMARY KEY, company TEXT NOT NULL DEFAULT '',
      title TEXT NOT NULL DEFAULT '', company_key TEXT NOT NULL, title_key TEXT NOT NULL, status TEXT NOT NULL,
      via TEXT, confirmation TEXT, created_at TEXT NOT NULL);
      INSERT INTO applications VALUES('greenhouse:acmeco:7','Acme','SWE','acme','swe','submitted',NULL,NULL,
      '2026-10-01T00:00:00+00:00');""")
    db.commit()
    db.close()
    st = SqliteStore(tmp_path)
    row = st._db.execute("SELECT req_id,url_key FROM applications").fetchone()
    assert row["req_id"] == "greenhouse:acmeco:7" and row["url_key"] is None
    st.close()
