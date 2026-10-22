import json

import pytest

from job_apply_mcp.config import Settings
from job_apply_mcp.provenance import (
    DatasetSource,
    Origin,
    ProvenanceDatasetSource,
    ProvenanceError,
    ProvenanceStore,
    tag_export,
)
from job_apply_mcp.services import Services

JD = "Fictional Corp seeks a data engineer to build ETL pipelines."
LONG = "I built ETL pipelines that cut nightly batch time by half for a fictional retailer."


@pytest.fixture
def store(tmp_path):
    s = ProvenanceStore(tmp_path / "p.db")
    yield s
    s.close()


def test_record_rules(store):
    r = store.record_text("answer", LONG, "user_written")
    assert r.origin is Origin.user_written and not r.llm_generated
    with pytest.raises(ProvenanceError):
        store.record_text("answer", "x", "llm_generated")  # needs model
    with pytest.raises(ProvenanceError):
        store.record_text("answer", "x", "user_verified")  # forged approval
    with pytest.raises(ProvenanceError):
        store.record_text("answer", "x", "user_written", parent="nope")
    with pytest.raises(ProvenanceError):
        store.record_text("answer", "  ", "user_written")


def test_verify_unchanged_and_edited(store):
    g = store.record_text("cover_letter", LONG, "llm_generated", model="m1", prompt="p", context=JD)
    assert g.prompt_hash and g.llm_generated
    v = store.verify_text(g.id)
    assert v.origin is Origin.user_verified and v.parent_id == g.id and v.diff_from_parent is None
    e = store.verify_text(g.id, edited_text=LONG + " Edited.", verified_by="me")
    assert e.origin is Origin.user_edited and "+" in e.diff_from_parent and e.verified_by == "me"
    same = store.verify_text(g.id, edited_text=LONG)
    assert same.origin is Origin.user_verified
    assert [r.id for r in store.lineage(e.id)] == [e.id, g.id]
    with pytest.raises(ProvenanceError):
        store.verify_text(v.id)  # only LLM text is verified
    with pytest.raises(ProvenanceError):
        store.verify_text("missing")
    assert store.get(g.id).origin is Origin.llm_generated  # original never mutated


def test_dataset_source_excludes_llm(store):
    store.record_text("answer", LONG, "user_written", context=JD)
    g = store.record_text("answer", "LLM ONLY DRAFT", "llm_generated", model="m")
    ok = store.record_text("answer", "Approved draft", "llm_generated", model="m")
    v = store.verify_text(ok.id)
    e = store.verify_text(g.id, edited_text="User rewrite")
    src = ProvenanceDatasetSource(store)
    assert isinstance(src, DatasetSource)
    got = src.items()
    assert {r.id for r in got} >= {v.id, e.id} and len(got) == 3
    assert all(r.origin is not Origin.llm_generated for r in got)
    assert len(src.items(["cover_letter"])) == 0


def test_export_tagging(store, tmp_path):
    u = store.record_text("answer", LONG, "user_written")
    g = store.record_text("answer", "draft", "llm_generated", model="m")
    f = tmp_path / "resume.pdf"
    f.write_text("x")
    r = tag_export(store, [u.id, g.id], "sidecar", str(f))
    side = json.loads((tmp_path / "resume.pdf.provenance.json").read_text())
    assert r["llm_ids"] == [g.id] and side["items"][0]["llm"] is False
    assert tag_export(store, [g.id], "metadata")["metadata"]["Provenance-LLM-Generated"] == g.id
    assert tag_export(store, [g.id], "none") == {"mode": "none"}
    with pytest.raises(ValueError):
        tag_export(store, [g.id], "sidecar")
    with pytest.raises(ValueError):
        tag_export(store, [g.id], "weird")


async def test_services_end_to_end(tmp_path):
    s = Settings()
    s.paths.data_dir = str(tmp_path)
    svc = Services(s)
    g = await svc.record_text("answer", LONG, "llm_generated", model="m", context=JD)
    assert g["origin"] == "llm_generated"
    v = await svc.verify_text(g["id"], edited_text=LONG + " More detail added here.")
    assert v["origin"] == "user_edited" and "diff" in v
    info = await svc.provenance(v["id"], include_text=True)
    assert info["ancestors"][0]["id"] == g["id"] and info["record"]["text"].startswith("I built")
    assert (await svc.provenance("zzz"))["error"]
    assert (tmp_path / "provenance.db").exists()
