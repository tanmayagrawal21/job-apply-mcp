from apply_fakes import FakeRouter

from job_apply_mcp.apply.writing import lint, summarize_posting, write_text
from job_apply_mcp.config import WritingCfg

CFG = WritingCfg()
GOOD = "\n\n".join(["Acme Robotics builds software for warehouse fleets. I built a fleet scheduler in Python." * 2] * 4)


def test_summarize_posting_picks_duty_sentences():
    out = summarize_posting("<p>Great perks and snacks galore for everyone here.</p><p>We are building warehouse "
                            "robots that cut order errors. You will design the control software.</p>", 200)
    assert "warehouse robots" in out and "snacks" not in out


def test_lint_catches_each_class():
    kinds = {v.kind for v in lint("I am passionate about this. I don't have experience with Rust. "
                                  "I would need to learn Go.", CFG)}
    assert {"banned_phrase", "contraction", "gap"} <= kinds
    assert lint(GOOD, CFG, company="Acme Robotics") == []
    assert any(v.kind == "company" for v in lint(GOOD, CFG, company="Globex"))
    assert any(v.kind == "length" for v in lint("a" * 50, CFG, max_len=10))
    ex = "the quick brown fox jumps over the lazy dog today again"
    assert any(v.kind == "copied" for v in lint(ex + " and more", CFG, exemplar=ex))


async def test_client_reviewer_returns_needs_review_after_lint_clean():
    drafts = iter(["I am excited to apply.", GOOD])
    r = FakeRouter(prose=lambda p: next(drafts))
    res = await write_text(r, CFG, kind="free_text", label="Why us?", company="Acme Robotics", title="SWE",
                           summary="s", facts=["f"])
    assert res.status == "needs_review" and res.text == GOOD and res.rubric
    assert [c for c, _ in r.calls] == ["narrative", "narrative"]  # one rewrite, no review tokens spent


async def test_router_reviewer_pass_and_fail_then_human():
    cfg = WritingCfg(reviewer="router", max_rounds=2)
    ok = await write_text(FakeRouter(prose=GOOD), cfg, kind="free_text", label="q", company="Acme Robotics",
                          title="t", summary="", facts=[])
    assert ok.status == "pass"
    bad = FakeRouter(prose=GOOD, review={"pass": False, "violations": ["unsupported claim"], "corrected": ""})
    res = await write_text(bad, cfg, kind="free_text", label="q", company="Acme Robotics", title="t",
                           summary="", facts=[])
    assert res.status == "needs_human" and res.violations[0].kind == "model"


async def test_persistent_lint_failure_is_needs_human():
    res = await write_text(FakeRouter(prose="I am passionate."), WritingCfg(max_rounds=2), kind="free_text",
                           label="q", company="Acme", title="t", summary="", facts=[])
    assert res.status == "needs_human" and res.rounds == 2
