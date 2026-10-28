"""Follow-up to #129 (reviewer + control notes). Fixtures: fictional company, generic markup.

1. An unknown boolean read-back fails verification on EVERY ATS (a hidden backing checkbox + Yes/No buttons that
   never took read back as unknown, which used to pass outside Ashby).
"""
from __future__ import annotations

import pytest
from test_qa_drivers_88_90 import _page, drv  # noqa: F401  (browser fixture)

from job_apply_mcp.apply.browser_playwright import ATS
from job_apply_mcp.core.models import FieldKind, FormField

LABEL = "Can you work from the Northwind Robotics office?"
Q = FormField(key="q-office", label=LABEL, kind=FieldKind.boolean, required=True)


def _yesno(commit: bool) -> str:
    """A generic (non-Ashby) Yes/No question: hidden backing checkbox + two aria-pressed buttons. With commit=False the
    click handler swallows the click, so the answer never takes."""
    js = ("document.querySelectorAll('.question button').forEach(b => { b.onclick = ev => { ev.preventDefault();"
          + (" document.querySelectorAll('.question button').forEach(x => x.setAttribute('aria-pressed',"
             " String(x === b))); document.querySelector('[name=q-office]').checked = b.textContent === 'Yes';"
             if commit else "") + " }; });")
    return ('<html><body><form><div class="question"><label>' + LABEL + ' *</label>'
            '<button type="button" aria-pressed="false">Yes</button><button type="button" aria-pressed="false">No'
            '</button><input type="checkbox" name="q-office" style="display:none"></div></form>'
            f'<script>{js}</script></body></html>')


@pytest.mark.parametrize("ats", ["lever", "greenhouse", "ashby"])
@pytest.mark.parametrize("want", [True, False])
async def test_uncommitted_toggle_fails_verify_on_every_ats(drv, ats, want):  # noqa: F811
    pg = await _page(drv, _yesno(commit=False), ats=ats)
    await drv._fill(pg, ATS[ats], Q, want)
    assert await drv._bool_state(pg, Q, await drv._locate(pg, Q)) is None
    assert await drv._verify(pg, [(Q, want)], ATS[ats]) == ["q-office"]


@pytest.mark.parametrize("ats", ["lever", "greenhouse"])
@pytest.mark.parametrize("want", [True, False])
async def test_committed_toggle_verifies_outside_ashby(drv, ats, want):  # noqa: F811
    pg = await _page(drv, _yesno(commit=True), ats=ats)
    assert await drv._fill(pg, ATS[ats], Q, want)
    assert await drv._verify(pg, [(Q, want)], ATS[ats]) == []
    assert await drv._verify(pg, [(Q, not want)], ATS[ats]) == ["q-office"]
