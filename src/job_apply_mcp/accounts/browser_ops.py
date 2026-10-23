# ruff: noqa: BLE001, S110, S112
"""Playwright implementation of LoginOps (ADR-0046). Extra: `browser`.

Secrets reach the page only through `Locator.fill(secret.reveal())` inside `fill_password`. Any Playwright error
there is reduced to its type name and returned as False, so no exception, log line or result can carry the value.
Selectors cover Workday (data-automation-id), iCIMS, SuccessFactors, Taleo and the generic Greenhouse/Lever
account pages; anything unrecognised returns False and the flow hands off to a human.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.secrets import Secret
from .login import LoginResult, PageKind, SignupResult, Terms
from .vault import normalize_host

USER_SEL = ("[data-automation-id=email], [data-automation-id=userName], input[type=email], "
            "input[autocomplete=username], input[autocomplete=email], input[name*=user i], input[id*=user i], "
            "input[name*=email i], input[id*=email i], input[name*=login i], input[id*=login i]")
PASS_SEL = "input[type=password]"
CONFIRM_SEL = ("[data-automation-id=verifyPassword], input[type=password][name*=confirm i], "
               "input[type=password][id*=confirm i], input[type=password][name*=verify i], "
               "input[type=password][id*=verify i], input[type=password][name*=retype i]")
SIGNIN_SUBMIT = ("[data-automation-id=signInSubmitButton], [data-automation-id=click_filter][aria-label*='Sign In' i], "
                 "button[type=submit], input[type=submit]")
SIGNUP_SUBMIT = ("[data-automation-id=createAccountSubmitButton], "
                 "[data-automation-id=click_filter][aria-label*='Create Account' i], button[type=submit], "
                 "input[type=submit]")
SIGNIN_LINK = ("[data-automation-id=utilityButtonSignIn], [data-automation-id=signInLink], "
               "a:text-matches('^ *(sign in|log in|login) *$', 'i'), "
               "button:text-matches('^ *(sign in|log in) *$', 'i')")
SIGNUP_LINK = ("[data-automation-id=createAccountLink], a:text-matches('create (an )?account|register|sign up', 'i'), "
               "button:text-matches('create (an )?account|sign up', 'i')")
SUBMIT_NAME = {"login": re.compile(r"^\s*(sign in|log in|login|continue|next)\s*$", re.IGNORECASE),
               "signup": re.compile(r"^\s*(create account|register|sign up|continue)\s*$", re.IGNORECASE)}

LOCKED = re.compile(r"account (is |has been )?(temporarily )?(locked|disabled|suspended)|too many (failed |unsuccessful )?"
                    r"(sign[- ]?in |login )?attempts|try again (in|after) \d+ (minutes|hours)", re.IGNORECASE)
WRONG = re.compile(r"(incorrect|invalid|wrong|unrecognized) (user ?name|e-?mail|password|credentials|sign[- ]?in)|"
                   r"(user ?name|e-?mail) (and|or) password (is |are |you entered )?(incorrect|invalid|not valid)|"
                   r"password (is )?incorrect|could not sign you in", re.IGNORECASE)
CODE = re.compile(r"(verification|security|one[- ]time) code|enter the \d+[- ]?digit|two[- ]factor|2fa", re.IGNORECASE)
VERIFY_EMAIL = re.compile(r"verify your (email|e-mail|account)|check your (email|e-mail|inbox)|verification (email|link)|"
                          r"(email|link) (has been )?sent to|activate your account", re.IGNORECASE)
EXISTS = re.compile(r"already (exists|registered|in use|have an account)|account with (this|that) e-?mail", re.IGNORECASE)

_TERMS_JS = r"""() => {
  const shown = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none') ||
           (e.closest('label') && e.closest('label').getBoundingClientRect().width > 0); };
  const out = [];
  document.querySelectorAll('input[type=checkbox], [role=checkbox]').forEach((e, i) => {
    if (!shown(e)) return;
    const id = e.id ? document.querySelector('label[for="' + CSS.escape(e.id) + '"]') : null;
    const lab = id || e.closest('label') || e.parentElement;
    const text = ((lab && lab.innerText) || e.getAttribute('aria-label') || '').trim().slice(0, 300);
    const a = lab && lab.querySelector('a[href]');
    const auto = e.getAttribute('data-automation-id') || '';
    out.push({i, text, href: a ? a.href : null,
              required: e.required || e.getAttribute('aria-required') === 'true' || auto === 'createAccountCheckbox'});
  });
  return out.slice(0, 20); }"""


class PlaywrightLoginOps:
    def __init__(self, page: Any, captcha_fn: Callable[[], Awaitable[bool]], *, expected_host: str = "",
                 settle_ms: int = 2500):
        self.page = page
        self.expected_host = normalize_host(expected_host) if expected_host else ""
        self._captcha = captcha_fn
        self.settle_ms = settle_ms
        self.last_error: str | None = None      # a type name only

    def url(self) -> str:
        return self.page.url

    async def _first_visible(self, sel: str):
        loc = self.page.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            return None
        for i in range(min(n, 10)):
            el = loc.nth(i)
            try:
                if await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    async def _visible_count(self, sel: str) -> int:
        loc = self.page.locator(sel)
        try:
            n = await loc.count()
            return sum([1 for i in range(min(n, 10)) if await loc.nth(i).is_visible()])
        except Exception:
            return 0

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded")
            await self.page.wait_for_timeout(self.settle_ms)
        except Exception:
            pass

    async def _text(self) -> str:
        try:
            return await self.page.locator("body").inner_text()
        except Exception:
            return ""

    async def captcha_visible(self) -> bool:
        try:
            return bool(await self._captcha())
        except Exception:
            return True          # fail closed: an unreadable state is treated as a challenge

    async def kind(self) -> PageKind:
        n = await self._visible_count(PASS_SEL)
        if n == 0:
            return "none"
        if n >= 2 or await self._first_visible(CONFIRM_SEL) is not None:
            return "signup"
        return "login"

    async def _click(self, sel: str) -> bool:
        el = await self._first_visible(sel)
        if el is None:
            return False
        try:
            await el.click()
        except Exception as e:
            self.last_error = type(e).__name__
            return False
        await self._settle()
        return True

    async def open_signin(self) -> bool:
        return await self._click(SIGNIN_LINK) and await self.kind() == "login"

    async def open_signup(self) -> bool:
        return await self._click(SIGNUP_LINK) and await self.kind() == "signup"

    async def _fill(self, el: Any, value: str) -> bool:
        try:
            await el.fill(value)
            return True
        except Exception as e:
            self.last_error = type(e).__name__
            return False

    async def fill_username(self, username: str) -> bool:
        el = await self._first_visible(USER_SEL)
        return el is not None and await self._fill(el, username)

    async def fill_password(self, secret: Secret, *, confirm: bool = False) -> bool:
        if confirm:
            el = await self._first_visible(CONFIRM_SEL)
            if el is None:
                els = self.page.locator(PASS_SEL)
                if await self._visible_count(PASS_SEL) < 2:
                    return True              # no confirm field on this form
                el = els.nth(1)
        else:
            el = await self._first_visible(PASS_SEL)
        if el is None or not await self._element_on_host(el):
            return False
        ok = await self._fill(el, secret.reveal())   # the ONLY place a secret value is handed to the page
        return ok

    async def _element_on_host(self, el: Any) -> bool:
        """The password field's own document (its frame, which may be a cross-origin iframe) must be on the
        expected host. Unreadable -> refuse (fail closed)."""
        if not self.expected_host:
            return False
        try:
            href = await el.evaluate("e => e.ownerDocument.location.href")
            ok = normalize_host(href) == self.expected_host
        except Exception as e:
            self.last_error = type(e).__name__
            return False
        if not ok:
            self.last_error = "FrameHostMismatch"
        return ok

    async def terms(self) -> list[Terms]:
        try:
            rows = await self.page.evaluate(_TERMS_JS)
        except Exception:
            return []
        return [Terms(ref=str(r["i"]), text=r.get("text") or "", required=bool(r.get("required")),
                      url=r.get("href")) for r in rows]

    async def accept_terms(self, t: Terms) -> bool:
        el = self.page.locator("input[type=checkbox], [role=checkbox]").nth(int(t.ref))
        try:
            if await el.get_attribute("role") == "checkbox":
                if (await el.get_attribute("aria-checked")) != "true":
                    await el.click()
            else:
                await el.check(force=True)
            return True
        except Exception as e:
            self.last_error = type(e).__name__
            return False

    async def submit(self, kind: PageKind) -> bool:
        sel = SIGNIN_SUBMIT if kind == "login" else SIGNUP_SUBMIT
        el = await self._first_visible(sel)
        if el is None:
            try:
                el = self.page.get_by_role("button", name=SUBMIT_NAME[kind]).first
                if not await el.is_visible():
                    return False
            except Exception:
                return False
        try:
            await el.click()
        except Exception as e:
            self.last_error = type(e).__name__
            return False
        await self._settle()
        return True

    async def login_result(self) -> LoginResult:
        if await self.captcha_visible():
            return "captcha"
        text = await self._text()
        if LOCKED.search(text):
            return "locked"
        if WRONG.search(text):
            return "wrong_password"
        if CODE.search(text):
            return "verification"
        return "ok" if await self._visible_count(PASS_SEL) == 0 else "unknown"

    async def signup_result(self) -> SignupResult:
        if await self.captcha_visible():
            return "captcha"
        text = await self._text()
        if EXISTS.search(text):
            return "exists"
        if VERIFY_EMAIL.search(text):
            return "verify_email"
        return "created" if await self._visible_count(PASS_SEL) == 0 else "unknown"
