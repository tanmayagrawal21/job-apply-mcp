"""Next-step guidance for any MCP client (ADR-0059).

Every status a tool returns carries a short machine-readable `next_action` and a one-line human `hint`, so a client
that has never seen this tool (ChatGPT, Claude, an IDE agent) knows what to do next without reading the docs.
Token-lean by design: one snake_case verb phrase plus one short sentence, never data from the posting or profile.
"""
from __future__ import annotations

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


def with_guide(row: dict[str, Any], status: str | None = None) -> dict[str, Any]:
    """Return `row` plus guidance for its status (an explicit next_action already in the row wins)."""
    if "next_action" in row:
        return row
    return {**row, **guide(status if status is not None else row.get("status"))}


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
