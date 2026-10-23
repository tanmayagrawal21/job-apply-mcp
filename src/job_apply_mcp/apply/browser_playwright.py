# ruff: noqa: S110, S112, BLE001
"""Playwright BrowserDriver for Greenhouse / Lever / Ashby application forms (ADR-0012). Extra: `browser`.

Deterministic, no LLM. Mechanics distilled from real runs:
  * one persistent context (user-configurable profile dir) shared by all jobs of this driver; one page per job
  * images/fonts/media and analytics hosts are blocked by route interception (configurable)
  * fields are found by id/name first, then by accessible label; react-selects are driven by clicking the
    visible option (never Enter, which can submit the form)
  * text first, choices next, resume upload LAST, then a read-back verify (+ one refill) before submit
  * exactly ONE real click on the submit button; success needs a confirmation URL, or confirmation text with
    the form gone (overlay text from extensions is ignored); a missing confirmation is never retried
  * CAPTCHA / verification code / login wall / attestation text -> needs_human or roadblock, never solved
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from ..core import registry
from ..core.models import ApplicationForm, FieldKind, FormField, SubmitResult
from .geo import location_queries, pick_location
from .mapper import (
    _DEMOGRAPHIC_LABEL,
    DEMOGRAPHIC_KEYS,
    best_option,
    canonical_key,
    decline_option,
    degree_queries,
    discipline_queries,
    exact_option,
    normalize,
    pick_degree,
    pick_school,
)
from .verification import VerificationMixin

# ------------------------------------------------------------------ pure helpers (unit-tested offline)


@dataclass(frozen=True)
class AtsProfile:
    name: str
    typing: str = "fill"                      # "fill" | "keys" (React state ignores programmatic setters)
    confirm_urls: tuple[str, ...] = ()
    confirm_text: tuple[str, ...] = ("thank you for applying", "application submitted", "submitted successfully",
                                     "thanks for applying", "your application has been")
    upload_settle_extra_s: float = 0.0
    submit_markers: tuple[str, ...] = ()      # URL fragments of the submit request (network signal)
    require_upload_chip: bool = False         # wait for the filename to show before submit


ATS: dict[str, AtsProfile] = {
    "greenhouse": AtsProfile("greenhouse", confirm_urls=("/confirmation",),
                             confirm_text=("thank you for applying", "thank you for your interest"),
                             submit_markers=("/applications", "/jobs/")),
    "lever": AtsProfile("lever", confirm_urls=("/thanks",), confirm_text=("application submitted", "thanks for applying", "application has been received"),
                        upload_settle_extra_s=3.0, submit_markers=("/apply",)),  # resume parse re-renders the form
    "ashby": AtsProfile("ashby", typing="keys", confirm_urls=("/success", "/confirmation"),
                        confirm_text=("application submitted", "thanks for applying", "thank you for applying",
                                     "successfully submitted", "your application was successfully submitted"),
                        submit_markers=("apisubmit", "submitapplication"),
                        require_upload_chip=True),
}


def detect_ats(url: str, ref: str = "") -> str | None:
    host = urlparse(url).netloc.lower()
    if "greenhouse.io" in host or "gh_jid=" in url or ref.startswith("greenhouse:"):
        return "greenhouse"
    if host.endswith("lever.co") or ref.startswith("lever:"):
        return "lever"
    if host.endswith("ashbyhq.com") or ref.startswith("ashby:"):
        return "ashby"
    return None


def greenhouse_embed_url(url: str, ref: str) -> str:
    """Company pages with ?gh_jid= embed the form in an iframe (file inputs unreachable); open the embed directly."""
    parts = ref.split(":")
    if (parts[0] == "greenhouse" and len(parts) >= 3 and "gh_jid=" in url
            and "greenhouse.io" not in urlparse(url).netloc):
        return f"https://job-boards.greenhouse.io/embed/job_app?for={parts[1]}&token={parts[2]}"
    return url


# captcha_policy="attempt" ABSOLUTE LIMITS (ADR-0036). This code will never, under any setting:
#   - solve a challenge or call a solver service;     - interact with a challenge frame (no clicks, no reads inside);
#   - add stealth / fingerprint plugins;               - spoof the webdriver flag or the user agent;
#   - remove automation flags from the browser;        - inject or fabricate a captcha token;
#   - retry a submission to improve a risk score;      - click Submit more than once per submission.
# A visible challenge, before or after the click, means STOP and hand the open tab to a human.
# Reads only the HOST page (element attributes and geometry, open shadow roots); never challenge-frame contents
# (ADR-0036). FAIL CLOSED: `widgets` = known vendors present, `visible` = interactive challenge shown, `unsafe` =
# anything unknown or inconclusive (unknown vendor, unrendered/zero-size/off-screen non-badge widget, error). Only a
# positive allowlist is passive: reCAPTCHA badge / invisible / script-only (no checkbox container), and hCaptcha or
# Turnstile explicitly declared invisible / interaction-only with no shown frame.
_CAPTCHA_JS = r"""() => {
  const w = new Set(), v = new Set(), u = new Set();
  const roots = [];
  const collect = root => { roots.push(root);
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) collect(e.shadowRoot); }); };
  collect(document);
  const all = sel => roots.flatMap(r => Array.from(r.querySelectorAll(sel)));
  const shown = el => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement || (n.getRootNode().host || null)) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
    }
    const r = el.getBoundingClientRect();
    return r.width >= 30 && r.height >= 30 && r.right > 0 && r.bottom > 0;
  };
  const src = e => (e.getAttribute('src') || '') + '';
  const KNOWN = /recaptcha|hcaptcha|turnstile|challenges\.cloudflare\.com/i;
  const VENDOR = /arkose|funcaptcha|geetest|datadome|perimeterx|px-cdn|px-client|humansecurity|kasada|awswaf|aws-waf|captcha|challenge|verify|\bbot\b|botd/i;
  const frames = all('iframe, frame, embed, object');
  const scripts = all('script[src]');
  for (const f of frames) {                       // any unrecognised challenge-ish frame -> unsafe
    const s = src(f);
    if (!KNOWN.test(s) && VENDOR.test(s)) u.add('unknown-frame');
  }
  for (const sc of scripts) {
    const s = src(sc);
    if (!KNOWN.test(s) && /arkose|funcaptcha|geetest|datadome|perimeterx|px-cdn|px-client|humansecurity|kasada|awswaf|aws-waf/i.test(s)) u.add('unknown-vendor');
  }
  if (all('[class*=funcaptcha i], [id*=funcaptcha i], [class*=geetest i], [id*=geetest i], [class*=arkose i], [id*=arkose i], [class*=datadome i], [id*=datadome i]').length) u.add('unknown-vendor');
  if (all('[data-sitekey]:not(.g-recaptcha):not(.h-captcha):not(.cf-turnstile)').length) u.add('unknown-widget');
  // reCAPTCHA
  const rcF = frames.filter(f => /recaptcha/i.test(src(f)));
  const rcBox = all('.g-recaptcha');
  if (all('.grecaptcha-badge').length || rcF.length || rcBox.length || scripts.some(x => /recaptcha/i.test(src(x)))) w.add('recaptcha');
  for (const c of rcBox) if (!/invisible/i.test(c.getAttribute('data-size') || '') && !shown(c.querySelector('iframe') || c) ) u.add('recaptcha-box-unrendered');
  for (const f of rcF) {
    const inv = /size=invisible/i.test(src(f)) || !!f.closest('.grecaptcha-badge');
    if (shown(f) && !inv) v.add('recaptcha');
    else if (!shown(f) && !inv && !/bframe/i.test(src(f))) u.add('recaptcha-frame-inconclusive');
  }
  // hCaptcha: passive only if declared invisible
  const hcF = frames.filter(f => /hcaptcha/i.test(src(f))), hcBox = all('.h-captcha');
  if (hcF.length || hcBox.length || scripts.some(x => /hcaptcha/i.test(src(x)))) {
    w.add('hcaptcha');
    const inv = hcBox.length > 0 && hcBox.every(c => /invisible/i.test(c.getAttribute('data-size') || '')) ||
                (hcF.length > 0 && hcF.every(f => /size=invisible/i.test(src(f)) || /frame=challenge/i.test(src(f))));
    if (!inv) u.add('hcaptcha-not-declared-invisible');
    for (const f of hcF) if (shown(f) && !/size=invisible/i.test(src(f))) v.add('hcaptcha');
  }
  // Turnstile: passive only if declared interaction-only / execute-on-demand
  const tsF = frames.filter(f => /turnstile|challenges\.cloudflare\.com/i.test(src(f))), tsBox = all('.cf-turnstile');
  if (tsF.length || tsBox.length || scripts.some(x => /turnstile/i.test(src(x)))) {
    w.add('turnstile');
    const decl = tsBox.length > 0 && tsBox.every(c => /interaction-only/i.test(c.getAttribute('data-appearance') || '') ||
                                                     /execute/i.test(c.getAttribute('data-execution') || ''));
    if (!decl) u.add('turnstile-not-declared-passive');
    for (const f of tsF) if (shown(f)) v.add('turnstile');
  }
  return {widgets: Array.from(w), visible: Array.from(v), unsafe: Array.from(u)}; }"""

# Frames whose URL looks like a captcha/bot-check vendor are NEVER evaluated (no challenge-frame interaction).
_CAPTCHA_FRAME_URL = re.compile(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkose|funcaptcha|geetest|datadome|"
                                r"perimeterx|px-cdn|humansecurity|kasada|awswaf|captcha|challenge|verify|\bbot\b",
                                re.IGNORECASE)


# Class/id markers found by the CDP scan (closed shadow roots included). Passive allowlist below (ADR-0036).
_CAPTCHA_MARKER = re.compile(r"arkose|funcaptcha|geetest|datadome|perimeterx|humansecurity|kasada|awswaf|aws-waf|"
                             r"captcha|turnstile|cf-chl", re.IGNORECASE)


def _marker_passive(tok: str, attrs: dict[str, str]) -> bool:
    t = tok.lower()
    if t.startswith(("grecaptcha-badge", "grecaptcha-logo", "grecaptcha-error", "g-recaptcha-response",
                     "h-captcha-response", "cf-turnstile-response")):
        return True
    if t == "g-recaptcha" or t == "h-captcha":
        return "invisible" in attrs.get("data-size", "").lower()
    if t == "cf-turnstile":
        return ("interaction-only" in attrs.get("data-appearance", "").lower()
                or "execute" in attrs.get("data-execution", "").lower())
    return False


_BLOCKERS: list[tuple[str, re.Pattern[str]]] = [
    ("captcha", re.compile(r"verify you are human|i'?m not a robot|checking if the site connection is secure|"
                           r"complete the security check|hcaptcha|select all (images|squares)", re.IGNORECASE)),
    ("verification", re.compile(r"(security|verification|confirmation) code|enter the \d+[- ]?(digit|character)|"
                                r"verify your (email|identity|phone)|we (sent|emailed) (you )?a code", re.IGNORECASE)),
    ("login", re.compile(r"sign in to (continue|apply)|log in to (continue|apply)|create (an )?account to apply", re.IGNORECASE)),
    ("duplicate", re.compile(r"already (applied|submitted)|eligible to apply once every|"
                             r"you have applied to this", re.IGNORECASE)),
]


def detect_blocker(text: str) -> str | None:
    for kind, pat in _BLOCKERS:
        if pat.search(text):
            return kind
    return None


def stop_phrase_hit(text: str, phrases: list[str]) -> str | None:
    low = text.lower()
    for p in phrases:
        if p.strip() and re.search(rf"(?<!\w){re.escape(p.lower())}(?!\w)", low):
            return p
    return None


def detect_confirmation(ats: AtsProfile, url: str, text: str, form_present: bool) -> bool:
    """URL pattern is decisive; text counts only once the submit form is gone (extension overlays also say
    'Application Submitted!' on a page where nothing was sent)."""
    path = urlparse(url).path.lower() + ("?" + urlparse(url).query.lower() if urlparse(url).query else "")
    if any(u in path for u in ats.confirm_urls) or "success" in urlparse(url).query.lower():
        return True
    low = text.lower()
    return (not form_present) and any(t in low for t in ats.confirm_text)


def fill_order(form: ApplicationForm, answers: dict[str, Any], files: dict[str, str]) -> list[tuple[FormField, Any]]:
    """Text-like first, then choices, files LAST (resume validation errors persist otherwise)."""
    rank = {FieldKind.file: 9, FieldKind.select: 5, FieldKind.multiselect: 5, FieldKind.location: 4,
            FieldKind.boolean: 6}
    rows = [(f, files[f.key] if f.kind == FieldKind.file else answers[f.key]) for f in form.fields
            if (f.key in answers and f.kind != FieldKind.file) or (f.kind == FieldKind.file and f.key in files)]
    return sorted(rows, key=lambda r: rank.get(r[0].kind, 0))


def same_value(kind: FieldKind, label: str, want: Any, got: str) -> bool:
    if canonical_key(label) == "phone" or "phone" in normalize(label):
        a, b = re.sub(r"\D", "", str(want)), re.sub(r"\D", "", got)
        return bool(a) and (b.endswith(a[-10:]) or a.endswith(b[-10:])) and bool(b)
    if re.match(r"\s*(https?://|www\.)", str(want), re.IGNORECASE):    # sites normalise scheme / www / slash
        return _bare_url(str(want)) == _bare_url(got)
    return normalize(str(want)) == normalize(got)


def _bare_url(u: str) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", u.strip().lower()).rstrip("/")


# ---- evidence + network signal (pure, unit-tested)

_ERR_WORDS = re.compile(r"error|invalid|failed|required|captcha|spam|denied|not allowed", re.IGNORECASE)


def evidence_dir(data_dir: str | Path, ref: str) -> Path:
    """<data_dir>/evidence/<ref-hash>; always under the data dir, never in a repo."""
    return Path(data_dir).expanduser() / "evidence" / hashlib.sha256(ref.encode()).hexdigest()[:12]


def is_submit_request(ats: AtsProfile, url: str, method: str) -> bool:
    low = url.lower()
    return method.upper() == "POST" and any(m in low for m in ats.submit_markers)


def network_verdict(ats: AtsProfile, records: list[dict[str, Any]]) -> str | None:
    """'success' | 'failure' | None from recorded ATS submit responses (latest decisive record wins)."""
    for r in reversed([r for r in records if r.get("submit")]):
        status, body = int(r.get("status", 0)), str(r.get("body", ""))
        low = body.lower()
        if status >= 400:
            return "failure"
        if ats.name == "ashby":
            if '"errors"' in low and not re.search(r'"errors"\s*:\s*(\[\s*\]|null)', low):
                return "failure"
            if "formsubmitsuccess" in low or re.search(r'"success"\s*:\s*true', low) or "submitsuccess" in low:
                return "success"
            if _ERR_WORDS.search(body.replace('"errors":null', "").replace('"errors":[]', "")):
                return "failure"
            continue                                   # ambiguous: fall back to URL/text
        if status < 400 and not _ERR_WORDS.search(body):
            return "success"
        return "failure"
    return None


def decide_outcome(ats: AtsProfile, url: str, text: str, form_present: bool,
                   records: list[dict[str, Any]]) -> str | None:
    """'submitted' | 'failed' | None. The network response outranks page text; URL/text are the fallback."""
    net = network_verdict(ats, records)
    if net == "success":
        return "submitted"
    if net == "failure":
        return "failed"
    return "submitted" if detect_confirmation(ats, url, text, form_present) else None


def trim(s: str, n: int) -> str:
    s = re.sub(r"[ \t]+", " ", s or "").strip()
    return s if len(s) <= n else s[:n] + "...[truncated]"


# JS: visible validation errors; returns keys/messages only, never input values.
_ERRORS_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const out = [];
  const key = e => e.getAttribute('name') || e.id || e.getAttribute('aria-label') || e.tagName.toLowerCase();
  document.querySelectorAll('[aria-invalid=true]').forEach(e => { if (vis(e)) out.push('invalid:' + key(e)); });
  document.querySelectorAll('[role=alert], [class*=banner i], [class*=alert i]').forEach(e => { const t = (e.innerText || '').trim();
    if (t && vis(e)) out.push('alert:' + t.slice(0, 120)); });
  document.querySelectorAll('[class*=error i]').forEach(e => { const t = (e.innerText || '').trim();
    if (t && t.length < 200 && vis(e) && !e.querySelector('[class*=error i]')) out.push('error:' + t.slice(0, 120)); });
  return Array.from(new Set(out)).slice(0, 20);
}"""


# ------------------------------------------------------------------ Playwright layer (live-tested only)

_BENIGN_ALERT = re.compile(r"cookie|privacy notice|job alert|future opportunities", re.IGNORECASE)
_MENU_OPTS = "[class*=select__menu] [role=option], [role=listbox] [role=option], [role=option], [class*=option]"
_SELECT_VALUES = "[class*=select__single-value], [class*=select__multi-value__label]"
_MENU_SCOPED = "[class*=select__menu] [role=option], [role=listbox] [role=option], [role=option]"
_MARK_SCOPE_JS = """e => { document.querySelectorAll('[data-jam-scope]').forEach(x => x.removeAttribute('data-jam-scope'));
  let n = e; while (n && !(n.className && String(n.className).includes('select__control'))) n = n.parentElement;
  if (!n) return false; let c = n.parentElement || n;
  const sh = n.closest('[class*=select-shell], [class*=select__container]'); if (sh) c = sh;
  c.setAttribute('data-jam-scope', '1'); return true; }"""
_COUNTRY_JS = """() => { const ids = ['country', 'phone-country', 'phone_country'];
  for (const i of ids) { const e = document.getElementById(i); if (e && e.offsetParent !== null) return i; } return ''; }"""
_EMPTY_REQUIRED_JS = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const clean = t => (t || '').replace(/\\*/g, '').replace(/\\s+/g, ' ').trim();
  const labelOf = e => { let t = '';
    const lb = e.getAttribute('aria-labelledby'); if (lb) t = lb.split(' ').map(i => (document.getElementById(i) || {}).textContent || '').join(' ');
    if (!t && e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) t = l.textContent; }
    if (!t) { const l = e.closest('label'); if (l) t = (l.querySelector('.application-label') || l).textContent.split('✱')[0]; }
    if (!t) t = e.getAttribute('aria-label') || e.name || e.id || ''; return t; };
  const req = e => e.required || e.getAttribute('aria-required') === 'true' || /\\*\\s*$/.test(labelOf(e).trim());
  const out = [], seen = new Set();
  for (const e of document.querySelectorAll('input, textarea, select')) {
    const type = (e.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'file', 'search'].includes(type) || e.disabled || e.readOnly) continue;
    if (e.closest('.iti__dropdown-content') || e.getAttribute('aria-hidden') === 'true') continue;
    if (!vis(e) && !(e.getAttribute('role') === 'combobox')) continue;
    const rs = e.getAttribute('role') === 'combobox' && e.closest('[class*=select__control]');
    if (!vis(e) && !rs) continue;
    if (!req(e)) continue;
    let empty = false, grp = '';
    if (rs) { const sh = e.closest('[class*=select__control]').parentElement;
      empty = !sh.querySelector('[class*=select__single-value], [class*=select__multi-value]'); }
    else if (type === 'checkbox' || type === 'radio') {       // a required GROUP needs one checked member
      const same = [...document.querySelectorAll('input[type=' + type + ']')].filter(x => x.name === e.name);
      empty = !same.some(x => x.checked); grp = e.name;
      const q = e.closest('.application-question, fieldset, [role=group]');
      const h = q && q.querySelector('.application-label, legend, [class*=label]');
      if (h) grp = clean(h.textContent.split('✱')[0]) || grp; }
    else if (e.tagName === 'SELECT') empty = !e.value;
    else empty = !(e.value || '').trim();
    if (!empty) continue;
    const lab = grp || clean(labelOf(e)) || e.id; const sk = grp ? e.name : (e.id || lab); if (seen.has(sk)) continue; seen.add(sk);
    out.push({id: e.id || '', label: lab.slice(0, 80), rs: !!rs});
  }
  return out.slice(0, 40); }"""
_SUBMIT_NAME = re.compile(r"^\s*(submit( application)?|send application|apply( now)?)\s*$", re.IGNORECASE)


def captcha_attempt_ok(policy: str, widgets: list, state: dict[str, Any], consent_ok: Callable[[], bool]) -> bool:
    """Click Submit once through a captcha widget? Only when every widget is passive/invisible AND (the trusted
    config says `attempt` OR a consent window covers captcha_attempt). A visible or unsafe challenge: never; the
    consent is not even consulted (ADR-0036, ADR-0048)."""
    passive = bool(widgets) and not state.get("visible", True) and not state.get("unsafe", True)
    return passive and (policy == "attempt" or consent_ok())


def _consent_captcha(settings, ref: str) -> bool:
    """captcha_attempt under an active consent window (ADR-0048). Any error -> False (handoff)."""
    try:
        from ..consent import use
        return use(settings, "captcha_attempt", f"ref={ref} passive widget only") is not None
    except Exception:
        return False


class PlaywrightDriver(VerificationMixin):
    name = "playwright"

    def __init__(self, settings: Settings):
        self.s = settings
        self.b = settings.browser
        self._pw: Any = None
        self._ctx: Any = None
        self._lock = asyncio.Lock()
        self._handoff_pages: set[int] = set()
        self._net: dict[int, list[dict[str, Any]]] = {}
        self._tasks: dict[int, list[asyncio.Task]] = {}

    # -- lifecycle ------------------------------------------------------------------------------
    async def _context(self):
        async with self._lock:
            if self._ctx is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as e:  # pragma: no cover
                    raise RuntimeError("install the 'browser' extra: uv sync --extra browser "
                                       "&& playwright install chromium") from e
                from pathlib import Path
                self._pw = await async_playwright().start()
                kw: dict[str, Any] = {"headless": self.b.headless}
                if self.b.channel:
                    kw["channel"] = self.b.channel
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    str(Path(self.b.user_data_dir).expanduser()), **kw)
                self._ctx.set_default_timeout(self.b.timeout_s * 1000)
                await self._ctx.route("**/*", self._route)
        return self._ctx

    async def _route(self, route):
        req = route.request
        host = urlparse(req.url).netloc
        if req.resource_type in self.b.block_resources or any(h in host for h in self.b.block_hosts):
            await route.abort()
        else:
            await route.continue_()

    async def aclose(self) -> None:
        if self._ctx is not None:
            await self._ctx.close()
            await self._pw.stop()
            self._ctx = self._pw = None

    # -- main entry -----------------------------------------------------------------------------
    async def fill_and_submit(self, apply_url: str, form: ApplicationForm, answers: dict[str, Any],
                              files: dict[str, str], *, dry_run: bool = True) -> SubmitResult:
        ref = form.ref
        ats_name = detect_ats(apply_url, ref)
        if ats_name is None:
            return SubmitResult(ref=ref, status="needs_human", via="browser",
                                detail="unsupported_site: no deterministic filler for this host")
        ats = ATS[ats_name]
        ctx = await self._context()
        page = await ctx.new_page()
        self._attach_recorder(page, ats)
        try:
            return await self._run(page, ats, greenhouse_embed_url(apply_url, ref) if ats_name == "greenhouse"
                                   else apply_url, form, answers, files, dry_run)
        except Exception as e:
            return SubmitResult(ref=ref, status="error", via="browser", detail=f"{type(e).__name__}: {e}"[:200])
        finally:
            if id(page) not in self._handoff_pages:      # awaiting_code/handoff pages keep their recorder
                self._net.pop(id(page), None)
                self._tasks.pop(id(page), None)
            if not self.b.keep_pages_open and id(page) not in self._handoff_pages:
                await page.close()

    async def _run(self, page, ats: AtsProfile, url: str, form: ApplicationForm, answers: dict[str, Any],
                   files: dict[str, str], dry_run: bool) -> SubmitResult:
        ref = form.ref
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("form input, form textarea, form select, input[type=file]", state="attached",
                                         timeout=self.b.timeout_s * 1000)
        except Exception:
            return SubmitResult(ref=ref, status="needs_human", via="browser", detail="form did not render")
        scope = await self._form_text(page)
        if (blk := detect_blocker(scope)) in ("captcha", "verification", "login"):
            return SubmitResult(ref=ref, status="roadblock", via="browser", detail=f"{blk} before filling")
        if (hit := stop_phrase_hit(scope, self.s.policy.stop_phrases)):
            return SubmitResult(ref=ref, status="needs_human", via="browser", detail=f"attestation: '{hit}'")

        steps = fill_order(form, answers, files)
        texty = [(f, v) for f, v in steps if f.kind != FieldKind.file]
        uploads = [(f, v) for f, v in steps if f.kind == FieldKind.file]
        failed: list[str] = []
        has_phone = any(canonical_key(f.label) == "phone" or "phone" in normalize(f.label) for f, _ in texty)
        if has_phone and not await self._ensure_phone_country(page):
            failed.append("phone_country")
        for f, v in texty:
            if not await self._fill(page, ats, f, v):
                failed.append(f.key)
        for f, v in uploads:                       # resume LAST
            if not await self._upload(page, f, v):
                failed.append(f.key)
        if uploads:
            await self._settle_upload(page, ats)
            if ats.require_upload_chip:
                for f, v in uploads:
                    if f.key not in failed and not await self._wait_chip(page, v):
                        failed.append(f.key)
        bad = await self._verify(page, texty, ats)
        if bad:                                     # uploads/parsers can wipe fields: one refill pass
            if "phone_country" in failed and await self._ensure_phone_country(page):
                failed.remove("phone_country")
            for f, v in texty:
                if f.key in bad:
                    await self._fill(page, ats, f, v)
            bad = await self._verify(page, [(f, v) for f, v in texty if f.key in bad], ats)
        problems = sorted(set(failed) | set(bad))
        optional = {f.key: f for f, _ in steps if not f.required}
        skipped = [k for k in problems if k in optional and await self._is_empty(page, optional[k])]
        problems = [k for k in problems if k not in skipped]   # an optional field left fully EMPTY never blocks
        if problems:
            ev = await self._save_evidence(page, ref, "fill-failed", extra={"fields": problems[:20]})
            return SubmitResult(ref=ref, status="needs_human", via="browser",
                                detail=f"could not fill/verify: {problems[:8]}{ev}")
        await self._fill_demographics(page, answers)
        await self._fill_education(page, answers)
        empty = await self._empty_required(page)
        if empty:                                   # late re-renders (resume parse) can wipe a field: refill once
            ids = {e["id"] for e in empty}
            for f, v in texty:
                if f.key in ids:
                    await self._fill(page, ats, f, v)
            empty = await self._empty_required(page)
        if empty:                                   # never submit (or call a dry run complete) with required gaps
            labels = [e["label"] for e in empty]
            ev = await self._save_evidence(page, ref, "incomplete", extra={"required_empty": labels})
            return SubmitResult(ref=ref, status="incomplete", via="browser",
                                detail=f"required fields still empty: {labels[:10]}{ev}")
        if self.b.presubmit_validation:
            errs = await self._validation_errors(page)
            if errs:
                ev = await self._save_evidence(page, ref, "presubmit-errors", extra={"errors": errs})
                return SubmitResult(ref=ref, status="needs_human", via="browser",
                                    detail=f"validation errors before submit (not clicked): {errs[:5]}{ev}")
        attempt = False
        if not dry_run and self.b.captcha_handoff:
            state = await self._captcha_state(page)
            widgets = state["widgets"]
            if widgets or state["unsafe"] or ats.name == "ashby":
                # captcha_policy="attempt" (ADR-0036): only a passive/invisible widget and NO visible challenge may be
                # clicked through, once, exactly like a person would. Anything else stays a human handoff.
                # An active consent window with captcha_attempt (ADR-0048) applies the same rule; nothing else changes.
                if captcha_attempt_ok(self.b.captcha_policy, widgets, state, lambda: _consent_captcha(self.s, ref)):
                    attempt = True
                else:      # never evade/solve bot checks: a human makes the final click
                    ev = await self._save_evidence(page, ref, "handoff", extra={"captcha": widgets, "unsafe": state["unsafe"]})
                    if self.b.headless:
                        return SubmitResult(ref=ref, status="needs_human", via="browser",
                                            detail=f"captcha-protected ({widgets or ['ashby']}); form filled and verified, "
                                                   f"not clicked; rerun with browser.headless=false{ev}")
                    self._handoff_pages.add(id(page))
                    return SubmitResult(ref=ref, status="handoff", via="browser", confirmation=page.url[:200],
                                        detail=f"form filled and verified in the open browser tab; a human must click "
                                               f"Submit (captcha-protected){ev}")
        if dry_run:
            ev = await self._save_evidence(page, ref, "dry-run")
            return SubmitResult(ref=ref, status="dry_run", via="browser",
                                detail=f"filled and verified {len(steps)} fields; not submitted{ev}")
        return await self._submit(page, ats, ref, attempt=attempt)

    # -- evidence / network ---------------------------------------------------------------------
    def _attach_recorder(self, page, ats: AtsProfile) -> None:
        recs: list[dict[str, Any]] = []
        tasks: list[asyncio.Task] = []
        self._net[id(page)], self._tasks[id(page)] = recs, tasks

        async def grab(resp) -> None:
            try:
                req = resp.request
                if req.method.upper() != "POST" or len(recs) >= 30:
                    return
                url = resp.url
                rec: dict[str, Any] = {"url": trim(url.split("#")[0], 200), "status": resp.status,
                                       "submit": is_submit_request(ats, url, req.method)}
                if rec["submit"]:
                    try:
                        rec["body"] = trim(await asyncio.wait_for(resp.text(), 3), self.b.evidence_body_chars)
                    except Exception:
                        rec["body"] = ""
                recs.append(rec)
            except Exception:
                pass

        page.on("response", lambda r: tasks.append(asyncio.ensure_future(grab(r))))

    async def _records(self, page) -> list[dict[str, Any]]:
        pending = self._tasks.get(id(page), [])
        if pending:
            await asyncio.wait(pending, timeout=4)
        return list(self._net.get(id(page), []))

    async def _validation_errors(self, page) -> list[str]:
        try:
            errs = list(await page.evaluate(_ERRORS_JS))
            return [e for e in errs if not _BENIGN_ALERT.search(e)]  # consent banners, job-alert sign-ups
        except Exception:
            return []

    async def _captcha_state(self, page) -> dict[str, list[str]]:
        """Fail-closed captcha classification (ADR-0036): `widgets`, `visible` challenge, `unsafe` reasons. Inspects
        the host page and non-vendor child frames; vendor/challenge frames are never evaluated. Any error is
        recorded as unsafe, which means HANDOFF, never a click."""
        out: dict[str, list[str]] = {"widgets": [], "visible": [], "unsafe": []}

        def merge(d: dict) -> None:
            for k, cur in out.items():
                cur += [x for x in d.get(k, []) if x not in cur]
        try:
            merge(await page.evaluate(_CAPTCHA_JS))
        except Exception:
            out["unsafe"].append("classifier-error")
            return out
        for fr in list(page.frames):
            if fr == page.main_frame:
                continue
            try:
                if _CAPTCHA_FRAME_URL.search((fr.url or "") + " " + (fr.name or "")):
                    # The browser tracks frames inside CLOSED shadow roots too. Vendor frames are never evaluated, but
                    # their <iframe> ELEMENT (in the parent) is checked for visibility; unresolvable -> unsafe.
                    merge(await self._frame_element_state(fr))
                    continue
                merge(await fr.evaluate(_CAPTCHA_JS))
            except Exception:
                out["unsafe"].append("frame-uninspectable")    # cross-origin/detached: cannot prove it is passive
        merge(await self._cdp_markers(page))
        return out

    async def _frame_element_state(self, fr) -> dict[str, list[str]]:
        """Geometry/visibility of a vendor frame's own <iframe> element. Known invisible badge or a hidden
        recaptcha bframe container are passive; a shown frame is a visible challenge; anything else is unsafe."""
        try:
            el = await fr.frame_element()
            url = fr.url or ""
            box = await el.bounding_box()
            shown = bool(await el.is_visible()) and bool(box) and box["width"] >= 30 and box["height"] >= 30 \
                and box["x"] + box["width"] > 0 and box["y"] + box["height"] > 0
            badge = bool(await el.evaluate("e => !!e.closest('.grecaptcha-badge')")) or "size=invisible" in url
            if badge:
                return {}
            if shown:
                return {"widgets": ["frame"], "visible": ["frame:" + (url.split("#")[0].split("?")[0][:60] or fr.name)]}
            if "bframe" in url:
                return {}
            return {"widgets": ["frame"], "unsafe": ["vendor-frame-hidden-or-offscreen"]}
        except Exception:
            return {"unsafe": ["vendor-frame-unresolvable"]}

    async def _cdp_markers(self, page) -> dict[str, list[str]]:
        """Scan the whole DOM, closed shadow roots included (CDP DOM.getDocument pierce), for vendor class/id markers.
        Contents of vendor iframes are not descended into. Any failure -> unsafe (HANDOFF)."""
        sess = None
        try:
            sess = await page.context.new_cdp_session(page)
            root = (await sess.send("DOM.getDocument", {"depth": -1, "pierce": True}))["root"]
            found: list[str] = []
            stack = [root]
            while stack:
                n = stack.pop()
                at = n.get("attributes") or []
                attrs = dict(zip(at[0::2], at[1::2]))
                toks = (attrs.get("class", "").split() + [attrs.get("id", "")])
                for t in toks:
                    if t and _CAPTCHA_MARKER.search(t) and not _marker_passive(t, attrs):
                        found.append(t[:40])
                vendor_iframe = n.get("nodeName", "").lower() == "iframe" and _CAPTCHA_FRAME_URL.search(
                    attrs.get("src", "") + " " + attrs.get("name", ""))
                kids = list(n.get("children") or []) + list(n.get("shadowRoots") or [])
                if n.get("templateContent"):
                    kids.append(n["templateContent"])
                if n.get("contentDocument") and not vendor_iframe:
                    kids.append(n["contentDocument"])
                stack.extend(kids)
            return {"widgets": ["marker"] if found else [], "unsafe": [f"marker:{m}" for m in sorted(set(found))[:5]]}
        except Exception:
            return {"unsafe": ["cdp-unavailable"]}
        finally:
            if sess is not None:
                try:
                    await sess.detach()
                except Exception:
                    pass

    async def _captcha_widgets(self, page) -> list[str]:
        """Invisible-captcha / bot-check widgets present on the page (explains silent rejections)."""
        return (await self._captcha_state(page))["widgets"]

    async def _button_summary(self, page) -> list[str]:
        try:
            return list(await page.evaluate("""() => Array.from(document.querySelectorAll('button, input[type=submit]'))
              .filter(b => b.getBoundingClientRect().width > 0)
              .map(b => ((b.innerText || b.value || '').trim().slice(0, 40)) + '|' + (b.type || '') + '|' +
                        (b.disabled ? 'disabled' : 'enabled') + '|' + (b.closest('form') ? 'in-form' : 'outside'))
              .slice(0, 15)"""))
        except Exception:
            return []

    async def _save_evidence(self, page, ref: str, stage: str, extra: dict[str, Any] | None = None) -> str:
        """Screenshot, trimmed visible text, URL, validation errors, submit responses. Returns ' evidence=<dir>'."""
        if not self.b.evidence:
            return ""
        try:
            d = evidence_dir(self.s.paths.data_dir, ref)
            d.mkdir(parents=True, exist_ok=True)
            tag = f"{stage}-{int(time.time())}"
            try:
                await page.screenshot(path=str(d / f"{tag}.png"), full_page=True)
            except Exception:
                pass
            try:
                text = await page.locator("body").inner_text()
            except Exception:
                text = ""
            meta = {"ref": ref, "stage": stage, "url": trim(page.url, 300),
                    "errors": await self._validation_errors(page), "network": await self._records(page),
                    "captcha_widgets": await self._captcha_widgets(page),
                    "submit_buttons": await self._button_summary(page),
                    "text": trim(text, self.b.evidence_text_chars), **(extra or {})}
            (d / f"{tag}.json").write_text(json.dumps(meta, indent=1))
            home = str(Path.home())
            shown = str(d).replace(home, "~", 1) if str(d).startswith(home) else str(d)
            return f" evidence={shown}"
        except Exception:
            return ""

    # -- field operations -----------------------------------------------------------------------
    async def _form_text(self, page) -> str:
        loc = page.locator("form")
        try:
            if await loc.count():
                return await loc.first.inner_text()
        except Exception:
            pass
        return await page.locator("body").inner_text()

    async def _locate(self, page, f: FormField):
        key = f.key
        cands = [page.locator(f'[id="{key}"]'), page.locator(f'[name="{key}"]')]
        label = re.sub(r"\s*\*\s*$", "", f.label).strip()
        cands.append(page.get_by_label(re.compile(rf"^\s*{re.escape(label)}\s*\*?\s*$", re.IGNORECASE)))
        cands.append(page.get_by_label(re.compile(re.escape(label), re.IGNORECASE)))
        for c in cands:
            try:
                n = await c.count()
                if not n:
                    continue
                for i in range(min(n, 6)):          # skip hidden duplicates / templates
                    if await c.nth(i).is_visible():
                        return c.nth(i)
                return c.first
            except Exception:
                continue
        return None

    async def _fill(self, page, ats: AtsProfile, f: FormField, value: Any) -> bool:
        loc = await self._locate(page, f)
        if loc is None and f.kind != FieldKind.boolean:
            return False
        try:
            if f.kind == FieldKind.boolean:
                return await self._set_boolean(page, f, loc, bool(value))
            if f.kind in (FieldKind.select, FieldKind.multiselect, FieldKind.location) or f.options or \
                    await self._is_rs(loc):          # a "text" question rendered as an async react-select
                if await self._is_rs(loc) and not f.options and f.kind == FieldKind.text:
                    f = f.model_copy(update={"kind": FieldKind.location})
                vals = value if isinstance(value, list) else [value]
                ok = True
                for v in vals:
                    ok &= await self._choose(page, f, loc, str(v))
                return ok
            text = str(value)
            if ats.typing == "keys" or f.key.startswith("urls[") or f.key == "location":
                await loc.click()
                await loc.press("ControlOrMeta+a")
                await loc.press("Backspace")
                await loc.press_sequentially(text, delay=5)
                await loc.blur()                  # commit React state / surface validation
            else:
                await loc.fill(text)
            if "location-input" in ((await loc.get_attribute("class")) or ""):      # Lever: pick a geocoded result
                res = page.locator(".dropdown-results > *").first
                try:
                    await res.wait_for(state="visible", timeout=4000)
                    await res.click()
                except Exception:
                    return False
            return True
        except Exception:
            return False

    @staticmethod
    async def _is_choice_input(loc) -> bool:
        try:
            return bool(await loc.evaluate("e => e.tagName === 'INPUT' && (e.type === 'radio' || e.type === 'checkbox')"))
        except Exception:
            return False

    async def _choose_input(self, page, f: FormField, value: str) -> bool:
        """Radio / checkbox group sharing one name (Lever custom questions): click the member whose value or label
        best matches `value`, then read the state back."""
        group = page.locator(f'input[name="{f.key}"]')
        vals = await group.evaluate_all("els => els.map(e => e.value)")
        labels = await group.evaluate_all("els => els.map(e => (e.closest('label') || {}).innerText || e.value)")
        pick = best_option(value, [v for v in vals if v]) or best_option(value, [t.strip() for t in labels if t])
        if pick is None:
            return False
        idx = next((i for i, (v, t) in enumerate(zip(vals, labels)) if pick in (v, t.strip())), None)
        if idx is None:
            return False
        box = group.nth(idx)
        if not await box.is_checked():
            await box.click()                       # real click: programmatic check does not register
        return bool(await box.is_checked())

    @staticmethod
    async def _checked_values(page, key: str) -> list[str]:
        return list(await page.locator(f'input[name="{key}"]').evaluate_all(
            "els => els.filter(e => e.checked).map(e => e.value)"))

    async def _choose(self, page, f: FormField, loc, value: str) -> bool:
        if await self._is_choice_input(loc):
            return await self._choose_input(page, f, value)
        tag = (await loc.evaluate("e => e.tagName")).upper()
        if tag == "SELECT":
            for kw in ({"label": value}, {"value": value}):
                try:
                    await loc.select_option(**kw, timeout=2000)
                    return True
                except Exception:
                    continue
            return False
        if tag == "FIELDSET":                                   # checkbox / radio group (Greenhouse multi_value_*)
            return await self._check_in_group(loc, value)
        marked = False
        try:
            marked = bool(await loc.evaluate(_MARK_SCOPE_JS))
        except Exception:
            pass
        scope = page.locator("[data-jam-scope]") if marked else page
        opts = scope.locator(_MENU_SCOPED if marked else _MENU_OPTS)
        async_lookup = f.kind == FieldKind.location or not f.options
        snapped = best_option(value, f.options) if f.options else None
        flt = value if async_lookup else (snapped or value)
        if f.kind == FieldKind.location:                        # geocoder: "City, ST" often returns nothing
            pick = await self._geo_pick(page, loc, opts, value)
            if pick is None:
                await self._close_menu(page)
                return False
            texts = [t.strip() for t in await opts.all_inner_texts()]
        else:
            await loc.click()
            await loc.press_sequentially(flt, delay=15)         # type a filter
            try:
                await opts.first.wait_for(state="visible", timeout=3500 if async_lookup else 2000)
            except Exception:
                if f.options:                                   # filter matched nothing: widen
                    await loc.press("ControlOrMeta+a")
                    await loc.press("Backspace")
                    try:
                        await opts.first.wait_for(state="visible", timeout=1500)
                    except Exception:
                        await self._close_menu(page)
                        return False
                else:
                    await self._close_menu(page)
                    return False
            texts = [t.strip() for t in await opts.all_inner_texts()]
            pick = best_option(value, texts) if texts else None
            if pick is None and async_lookup and texts:
                pick = texts[0]
        if pick is None:
            await self._close_menu(page)
            return False
        idx = next(i for i, t in enumerate(texts) if t == pick)
        try:
            await opts.nth(idx).click(timeout=3000)           # CLICK the option element itself (never Enter)
        except Exception:
            await self._close_menu(page)
            return False
        if f.kind == FieldKind.multiselect:
            await page.keyboard.press("Escape")
        committed = await self._committed(page, scope if marked else None, pick)
        if not committed:
            await self._close_menu(page)
        return committed

    @staticmethod
    async def _geo_pick(page, loc, opts, value: str) -> str | None:
        """Try location_queries() in order; return the first suggestion pick_location() accepts (never a guess
        that contradicts the state/country given)."""
        for q in location_queries(value):
            await loc.click()
            await loc.press("ControlOrMeta+a")
            await loc.press("Backspace")
            await loc.press_sequentially(q, delay=15)
            try:
                await opts.first.wait_for(state="visible", timeout=3500)
            except Exception:
                continue
            texts = [t.strip() for t in await opts.all_inner_texts()]
            pick = pick_location(value, texts)
            if pick is not None:
                return pick
        return None

    @staticmethod
    async def _check_in_group(group, value: str) -> bool:
        """Tick the box whose label snaps to `value` (best_option: never a different answer), then read it back."""
        boxes = group.locator("input[type=checkbox], input[type=radio]")
        labels = [t.strip() for t in await group.locator("label").all_inner_texts()]
        pick = best_option(value, labels) if labels else None
        if pick is None:
            return False
        for i in range(await boxes.count()):
            box = boxes.nth(i)
            text = (await box.evaluate("e => (e.labels && e.labels[0] ? e.labels[0].innerText : '')")).strip()
            if text == pick:
                if not await box.is_checked():
                    await box.check(timeout=3000)
                return await box.is_checked()
        return False

    async def _is_empty(self, page, f: FormField) -> bool:
        """True only when the control holds no value at all (no text, no checked box, no select chip). Anything
        partially filled counts as non-empty, so a half-applied answer still blocks the submit."""
        loc = await self._locate(page, f)
        if loc is None:
            return False
        try:
            if await self._is_rs(loc):
                await loc.evaluate(_MARK_SCOPE_JS)
                chips = await page.locator("[data-jam-scope]").locator(_SELECT_VALUES).count()
                return chips == 0 and not (await loc.input_value()).strip()
            kind = await loc.evaluate("e => (e.type || e.tagName).toLowerCase()")
            if kind == "fieldset":
                return await loc.locator("input:checked").count() == 0
            if kind in ("checkbox", "radio"):
                name = await loc.get_attribute("name")
                if not name:
                    return not await loc.is_checked()
                return await page.locator(f"input[name='{name}']:checked").count() == 0
            if kind == "select" or kind.startswith("select"):
                return not (await loc.evaluate("e => e.value || ''")).strip()
            return not (await loc.input_value()).strip()
        except Exception:
            return False

    @staticmethod
    async def _close_menu(page) -> None:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    @staticmethod
    async def _committed(page, scope, pick: str) -> bool:
        """The choice is only real once it shows as the select's single-value / multi-value chip."""
        for _ in range(8):
            try:
                root = scope if scope is not None else page
                got = [normalize(t) for t in await root.locator(_SELECT_VALUES).all_inner_texts()]
                np = normalize(pick)
                if any(g and (g == np or g in np or np in g) for g in got):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.15)
        return False

    async def _empty_required(self, page) -> list[dict[str, Any]]:
        """Every visible required control (asterisk label / aria-required / required) that is still empty."""
        try:
            return list(await page.evaluate(_EMPTY_REQUIRED_JS))
        except Exception:
            return []

    async def _fill_demographics(self, page, answers: dict[str, Any]) -> None:
        """EEO selects that exist only in the DOM (not in the job API): choose the profile's value, else the
        decline / 'I don't wish to answer' option. Never invents a demographic; no decline option -> left empty."""
        for row in await self._empty_required(page):
            lab = row["label"]
            ck = canonical_key(lab)
            if not (row["rs"] and row["id"] and (ck in DEMOGRAPHIC_KEYS or _DEMOGRAPHIC_LABEL.search(lab))):
                continue
            want = str(answers.get(f"demographic:{ck}") or "decline")
            loc = page.locator(f'[id="{row["id"]}"]').first
            try:
                await loc.scroll_into_view_if_needed()
                marked = bool(await loc.evaluate(_MARK_SCOPE_JS))
                opts = (page.locator("[data-jam-scope]") if marked else page).locator(_MENU_SCOPED)
                await loc.click()
                await opts.first.wait_for(state="visible", timeout=2500)
                texts = [t.strip() for t in await opts.all_inner_texts()]
                low = normalize(want)
                pick = decline_option(texts) if low.startswith("decline") or low == "prefer not to say" else (
                    exact_option(want, texts) or decline_option(texts))
                if pick is None:
                    await self._close_menu(page)
                    continue
                await opts.nth(texts.index(pick)).click(timeout=3000)
                if not await self._committed(page, page.locator("[data-jam-scope]") if marked else None, pick):
                    await self._close_menu(page)
            except Exception:
                await self._close_menu(page)

    async def _rs_pick(self, page, el_id: str, queries: list[str], choose) -> str | None:
        """Type each query into the react-select `el_id`, let `choose(option_texts)` pick, CLICK it and confirm
        it shows as this select's value. Returns the committed text, or None (field left empty)."""
        loc = page.locator(f'[id="{el_id}"]').first
        try:
            await loc.scroll_into_view_if_needed()
            marked = bool(await loc.evaluate(_MARK_SCOPE_JS))
        except Exception:
            return None
        scope = page.locator("[data-jam-scope]") if marked else None
        opts = (scope or page).locator(_MENU_SCOPED)
        for q in queries:
            try:
                await loc.click()
                await loc.press("ControlOrMeta+a")
                await loc.press("Backspace")
                await loc.press_sequentially(q, delay=15)
                try:
                    await opts.first.wait_for(state="visible", timeout=3500)
                except Exception:
                    continue
                await asyncio.sleep(0.3)                  # async lists redraw after the first paint
                texts = [t.strip() for t in await opts.all_inner_texts()]
                pick = choose(texts)
                if pick is None or pick not in texts:
                    continue
                await opts.nth(texts.index(pick)).click(timeout=3000)
                if await self._committed(page, scope, pick):
                    return pick
            except Exception:
                pass
            await self._close_menu(page)
        await self._close_menu(page)
        return None

    async def _fill_education(self, page, answers: dict[str, Any]) -> None:
        """Greenhouse job-boards Education block, entry 0 = most recent degree (ADR-0029). School must match
        exactly (else an 'Other' option, else left empty); degree and discipline snap to the offered options;
        years are typed and read back. Anything not filled stays empty and the run reports `incomplete`."""
        row = answers.get("education:0")
        if not isinstance(row, dict) or not await page.locator(
                "[id='school--0'], [id='degree--0'], [id='discipline--0'], [id='end-year--0']").count():
            return                                   # some boards ask only degree / discipline / end year

        async def present(el_id: str) -> bool:
            return bool(await page.locator(f'[id="{el_id}"]').count())

        async def has_value(el_id: str) -> bool:
            try:
                loc = page.locator(f'[id="{el_id}"]').first
                if not await loc.evaluate(_MARK_SCOPE_JS):
                    return False
                return bool(await page.locator("[data-jam-scope]").locator(_SELECT_VALUES).count())
            except Exception:
                return False

        school, degree, disc = (str(row.get(k) or "").strip() for k in ("school", "degree", "discipline"))
        if school and await present("school--0") and not await has_value("school--0"):
            await self._rs_pick(page, "school--0", [school, "Other"], lambda t: pick_school(school, t))
        if degree and await present("degree--0") and not await has_value("degree--0"):
            await self._rs_pick(page, "degree--0", degree_queries(degree), lambda t: pick_degree(degree, t))
        if disc and await present("discipline--0") and not await has_value("discipline--0"):
            await self._rs_pick(page, "discipline--0", discipline_queries(disc), lambda t: best_option(disc, t))
        for el_id, key in (("start-year--0", "start_year"), ("end-year--0", "end_year")):
            want = str(row.get(key) or "")
            loc = page.locator(f'[id="{el_id}"]').first
            if not want or not await loc.count():
                continue
            try:
                if (await loc.input_value()).strip() == want:
                    continue
                await loc.click()
                await loc.press("ControlOrMeta+a")
                await loc.press("Backspace")
                await loc.press_sequentially(want, delay=15)
                if (await loc.input_value()).strip() != want:
                    await loc.fill("")                       # wrong read-back: leave empty -> incomplete
            except Exception:
                pass
        for el_id, key in (("start-month--0", "start_month"), ("end-month--0", "end_month")):
            mm = str(row.get(key) or "")
            if not (mm.isdigit() and 1 <= int(mm) <= 12) or not await page.locator(f'[id="{el_id}"]').count():
                continue
            name = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"][int(mm) - 1]
            if not await has_value(el_id):
                await self._rs_pick(page, el_id, [name], lambda t, n=name: next((o for o in t if normalize(o) == normalize(n)), None))

    async def _ensure_phone_country(self, page) -> bool:
        """Intl-tel widgets reset the number when the country changes: pick United States (+1) BEFORE typing the phone."""
        try:
            cid = await page.evaluate(_COUNTRY_JS)
        except Exception:
            cid = ""
        if cid:
            loc = page.locator(f'[id="{cid}"]').first
            try:
                marked = bool(await loc.evaluate(_MARK_SCOPE_JS))
                if marked:
                    cur = " ".join(await page.locator("[data-jam-scope]").locator(_SELECT_VALUES).all_inner_texts())
                    if "united states" in cur.lower() or "+1" in cur:
                        return True
            except Exception:
                pass
            ff = FormField(key=cid, label="Country", kind=FieldKind.select)
            for _ in range(2):
                if await self._choose(page, ff, loc, "United States"):
                    return True
                await asyncio.sleep(0.5)
                try:                                  # the pick may have landed even if the first read-back raced
                    cur = " ".join(await page.locator("[data-jam-scope]").locator(_SELECT_VALUES).all_inner_texts())
                    if "united states" in cur.lower() or "+1" in cur:
                        return True
                except Exception:
                    pass
            return False
        flag = page.locator(".iti__selected-country, .iti__selected-flag, .iti__flag-container button").first
        try:
            if not await flag.count():
                return True                     # no country widget on this form
            title = ((await flag.get_attribute("title")) or "") + (await flag.inner_text())
            if "united states" in title.lower() or "+1" in title:
                return True
            await flag.click()
            us = page.locator("li.iti__country[data-country-code=us], [role=option]:has-text('United States')").first
            await us.wait_for(state="visible", timeout=3000)
            await us.click()
            title = ((await flag.get_attribute("title")) or "") + (await flag.inner_text())
            return "united states" in title.lower() or "+1" in title
        except Exception:
            await self._close_menu(page)
            return False

    @staticmethod
    async def _is_rs(loc) -> bool:
        try:
            return bool(loc is not None and await loc.evaluate(
                "e => e.getAttribute('role') === 'combobox' && !!e.closest('[class*=select__control]')"))
        except Exception:
            return False

    async def _set_boolean(self, page, f: FormField, loc, want: bool) -> bool:
        if await self._is_rs(loc):                 # Greenhouse renders Yes/No questions as react-selects
            ff = FormField(key=f.key, label=f.label, kind=FieldKind.select, options=["Yes", "No"])
            return await self._choose(page, ff, loc, "Yes" if want else "No")
        if loc is not None:
            try:
                typ = (await loc.get_attribute("type")) or ""
                if typ == "checkbox":
                    if await loc.is_checked() != want:
                        await loc.click()      # real click: programmatic check does not register
                    return await loc.is_checked() == want
            except Exception:
                pass
        label = re.sub(r"\s*\*\s*$", "", f.label).strip()
        group = page.get_by_role("group", name=re.compile(re.escape(label), re.IGNORECASE))
        if not await group.count():
            group = page.locator("fieldset, [role=group], div[class*=question], div[class*=field]").filter(
                has_text=label).last
        pat = re.compile(r"^\s*yes\s*$" if want else r"^\s*no\s*$", re.IGNORECASE)
        for role in ("radio", "button"):
            btn = group.get_by_role(role, name=pat)
            if await btn.count():
                for _ in range(2):                 # real click, then confirm the pressed/checked state
                    await btn.first.click()
                    st = await self._btn_state(btn.first)
                    if st is None or st:
                        return True
                return False
        return False

    @staticmethod
    async def _btn_state(btn) -> bool | None:
        try:
            pressed = await btn.get_attribute("aria-pressed")
            if pressed is not None:
                return pressed == "true"
            checked = await btn.get_attribute("aria-checked")
            if checked is not None:
                return checked == "true"
            if (await btn.get_attribute("type")) == "radio":
                return await btn.is_checked()
        except Exception:
            pass
        return None

    async def _bool_state(self, page, f: FormField, loc) -> bool | None:
        """Current Yes/No state of a boolean field (checkbox, radio pair, or aria-pressed buttons); None if unknown."""
        if await self._is_rs(loc):
            try:
                await loc.evaluate(_MARK_SCOPE_JS)
                v = " ".join(await page.locator("[data-jam-scope]").locator(_SELECT_VALUES).all_inner_texts())
                return {"yes": True, "no": False}.get(normalize(v))
            except Exception:
                return None
        try:
            if loc is not None and (await loc.get_attribute("type")) == "checkbox":
                return await loc.is_checked()
        except Exception:
            pass
        label = re.sub(r"\s*\*\s*$", "", f.label).strip()
        group = page.get_by_role("group", name=re.compile(re.escape(label), re.IGNORECASE))
        if not await group.count():
            group = page.locator("fieldset, [role=group], div[class*=question], div[class*=field]").filter(
                has_text=label).last
        for role in ("radio", "button"):
            for want, pat in ((True, r"^\s*yes\s*$"), (False, r"^\s*no\s*$")):
                btn = group.get_by_role(role, name=re.compile(pat, re.IGNORECASE))
                if await btn.count() and await self._btn_state(btn.first):
                    return want
        return None

    async def _upload(self, page, f: FormField, path: str) -> bool:
        cands = [page.locator(f'input[type=file][id="{f.key}"]'), page.locator(f'input[type=file][name="{f.key}"]')]
        n = normalize(f.label)
        if "cover" not in n:
            cands.append(page.locator("input[type=file][name=resume], input[type=file][id*=resume i]"))
        cands.append(page.locator("input[type=file]"))
        for c in cands:
            try:
                if await c.count():
                    await c.first.set_input_files(path)
                    return True
            except Exception:
                continue
        return False

    async def _settle_upload(self, page, ats: AtsProfile) -> None:
        """Short explicit wait: poll until the parser/uploader text disappears (cap = upload_settle_s)."""
        deadline = self.b.upload_settle_s + ats.upload_settle_extra_s
        busy = re.compile(r"analy[sz]ing|uploading|parsing|processing", re.IGNORECASE)
        waited = 0.0
        await asyncio.sleep(0.4)
        while waited < deadline:
            if not busy.search(await page.locator("body").inner_text()):
                break
            await asyncio.sleep(0.4)
            waited += 0.4

    async def _wait_chip(self, page, path: str) -> bool:
        """Ashby shows the chosen filename once the upload finished; submit only after it appears."""
        name = Path(path).name.lower()
        stem = Path(path).stem.lower()[:20]
        deadline = time.monotonic() + max(self.b.upload_settle_s, 4.0) + 6.0
        while time.monotonic() < deadline:
            try:
                low = (await page.locator("body").inner_text()).lower()
            except Exception:
                low = ""
            if name in low or (stem and stem in low):
                return True
            await asyncio.sleep(0.4)
        return False

    async def _verify(self, page, rows: list[tuple[FormField, Any]], ats: AtsProfile | None = None) -> list[str]:
        bad: list[str] = []
        shown = " | ".join(normalize(t) for t in await page.locator(_SELECT_VALUES).all_inner_texts())
        for f, v in rows:
            if f.kind == FieldKind.boolean:
                state = await self._bool_state(page, f, await self._locate(page, f))
                if (state is None and ats is not None and ats.name == "ashby") or \
                        (state is not None and state != bool(v)):
                    bad.append(f.key)
                continue
            if f.kind == FieldKind.location:
                loc = await self._locate(page, f)
                try:
                    got = normalize(await loc.input_value()) if loc is not None else ""
                except Exception:
                    got = ""
                own = ""
                try:
                    if loc is not None and await loc.evaluate(_MARK_SCOPE_JS):
                        own = " ".join(normalize(t) for t in await page.locator("[data-jam-scope]")
                                       .locator(_SELECT_VALUES).all_inner_texts())
                except Exception:
                    pass
                if normalize(str(v)).split(" ")[0] not in (got + " " + own):
                    bad.append(f.key)
                continue
            if f.kind in (FieldKind.select, FieldKind.multiselect) or f.options:
                loc = await self._locate(page, f)
                if loc is not None and await self._is_choice_input(loc):
                    got = [normalize(x) for x in await self._checked_values(page, f.key)]
                    want = [normalize(str((best_option(o, f.options) if f.options else None) or o))
                            for o in (v if isinstance(v, list) else [v])]
                    if not got or any(w not in got for w in want):
                        bad.append(f.key)
                    continue
                try:
                    if loc is not None and (await loc.evaluate("e => e.tagName")).upper() == "SELECT":
                        got = await loc.evaluate("e => e.selectedOptions[0] ? e.selectedOptions[0].text : ''")
                        if not got or (f.options and not best_option(v if isinstance(v, str) else v[0], [got])):
                            bad.append(f.key)
                        continue
                    if loc is not None and (await loc.evaluate("e => e.tagName")).upper() == "FIELDSET":
                        ticked = [normalize(t) for t in await loc.evaluate(
                            "e => [...e.querySelectorAll('input:checked')].map(i => i.labels && i.labels[0] ? i.labels[0].innerText : '')")]
                        if any(normalize(str(one)) not in " | ".join(ticked) and not best_option(one, ticked)
                               for one in (v if isinstance(v, list) else [v])):
                            bad.append(f.key)
                        continue
                except Exception:
                    bad.append(f.key)
                    continue
                own = shown
                try:                                    # read back THIS select's own value, not the whole page's
                    if loc is not None and await loc.evaluate(_MARK_SCOPE_JS):
                        own = " | ".join(normalize(t) for t in await page.locator("[data-jam-scope]")
                                         .locator(_SELECT_VALUES).all_inner_texts())
                except Exception:
                    pass
                for one in (v if isinstance(v, list) else [v]):
                    want = normalize(str((best_option(one, f.options) if f.options else None) or one))
                    if f.kind == FieldKind.location:
                        want = want.split(" ")[0]
                    if want not in own:
                        bad.append(f.key)
                        break
                continue
            loc = await self._locate(page, f)
            if await self._is_rs(loc):
                try:
                    await loc.evaluate(_MARK_SCOPE_JS)
                    own = " ".join(normalize(t) for t in await page.locator("[data-jam-scope]")
                                   .locator(_SELECT_VALUES).all_inner_texts())
                except Exception:
                    own = ""
                if normalize(str(v)).split(" ")[0] not in own:
                    bad.append(f.key)
                continue
            try:
                got = await loc.input_value() if loc is not None else ""
            except Exception:
                got = ""
            if not same_value(f.kind, f.label, v, got):
                bad.append(f.key)
        return bad

    # -- submit ---------------------------------------------------------------------------------
    async def _pick_submit(self, page):
        """The real submit button: visible, enabled, preferring ones inside the application <form> whose text is
        'Submit application'/'Submit'. Still refuses (None, count) if more than one candidate remains."""
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

        tiers = [page.locator("form").get_by_role("button", name=_SUBMIT_NAME),
                 page.locator("form button[type=submit], form input[type=submit]"),
                 page.get_by_role("button", name=_SUBMIT_NAME),
                 page.locator("button[type=submit], input[type=submit]")]
        seen = 0
        for t in tiers:
            c = await usable(t)
            if len(c) == 1:
                return c[0], 1
            seen = max(seen, len(c))
            if len(c) > 1:
                break
        return None, seen

    async def _submit(self, page, ats: AtsProfile, ref: str, *, attempt: bool = False) -> SubmitResult:
        target, n = await self._pick_submit(page)
        if target is None:
            return SubmitResult(ref=ref, status="needs_human", via="browser",
                                detail=f"{n} submit buttons found; refusing to guess")
        if (blk := detect_blocker(await page.locator("body").inner_text())) in ("captcha", "verification"):
            return SubmitResult(ref=ref, status="roadblock", via="browser", detail=f"{blk} challenge visible")
        await target.scroll_into_view_if_needed()
        await target.click()                                   # the ONE submit click; never repeated
        return await self._conclude(page, ats, ref, attempt=attempt)

    async def _conclude(self, page, ats: AtsProfile, ref: str, *, after_code: bool = False,
                        attempt: bool = False) -> SubmitResult:
        """Poll for the outcome after a click. A verification-code prompt becomes `awaiting_code` (ADR-0026)."""
        deadline = time.monotonic() + self.b.confirmation_wait_s
        text, outcome = "", None
        while True:
            await asyncio.sleep(0.5)
            records = await self._records(page)
            try:
                text = await page.locator("body").inner_text()
                still = await page.get_by_role("button", name=_SUBMIT_NAME).count() > 0
            except Exception:                              # page navigated mid-read: retry next poll
                text, still = "", True
            outcome = decide_outcome(ats, page.url, text, still, records)
            if outcome:
                break
            if attempt and (shown := [x for k in ("visible", "unsafe") for x in (await self._captcha_state(page))[k]]):
                # A challenge appeared after our single click: STOP. No interaction, no retry; tab stays open (ADR-0036).
                self._handoff_pages.add(id(page))
                ev = await self._save_evidence(page, ref, "captcha-challenge", extra={"captcha": shown})
                return SubmitResult(ref=ref, status="roadblock", via="browser", confirmation=page.url[:200],
                                    detail=f"captcha challenge or inconclusive widget state {shown} after the single submit click; stopped, "
                                           f"tab left open for a human; do NOT retry{ev}")
            blk = detect_blocker(text)
            stop = ("duplicate", "captcha") if after_code else ("duplicate", "captcha", "verification")
            if blk in stop or time.monotonic() >= deadline:     # after a code the old prompt lingers until the reply
                break
        if after_code:
            await self._redact(page)                           # never screenshot the typed code
        if outcome == "submitted":
            ev = await self._save_evidence(page, ref, "submitted")
            return SubmitResult(ref=ref, status="submitted", via="browser", confirmation=page.url[:200],
                                detail=ev.strip() or None)
        blk = detect_blocker(text)
        stage = "failed" if outcome == "failed" else (blk or "no-confirmation")
        ev = await self._save_evidence(page, ref, stage)
        if blk == "duplicate":
            return SubmitResult(ref=ref, status="duplicate", via="browser",
                                detail=f"site reports a prior application{ev}")
        if blk == "verification" and (pending := await self._await_code(page, ats, ref)) is not None:
            return pending
        if blk in ("captcha", "verification"):
            if attempt:
                self._handoff_pages.add(id(page))          # keep the tab open for the human (ADR-0036)
            return SubmitResult(ref=ref, status="roadblock", via="browser", detail=f"{blk} after submit{ev}")
        errs = (await self._validation_errors(page))[:5]
        why = ("ATS rejected the submission" if outcome == "failed"
               else "FAILED: submit clicked but no success confirmation appeared (treat as NOT submitted)")
        # After an attempt-policy click, never return a re-enterable state: roadblock -> engine `unknown` (ADR-0036).
        return SubmitResult(ref=ref, status="roadblock" if attempt else "needs_human", via="browser",
                            detail=f"{why}; errors={errs}; do NOT retry, check manually.{ev}")


@registry.browser_drivers.register("playwright")
def _make(settings: Settings, **_: Any) -> PlaywrightDriver:
    return PlaywrightDriver(settings)
