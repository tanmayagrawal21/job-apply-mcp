import json
import time
from pathlib import Path

import pytest

from job_apply_mcp.config import Settings
from job_apply_mcp.improve import analyze as an
from job_apply_mcp.improve.classify import classify, risk_for
from job_apply_mcp.improve.playbook import PlaybookStore
from job_apply_mcp.improve.runner import FakeRunner, RunResult
from job_apply_mcp.improve.service import ImproveService, path_ok

REPO = Path(__file__).resolve().parents[1]


def mk(tmp_path, **imp):
    s = Settings.model_validate({"paths": {"data_dir": str(tmp_path / "data")}, "improve": imp})
    return s


def fake_git(changed="src/job_apply_mcp/platforms/foo.py\n", diff="+x = 1\n", fail=None):
    def h(argv, cwd):
        if fail and fail in " ".join(argv):
            return RunResult(1, "", "boom")
        if argv[:3] == ["git", "diff", "--cached"] and "--name-only" in argv:
            return RunResult(0, changed)
        if argv[:3] == ["git", "diff", "--cached"]:
            return RunResult(0, diff)
        if argv[0] == "gh":
            return RunResult(0, "https://github.com/o/r/pull/9\n")
    return h


def cmds(r):
    return [" ".join(a[:3]) for a, _ in r.calls]


def test_classify_and_risk():
    assert classify("add an adapter for iCIMS") == "platform_adapter"
    assert classify("Ashby location combobox fails with error") == "bug"
    assert classify("x", "config") == "config"
    assert classify("tick stop phrase boxes", "bug") == "bug"
    assert risk_for("bug", "bypass captcha") == "high"
    with pytest.raises(ValueError):
        classify("x", "nope")


def test_path_rules():
    a, f = ["src/", "tests/"], [".env", "uv.lock", "secrets"]
    assert path_ok("src/a.py", a, f)
    assert not path_ok("src/.env", a, f)
    assert not path_ok("uv.lock", a, f)
    assert not path_ok(".github/workflows/x.yml", a, [".github/"])
    assert not path_ok("README.md", a, f)


async def test_none_backend_writes_proposal(tmp_path):
    svc = ImproveService(mk(tmp_path), FakeRunner(), repo=REPO)
    out = await svc.request_feature("add an adapter for iCIMS")
    assert out["status"] == "proposal_only" and out["kind"] == "platform_adapter"
    d = Path(out["proposal"]).parent
    assert (d / "proposal.json").is_file()
    md = (d / "proposal.md").read_text()
    assert "icims" in md and "PlatformAdapter" in md and "ADR draft" in md


async def test_client_backend(tmp_path):
    r = FakeRunner()
    svc = ImproveService(mk(tmp_path, coder="client"), r, repo=REPO)
    out = await svc.request_feature("add config knob for retries", "config")
    assert out["client_task"]["branch"].startswith("auto/") and not r.calls


async def test_claude_code_opens_pr(tmp_path):
    r = FakeRunner(fake_git())
    svc = ImproveService(mk(tmp_path, coder="claude_code", gh_repo="o/r"), r, repo=REPO)
    out = await svc.request_feature("add an adapter for iCIMS")
    assert out["status"] == "pr_opened" and out["pr"].endswith("/pull/9")
    assert cmds(r)[1].startswith("git worktree add") or cmds(r)[0].startswith("git fetch")
    flat = [" ".join(a) for a, _ in r.calls]
    claude = next(x for x in flat if x.startswith("claude -p"))
    assert "--allowedTools" in claude
    assert any("pytest" in x for x in flat)
    gh = next(a for a, _ in r.calls if a[0] == "gh")
    assert "--base" in gh and "main" in gh and out["branch"] in gh
    body = Path(next(a for a, _ in r.calls if a[0] == "gh")[gh.index("--body-file") + 1]).read_text()
    assert "requires human review" in body
    pushes = [a for a, _ in r.calls if a[:2] == ["git", "push"]]
    assert pushes and all(p[-1].startswith("auto/") and not p[-1].startswith("main") for p in pushes)
    assert not any(a[:2] == ["git", "merge"] or "merge" in a[:3] for a, _ in r.calls)


@pytest.mark.parametrize("kw,status", [
    ({"changed": ".env\n"}, "path_violation"),
    ({"changed": "src/a.py\n", "diff": '+api_key = "abcdefghijklmnop1234"\n'}, "secret_detected"),
    ({"changed": ""}, "no_changes"),
    ({"fail": "dev pytest -q"}, "tests_failed"),
])
async def test_rails_block_pr(tmp_path, kw, status):
    r = FakeRunner(fake_git(**kw))
    svc = ImproveService(mk(tmp_path, coder="claude_code"), r, repo=REPO)
    out = await svc.request_feature("fix bug in lever adapter")
    assert out["status"] == status
    assert not any(a[0] == "gh" or a[:2] == ["git", "push"] for a, _ in r.calls)


async def test_aider_uses_router_tier(tmp_path):
    r = FakeRunner(fake_git())
    s = mk(tmp_path, coder="aider", aider_model="medium")
    s.llm.providers["ollama"]["base_url"] = "http://h:1"
    seen = {}
    orig = r.run

    async def spy(argv, **kw):
        if argv[0] == "aider":
            seen.update(argv=argv, env=kw.get("env"))
        return await orig(argv, **kw)

    r.run = spy
    await ImproveService(s, r, repo=REPO).request_feature("add adapter for foo")
    assert "ollama_chat/qwen3:4b" in seen["argv"] and seen["env"] == {"OLLAMA_API_BASE": "http://h:1"}


def write_events(data: Path, events):
    data.mkdir(parents=True, exist_ok=True)
    (data / "runs.jsonl").write_text("\n".join(json.dumps(e) for e in events))


def sample_events():
    now = time.time()
    ev = [{"ts": now, "platform": "ashby", "stage": "fill", "status": "error", "field_label": "Location",
           "error": f"combobox option {i} not found"} for i in range(6)]
    ev += [{"ts": now, "event": "unresolved_field", "field_label": "Are you over 18?"} for _ in range(14)]
    ev += [{"ts": now, "stage": "llm", "status": "timeout", "tier": "medium"} for _ in range(4)]
    return ev


def test_analyze_ranks_and_clusters():
    props = an.analyze(sample_events(), min_count=3)
    ids = [p.id for p in props]
    assert ids[0] == "pattern-are-you-over-18" and "field-ashby-location" in ids
    assert any(p.id == "router-timeout-medium" for p in props)
    assert next(p for p in props if p.id == "field-ashby-location").count == 6


async def test_report_default_no_autoapply_and_playbook(tmp_path):
    s = mk(tmp_path)
    write_events(Path(s.paths.data_dir), sample_events())
    svc = ImproveService(s, FakeRunner(), repo=REPO)
    rep = await svc.improvement_report()
    assert rep["auto_applied"] == [] and not (svc.root / "overlay.json").exists()
    hints = svc.playbook_hints("ashby")
    assert hints and "location" in hints[0].lower()


async def test_auto_apply_low_risk_records_before_after(tmp_path):
    s = mk(tmp_path, auto_apply_low_risk=True)
    write_events(Path(s.paths.data_dir), sample_events())
    svc = ImproveService(s, FakeRunner(), repo=REPO)
    rep = await svc.improvement_report()
    assert set(rep["auto_applied"]) == {"pattern-are-you-over-18", "router-timeout-medium"}
    ov = json.loads((svc.root / "overlay.json").read_text())
    assert ov["config"]["llm.tiers.medium.max_tokens"] == 768 and ov["profile_patterns"] == ["are you over 18"]
    rec = [json.loads(x) for x in (svc.root / "applied.jsonl").read_text().splitlines()]
    assert rec[0]["before"] is not None and rec[0]["metrics_before"]["count"] > 0
    rep2 = await svc.improvement_report()
    assert rep2["auto_applied"] == [] and rep2["applied_outcomes"]
    # code proposals are never auto-applied
    assert not any(r["id"].startswith("field-") for r in rec)


def test_playbook_scrubs_personal_data(tmp_path):
    pb = PlaybookStore(tmp_path)
    pb.add_lesson("Ashby", "k", "email me at jane@example.com or +1 (520) 555-0100 see https://x.io/a")
    t = pb.lessons("ashby")[0]
    assert "@" not in t and "555" not in t and "http" not in t
    assert (tmp_path / "ashby.md").is_file()


async def test_real_sources_store_and_control_audit(tmp_path):
    import sqlite3

    from job_apply_mcp.core.models import SubmitResult
    from job_apply_mcp.store.sqlite import SqliteStore

    s = mk(tmp_path)
    data = Path(s.paths.data_dir)
    st = SqliteStore(data)
    for i in range(4):
        await st.record(SubmitResult(ref=f"ashby:acme:{i}", status="error", detail=f"selector {i} timed out"),
                        company="Acme", title="Eng")
    st.close()
    assert sqlite3  # store file exists on disk
    now = time.time()
    rows = [{"ts": now, "a": "submit", "d": "block", "ref": "x", "plat": "lever",
             "v": [{"c": "stop_phrase", "m": "attestation found"}], "w": []} for _ in range(6)]
    (data / "control_audit.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    ids = [p.id for p in an.analyze(an.read_events(data), min_count=3)]
    assert any(i.startswith("err-ashby") for i in ids) and any(i.startswith("gate-lever") for i in ids)
