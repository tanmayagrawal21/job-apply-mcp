# ruff: noqa: S110, S112, BLE001
"""Human-provided verification codes (ADR-0026). Kept out of browser_playwright.py on purpose.

After the ONE submit click a site may ask for an emailed/texted code. This module:
  * detects the prompt (text pattern AND a visible code input), leaves the page open and registers a pending
    entry with an expiry,
  * validates a code the USER typed (format only: charset and length from the input's own constraints),
  * enters it with real keystrokes, clicks the page's verify/submit button once, and hands the page back to the
    driver's normal confirmation detection.

The tool never reads mail, never fetches a code and never stores or logs one: codes live only in function
arguments, error messages and evidence are built without them, and the code inputs are blanked before any
screenshot taken after entry. The application is never re-submitted.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import VerificationCfg
from ..core.models import SubmitResult

PROMPT_RE = re.compile(
    r"(security|verification|confirmation|one[- ]time|login|access) (code|password|pin)|"
    r"enter (the|your) (\d+[- ]?(digit|character|char)s? )?(code|pin)|"
    r"code (that )?(we|was|has been)( just)? (sent|emailed|texted)|"
    r"we (just )?(sent|emailed|texted) (you |a |an )?(\d+[- ]?(digit|character)s? )?(code|link)|"
    r"verify your (email|identity|phone|number)|check your (email|inbox|phone|messages) for",
    re.IGNORECASE)

# Inputs that look like a code field, minus things that merely contain "code" (zip, country, promo ...).
_FIND_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const sel = ['input[autocomplete="one-time-code"]', 'input[id^="security-input"]', 'input[name*="code" i]',
    'input[id*="code" i]', 'input[name*="otp" i]', 'input[id*="otp" i]', 'input[placeholder*="code" i]',
    'input[aria-label*="code" i]', 'input[aria-label*="security" i]'].join(',');
  const bad = /zip|postal|country|area|promo|coupon|referr|phone|discount/i;
  const skip = ['hidden', 'checkbox', 'radio', 'file', 'submit', 'button', 'image'];
  document.querySelectorAll('[data-jam-code]').forEach(e => e.removeAttribute('data-jam-code'));
  const all = [...document.querySelectorAll(sel)].filter(e => vis(e) && !skip.includes(e.type)
    && !bad.test((e.name || '') + ' ' + (e.id || '')));
  all.forEach((e, i) => e.setAttribute('data-jam-code', String(i)));
  return all.map((e, i) => ({i, maxlength: e.maxLength > 0 ? e.maxLength : null, pattern: e.pattern || null,
    inputmode: e.inputMode || '', type: e.type || 'text'}));
}"""
_REDACT_JS = "() => document.querySelectorAll('[data-jam-code]').forEach(e => { e.value = ''; })"
_VERIFY_NAME = re.compile(r"^\s*(verify( code)?|confirm|continue|next|submit( application)?|send application)\s*$",
                          re.IGNORECASE)
_DIGIT_PATTERNS = ("[0-9]", r"\d")


# ------------------------------------------------------------------ pure helpers (unit-tested offline)


@dataclass(frozen=True)
class CodeConstraints:
    length: int | None = None           # exact length (split inputs, or maxlength > 1 on a single input)
    min_len: int = 4
    max_len: int = 12
    digits_only: bool = False
    pattern: str | None = None          # the input's HTML `pattern` (full match)
    split: int = 0                      # >0: that many one-character inputs


def constraints_from(inputs: list[dict[str, Any]], cfg: VerificationCfg) -> CodeConstraints | None:
    """None when the inputs are ambiguous (several non-split fields): a human should handle it."""
    if not inputs:
        return None
    if len(inputs) > 1:
        if all(i.get("maxlength") == 1 for i in inputs):
            return CodeConstraints(length=len(inputs), split=len(inputs), digits_only=_digits(inputs[0]),
                                   min_len=len(inputs), max_len=len(inputs))
        return None
    i = inputs[0]
    ml = i.get("maxlength")
    length = ml if isinstance(ml, int) and ml > 1 else None
    return CodeConstraints(length=length, min_len=cfg.min_length, max_len=cfg.max_length,
                           digits_only=_digits(i), pattern=i.get("pattern") or None)


def _digits(i: dict[str, Any]) -> bool:
    pat = i.get("pattern") or ""
    return (i.get("inputmode") in ("numeric", "decimal") or i.get("type") in ("number", "tel")
            or (bool(pat) and any(p in pat for p in _DIGIT_PATTERNS) and not re.search(r"[a-zA-Z]-?[a-zA-Z]", pat)))


def normalize_code(code: str) -> str:
    return re.sub(r"[\s\-]", "", code or "")


def validate_code(code: str, c: CodeConstraints) -> str | None:
    """Format check only. The returned message never contains the code."""
    n = normalize_code(code)
    if not n:
        return "code is empty"
    if not re.fullmatch(r"[A-Za-z0-9]+", n):
        return "code may contain only letters and digits (spaces and hyphens are ignored)"
    if c.digits_only and not n.isdigit():
        return "this page expects a numeric code"
    if c.length is not None and len(n) != c.length:
        return f"this page expects a {c.length}-character code"
    if c.length is None and not (c.min_len <= len(n) <= c.max_len):
        return f"code length must be {c.min_len}-{c.max_len} characters"
    if c.pattern:
        try:
            if not re.fullmatch(c.pattern, n):
                return "code does not match the format this page requires"
        except re.error:
            pass                                    # a JS-only pattern: the page itself will judge
    return None


@dataclass
class Pending:
    id: str
    ref: str
    page: Any
    ats: Any
    constraints: CodeConstraints
    expires: float
    attempts_left: int
    busy: bool = False
    company: str = ""

    def view(self, now: float) -> dict[str, Any]:
        return {"pending_id": self.id, "ref": self.ref, "expires_in_s": max(0, int(self.expires - now)),
                "attempts_left": self.attempts_left}


@dataclass
class CodeFlow:
    cfg: VerificationCfg
    clock: Callable[[], float] = time.time
    notifier: Callable[[str], Any] | None = None
    pending: dict[str, Pending] = field(default_factory=dict)

    def by_page(self, page: Any) -> Pending | None:
        return next((p for p in self.pending.values() if p.page is page), None)

    def expired(self) -> list[Pending]:
        now = self.clock()
        return [p for p in self.pending.values() if p.expires <= now and not p.busy]


# ------------------------------------------------------------------ desktop notification (optional)


async def desktop_notify(message: str) -> None:
    """Best effort; the message is fixed text with no code and no personal data."""
    try:
        if sys.platform == "darwin":
            script = 'display notification (item 1 of argv) with title "job-apply-mcp"'
            argv = ["osascript", "-e", "on run argv", "-e", script, "-e", "end run", message]
        elif shutil.which("notify-send"):
            argv = ["notify-send", "job-apply-mcp", message]
        else:
            return
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 5)
    except Exception:
        pass


# ------------------------------------------------------------------ driver mixin


class VerificationMixin:
    """Methods PlaywrightDriver gains. Expects: self.s (Settings), self._handoff_pages, self._net, self._tasks,
    self._save_evidence(page, ref, stage, extra) and self._conclude(page, ats, ref)."""

    _codeflow: CodeFlow | None = None

    @property
    def codes(self) -> CodeFlow:
        if self._codeflow is None:
            cfg = self.s.verification                                    # type: ignore[attr-defined]
            self._codeflow = CodeFlow(cfg, notifier=desktop_notify if cfg.notify else None)
        return self._codeflow

    # -- detection (called by the driver after the submit click) ----------------------------------
    async def _await_code(self, page, ats, ref: str) -> SubmitResult | None:
        """A pending-code result if the page now asks for a code, else None (caller keeps its old behaviour)."""
        cfg = self.codes.cfg
        if not cfg.enabled:
            return None
        try:
            text = await page.locator("body").inner_text()
            inputs = await page.evaluate(_FIND_JS) if PROMPT_RE.search(text) else []
        except Exception:
            return None
        if not inputs:
            return None
        con = constraints_from(inputs, cfg)
        existing = self.codes.by_page(page)
        if existing is not None:
            await self._redact(page)                                     # never screenshot a typed code
        if existing is not None and existing.attempts_left <= 0:         # the entered code was not accepted
            return await self._to_handoff(existing, "the entered code was not accepted; attempts exhausted")
        if con is None:
            ev = await self._save_evidence(page, ref, "verification-ambiguous")     # type: ignore[attr-defined]
            self._handoff_pages.add(id(page))                                       # type: ignore[attr-defined]
            return SubmitResult(ref=ref, status="handoff", via="browser", confirmation=page.url[:200],
                                detail=f"verification prompt with several code fields; enter the code in the open "
                                       f"browser tab yourself{ev}")
        p = existing or Pending(secrets.token_urlsafe(9), ref, page, ats, con,
                                self.codes.clock() + cfg.code_ttl_s, max(1, cfg.max_attempts))
        p.constraints, p.busy = con, False
        if existing is not None:
            p.expires = self.codes.clock() + cfg.code_ttl_s
        self.codes.pending[p.id] = p
        self._handoff_pages.add(id(page))                                           # type: ignore[attr-defined]
        ev = await self._save_evidence(page, ref, "awaiting-code")                  # type: ignore[attr-defined]
        if self.codes.notifier:
            try:
                await self.codes.notifier("A verification code is needed to finish an application. "
                                          "Check your email/phone and enter it.")
            except Exception:
                pass
        return SubmitResult(ref=ref, status="awaiting_code", via="browser", confirmation=page.url[:200],
                            pending_id=p.id,
                            detail=(f"the site sent a verification code to the applicant; ask the USER for it and call "
                                    f"provide_verification_code (expires in {cfg.code_ttl_s}s; "
                                    f"{p.constraints.length or str(p.constraints.min_len) + '-' + str(p.constraints.max_len)}"
                                    f" chars){ev}"))

    # -- queries ----------------------------------------------------------------------------------
    def pending_info(self, pending_id: str) -> dict[str, Any] | None:
        p = self.codes.pending.get(pending_id)
        return None if p is None else p.view(self.codes.clock())

    def pending_list(self) -> list[dict[str, Any]]:
        now = self.codes.clock()
        return [p.view(now) for p in self.codes.pending.values()]

    def pending_check(self, pending_id: str, code: str) -> str | None:
        """Format error for a pending id (message never contains the code), or None when acceptable."""
        p = self.codes.pending.get(pending_id)
        return "unknown or expired pending_id" if p is None else validate_code(code, p.constraints)

    # -- the human's code -------------------------------------------------------------------------
    async def provide_code(self, pending_id: str, code: str) -> SubmitResult | None:
        """None for an unknown id. Format problems and expiry never touch the page's submit button."""
        p = self.codes.pending.get(pending_id)
        if p is None:
            return None
        if p.expires <= self.codes.clock():
            return await self._to_handoff(p, "verification code expired before it was entered")
        if p.busy or p.attempts_left <= 0:
            return SubmitResult(ref=p.ref, status="needs_human", via="browser", pending_id=p.id,
                                detail="a code attempt is already in progress or used; check the open browser tab")
        if (err := validate_code(code, p.constraints)):
            return SubmitResult(ref=p.ref, status="awaiting_code", via="browser", pending_id=p.id,
                                detail=f"invalid code format: {err}. Not entered; ask the user to re-check it.")
        p.busy = True
        p.attempts_left -= 1                                           # one attempt per code, spent before typing
        try:
            clicked = await self._enter_and_click(p, normalize_code(code))
        except Exception:                                              # message deliberately dropped: no details
            clicked = False
        if not clicked:
            return await self._to_handoff(p, "could not enter the code or find a single verify button")
        res = await self._conclude(p.page, p.ats, p.ref, after_code=True)          # type: ignore[attr-defined]
        if res.status != "awaiting_code":
            self.codes.pending.pop(p.id, None)
            await self._finish_page(p.page, keep=res.status != "submitted")
        else:
            res.pending_id = p.id
        return res

    async def _enter_and_click(self, p: Pending, code: str) -> bool:
        page, delay = p.page, self.codes.cfg.typing_delay_ms
        if p.constraints.split:
            for i, ch in enumerate(code[: p.constraints.split]):
                loc = page.locator(f'[data-jam-code="{i}"]')
                await loc.click()
                await loc.fill("")
                await loc.press_sequentially(ch, delay=delay)
        else:
            loc = page.locator('[data-jam-code="0"]')
            await loc.click()
            await loc.fill("")
            await loc.press_sequentially(code, delay=delay)
        btn = await self._pick_verify(page)
        if btn is None:
            await self._redact(page)
            return False
        await btn.click()                                               # the ONE click for this code
        return True

    async def _pick_verify(self, page):
        async def usable(loc) -> list:
            out = []
            for i in range(await loc.count()):
                b = loc.nth(i)
                try:
                    if await b.is_visible() and await b.is_enabled():
                        out.append(b)
                except Exception:
                    continue
            return out

        scope = page.locator('[data-jam-code="0"]').locator("xpath=ancestor::form[1]")
        tiers = [scope.get_by_role("button", name=_VERIFY_NAME),
                 scope.locator("button[type=submit], input[type=submit]"),
                 page.get_by_role("button", name=_VERIFY_NAME)]
        for t in tiers:
            c = await usable(t)
            if len(c) == 1:
                return c[0]
            if len(c) > 1:
                return None                                              # never guess
        return None

    @staticmethod
    async def _redact(page) -> None:
        try:
            await page.evaluate(_REDACT_JS)
        except Exception:
            pass

    # -- expiry / handoff -------------------------------------------------------------------------
    async def _to_handoff(self, p: Pending, why: str) -> SubmitResult:
        self.codes.pending.pop(p.id, None)
        await self._redact(p.page)
        ev = await self._save_evidence(p.page, p.ref, "code-handoff")              # type: ignore[attr-defined]
        self._handoff_pages.add(id(p.page))                                         # type: ignore[attr-defined]
        return SubmitResult(ref=p.ref, status="handoff", via="browser", confirmation=p.page.url[:200],
                            detail=f"{why}; the application was already submitted once and is NOT retried. "
                                   f"Finish or verify it in the open browser tab, then resolve_submission.{ev}")

    async def expire_pending(self) -> list[SubmitResult]:
        """Hand off every prompt whose window passed (evidence saved, page left open for the human)."""
        return [await self._to_handoff(p, "verification code window expired") for p in self.codes.expired()]

    async def _finish_page(self, page, *, keep: bool) -> None:
        pid = id(page)
        self._net.pop(pid, None)                                                    # type: ignore[attr-defined]
        self._tasks.pop(pid, None)                                                  # type: ignore[attr-defined]
        if not keep:
            self._handoff_pages.discard(pid)                                        # type: ignore[attr-defined]
            if not self.b.keep_pages_open:                                          # type: ignore[attr-defined]
                try:
                    await page.close()
                except Exception:
                    pass
