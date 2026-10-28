"""Next-step guidance for any MCP client (ADR-0059).

Every status a tool returns carries a short machine-readable `next_action` and a one-line human `hint`, so a client
that has never seen this tool (ChatGPT, Claude, an IDE agent) knows what to do next without reading the docs.
Token-lean by design: one snake_case verb phrase plus one short sentence, never data from the posting or profile.
"""
from __future__ import annotations

import re
from typing import Any

# status -> (next_action, hint). Hints never promise an outcome and never tell a client to bypass the tool.
GUIDE: dict[str, tuple[str, str]] = {
    "planned": ("submit_application",
                "Plan ready. Confirm with the user, then call submit_application with approval_id."),
    "needs_input": ("ask_user",
                    "Ask the user each client_tasks label (with its options), then submit_application with answers."),
    "needs_human": ("ask_user",
                    "A person must act: tell the user what `detail`/`why` says. Do not retry or fill it yourself."),
    "needs_review": ("revise_answers",
                     "Rewrite the client_tasks drafts to fix the listed violations, then call submit_application."),
    "incomplete": ("ask_user",
                   "Required fields stayed empty (see detail). Ask the user for them, then resubmit with answers."),
    "handoff": ("user_clicks_submit",
                "The form is filled in an open tab. Ask the user to click Submit there; list_applications confirms."),
    "unknown": ("verify_with_user",
                "Outcome unclear; never resubmit. Ask the user to check their email or the portal."),
    "roadblock": ("ask_user",
                  "The site blocked automation (login, captcha or error). Ask the user to finish in the browser."),
    "awaiting_code": ("provide_verification_code",
                      "Ask the user for the code they received, then call provide_verification_code."),
    "awaiting_approval": ("approve",
                          "Ask the user to approve, then call submit_application with approval_id."),
    "captcha_rejected": ("tell_user",
                         "The site rejected the automated submit (captcha); never retry. The user may submit it."),
    "submitted": ("confirm_to_user", "Submitted. Tell the user; the confirmation email is matched later."),
    "dry_run": ("show_user", "Dry run: nothing was sent. Show the user what would be submitted."),
    "duplicate": ("stop", "Already applied (or in progress) for this posting; do not resubmit."),
    "skipped": ("stop", "Skipped by policy or the posting is closed; tell the user why."),
    "error": ("fix_input", "Read error/detail, correct the call, and retry once; call health if it repeats."),
}

# plan gate decisions that are not "submit"
_GATE: dict[str, str] = {"skip": "skipped", "needs_human": "needs_human", "duplicate": "duplicate"}


def guide(status: str | None) -> dict[str, str]:
    """{"next_action", "hint"} for a status; {} for a status with no guidance (never raises)."""
    g = GUIDE.get(str(status or ""))
    return {"next_action": g[0], "hint": g[1]} if g else {}


# next_actions that hand the work to the person
HUMAN_ACTIONS = frozenset({"ask_user", "verify_with_user", "user_clicks_submit", "approve",
                           "provide_verification_code", "revise_answers"})
# ADR-0067: a tool_gap inside an active consent window never becomes the person's job
_GAP_IN_WINDOW: dict[str, tuple[str, str]] = {
    "unknown": ("reconcile", "Outcome unclear; never resubmit. Run ingest_confirmations; do not ask the user."),
    "": ("retry_via_tool", "Tool gap in the consent window: retry via the tool or request_feature; do not ask the user."),
}

# submission states that override a row's status for guidance (an unclear outcome is never "retry")
_STATE_GUIDE = {"submitting": "unknown", "unknown": "unknown", "handoff": "handoff", "awaiting_code": "awaiting_code"}
# the application may already be sent: whatever the status or category, the next step is reconcile (ADR-0025)
_RECONCILE_STATES = frozenset({"submitting", "unknown"})
RECONCILE = {"next_action": "reconcile",
             "hint": "May already be sent; never resubmit. User finishes any open tab; run ingest_confirmations."}
# a handoff raised AFTER the submit click (code prompts, "already submitted once"): reconcile, never retry
POST_CLICK = re.compile(r"already submitted|submitted once|not retried|verification prompt|code field|"
                        r"enter the code|after (?:the )?submit|may already be sent", re.IGNORECASE)
# filled in an open tab or waiting for a code: never steered to a retry (a retry could submit twice)
NO_RETRY = frozenset({"handoff", "awaiting_code", "submitting", "unknown", "captcha_rejected"})


def with_guide(row: dict[str, Any], status: str | None = None, *, cat: str | None = None,
               window: bool = False, state: str | None = None, final_click: str = "agent",
               captcha_handoff: bool = True) -> dict[str, Any]:
    """Return `row` plus guidance for its status (an explicit next_action already in the row wins). The submission
    `state` wins over an error status (ADR-0059); in a consent window a tool_gap never asks the user (ADR-0067)."""
    if "next_action" in row:
        return row
    st = str(status if status is not None else row.get("status") or "")
    detail = str(row.get("detail") or "")
    if state in _RECONCILE_STATES or st == "unknown" or (st != "awaiting_code" and POST_CLICK.search(detail)):
        return {**row, **RECONCILE}           # whatever the status or category (control review, ADR-0067)
    if state in _STATE_GUIDE and st in ("error", "needs_human", "roadblock", "unknown", ""):
        st = _STATE_GUIDE[state]
    g = guide(st)
    if st in NO_RETRY or state in NO_RETRY:
        window = False
    if cat == "tool_gap" and window and g.get("next_action") in HUMAN_ACTIONS:
        a, h = _GAP_IN_WINDOW.get(st, _GAP_IN_WINDOW[""])
        g = {"next_action": a, "hint": h}
    if st == "dry_run" and g:                  # ADR-0071: never let a captcha-protected dry run read as "good to go"
        from .captcha_report import hint  # wording follows final_click / captcha_handoff (report only)
        if (h := hint(str(row.get("captcha_mode") or ""), final_click=final_click,
                      captcha_handoff=captcha_handoff)):
            g = {**g, "hint": h}
    return {**row, **g}


def plan_status(compact: dict[str, Any]) -> str:
    """The client-facing status of a plan_application / draft_answers result."""
    gate = str(compact.get("gate") or "")
    if gate and gate != "submit":
        return _GATE.get(gate, "needs_human")
    tasks = compact.get("client_tasks") or []
    if any(isinstance(t, dict) and t.get("needs_human") for t in tasks):
        return "needs_human"
    if compact.get("unresolved") or tasks:
        return "needs_input"
    return "planned"
