"""Client-facing captcha summary for dry-run results (ADR-0071, issue #111).

Pure result shaping: it reads the fail-closed classifier output the browser driver already computes
(`{"widgets", "visible", "unsafe"}`, ADR-0036) and reports it. It never changes what a live submit does.

`captcha`       list of {kind, visible, enterprise}; visible/enterprise are true, false or null (unknown)
`captcha_mode`  none      no widget and nothing inconclusive: a live submit clicks Submit normally
                implicit  only invisible/passive widgets: a live submit clicks Submit once if captcha_policy="attempt"
                          or a consent window allows captcha_attempt, else the user clicks (never evaded)
                challenge a visible challenge: a live submit stops and the user finishes it
                unclear   the state could not be proven passive: a live submit hands the final click to the user
"""
from __future__ import annotations

from typing import Any

MODES = ("none", "implicit", "challenge", "unclear")

# ATSs whose live submit is always treated as captcha-protected, widget seen or not (browser_playwright, ADR-0012)
_ALWAYS_PROTECTED = frozenset({"ashby"})

HINTS: dict[str, str] = {
    "implicit": ("Dry run: nothing was sent. Only an invisible captcha: a live submit clicks once only if "
                 "captcha_policy='attempt' or a consent window allows it, else the user clicks; it may be rejected."),
    "challenge": ("Dry run: nothing was sent. A visible captcha challenge is on the form: a live submit stops "
                  "and the user must finish it."),
    "unclear": ("Dry run: nothing was sent. The captcha state could not be proven passive: a live submit hands "
                "the final click to the user."),
}

_SENT = "Dry run: nothing was sent. "


def hint(mode: str | None, *, final_click: str = "agent", captcha_handoff: bool = True) -> str | None:
    """The dry-run hint for `mode` under the settings a live submit would use (report only, never behaviour).
    final_click="human" (config or per call): the user always clicks Submit. captcha_handoff=false (tests only):
    a live submit clicks normally, captcha or not. None: no captcha-specific hint (mode absent, or none + agent)."""
    if mode not in MODES:
        return None
    if final_click == "human":
        if mode == "challenge":
            return _SENT + ("final_click='human': a live submit fills and verifies; the user finishes the visible "
                            "captcha challenge and clicks Submit.")
        return _SENT + "final_click='human': a live submit fills and verifies; the user clicks Submit in the open tab."
    if not captcha_handoff and mode != "none":
        lead = "A visible captcha challenge is on the form. " if mode == "challenge" else ""
        return _SENT + lead + ("captcha_handoff=false: a live submit clicks Submit with no captcha handoff; "
                               "the site may reject it.")
    return HINTS.get(mode)


def _kind_visible(kind: str, visible: list[str]) -> bool:
    if kind == "frame":
        return any(v.startswith("frame:") for v in visible)
    return kind in visible


def summarize(state: dict[str, Any] | None, *, ats: str = "", enterprise: bool | None = None,
              ) -> tuple[list[dict[str, Any]], str]:
    """(captcha items, captcha_mode) from a `_captcha_state` dict. `enterprise` applies to reCAPTCHA only."""
    if not isinstance(state, dict):
        return [], "unclear"
    widgets = [str(w) for w in state.get("widgets") or []]
    visible = [str(v) for v in state.get("visible") or []]
    unsafe = [str(u) for u in state.get("unsafe") or []]
    items: list[dict[str, Any]] = []
    for w in widgets:
        if _kind_visible(w, visible):
            vis: bool | None = True
        else:
            vis = False if not unsafe else None
        items.append({"kind": w, "visible": vis, "enterprise": enterprise if w == "recaptcha" else None})
    for v in visible:                       # a visible challenge without a matching widget entry
        k = "frame" if v.startswith("frame:") else v
        if not any(i["kind"] == k for i in items):
            items.append({"kind": k, "visible": True, "enterprise": None})
    if unsafe and not widgets:
        items.append({"kind": "unknown", "visible": None, "enterprise": None})
    forced = ats.lower() in _ALWAYS_PROTECTED and not widgets and not unsafe
    if forced:
        items.append({"kind": f"ats:{ats.lower()}", "visible": None, "enterprise": None})
    if visible:
        mode = "challenge"
    elif unsafe or forced:
        mode = "unclear"
    elif widgets:
        mode = "implicit"
    else:
        mode = "none"
    return items, mode


def counts(rows: list[dict[str, Any]], key: str = "captcha_mode") -> dict[str, int]:
    """{mode: n} over rows that carry a captcha mode (rows without one were not inspected)."""
    out: dict[str, int] = {}
    for r in rows:
        m = r.get(key) if isinstance(r, dict) else None
        if m in MODES:
            out[m] = out.get(m, 0) + 1
    return out
