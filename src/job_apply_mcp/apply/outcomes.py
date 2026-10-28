"""Per-ATS post-submit outcome classification (ADR-0066). Pure; offline-testable.

Keys only on stable strings of the shipped ATS front ends (error codes, class names, data-qa values, GraphQL op /
error names), never on minified identifiers. Source: a read-only study of the Greenhouse, Lever and Ashby bundles
(2026-10-27). Kinds:

  success            the ATS accepted the application
  code_requested     Greenhouse low reCAPTCHA score -> the site's own emailed security-code step (ADR-0026 flow)
  captcha_rejected   the ATS refused the submission on its captcha verdict; terminal, never retried or evaded
  blocked            Ashby accepted the request but blocks the application (duplicate / limit); text reported
  validation_error   the form was not accepted (field errors)
  not_sent           Ashby refused to send because an upload / autofill was still in flight; nothing was sent
  None               not decided yet (keep polling; after the deadline the caller reports `unknown`)

Nothing here clicks, solves or bypasses anything: it only reads responses and the page.
"""
from __future__ import annotations

import re
from typing import Any

# ---- Greenhouse (job-boards + embed): JSON error body {"code": ..., "security_code_recipient": ...}
_GH_CODE = re.compile(r'"code"\s*:\s*"([a-z-]+)"')
_GH_RECIPIENT = re.compile(r'"security_code_recipient"\s*:\s*"[^"]+"')
GH_CAPTCHA_MESSAGE = "please complete the recaptcha and resubmit your application"
# ---- Ashby (GraphQL ApiSubmit*): errors[].extensions.ashbyErrorType
_ASHBY_SCORE = "RECAPTCHA_SCORE_BELOW_THRESHOLD"
_ASHBY_BLOCK = re.compile(r'"blockMessageForCandidateHtml"\s*:\s*"((?:[^"\\]|\\.)*)"')
_ASHBY_ERRMSG = re.compile(r'"errorMessages"\s*:\s*\[\s*"')
ASHBY_UPDATING = re.compile(r"we['’]re updating your application", re.IGNORECASE)
ASHBY_CAPTCHA_TOAST = re.compile(r"problem verifying this submission with captcha", re.IGNORECASE)
_LEVER_CAPTCHA = re.compile(r"captcha|verify (that )?you are (a )?human|robot", re.IGNORECASE)


def _submits(records: list[dict[str, Any]], since: int) -> list[dict[str, Any]]:
    return [r for r in records[since:] if r.get("submit")]


def _body(rec: dict[str, Any]) -> str:
    return str(rec.get("_cls") or rec.get("body") or "")


def _greenhouse_net(rec: dict[str, Any]) -> tuple[str, str] | None:
    body, status = _body(rec), int(rec.get("status") or 0)
    m = _GH_CODE.search(body)
    code = m.group(1) if m else ""
    if code == "captcha-failed":
        if _GH_RECIPIENT.search(body):
            return "code_requested", "Greenhouse sent an emailed security code (low reCAPTCHA score)"
        return "captcha_rejected", "Greenhouse rejected the reCAPTCHA verdict (captcha-failed, no code fallback)"
    if code == "captcha-retry":
        return "captcha_rejected", "Greenhouse asked for a captcha retry (captcha-retry); one attempt policy: stopped"
    if code in ("invalid-security-code", "expired-security-code", "security-code-error"):
        return "code_requested", f"Greenhouse did not accept the security code ({code})"
    if code in ("invalid-attributes", "invalid-attachment"):
        # ADR-0068: a verdict only in Greenhouse's own error shape AND a 400/422 (same as explicit_rejection). On any
        # other status (5xx, 403 WAF, 408, ...) the request may have been processed: undecided -> unknown.
        if status in (400, 422):
            return "validation_error", f"Greenhouse rejected fields ({code})"
        return None
    if code:
        return None                                    # unrecognised code: DOM / URL decide
    if 200 <= status < 300:
        return "success", "Greenhouse accepted the application (2xx)"
    return None


def _ashby_net(rec: dict[str, Any]) -> tuple[str, str] | None:
    body = _body(rec)
    if _ASHBY_SCORE in body:
        return "captcha_rejected", f"Ashby rejected the submission: {_ASHBY_SCORE}"
    if (m := _ASHBY_BLOCK.search(body)) and m.group(1).strip():
        return "blocked", "Ashby blocked the application: " + re.sub(r"<[^>]+>", " ", m.group(1))[:200].strip()
    if "FormSubmitSuccess" in body:
        return "success", "Ashby returned FormSubmitSuccess"
    if _ASHBY_ERRMSG.search(body):
        return "validation_error", "Ashby re-rendered the form with errors"
    return None


def classify(ats: str, records: list[dict[str, Any]], dom: dict[str, Any], *, since: int = 0,
             after_code: bool = False, requested: bool = True) -> tuple[str, str] | None:
    """(kind, reason) or None (undecided). `records` = recorded POST responses (submit ones carry `body`); only those
    at index >= `since` (made after this click) count. `dom` = _OUTCOME_DOM_JS snapshot. The network verdict
    outranks the page; the page is the fallback. `after_code`: the old code prompt may linger, so the DOM prompt is
    not read as a new request. `requested`: a submit-endpoint REQUEST was issued after this click (or that is unknown);
    then the outcome is never `not_sent`, whatever the page shows (an in-flight or failed request may have arrived)."""
    subs = _submits(records, since)
    if subs:
        net = {"greenhouse": _greenhouse_net, "ashby": _ashby_net}.get(ats)
        if net and (v := net(subs[-1])):
            return v
    if ats == "greenhouse":
        if dom.get("gh_code") and not after_code:
            return "code_requested", "Greenhouse shows the email security-code step"
        if GH_CAPTCHA_MESSAGE in str(dom.get("gh_submit_error") or "").lower():
            return "captcha_rejected", "Greenhouse: " + str(dom["gh_submit_error"])[:200]
    elif ats == "ashby":
        if dom.get("ashby_success"):
            return "success", "Ashby success panel"
        if dom.get("ashby_blocked"):
            return "blocked", "Ashby blocked the application: " + str(dom["ashby_blocked"])[:200]
        fail = str(dom.get("ashby_failure") or "")
        if fail and ("try these steps" in fail.lower() or _ASHBY_SCORE in fail):
            return "captcha_rejected", "Ashby reCAPTCHA score rejection: " + fail[:200]
        if dom.get("captcha_toast"):
            return "captcha_rejected", "Ashby could not verify the submission with CAPTCHA"
        if dom.get("corrections") and not subs and not requested:
            # Client-side banner, nothing sent: re-enterable. After a sent request only Ashby's own response is a
            # verdict (_ashby_net above); a banner beside an undecided request falls through -> unknown (ADR-0068).
            return "validation_error", "Ashby: your form needs corrections"
        if dom.get("updating_toast") and not subs and not requested:
            return "not_sent", "Ashby was still updating the application (upload in flight); nothing was sent"
    elif ats == "lever":
        if dom.get("lever_success"):
            return "success", "Lever: " + str(dom["lever_success"])[:100]
        err = str(dom.get("lever_error") or "")
        if err and subs and _LEVER_CAPTCHA.search(err):
            return "captcha_rejected", "Lever: " + err[:200]
        if err and subs:
            return "validation_error", "Lever: " + err[:200]
    return None


# Read-only page snapshot for classify(); host page only (never a challenge frame).
OUTCOME_DOM_JS = r"""() => {
  const vis = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const txt = s => { const e = document.querySelector(s); return e && vis(e) ? (e.innerText || '').trim().slice(0, 400) : ''; };
  const body = (document.body && document.body.innerText) || '';
  const gc = document.querySelector('fieldset#email-verification');
  return {
    gh_code: vis(gc),
    gh_submit_error: txt('#submit-error'),
    ashby_success: vis(document.querySelector('.ashby-application-form-success-container')),
    ashby_blocked: txt('.ashby-application-form-blocked-application-container'),
    ashby_failure: txt('.ashby-application-form-failure-container'),
    lever_success: txt('[data-qa="msg-submit-success"]'),
    lever_error: [...document.querySelectorAll('.application .error-message, .application-error')].filter(vis)
      .map(e => (e.innerText || '').trim()).filter(Boolean).join(' | ').slice(0, 400),
    updating_toast: /we['’]re updating your application/i.test(body),
    captcha_toast: /problem verifying this submission with captcha/i.test(body),
    corrections: /your form needs corrections/i.test(body),
  }; }"""
