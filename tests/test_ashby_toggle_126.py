"""#126: Ashby Yes/No toggles. The hidden checkbox behind the two aria-pressed buttons is checked only for "Yes", so a
committed "No" read as "nothing selected" and blocked the run. Fixture: fictional company, Ashby markup shape as
observed in a dry run (button[aria-pressed][data-option] x2 + one display:none checkbox per [data-field-path])."""
from __future__ import annotations

import pytest
from test_qa_drivers_88_90 import _page, drv  # noqa: F401  (browser fixture)

from job_apply_mcp.apply.browser_playwright import ATS
from job_apply_mcp.core.handoff import NOT_COMMITTED, TOOL_GAP, UNKNOWN_FACT, categorize
from job_apply_mcp.core.models import FieldKind, FormField
from job_apply_mcp.core.next_steps import with_guide


def _toggle(key: str, label: str) -> str:
    return (f'<div class="_fieldEntry_x1 ashby-application-form-field-entry" data-field-path="{key}">'
            f'<label class="_heading_x2 _required_x2_91 ashby-application-form-question-title" for="{key}">{label}</label>'
            '<div class="_container_x3 _yesno_x4 ashby-application-form-input-yesno">'
            '<button class="ashby-application-form-input-yesno-option" aria-pressed="false" data-option="yes">Yes</button>'
            '<button class="ashby-application-form-input-yesno-option" aria-pressed="false" data-option="no">No</button>'
            f'<input type="checkbox" tabindex="-1" name="{key}" style="display:none"></div></div>')


# React-like: the pressed state commits a little after the click; clicking the active option clears it (as Ashby does)
FORM = ("<html><body><form>" + _toggle("q-hq", "Can you work from the Northwind Robotics office (4 days a week)?")
        + _toggle("q-reloc", "Do you require relocation to the Example Bay Area?") + "</form><script>"
        "window.clicks = 0;"
        "document.querySelectorAll('[data-field-path]').forEach(fe => fe.querySelectorAll('button').forEach(b => {"
        " b.onclick = ev => { ev.preventDefault(); window.clicks++; const on = b.getAttribute('aria-pressed') !== 'true';"
        "  setTimeout(() => { fe.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(on && x === b)));"
        "   fe.querySelector('input').checked = on && b.dataset.option === 'yes'; }, 300); }; }));"
        "</script></body></html>")
HQ = FormField(key="q-hq", label="Can you work from the Northwind Robotics office (4 days a week)?",
               kind=FieldKind.boolean, required=True)
RELOC = FormField(key="q-reloc", label="Do you require relocation to the Example Bay Area?", kind=FieldKind.boolean,
                  required=True)


async def test_committed_no_is_not_reported_empty(drv):  # noqa: F811
    pg = await _page(drv, FORM, ats="ashby")
    assert await drv._fill(pg, ATS["ashby"], HQ, True)
    assert await drv._fill(pg, ATS["ashby"], RELOC, False)
    assert await pg.evaluate("window.clicks") == 2           # one click each, despite the delayed commit
    assert await drv._verify(pg, [(HQ, True), (RELOC, False)], ATS["ashby"]) == []
    assert await drv._empty_required(pg) == []               # was: the relocation toggle (No) reported empty


async def test_unanswered_toggle_still_blocks_and_never_verifies_as_no(drv):  # noqa: F811
    pg = await _page(drv, FORM, ats="ashby")
    assert await drv._fill(pg, ATS["ashby"], HQ, True)
    assert [r["id"] for r in await drv._empty_required(pg)] == ["q-reloc"]
    # the hidden checkbox is unchecked for "not answered" too: it must not pass a No read-back
    assert await drv._verify(pg, [(RELOC, False)], ATS["ashby"]) == ["q-reloc"]


async def test_refill_never_reclicks_an_answered_toggle(drv):  # noqa: F811
    pg = await _page(drv, FORM, ats="ashby")
    assert await drv._fill(pg, ATS["ashby"], RELOC, False)
    assert await drv._fill(pg, ATS["ashby"], RELOC, False)   # the late refill pass: already pressed -> no click
    assert await pg.evaluate("window.clicks") == 1
    assert await drv._bool_state(pg, RELOC, await drv._locate(pg, RELOC)) is False


async def test_similar_sibling_toggle_is_never_used(drv):  # noqa: F811
    twin = ("<html><body><form>" + _toggle("q-a", "Do you require relocation?")
            + _toggle("q-b", "Do you require relocation? (second office)") + "</form><script>"
            "document.querySelectorAll('[data-field-path]').forEach(fe => fe.querySelectorAll('button').forEach(b => {"
            " b.onclick = ev => { ev.preventDefault(); fe.querySelectorAll('button').forEach("
            "x => x.setAttribute('aria-pressed', String(x === b))); }; }));</script></body></html>")
    pg = await _page(drv, twin, ats="ashby")
    a = FormField(key="q-a", label="Do you require relocation?", kind=FieldKind.boolean, required=True)
    assert await drv._fill(pg, ATS["ashby"], a, False)
    pressed = await pg.evaluate("() => [...document.querySelectorAll('[aria-pressed=true]')]"
                                ".map(b => b.closest('[data-field-path]').dataset.fieldPath)")
    assert pressed == ["q-a"]


def test_incomplete_with_only_planned_fields_is_a_tool_gap():
    detail = f"required fields still empty: ['Relocate? ({NOT_COMMITTED})']"
    assert categorize("incomplete", detail) == TOOL_GAP
    g = with_guide({"status": "incomplete", "detail": detail}, cat=TOOL_GAP)
    assert g["next_action"] == "request_feature" and "Do not ask the user" in g["hint"]


@pytest.mark.parametrize("unresolved", [["q-dom-only"]])
def test_incomplete_with_an_unplanned_field_lists_it_and_asks(unresolved):
    detail = f"required fields still empty: ['Relocate? ({NOT_COMMITTED})', 'Shirt size']"
    assert categorize("incomplete", detail, unresolved=unresolved) == UNKNOWN_FACT
    assert with_guide({"status": "incomplete", "detail": detail}, cat=UNKNOWN_FACT)["next_action"] == "ask_user"
