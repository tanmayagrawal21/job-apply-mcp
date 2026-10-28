"""Follow-up to #129 (control WARN): a React commit that lands late can flip a planned Yes/No off AFTER the fill and
every read-back. The last read before the one submit click re-reads every planned toggle (read-only, no clicks); a
toggle no longer in its planned state means nothing is clicked and the result is `incomplete` / NOT_COMMITTED.
Local fixture only (fictional company)."""
from __future__ import annotations

from test_apply_browser_local import ANS, FORM, Srv, drv, page, resume  # noqa: F401  (fixtures)

from job_apply_mcp.core.handoff import NOT_COMMITTED, TOOL_GAP, categorize

# The page beacons every Submit click (GET /clicked). window.lateCommit() is the delayed commit: it lands a moment
# later and flips the pressed Yes back off, as a stale React update would.
EXTRA = ("document.getElementById('sub').addEventListener('click', () => { fetch('/clicked'); });"
         "window.lateCommit = () => setTimeout(() => document.querySelectorAll('[aria-pressed]')"
         ".forEach(x => x.setAttribute('aria-pressed', 'false')), 50);")


def _late_commit_after_scans(drv, monkeypatch):  # noqa: F811
    """Fire the late commit right after the last pre-submit scan in _run (every read-back already passed)."""
    orig = drv._native_invalid

    async def scan_then_flip(pg):
        out = await orig(pg)
        await pg.evaluate("window.lateCommit()")
        await pg.wait_for_timeout(200)
        return out
    monkeypatch.setattr(drv, "_native_invalid", scan_then_flip)


async def test_late_commit_flipping_a_toggle_off_blocks_the_submit_click(drv, resume, monkeypatch):  # noqa: F811
    srv = Srv(page(extra=EXTRA), '{"data":{"submitApplicationForm":{"__typename":"FormSubmitSuccess"}}}')
    _late_commit_after_scans(drv, monkeypatch)
    calls: list[int] = []
    orig_drift = drv._toggle_drift

    async def spy(pg, toggles):
        calls.append(len(toggles))
        return await orig_drift(pg, toggles)
    monkeypatch.setattr(drv, "_toggle_drift", spy)
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=False)
    assert res.status == "incomplete", res.detail
    assert NOT_COMMITTED in res.detail and "Submit not clicked" in res.detail
    assert srv.clicks == 0 and srv.posts == 0                # no submit click, nothing sent
    assert calls == [1]                                      # the re-read ran once, on the one planned toggle
    assert res.unresolved == [] and res.client_tasks == []
    assert categorize(res.status, res.detail, unresolved=res.unresolved, client_tasks=res.client_tasks) == TOOL_GAP


async def test_toggle_still_in_planned_state_submits(drv, resume, monkeypatch):  # noqa: F811
    srv = Srv(page(extra=EXTRA), '{"data":{"submitApplicationForm":{"__typename":"FormSubmitSuccess"}}}')
    res = await drv.fill_and_submit(srv.url, FORM, ANS, resume, dry_run=False)
    assert res.status == "submitted", res.detail
    assert srv.clicks == 1 and srv.posts == 1


async def test_drift_reread_never_clicks(drv, resume):  # noqa: F811
    from job_apply_mcp.apply.browser_playwright import ATS
    srv = Srv(page(extra=EXTRA + "window.toggleClicks = 0; document.querySelectorAll('[aria-pressed]')"
                   ".forEach(b => b.addEventListener('click', () => window.toggleClicks++));"), "{}")
    pg = await (await drv._context()).new_page()
    await pg.goto(srv.url)
    auth = next(f for f in FORM.fields if f.key == "auth")
    assert await drv._fill(pg, ATS["ashby"], auth, True)
    n = await pg.evaluate("window.toggleClicks")
    assert await drv._toggle_drift(pg, [(auth, True)]) == []
    assert [d["id"] for d in await drv._toggle_drift(pg, [(auth, False)])] == ["auth"]
    await pg.evaluate("window.lateCommit()")
    await pg.wait_for_timeout(200)
    assert [d["id"] for d in await drv._toggle_drift(pg, [(auth, True)])] == ["auth"]
    assert await pg.evaluate("window.toggleClicks") == n      # read-only: no clicks added
    await pg.close()
