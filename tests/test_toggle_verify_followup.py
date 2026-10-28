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


# 2. _EMPTY_REQUIRED_JS: "answered by a pressed button" applies only to a genuine Yes/No pair next to a hidden backing
#    checkbox. A required checkbox is judged by its own checked state, whatever buttons sit beside it.

def _box(inner: str) -> str:
    return f'<html><body><form><div class="form-field">{inner}</div></form></body></html>'


CONSENT = ('<input type="checkbox" id="c-privacy" name="c-privacy" required>'
           '<label for="c-privacy">I have read the Northwind Robotics privacy notice *</label>')


@pytest.mark.parametrize("ats", ["lever", "greenhouse", "ashby"])
async def test_consent_box_next_to_unrelated_pressed_button_is_empty(drv, ats):  # noqa: F811
    pg = await _page(drv, _box(CONSENT + '<button type="button" aria-pressed="true">Remember me</button>'), ats=ats)
    assert [r["id"] for r in await drv._empty_required(pg)] == ["c-privacy"]


async def test_visible_box_next_to_pressed_yes_no_pair_is_still_judged_by_itself(drv):  # noqa: F811
    pair = '<button aria-pressed="true">Yes</button><button aria-pressed="false">No</button>'
    pg = await _page(drv, _box(CONSENT + pair), ats="ashby")
    assert [r["id"] for r in await drv._empty_required(pg)] == ["c-privacy"]


HIDDEN = '<label>Do you hold a forklift licence? *</label><input type="checkbox" name="q-lic" style="display:none">'


@pytest.mark.parametrize("buttons", [
    '<button aria-pressed="true">Show more</button>',                                   # one unrelated button
    ('<button aria-pressed="true">Yes</button><button aria-pressed="false">No</button>'
     '<button aria-pressed="false">Maybe</button>'),                                    # not a pair
    '<button aria-pressed="true">Expand</button><button>Collapse</button>',             # neither yes/no nor aria pair
])
async def test_hidden_box_without_a_genuine_pair_is_empty(drv, buttons):  # noqa: F811
    pg = await _page(drv, _box(HIDDEN + buttons), ats="ashby")
    assert [r["id"] for r in await drv._empty_required(pg)] == ["q-lic"]


@pytest.mark.parametrize("buttons", [
    '<button aria-pressed="false">Yes</button><button aria-pressed="true">No</button>',
    ('<button data-option="yes" aria-pressed="true"> yes </button>'
     '<button data-option="no" aria-pressed="false">no</button>'),
])
async def test_hidden_box_with_a_pressed_yes_no_pair_is_answered(drv, buttons):  # noqa: F811
    pg = await _page(drv, _box(HIDDEN + buttons), ats="ashby")
    assert await drv._empty_required(pg) == []


async def test_hidden_box_with_an_unpressed_yes_no_pair_is_empty(drv):  # noqa: F811
    pair = '<button aria-pressed="false">Yes</button><button aria-pressed="false">No</button>'
    pg = await _page(drv, _box(HIDDEN + pair), ats="ashby")
    assert [r["id"] for r in await drv._empty_required(pg)] == ["q-lic"]
