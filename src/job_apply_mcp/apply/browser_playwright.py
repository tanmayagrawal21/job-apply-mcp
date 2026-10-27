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
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import consent
from ..config import Settings
from ..core import registry
from ..core.models import ApplicationForm, FieldKind, FormField, SubmitResult
from ..core.secrets import redact
from .closed import CHALLENGE_REASON, challenge_label, closed_text
from .geo import location_queries, pick_location
from .mapper import (
    _DEMOGRAPHIC_LABEL,
    DEMOGRAPHIC_KEYS,
    best_option,
    canonical_key,
    degree_queries,
    discipline_queries,
    normalize,
    pick_degree,
    pick_school,
    privacy_ack_value,
)
from .ports import final_click
from .verification import VerificationMixin

log = logging.getLogger(__name__)

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


# Applicant-tracking systems whose apply flow needs a candidate account (ADR-0046).
_ACCOUNT_ATS = ("myworkdayjobs.com", "myworkdaysite.com", "icims.com", "successfactors.com", "successfactors.eu",
                "taleo.net")


def account_ats(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _ACCOUNT_ATS)


def _email_answer(form: ApplicationForm, answers: dict[str, Any]) -> str | None:
    for f in form.fields:
        v = answers.get(f.key)
        if isinstance(v, str) and "@" in v and (canonical_key(f.label) == "email" or "email" in f.key.lower()):
            return v.strip()
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


_SCRIPT_UNKNOWN_VENDOR = re.compile(r"arkose|funcaptcha|geetest|datadome|perimeterx|humansecurity|kasada|awswaf|aws-waf",
                                    re.IGNORECASE)


def _marker_passive(tok: str, attrs: dict[str, str], node_name: str = "") -> bool:
    t = tok.lower()
    # A marker on a <script> element is a loader (e.g. Ashby's <script id="recaptchaScript">), not a widget: passive
    # per ADR-0036 (script-only reCAPTCHA). Unknown vendors stay unsafe; widgets/frames are judged separately.
    if node_name.upper() == "SCRIPT":
        return not _SCRIPT_UNKNOWN_VENDOR.search(tok)
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


# Choice groups and EEO / self-ID selects use EXACT option matching (ADR-0039): case, punctuation and whitespace are
# normalised, then the texts must be equal. The ONLY other accepted equivalence is this explicit decline table, so
# "Decline to self-identify" can select "I don't wish to answer". No substring, no fuzzy score: Female never selects
# Male, Woman never Man, Asian never Asian Indian. No exact match -> the field stays blank.
_DECLINE_SYNONYMS = frozenset(normalize(x) for x in (
    "decline", "decline to answer", "decline to self identify", "decline to self-identify", "decline to identify",
    "decline to state", "i decline to answer", "i decline to self identify", "i decline to self-identify",
    "i decline to identify", "i don't wish to answer", "i do not wish to answer", "i don't want to answer",
    "i do not want to answer", "prefer not to say", "prefer not to answer", "i prefer not to say",
    "i prefer not to answer", "i choose not to disclose", "choose not to disclose", "i wish not to answer",
    "i don't wish to self identify", "i do not wish to self identify", "i don't wish to self-identify",
    "i do not wish to self-identify"))
# Options the driver never ticks unless the planned field ITSELF is about them.
_CONSENT_WORDS = re.compile(r"consent|\bsms\b|text messag|whats ?app|marketing|newsletter|promotion|opt[ -]?in\b", re.IGNORECASE)


def exact_choice(value: Any, options: list[str]) -> str | None:
    """The option equal to `value` after normalisation, or (for a decline value) the single decline-table option."""
    nv = normalize(str(value or ""))
    if not nv:
        return None
    for o in options:
        if normalize(o) == nv:
            return o
    return exact_decline(options) if nv in _DECLINE_SYNONYMS else None


def exact_decline(options: list[str]) -> str | None:
    """The single option in the explicit decline table; none or several -> None."""
    hits = [o for o in options if normalize(o) in _DECLINE_SYNONYMS]
    return hits[0] if len(hits) == 1 else None


_COUNTRY_ALIASES = {"us": "United States", "usa": "United States", "u s": "United States", "u s a": "United States",
                    "united states of america": "United States", "america": "United States",
                    "uk": "United Kingdom", "gb": "United Kingdom", "great britain": "United Kingdom",
                    "england": "United Kingdom", "uae": "United Arab Emirates"}


def phone_country_name(country: str) -> str:
    """The profile's country as phone widgets list it ('' when none is configured: the widget is left alone)."""
    c = (country or "").strip()
    return _COUNTRY_ALIASES.get(normalize(c), c)


def same_country(name: str, shown: str) -> bool:
    """`shown` (a chip or a flag title such as 'United States: +1') IS `name`, exactly (dial code removed)."""
    return country_option(name, [shown.strip()]) is not None


def country_option(name: str, options: list[str]) -> str | None:
    """The option that IS `name` once a trailing dial code ('+1', '(+44)') is removed; never a near miss
    (no 'United States Minor Outlying Islands' for 'United States')."""
    want = normalize(name)
    if not want:
        return None
    for o in options:
        if normalize(re.sub(r"\(?\+\d[\d\s-]*\)?\s*$", "", o)) == want:
            return o
    return None


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
# The field's own container, for read-backs that must never match ANOTHER field's value (ADR-0039).
_COMMIT_SCOPE_JS = """e => { document.querySelectorAll('[data-jam-commit]').forEach(x => x.removeAttribute('data-jam-commit'));
  const ctl = e.closest('[class*=select__control]');
  let c = ctl ? (ctl.closest('[class*=select-shell], [class*=select__container]') || ctl.parentElement || ctl)
              : e.closest('[data-field-path], fieldset, [role=group], [role=radiogroup], .field-wrapper, .application-question');
  if (!c) c = e.parentElement; if (!c) return false;
  c.setAttribute('data-jam-commit', '1'); return true; }"""
# Server-rendered forms (Greenhouse job-boards is Remix) show inputs before the JS app hydrates; typing or clicking
# before that is silently lost (ADR-0039). Ready = document complete and, on an SSR page, the first form control
# carries React's own property keys.
_HYDRATED_JS = """() => { if (document.readyState !== 'complete') return false;
  const ssr = !!(window.__remixContext || window.__reactRouterContext || window.__NEXT_DATA__ || document.querySelector('[data-reactroot]'));
  if (!ssr) return true;
  const e = document.querySelector('form input:not([type=hidden]), form textarea, form select');
  return !!e && Object.keys(e).some(k => k.startsWith('__react')); }"""
_CHOICE = "input[type=checkbox], input[type=radio]"
# Ashby-style autocomplete (ADR-0056): input[role=combobox][aria-autocomplete] outside any react-select control; its
# listbox floats elsewhere in the DOM and is named by aria-controls. Option texts / click are scoped to THAT listbox.
_ARIA_COMBO_JS = """e => e.tagName === 'INPUT' && e.getAttribute('role') === 'combobox' && e.hasAttribute('aria-autocomplete')
  && !e.closest('[class*=select__control]')"""
_COMBO_LISTBOX = """const lb = (e => { const id = e.getAttribute('aria-controls') || e.getAttribute('aria-owns');
    return id ? document.getElementById(id) : null; })(e);
  const opts = lb ? [...lb.querySelectorAll('[role=option]')].filter(o => o.offsetWidth || o.offsetHeight || o.getClientRects().length) : [];"""
_COMBO_OPTIONS_JS = "e => { " + _COMBO_LISTBOX + " return opts.map(o => (o.innerText || o.textContent || '').trim()); }"
_IS_GROUP_JS = """e => !['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(e.tagName)
  && !!e.querySelector('input[type=checkbox], input[type=radio]')"""
# A choice group's members THAT BELONG TO THE FIELD (ADR-0039): when some member's name/id carries the field key, only
# those count (a wrapper may also hold another question or an SMS consent block); consent / SMS / marketing options are
# dropped unless the planned field's own label is about consent. Returns index, option text, value and checked state.
_MEMBERS_JS = """(els, a) => { const key = a[0] || '', fl = a[1] || '';
  const CONS = /consent|\\bsms\\b|text messag|whats ?app|marketing|newsletter|promotion|opt[ -]?in\\b/i;
  let rows = els.map((e, i) => ({i, name: e.name || '', id: e.id || '', value: e.value || '', checked: !!e.checked,
    text: ((e.labels && e.labels[0] ? e.labels[0].innerText : '') || (e.closest('label') || {}).innerText
           || e.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim()}));
  if (key) { const own = rows.filter(r => r.name === key || r.name.endsWith('_' + key) || r.id === key
      || r.id.startsWith(key + '_') || r.id.includes('_' + key + '-labeled-'));
    if (own.length) rows = own; }
  if (!CONS.test(fl)) rows = rows.filter(r => !CONS.test(r.text) && !CONS.test(r.name) && !CONS.test(r.value));
  return rows; }"""
# Every visible required control that is still empty (ADR-0039). Text inputs / react-selects: required markup or the
# API marks them required. Radio/checkbox groups and sets (wrapped or not; unnamed boxes grouped by field container)
# and native selects follow the INVERTED rule: nothing selected -> gap, unless the API form marks that exact field
# required:false. Only an explicit allowlist (marketing / SMS opt-ins, job-alert widgets) is exempt, and is logged. A group is a
# fieldset / role=group / role=radiogroup / consent block holding choice inputs; it is required when the group or a
# member says so (required, aria-required) or its legend/heading carries an asterisk or a "required" class; an
# unanswered consent radio group always counts. Custom-drawn boxes hide the real input, so visibility is the group's.
_EMPTY_REQUIRED_JS = """(a) => { a = a || {}; const OPTK = [...new Set(a.opt || [])], REQK = new Set(a.req || []);
  const vis = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const clean = t => (t || '').replace(/[*✱]/g, '').replace(/\\s+/g, ' ').trim();
  const labelOf = e => { let t = '';
    const lb = e.getAttribute('aria-labelledby'); if (lb) t = lb.split(' ').map(i => (document.getElementById(i) || {}).textContent || '').join(' ');
    if (!t && e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) t = l.textContent; }
    if (!t) { const l = e.closest('label'); if (l) t = (l.querySelector('.application-label') || l).textContent.split('✱')[0]; }
    if (!t) t = e.getAttribute('aria-label') || e.name || e.id || ''; return t; };
  const req = e => e.required || e.getAttribute('aria-required') === 'true' || /\\*\\s*$/.test(labelOf(e).trim())
    || REQK.has(e.id) || REQK.has(e.name);
  const CH = 'input[type=checkbox], input[type=radio]';
  const GS = 'fieldset, [role=group], [role=radiogroup], [class*=consentRadioGroup i], [class*=consent-radio i]';
  // Explicit allowlist of groups that are not application questions (left empty on purpose; logged by the caller).
  // Exempt ONLY a field whose OWN label/legend states a consent intent (never a bare word, never container text),
  // and ONLY when no required markup and no API required:true applies (control review 4).
  // FAIL CLOSED (control review 6): an opt-in is exempt only if the WHOLE label (trimmed, ^...$) is one of a few short
  // templates: fixed lead-in + consent verb + "to receive" + channel + an optional short tail ("about this/my
  // application", "about future/new jobs/roles/opportunities", "from <company>") + an optional period; or a bare
  // job-alert / newsletter / talent-community sign-up. Any second clause (and / or / ; / ? / a further sentence) or
  // any attestation, legal or eligibility term anywhere in the label (or a policy stop phrase) disqualifies it.
  const LEAD = '(?:(?:please )?(?:check|select|tick) (?:yes|no|yes or no|the box)(?: to indicate)?(?: (?:your|if you|that you))?|yes,?|i|we|you)? ?';
  const VERB = "(?:agree(?:ment)?|consent(?:ing)?|opt(?:ing)?[ -]?in|subscrib(?:e|ing)|sign(?:ing)? (?:me )?up|would like|'d like|wish|want)";
  const CHAN = '(?:(?:sms|text|text message|e-?mail|marketing) (?:messages?|updates|e-?mails?|communications?|alerts|notifications)|text messages?|sms|e-?mails?|marketing e-?mails?|marketing communications?|updates|job alerts?|newsletters?)';
  const TAIL = "(?: (?:about (?:this|my|your) application|about (?:future|new) (?:jobs|roles|opportunities)|from [a-z0-9&'. -]{1,40}))?";
  const CONSENT_RE = [
    new RegExp('^' + LEAD + VERB + ' to receiv(?:e|ing) ' + CHAN + TAIL + '\\\\.?$'),
    /^(?:please )?(?:create (?:a )?job alert|(?:send me|get|receive) job alerts?|subscribe to (?:our |the )?(?:newsletter|mailing list)|join (?:our )?talent (?:community|network))(?: for (?:similar|new) (?:roles|jobs|opportunities))?\\.?$/];
  const NOT_OPTIN = /\\b(certif\\w*|confirm\\w*|attest\\w*|true|accura\\w*|authori[sz]\\w*|eligib\\w*|18|age|background|arbitrat\\w*|relocat\\w*|sponsor\\w*|visa|criminal|legal|terms|waive\\w*|dispute\\w*|signature|clearance|citizen\\w*|ssn|social security|monitor\\w*|drug\\w*|nda|non-disclosure)\\b/;
  const STOP = (a.stop || []).map(x => String(x).toLowerCase().trim()).filter(Boolean);
  const CONSENT = { test: raw => { const t = (raw || '').toLowerCase().replace(/\\s+/g, ' ').trim();
    if (!t || NOT_OPTIN.test(t) || STOP.some(p => t.includes(p))) return false;
    if (/\\band\\b|\\bor\\b|;|\\?|[.!]\\s*\\S/.test(t.replace('yes or no', ''))) return false;      // a second clause anywhere
    return CONSENT_RE.some(r => r.test(t)); } };
  const REQ_ATTR = n => n.getAttribute('aria-required') === 'true' || n.hasAttribute('required')
    || (n.hasAttribute('data-required') && n.getAttribute('data-required') !== 'false') || /(^|[\\s_-])required([\\s_-]|$)/i.test(String(n.className || ''));
  const markedRequired = (members, container, headEl, text) => {
    if (members.some(m => m.required || m.getAttribute('aria-required') === 'true')) return true;
    if (members.some(m => (m.getAttribute('aria-describedby') || '').split(' ').some(i => i && /\\brequired\\b/i.test((document.getElementById(i) || {}).textContent || '')))) return true;
    if (/[*✱]/.test(text || '') || /\\brequired\\b/i.test(text || '')) return true;
    if (headEl) { if (REQ_ATTR(headEl) || headEl.querySelector('[class*=required i]')) return true;
      for (const pe of ['::after', '::before']) { const c = getComputedStyle(headEl, pe).content; if (c && /[*✱]/.test(c)) return true; } }
    for (let n = container, d = 0; n && n !== document.body && n.tagName !== 'FORM' && d < 5; n = n.parentElement, d++) {
      if ([...n.querySelectorAll(CH)].some(x => !members.includes(x))) break;
      if (REQ_ATTR(n)) return true; }
    return false; };
  const apiRequired = xs => xs.filter(Boolean).some(x => [...REQK].some(k => keyMatch(x, k)));
  const keyMatch = (x, k) => x === k || x.endsWith('_' + k) || x.startsWith(k + '_') || x.includes('_' + k + '-labeled-');
  const apiOptional = xs => xs.filter(Boolean).some(x => OPTK.includes(x));      // EXACT name/id only
  const optText = m => clean((m.labels && m.labels[0] ? m.labels[0].innerText : '') || (m.closest('label') || {}).innerText || m.value || '');
  const HEAD = 'legend, label, h1, h2, h3, h4, h5, h6, strong, b, .application-label, [class*=label i], [class*=question i], [class*=title i]';
  const HELP = 'small, [class*=help i], [class*=desc i], [class*=hint i], [class*=instruction i], [class*=subtitle i]';
  const out = [], seen = new Set(), done = new Set();
  document.querySelectorAll('[data-jam-group]').forEach(x => x.removeAttribute('data-jam-group'));
  let gi = 0;
  // MARKUP-OPTIONAL (ADR-0056, control review on #91): on Lever and Ashby a choice group (Lever: also a select or a
  // text input) is OPTIONAL only when (1) its question container has exactly ONE label/title of its own and (2) NO
  // required signal exists anywhere in that container. Everything else (no title, several titles, any signal, API
  // required:true) is required or ambiguous -> the inverted rule (empty blocks). Signals: required / aria-required /
  // data-required on the container or any descendant; a class matching /(^|[\\s_-])required([\\s_-]|$)/ on any of them;
  // an asterisk, or the word "required" as a marker (a whole text node, or at its start or end: "(Required)",
  // "Required", "This field is required"), in the container text outside option labels (the title included); a
  // ::before/::after asterisk on any non-option element. "required" inside a sentence ("retention is required by
  // applicable law") is prose, not a marker.
  const LEVER = a.ats === 'lever';
  const LQ = '.application-question';
  const REQ_CLS = /(^|[\\s_-])required([\\s_-]|$)/i;
  // required wording (control review 2 on #91): a signal ANYWHERE in the container text outside option labels; a
  // bracketed marker always counts; the ONLY exception is a legal-retention phrase ("is required by applicable law"),
  // removed before the test so any other marker in the same text still counts
  const REQ_WORDS = /\\brequired\\b|\\bmandatory\\b|\\bobligatory\\b|\\bmust\\s+(?:answer|complete|select|choose|fill|provide)\\b|pflicht|obligatori[oa]|obligatoire|obbligatorio|verplicht|obrigat[oó]rio|必須|必填/i;
  const REQ_BRACKET = /[(\\[]\\s*(?:required|mandatory)\\s*[)\\]]/i;
  const REQ_LEGAL = /\\b(?:is|are|be|been|being)\\s+required\\s+(?:by|under|pursuant\\s+to)\\s+(?:the\\s+)?(?:applicable\\s+)?(?:laws?|regulations?|statute|legislation)\\b/gi;
  const clsOf = n => String((n.className && n.className.baseVal !== undefined) ? n.className.baseVal : (n.className || ''));
  const attrReq = n => n.hasAttribute('required') || n.getAttribute('aria-required') === 'true'
    || (n.hasAttribute('data-required') && String(n.getAttribute('data-required')).toLowerCase() !== 'false');
  const reqSignal = (q, members, title) => {
    const els = [q, ...q.querySelectorAll('*')];
    if (els.some(n => attrReq(n) || REQ_CLS.test(clsOf(n)))) return true;
    const opts = members.filter(m => m.type === 'checkbox' || m.type === 'radio');   // a select's label is the question
    const ids = new Set(opts.map(m => m.id).filter(Boolean));
    const optLab = n => { const l = n && n.closest && n.closest('label');
      return !!l && l !== title && q.contains(l) && (opts.some(m => l.contains(m)) || ids.has(l.htmlFor)); };
    const tw = document.createTreeWalker(q, NodeFilter.SHOW_TEXT);
    let txt = '';
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      const el = n.parentElement; const t = (n.textContent || '').trim();
      if (!t || !el || el.closest('script, style, noscript, option') || optLab(el)) continue;
      if (/[*✱]/.test(t)) return true;
      txt += ' ' + t; }
    if (REQ_BRACKET.test(txt) || REQ_WORDS.test(txt.replace(REQ_LEGAL, ' '))) return true;
    for (const n of els.slice(0, 400)) { if (optLab(n) && n !== title) continue;
      for (const pe of ['::before', '::after']) { const c = getComputedStyle(n, pe).content;
        if (c && c !== 'none' && /[*✱]/.test(c)) return true; } }
    return false; };
  const markupOptional = (q, members, titles, keys) => !!q && titles.length === 1 && !reqSignal(q, members, titles[0])
    && !apiRequired(keys) && !keys.some(k => k && REQK.has(k));
  // Lever titles: the question's own .application-label(s); a lone checkbox with no question label is titled by its
  // own <label> (the future-opportunities consent).
  const leverTitles = (q, members) => { const ls = [...q.querySelectorAll('.application-label')];
    if (ls.length || members.length !== 1) return ls;
    const l = members[0].closest('label') || (members[0].id && q.querySelector('label[for="' + CSS.escape(members[0].id) + '"]'));
    return l ? [l] : []; };
  const leverText = h => { if (!h) return ''; const c = h.cloneNode(true);
    c.querySelectorAll('.required, input, ul, select, textarea').forEach(x => x.remove()); return c.textContent; };
  const ashbyOptional = (members, idCands, names) => {
    if (a.ats !== 'ashby') return false;
    const fe = members[0].closest('[data-field-path]');
    if (!fe) return false;
    const titles = [...fe.querySelectorAll('.ashby-application-form-question-title')];
    return markupOptional(fe, members, titles, idCands.concat(names, [fe.getAttribute('data-field-path')])); };
  // INVERTED RULE (ADR-0039): a visible choice group/set with NOTHING selected is a gap unless the API form marks
  // that exact field required:false (matched by name/id). Unknown or unmatched -> required.
  // ASHBY (ADR-0056): the [data-field-path] entry is the container and its .ashby-application-form-question-title
  // (or legend) the title; a required title carries a hashed `_required_<hash>` class (caught by REQ_CLS).
  const emit = (members, container, text, idCands, consent, headEl) => {
    if (members.some(m => m.checked)) return;
    const names = members.flatMap(m => [m.name, m.id]);
    if (apiOptional(idCands.concat(names))) return;
    if (ashbyOptional(members, idCands, names)) return;
    const opts = members.map(optText).slice(0, 12);
    const exempt = !apiRequired(idCands.concat(names)) && !markedRequired(members, container, headEl, text)
      && CONSENT.test(clean(text));
    const id = idCands.find(Boolean) || '';
    const sk = 'c:' + id + ':' + gi + ':' + names.join(','); if (seen.has(sk)) return; seen.add(sk);
    let gid = '';
    if (container && [...container.querySelectorAll(CH)].filter(x => !x.disabled).length === members.length) {
      container.setAttribute('data-jam-group', String(gi)); gid = String(gi); gi++; }
    out.push({id, label: (clean(text) || 'unlabelled choice set ' + id).slice(0, 80), text: clean(text).slice(0, 1500),
              rs: false, group: gid, kind: members[0].type, consent: !!consent, opts, exempt});
  };
  if (LEVER) {
    const qs = new Map();
    for (const e of document.querySelectorAll(CH)) { const q = e.closest(LQ); if (!q || e.disabled) continue;
      if (!qs.has(q)) qs.set(q, []); qs.get(q).push(e); }
    for (const [q, members] of qs) {
      members.forEach(m => done.add(m));
      if (!members.some(m => vis(m) || vis(m.closest('label') || m.parentElement))) continue;
      const names = members.flatMap(m => [m.name, m.id]);
      const titles = leverTitles(q, members);
      if (markupOptional(q, members, titles, names)) continue;            // one title, no required signal: optional
      const head = titles[0] || null;
      let text = titles.length === 1 ? leverText(head) : '';
      if (!clean(text)) text = clean([...q.querySelectorAll('.application-label')].map(leverText).join(' / '))
        || (members[0].closest('label') || {}).textContent || '';
      emit(members, q, text, [members[0].name], false, head);
    }
  }
  for (const g of document.querySelectorAll(GS)) {
    const members = [...g.querySelectorAll(CH)].filter(m => m.closest(GS) === g && !m.disabled);
    if (!members.length || !vis(g)) continue;
    members.forEach(m => done.add(m));
    const ids = new Set(members.map(m => m.id).filter(Boolean));
    let head = null; const lb = g.getAttribute('aria-labelledby');
    if (lb) head = document.getElementById(lb.split(' ')[0]);
    head = head || g.querySelector(':scope > legend') || g.querySelector('[class*=question-title], .application-label')
      || [...g.querySelectorAll('label')].find(l => !l.querySelector(CH) && !ids.has(l.htmlFor));
    const consent = members.every(m => m.type === 'radio') && (/consent/i.test(g.className + ' ' + members.map(m => m.name).join(' ')));
    let text = head ? head.textContent : (g.getAttribute('aria-label') || '');
    if (consent && !clean(text)) { const d = g.parentElement && g.parentElement.querySelector('[class*=consent i]:not(input)');
      text = d && d !== g ? d.textContent : members.map(m => (m.closest('label') || {}).textContent || '').join(' / '); }
    const fp = g.closest('[data-field-path]');
    emit(members, g, text, [g.id, fp && !consent && fp.getAttribute('data-field-path'), members[0].name], consent, head);
  }
  // Sets outside any group container: named members share a name; UNNAMED boxes are grouped by their nearest
  // common field container (never one set per box).
  const FC = '[data-field-path], .field-wrapper, .application-question, .form-group, .form-field, [class*=question i], [class*=field-entry i]';
  const fieldOf = e => e.closest(FC) || (e.closest('ul, ol') || {}).parentElement
    || (e.parentElement && e.parentElement.parentElement) || e;
  const sets = new Map();
  for (const e of document.querySelectorAll(CH)) { if (done.has(e) || e.disabled) continue;
    const k = e.name ? e.type + ':' + e.name : fieldOf(e);
    if (!sets.has(k)) sets.set(k, []); sets.get(k).push(e); }
  for (const [k, members] of sets) {
    members.forEach(m => done.add(m));
    if (!members.some(m => vis(m) || vis(m.closest('label') || m.parentElement))) continue;
    const ids = new Set(members.map(m => m.id).filter(Boolean));
    const optLabel = n => { const l = n && n.closest && n.closest('label'); return !!(l && (l.querySelector(CH) || ids.has(l.htmlFor))); };
    let c = members[0].parentElement; while (c && !members.every(m => c.contains(m))) c = c.parentElement;
    let wrap = c;
    for (let n = c, d = 0; n && n !== document.body && n.tagName !== 'FORM' && d < 6; n = n.parentElement, d++) {
      if ([...n.querySelectorAll(CH)].some(x => !members.includes(x))) break;
      wrap = n; }
    // question text: nearest preceding heading-like element inside the wrapper (help text skipped), else nearest text
    let text = '', any = '', hel = null, anyEl = null;
    const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    tw.currentNode = members[0];
    for (let n = tw.previousNode(); n; n = tw.previousNode()) {
      const el = n.parentElement; const t = (n.textContent || '').trim();
      if (!t || !el || el.closest('script, style, noscript') || optLabel(el)) continue;
      if (!wrap || !wrap.contains(n)) break;
      if (/^[(\\[]?\\s*(required|\\*|✱)\\s*[)\\]]?$/i.test(t)) continue;           // a bare marker / badge, not the question
      const blk = el.closest(HEAD); if (blk && wrap.contains(blk) && !el.closest(HELP)) { text = blk.textContent; hel = blk; break; }
      if (!any && !el.closest(HELP)) { any = (clean(t).length < 3 && el.parentElement ? el.parentElement.textContent : t); anyEl = el; } }
    if (!text) { text = any; hel = anyEl; }
    if (!text && members.length === 1) text = labelOf(members[0]);
    const fp = members[0].closest('[data-field-path]');
    emit(members, c, text, [fp && fp.getAttribute('data-field-path'), (typeof k === 'string' ? members[0].name : '') || (c && c.id)], false, hel);
  }
  // the application form = the <form> holding the API field keys (Ashby has none: whole page)
  const KEYS = new Set(a.keys || []);
  const appForm = [...document.querySelectorAll('form')].find(f => [...f.elements].some(x => KEYS.has(x.id) || KEYS.has(x.name)));
  for (const e of document.querySelectorAll('input, textarea, select')) {
    if (done.has(e)) continue;
    if (appForm && e.form && e.form !== appForm) continue;             // another form (e.g. a job-alert sign-up)
    const type = (e.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'file', 'search', 'checkbox', 'radio'].includes(type) || e.disabled || e.readOnly) continue;
    if (e.closest('.iti__dropdown-content') || e.getAttribute('aria-hidden') === 'true') continue;
    const rs = e.getAttribute('role') === 'combobox' && e.closest('[class*=select__control]');
    if (!vis(e) && !rs) continue;
    const native = e.tagName === 'SELECT';
    const lq = LEVER ? e.closest(LQ) : null;
    const fpk = (e.closest('[data-field-path]') || {getAttribute: () => ''}).getAttribute('data-field-path');
    // Lever (ADR-0056): optional only with one question label and no required signal in the question; otherwise a
    // native select follows the inverted rule and a text input counts as required when the question is ambiguous
    const lopt = !!lq && markupOptional(lq, [e], [...lq.querySelectorAll('.application-label')], [e.id, e.name]);
    const isReq = req(e) || (!!lq && !lopt) || (!!fpk && REQK.has(fpk));
    // native selects follow the inverted rule too: empty -> gap unless the API marks the field required:false
    if (!(native && !LEVER ? (isReq || !apiOptional([e.id, e.name])) : isReq)) continue;
    let empty = false;
    if (rs) { const sh = e.closest('[class*=select__control]').parentElement;
      empty = !sh.querySelector('[class*=select__single-value], [class*=select__multi-value]'); }
    else if (native) empty = !e.value;
    else empty = !(e.value || '').trim();
    if (!empty) continue;
    const fpl = fpk ? (e.closest('[data-field-path]').querySelector('label') || {}).textContent : '';
    const lab = clean(lq ? leverText(lq.querySelector('.application-label')) : '') || clean(labelOf(e)) || clean(fpl) || fpk || e.id;
    const sk = e.id || lab; if (seen.has(sk)) continue; seen.add(sk);
    out.push({id: e.id || '', label: lab.slice(0, 80), rs: !!rs,
              exempt: native && !req(e) && !apiRequired([e.id, e.name]) && CONSENT.test(lab)});
  }
  return out.slice(0, 40); }"""
# Native constraint check of the APPLICATION form only (control review 3): form.checkValidity(); never
# reportValidity, never submit. Native constraints are required markup, so nothing here is ever exempt.
_VALIDITY_JS = """(a) => { a = a || {}; const keys = new Set(a.keys || []);
  const clean = t => (t || '').replace(/[*✱]/g, '').replace(/\\s+/g, ' ').trim();
  const labelOf = e => { let t = ''; if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) t = l.textContent; }
    if (!t) { const l = e.closest('label'); if (l) t = l.textContent; }
    return clean(t || e.getAttribute('aria-label') || e.name || e.id || ''); };
  const bad = [], exempt = [];
  for (const f of document.querySelectorAll('form')) {
    const els = [...f.elements];
    if (!els.some(e => keys.has(e.id) || keys.has(e.name))) continue;          // only the application form
    const constrained = els.some(e => e.required || e.pattern || e.min || e.max || e.minLength > 0
      || ['email', 'url', 'number', 'tel'].includes((e.type || '').toLowerCase()));
    if (!constrained || f.checkValidity()) continue;                            // checkValidity only: no UI, no submit
    // a checkbox GROUP whose members each carry `required` (Greenhouse) is satisfied by one ticked member
    const grpOk = e => e.type === 'checkbox' && e.name && !!f.querySelector('input[type=checkbox][name="' + CSS.escape(e.name) + '"]:checked');
    const raw = els.filter(e => e.willValidate && !e.validity.valid);
    const inv = raw.filter(e => !(e.validity.valueMissing && grpOk(e)));
    for (const e of inv) bad.push(labelOf(e).slice(0, 80) || 'invalid field');   // constrained -> required wins
    if (!raw.length) bad.push('form failed native validation');
  }
  return {bad: [...new Set(bad)].slice(0, 20), exempt: [...new Set(exempt)].slice(0, 20)}; }"""
# Visible, still-empty selects (react-select comboboxes and native <select>), required or not, with their labels.
# Used only for DOM-only voluntary self-identification questions (_fill_demographics).
_DOM_SELECTS_JS = """(a) => { const OPTK = (a && a.opt) || [];
  const apiOpt = xs => xs.filter(Boolean).some(x => OPTK.includes(x));
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const clean = t => (t || '').replace(/[*✱]/g, '').replace(/\\s+/g, ' ').trim();
  const labelOf = e => { let t = '';
    const lb = e.getAttribute('aria-labelledby'); if (lb) t = lb.split(' ').map(i => (document.getElementById(i) || {}).textContent || '').join(' ');
    if (!t && e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) t = l.textContent; }
    if (!t) t = e.getAttribute('aria-label') || ''; return t; };
  const out = [];
  for (const e of document.querySelectorAll('input[role=combobox], select')) {
    if (!e.id || e.disabled || e.closest('.iti__dropdown-content')) continue;
    const ctl = e.tagName === 'INPUT' && e.closest('[class*=select__control]');
    if (e.tagName === 'INPUT' && !ctl) continue;
    if (!vis(ctl || e)) continue;
    const empty = ctl ? !ctl.parentElement.querySelector('[class*=select__single-value], [class*=select__multi-value]') : !e.value;
    if (!empty) continue;
    const raw = labelOf(e);
    const required = e.required || e.getAttribute('aria-required') === 'true' || /[*✱]\\s*$/.test(raw.trim())
      || (e.tagName === 'SELECT' && !apiOpt([e.id, e.name]));          // native: inverted rule (ADR-0039)
    out.push({id: e.id, label: clean(raw).slice(0, 120), required, native: e.tagName === 'SELECT'});
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
        self._api_keys: dict[int, dict[str, list[str]]] = {}   # page -> API required / optional keys (ADR-0039)
        self._exempt: dict[int, list[str]] = {}                # page -> allowlisted opt-in groups left empty (logged)
        self._combo: dict[tuple[int, str], str | None] = {}    # (page, key) -> committed autocomplete text / None=failed
        # final_click="human" (ADR-0058): ref -> background confirmation watcher; the engine sets the callback
        self._watchers: dict[str, asyncio.Task] = {}
        # keep_unsubmitted_open (ADR-0067): pages the tool started filling; filled-but-unsubmitted tabs it kept
        self._filled: set[int] = set()
        self._kept: dict[int, dict[str, Any]] = {}
        self.on_human_confirmed: Callable[[SubmitResult], Awaitable[Any]] | None = None

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
        if self._kept and not self.b.keep_pages_open:
            log.warning("closing the browser with %d filled-but-unsubmitted tab(s); they are listed in "
                        "kept_tabs.json for the person", len(self._kept))
        for t in self._watchers.values():
            t.cancel()
        self._watchers.clear()
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
            detail = "unsupported_site: no deterministic filler for this host"
            if account_ats(apply_url):           # sign in (vault, consent-gated) so a human continues logged in
                detail += "; " + await self.ensure_login(apply_url, _email_answer(form, answers), dry_run=dry_run)
            return SubmitResult(ref=ref, status="needs_human", via="browser", detail=detail)
        ats = ATS[ats_name]
        ctx = await self._context()
        page = await ctx.new_page()
        self._attach_recorder(page, ats)
        res: SubmitResult | None = None
        try:
            res = await self._run(page, ats, greenhouse_embed_url(apply_url, ref) if ats_name == "greenhouse"
                                  else apply_url, form, answers, files, dry_run)
            return res
        except Exception as e:
            res = SubmitResult(ref=ref, status="error", via="browser", detail=f"{type(e).__name__}: {e}"[:200])
            return res
        finally:
            keep_unsent = self._keep_unsubmitted(page, res)
            self._api_keys.pop(id(page), None)
            self._exempt.pop(id(page), None)
            for k in [k for k in self._combo if k[0] == id(page)]:
                self._combo.pop(k, None)
            if id(page) not in self._handoff_pages:      # awaiting_code/handoff pages keep their recorder
                self._net.pop(id(page), None)
                self._tasks.pop(id(page), None)
            self._filled.discard(id(page))
            if keep_unsent:
                self._remember_kept(page, ref, res)
            elif not self.b.keep_pages_open and id(page) not in self._handoff_pages:
                await page.close()

    # -- keep_unsubmitted_open (ADR-0067) -------------------------------------------------------
    _NOT_KEPT = frozenset({"submitted", "skipped", "duplicate"})

    def _keep_unsubmitted(self, page, res: SubmitResult | None) -> bool:
        """Keep a tab the tool FILLED but did not submit (needs_human, incomplete, handoff, roadblock, a filled
        dry run, no confirmation, error after filling). Closed postings and never-filled pages still close."""
        if not self.b.keep_unsubmitted_open or self.b.headless or id(page) not in self._filled:
            return False
        return res is None or res.status not in self._NOT_KEPT

    def _remember_kept(self, page, ref: str, res: SubmitResult | None) -> None:
        pid = id(page)
        self._kept[pid] = {"ref": ref, "status": res.status if res else "error",
                           "url": str(getattr(page, "url", "") or "")[:300]}
        on_close = getattr(page, "on", None)
        if callable(on_close):
            try:
                on_close("close", lambda *_: self._forget_kept(pid))
            except Exception:
                pass
        self._write_kept()

    def _forget_kept(self, pid: int) -> None:
        if self._kept.pop(pid, None) is not None:
            self._write_kept()

    def kept_tabs(self) -> list[dict[str, Any]]:
        """Filled-but-unsubmitted tabs still open (no secrets: ref, status and the page URL only)."""
        return list(self._kept.values())

    def _write_kept(self) -> None:
        """<data_dir>/kept_tabs.json lists the kept tabs so they can be reopened if the browser goes away."""
        try:
            p = Path(self.s.paths.data_dir).expanduser() / "kept_tabs.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.kept_tabs(), indent=1), encoding="utf-8")
        except Exception:
            log.debug("could not write kept_tabs.json", exc_info=True)

    async def _run(self, page, ats: AtsProfile, url: str, form: ApplicationForm, answers: dict[str, Any],
                   files: dict[str, str], dry_run: bool) -> SubmitResult:
        ref = form.ref
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("form input, form textarea, form select, input[type=file]", state="attached",
                                         timeout=self.b.timeout_s * 1000)
        except Exception:
            try:
                body = await page.locator("body").inner_text()
            except Exception:
                body = ""
            if (m := closed_text(body)):                # ADR-0056: a closed posting is skipped, not an owner action
                return SubmitResult(ref=ref, status="skipped", via="browser", detail=f"closed: page says '{m}'")
            return SubmitResult(ref=ref, status="needs_human", via="browser", detail="form did not render")
        await self._await_hydration(page)
        scope = await self._form_text(page)
        if (m := closed_text(scope)):
            return SubmitResult(ref=ref, status="skipped", via="browser", detail=f"closed: page says '{m}'")
        blk = detect_blocker(scope)
        creds = consent.active(self.s, "credentials")       # ADR-0046: without consent, behavior is unchanged
        if creds and (blk == "login" or (blk is None and await self._password_visible(page))):
            out = await self._login_wall(page, url, _email_answer(form, answers), dry_run)
            if out.status not in ("logged_in", "created"):
                return SubmitResult(ref=ref, status="roadblock", via="browser", detail=out.detail())
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector("form input, form textarea, form select, input[type=file]",
                                             state="attached", timeout=self.b.timeout_s * 1000)
            except Exception:
                return SubmitResult(ref=ref, status="needs_human", via="browser",
                                    detail=f"{out.detail()}; form did not render after sign-in")
            scope = await self._form_text(page)
            blk = detect_blocker(scope)
            if blk is None and await self._password_visible(page):
                blk = "login"
        if blk in ("captcha", "verification", "login"):
            return SubmitResult(ref=ref, status="roadblock", via="browser", detail=f"{blk} before filling")
        if challenge_label(scope):                      # comprehension check: never answered (ADR-0056)
            return SubmitResult(ref=ref, status="needs_human", via="browser",
                                detail=CHALLENGE_REASON.format(labels="on the page"))
        if (hit := stop_phrase_hit(scope, self.s.policy.stop_phrases)):
            return SubmitResult(ref=ref, status="needs_human", via="browser", detail=f"attestation: '{hit}'")

        self._api_keys[id(page)] = {"req": [f.key for f in form.fields if f.required and f.kind != FieldKind.file],
                                    "opt": [f.key for f in form.fields if not f.required],
                                    "keys": [f.key for f in form.fields], "stop": list(self.s.policy.stop_phrases),
                                    "ats": ats.name}
        steps = fill_order(form, answers, files)
        self._filled.add(id(page))                   # from here on the tab holds the tool's work (ADR-0067)
        texty = [(f, v) for f, v in steps if f.kind != FieldKind.file]
        uploads = [(f, v) for f, v in steps if f.kind == FieldKind.file]
        failed: list[str] = []
        has_phone = any(canonical_key(f.label) == "phone" or "phone" in normalize(f.label) for f, _ in texty)
        country = phone_country_name(str(answers.get("contact:country") or ""))
        if has_phone and not await self._ensure_phone_country(page, country):
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
            if "phone_country" in failed and await self._ensure_phone_country(page, country):
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
        demo_bad = await self._fill_demographics(page, answers)
        await self._fill_education(page, answers)
        await self._fill_privacy_groups(page, answers)
        empty = await self._empty_required(page)
        if empty:                                   # late re-renders (resume parse) can wipe a field: refill once
            ids = {e["id"] for e in empty}
            for f, v in texty:
                if f.key in ids:
                    await self._fill(page, ats, f, v)
            empty = await self._empty_required(page)
        empty += [{"id": "", "label": f"{lab} (self-ID read-back mismatch)"} for lab in demo_bad]
        known = {e["label"] for e in empty}
        empty += [{"id": "", "label": f"{lab} (native validation)"} for lab in await self._native_invalid(page)
                  if lab not in known]
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
        if not dry_run and final_click(self.b.final_click) == "human":   # config or per call (ADR-0060)
            return await self._human_handoff(page, ats, ref)       # ADR-0058: never clicks
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
            ev = await self._save_evidence(page, ref, "dry-run", extra={"exempt_optin": self._exempt.get(id(page), [])})
            return SubmitResult(ref=ref, status="dry_run", via="browser",
                                detail=f"filled and verified {len(steps)} fields; not submitted{ev}")
        return await self._submit(page, ats, ref, attempt=attempt)

    # -- final_click="human" (ADR-0058) ----------------------------------------------------------
    async def _human_handoff(self, page, ats: AtsProfile, ref: str) -> SubmitResult:
        """The form is filled and verified: keep the tab, bring it to the front, and let the person click Submit.
        This path NEVER clicks. A background watcher reports the outcome (see _watch_human_click)."""
        ev = await self._save_evidence(page, ref, "handoff-final-click")
        if self.b.headless:
            return SubmitResult(ref=ref, status="needs_human", via="browser",
                                detail=f"form filled and verified, not clicked (final_click=human); set "
                                       f"browser.headless=false so a person can click Submit{ev}")
        self._handoff_pages.add(id(page))
        try:
            await page.bring_to_front()
        except Exception:
            pass
        old = self._watchers.pop(ref, None)
        if old is not None and not old.done():
            old.cancel()
        self._watchers[ref] = asyncio.create_task(self._watch_human_click(page, ats, ref))
        return SubmitResult(ref=ref, status="handoff", via="browser", confirmation=page.url[:200],
                            detail=f"form filled and verified; click Submit in the open tab{ev}")

    async def _watch_human_click(self, page, ats: AtsProfile, ref: str) -> SubmitResult | None:
        """Poll the handed-off tab (browser.human_click_poll_s, up to human_click_timeout_s) with the SAME success
        detection as the agent-click path. Read-only: never clicks, types or retries. Success -> evidence
        'confirmed-after-human-click' and the engine callback; timeout, a closed tab or any error -> None (the ref stays
        a handoff for reconcile / ingest_confirmations)."""
        deadline = time.monotonic() + max(0.0, float(self.b.human_click_timeout_s))
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(max(0.05, float(self.b.human_click_poll_s)))
                if page.is_closed():
                    return None
                try:
                    text = await page.locator("body").inner_text()
                    still = await page.get_by_role("button", name=_SUBMIT_NAME).count() > 0
                except Exception:                      # navigating after the click: read again next poll
                    continue
                if decide_outcome(ats, page.url, text, still, await self._records(page)) == "submitted":
                    ev = await self._save_evidence(page, ref, "confirmed-after-human-click")
                    res = SubmitResult(ref=ref, status="submitted", via="browser", confirmation=page.url[:200],
                                       detail=f"confirmed after human click{ev}")
                    if self.on_human_confirmed is not None:
                        await self.on_human_confirmed(res)
                    return res
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("final-click watcher for %s stopped on an error; left as handoff", ref, exc_info=True)
            return None

    async def wait_human_click(self, ref: str) -> SubmitResult | None:
        """Await the ref's watcher (run_applications in final_click=human mode). None = no watcher / no confirmation."""
        t = self._watchers.get(ref)
        if t is None:
            return None
        try:
            return await asyncio.shield(t)
        except Exception:
            return None

    # -- evidence / network ---------------------------------------------------------------------
    # -- login walls (ADR-0046) ------------------------------------------------------------------
    @staticmethod
    async def _password_visible(page) -> bool:
        try:
            loc = page.locator("input[type=password]")
            return any([await loc.nth(i).is_visible() for i in range(min(await loc.count(), 5))])
        except Exception:
            return False

    async def _login_wall(self, page, url: str, email: str | None, dry_run: bool):
        """Vault login / account creation for this page's exact host. Secrets go keychain -> Locator.fill only;
        the returned outcome (and so the SubmitResult detail) never contains one."""
        from ..accounts.browser_ops import PlaywrightLoginOps
        from ..accounts.runtime import manager_for

        async def captcha() -> bool:
            st = await self._captcha_state(page)
            return bool(st["visible"] or st["unsafe"])

        start = page.url or url
        return await manager_for(self.s).handle_wall(PlaywrightLoginOps(page, captcha, expected_host=start), start,
                                                     email=email, dry_run=dry_run)

    async def ensure_login(self, url: str, email: str | None = None, *, dry_run: bool = True) -> str:
        """Open `url`; if it shows a login wall, handle it. Returns a short, secret-free status line."""
        ctx = await self._context()
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            if not await self._password_visible(page) and detect_blocker(await page.locator("body").inner_text()) != "login":
                return "no login wall"
            return (await self._login_wall(page, url, email, dry_run)).detail()
        except Exception as e:
            return f"login check failed ({type(e).__name__})"
        finally:
            if not self.b.keep_pages_open:
                await page.close()

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
                    if t and _CAPTCHA_MARKER.search(t) and not _marker_passive(t, attrs, n.get("nodeName", "")):
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
            (d / f"{tag}.json").write_text(redact(json.dumps(meta, indent=1)))   # ADR-0046 defense in depth
            home = str(Path.home())
            shown = str(d).replace(home, "~", 1) if str(d).startswith(home) else str(d)
            return f" evidence={shown}"
        except Exception:
            return ""

    # -- field operations -----------------------------------------------------------------------
    async def _await_hydration(self, page) -> bool:
        """Wait (cap browser.hydrate_wait_s) until a server-rendered form is live; input typed or clicked before
        hydration is silently dropped by React (relativity fill failures, ADR-0039). False = cap reached; the run
        continues and the read-back / required-field scan still judge the result."""
        deadline = time.monotonic() + max(0.0, self.b.hydrate_wait_s)
        while True:
            try:
                if await page.evaluate(_HYDRATED_JS):
                    return True
            except Exception:
                pass                                   # navigation in progress: poll again
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _fill_privacy_groups(self, page, answers: dict[str, Any]) -> None:
        """Required radio/checkbox groups that exist only in the DOM (not in the job API), e.g. Ashby's consent block
        under the phone field. Answered ONLY when the owner opted in (pref:acknowledge_privacy_notices) AND the group
        is a plain privacy-notice acknowledgement with exactly one affirmative option (ADR-0034 rules, options
        included). Anything else (SMS / marketing consent, terms, attestations) stays empty -> `incomplete`."""
        if answers.get("pref:acknowledge_privacy_notices") is not True:
            return
        for row in await self._empty_required(page):
            if not row.get("group"):
                continue
            # the FULL question text is judged (the 80-char label could cut off an excluded topic such as SMS)
            ff = FormField(key=row["id"] or "dom-group", label=row.get("text") or row["label"], kind=FieldKind.select,
                           options=[o for o in row.get("opts") or [] if o])
            if _CONSENT_WORDS.search(ff.label) or any(_CONSENT_WORDS.search(o) for o in ff.options):
                continue                           # consent / SMS / marketing: never ticked for a DOM-only group
            want = privacy_ack_value(ff, self.s.policy.stop_phrases)
            if want is None:
                continue
            try:
                await self._check_in_group(page.locator(f'[data-jam-group="{row["group"]}"]').first, str(want),
                                           ff.model_copy(update={"key": ""}))
            except Exception:
                pass

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
        # Ashby wraps each question in [data-field-path=<key>]; radio/checkbox members carry entry-scoped names
        # (or the option text), so the group container is the only stable handle (ADR-0039).
        fp = f'[data-field-path="{key}"]'
        own = (f'label[for="{key}"], input[name="{key}"], input[name$="_{key}"], input[id*="_{key}-labeled-"]')
        cands += [page.locator(f"{fp} :is(fieldset, [role=radiogroup], [role=group]):has({own})")]
        # Ashby location / autocomplete: an id-less input[role=combobox] inside the field entry (ADR-0056)
        cands += [page.locator(f"{fp} input[role=combobox][aria-autocomplete]")]
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
        """Radio / checkbox group sharing one name (Lever custom questions): tick the member whose option text or value
        EXACTLY matches `value`, then read the state back."""
        return await self._tick_member(page.locator(f'input[name="{f.key}"]'), page, value, f)

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
        if await self._is_group(loc):            # checkbox / radio group (Greenhouse multi_value_*, Ashby fieldsets)
            return await self._check_in_group(loc, value, f)
        if await self._is_aria_combo(loc):       # Ashby location autocomplete (ADR-0056)
            return await self._combo_commit(page, f, loc, value)
        marked = False
        try:
            marked = bool(await loc.evaluate(_MARK_SCOPE_JS))
        except Exception:
            pass
        scope = page.locator("[data-jam-scope]") if marked else page
        opts = scope.locator(_MENU_SCOPED if marked else _MENU_OPTS)
        # EEO / self-ID questions: exact option matching only, never a substring or fuzzy snap (ADR-0039)
        eeo = canonical_key(f.label) in DEMOGRAPHIC_KEYS or bool(_DEMOGRAPHIC_LABEL.search(f.label))
        match = exact_choice if eeo else best_option
        async_lookup = (f.kind == FieldKind.location or not f.options) and not eeo
        snapped = match(value, f.options) if f.options else None
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
            pick = match(value, texts) if texts else None
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
        committed = await self._committed(page, loc, pick)
        if not committed:
            await self._close_menu(page)
        return committed

    @staticmethod
    async def _is_aria_combo(loc) -> bool:
        """An ARIA autocomplete combobox that is NOT a react-select (Ashby's location field): the committed value is
        the input's own text, and the listbox is a floating element named by aria-controls."""
        try:
            return bool(loc is not None and await loc.evaluate(_ARIA_COMBO_JS))
        except Exception:
            return False

    async def _combo_options(self, page, loc, deadline: float) -> list[str]:
        """Wait (bounded) until THIS combobox's listbox shows options and their texts stop changing (the list is
        fetched per keystroke; an early read can be a stale result). [] when nothing showed in time."""
        last: list[str] | None = None
        while time.monotonic() < deadline:
            try:
                texts = list(await loc.evaluate(_COMBO_OPTIONS_JS))
            except Exception:
                texts = []
            if texts and texts == last:
                return texts
            last = texts
            await asyncio.sleep(0.25)
        return last or []

    async def _combo_commit(self, page, f: FormField, loc, value: str) -> bool:
        """Type, wait for the listbox, click the best option for `value` (location: the city + region/country the
        answer names, via pick_location; other fields: exact/best option), then verify the input SHOWS the committed
        option and the list closed. Bounded by browser.combobox_commit_s (<= 20 s). FAIL CLOSED: no acceptable
        option or no commit -> the typed text is cleared (an uncommitted value is never left behind) and False."""
        k = (id(page), f.key)
        if k in self._combo and self._combo[k] is None:      # already failed on this page: do not burn the budget twice
            return False
        deadline = time.monotonic() + min(20.0, float(self.b.combobox_commit_s))
        geo = f.kind == FieldKind.location
        for q in (location_queries(value) if geo else [value]):
            if time.monotonic() >= deadline:
                break
            try:
                await loc.click(timeout=3000)
                await loc.press("ControlOrMeta+a")
                await loc.press("Backspace")
                await loc.press_sequentially(q, delay=15)
            except Exception:
                break
            texts = await self._combo_options(page, loc, min(deadline, time.monotonic() + 6.0))
            if not texts:
                continue
            pick = pick_location(value, texts) if geo else (exact_choice(value, texts) or best_option(value, texts))
            if pick is None:
                continue
            try:                                            # a real click on the option itself (never Enter)
                lb = await loc.get_attribute("aria-controls") or await loc.get_attribute("aria-owns")
                if not lb:
                    continue
                await page.locator(f'[id="{lb}"]').locator("[role=option]:visible").nth(texts.index(pick)) \
                    .click(timeout=3000)
            except Exception:
                continue
            while time.monotonic() < deadline:
                try:
                    st = await loc.evaluate("e => [e.value || '', e.getAttribute('aria-expanded') || '']")
                except Exception:
                    break
                if normalize(st[0]) == normalize(pick) and st[1] != "true":
                    self._combo[k] = pick
                    return True
                await asyncio.sleep(0.2)
        self._combo[k] = None
        try:                                                # fail closed: never leave uncommitted text behind
            await loc.press("ControlOrMeta+a")
            await loc.press("Backspace")
        except Exception:
            pass
        await self._close_menu(page)
        return False

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
    async def _is_group(loc) -> bool:
        """A container (fieldset, role=group, Ashby field entry ...) whose answer is a ticked radio / checkbox."""
        try:
            return bool(loc is not None and await loc.evaluate(_IS_GROUP_JS))
        except Exception:
            return False

    @staticmethod
    async def _members(boxes, f: FormField | None) -> list[dict[str, Any]]:
        return list(await boxes.evaluate_all(_MEMBERS_JS, [f.key if f else "", f.label if f else ""]))

    async def _check_in_group(self, group, value: str, f: FormField | None = None) -> bool:
        return await self._tick_member(group.locator(_CHOICE), group, value, f)

    async def _tick_member(self, boxes, scope, value: str, f: FormField | None) -> bool:
        """Tick the field's member whose option text (else value attribute) EXACTLY equals `value` (exact_choice:
        normalised equality or the explicit decline table; never a substring or fuzzy match), then read it back.
        Custom-drawn boxes cover the real input, so the click falls back to the option's label."""
        rows = await self._members(boxes, f)
        pick = exact_choice(value, [r["text"] for r in rows if r["text"]])
        row = next((r for r in rows if pick is not None and r["text"] == pick), None)
        if row is None:
            nv = normalize(str(value))
            row = next((r for r in rows if r["value"] not in ("", "on") and normalize(r["value"]) == nv), None)
        if row is None:
            return False
        box = boxes.nth(row["i"])
        if await box.is_checked():
            return True
        bid = await box.get_attribute("id")
        label = scope.locator(f'label[for="{bid}"]') if bid else box.locator("xpath=ancestor::label[1]")
        for act in (lambda: box.check(timeout=2000), lambda: label.first.click(timeout=2000),
                    lambda: box.check(force=True, timeout=2000)):
            try:
                await act()
            except Exception:
                pass
            if await box.is_checked():
                return True
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
            if await self._is_group(loc):
                return not any(r["checked"] for r in await self._members(loc.locator(_CHOICE), f))
            kind = await loc.evaluate("e => (e.type || e.tagName).toLowerCase()")
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
    async def _committed(page, loc, pick: str) -> bool:
        """The choice is only real once it shows as THIS select's single-value / multi-value chip. The read-back is
        always scoped to the field's own container; no container -> not committed (never another select's chip)."""
        for _ in range(8):
            try:
                if loc is None or not await loc.evaluate(_COMMIT_SCOPE_JS):
                    return False
                root = page.locator("[data-jam-commit]")
                got = [normalize(t) for t in await root.locator(_SELECT_VALUES).all_inner_texts()]
                np = normalize(pick)
                if any(g and (g == np or g in np or np in g) for g in got):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.15)
        return False

    async def _empty_required(self, page) -> list[dict[str, Any]]:
        """Every visible required control (asterisk label / aria-required / required) that is still empty. Fields the
        API form marks required count too. A scan that fails is itself reported (never an empty, 'verified' list)."""
        try:
            rows = list(await page.evaluate(_EMPTY_REQUIRED_JS, self._api_keys.get(id(page), {})))
        except Exception as e:
            return [{"id": "", "label": f"required-field scan failed ({type(e).__name__})", "rs": False}]
        ex = [r["label"] for r in rows if r.get("exempt")]
        if ex:
            self._note_exempt(page, ex)
        return [r for r in rows if not r.get("exempt")]

    def _note_exempt(self, page, labels: list[str]) -> None:
        cur = self._exempt.setdefault(id(page), [])
        new = [x for x in labels if x not in cur]
        if new:
            cur.extend(new)
            log.info("left empty on purpose (opt-in / job-alert allowlist): %s", new)

    async def _native_invalid(self, page) -> list[str]:
        """form.checkValidity() on the application form (only when it has native constraints). Never reportValidity,
        never submit. A failing check is itself reported."""
        try:
            res = await page.evaluate(_VALIDITY_JS, {"keys": self._api_keys.get(id(page), {}).get("keys", [])})
        except Exception as e:
            return [f"native validity check failed ({type(e).__name__})"]
        if res.get("exempt"):
            self._note_exempt(page, list(res["exempt"]))
        return list(res.get("bad") or [])

    async def _fill_demographics(self, page, answers: dict[str, Any]) -> list[str]:
        """Voluntary EEO / self-ID selects that exist only in the DOM (not in the job API; planned fields are left to
        the plan). EXACT matching only (exact_choice: normalised equality or the explicit decline table; never a
        substring or fuzzy match, so Female never selects Male). The profile's [demographics] value is used when an
        option equals it; a profile value with no equal option leaves the select BLANK (a required one then makes the
        run `incomplete`). With no profile value: a REQUIRED select gets the decline-table option, an OPTIONAL one
        stays blank. Every pick is read back from this field and compared EXACTLY; on a mismatch the select is
        blanked, and a required one (or one that cannot be blanked) is returned as a problem -> `incomplete`."""
        bad: list[str] = []
        try:
            rows = list(await page.evaluate(_DOM_SELECTS_JS, self._api_keys.get(id(page), {})))
        except Exception:
            return bad
        for row in rows:
            lab, el_id = row["label"], row["id"]
            ck = canonical_key(lab)
            if not el_id or el_id in answers or not (ck in DEMOGRAPHIC_KEYS or _DEMOGRAPHIC_LABEL.search(lab)):
                continue
            prof = str(answers.get(f"demographic:{ck}") or "").strip() if ck in DEMOGRAPHIC_KEYS else ""
            if not prof and not row["required"]:
                continue                                   # optional, nothing in the profile: stay blank
            declined = not prof or normalize(prof) in _DECLINE_SYNONYMS

            def choose(texts: list[str], prof=prof, declined=declined) -> str | None:
                texts = [t for t in texts if t and normalize(t) not in ("select", "")]
                return exact_decline(texts) if declined else exact_choice(prof, texts)
            loc = page.locator(f'[id="{el_id}"]').first
            try:
                await loc.scroll_into_view_if_needed()
                if row["native"]:
                    texts = [t.strip() for t in await loc.locator("option").all_inner_texts()]
                    pick = choose(texts)
                    if pick is None:
                        continue
                    await loc.select_option(label=pick, timeout=2000)
                    got = await loc.evaluate("e => e.selectedOptions[0] ? e.selectedOptions[0].text : ''")
                    if normalize(got) != normalize(pick):              # exact read-back failed: blank it
                        try:
                            await loc.select_option(value="", timeout=2000)
                        except Exception:
                            pass
                        blank = not (await loc.evaluate("e => e.value || ''"))
                        if row["required"] or not blank:
                            bad.append(lab)
                    continue
                marked = bool(await loc.evaluate(_MARK_SCOPE_JS))
                opts = (page.locator("[data-jam-scope]") if marked else page).locator(_MENU_SCOPED)
                await loc.click()
                await opts.first.wait_for(state="visible", timeout=2500)
                texts = [t.strip() for t in await opts.all_inner_texts()]
                pick = choose(texts)
                if pick is None:
                    await self._close_menu(page)
                    continue
                await opts.nth(texts.index(pick)).click(timeout=3000)
                await self._close_menu(page)
                shown: list[str] = []
                for _ in range(8):                                     # exact read-back from THIS select only
                    if await loc.evaluate(_COMMIT_SCOPE_JS):
                        shown = [t.strip() for t in await page.locator("[data-jam-commit]").locator(_SELECT_VALUES)
                                 .all_inner_texts()]
                    if shown:
                        break
                    await asyncio.sleep(0.15)
                if [normalize(t) for t in shown] != [normalize(pick)]:
                    clear = page.locator("[data-jam-commit] [class*=select__clear-indicator]")
                    try:
                        if await clear.count():
                            await clear.first.click(timeout=2000)
                    except Exception:
                        pass
                    left = await page.locator("[data-jam-commit]").locator(_SELECT_VALUES).count()
                    if row["required"] or left:
                        bad.append(lab)
            except Exception:
                await self._close_menu(page)
                if row["required"]:
                    bad.append(lab)
        return bad

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
                if await self._committed(page, loc, pick):
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

    async def _ensure_phone_country(self, page, country: str) -> bool:
        """Intl-tel widgets reset the number when the country changes: pick the PROFILE's country BEFORE typing the
        phone. No configured country -> the widget is left as the site set it (a required empty one is reported by
        the required-field scan); the tool never guesses a country (ADR-0039)."""
        if not country:
            return True
        try:
            cid = await page.evaluate(_COUNTRY_JS)
        except Exception:
            cid = ""
        if cid:
            loc = page.locator(f'[id="{cid}"]').first

            async def current() -> list[str]:
                try:
                    if await loc.evaluate(_COMMIT_SCOPE_JS):
                        return list(await page.locator("[data-jam-commit]").locator(_SELECT_VALUES).all_inner_texts())
                except Exception:
                    pass
                return []
            if any(same_country(country, t) for t in await current()):    # exact, never a substring
                return True
            for _ in range(2):
                if await self._rs_pick(page, cid, [country], lambda t: country_option(country, t)):
                    return True
                await asyncio.sleep(0.5)
            return False
        flag = page.locator(".iti__selected-country, .iti__selected-flag, .iti__flag-container button").first
        try:
            if not await flag.count():
                return True                     # no country widget on this form
            if same_country(country, (await flag.get_attribute("title")) or ""):     # exact, never a substring
                return True
            await flag.click()
            items = page.locator("li.iti__country, .iti__country-list [role=option]")
            await items.first.wait_for(state="visible", timeout=3000)
            names = list(await items.evaluate_all(
                "els => els.map(e => ((e.querySelector('.iti__country-name') || e).textContent || '').trim())"))
            pick = country_option(country, names)
            if pick is None:
                await self._close_menu(page)
                return False
            await items.nth(names.index(pick)).click()
            return same_country(country, (await flag.get_attribute("title")) or "")
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
                # Ashby keeps a HIDDEN checkbox behind its Yes/No buttons: clicking it waited the full default
                # timeout (30 s per question) before falling through to the buttons (ADR-0056)
                if typ == "checkbox" and await loc.is_visible():
                    if await loc.is_checked() != want:
                        await loc.click(timeout=3000)      # real click: programmatic check does not register
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
                if await self._is_aria_combo(loc):      # committed option text, exactly (ADR-0056)
                    pick = self._combo.get((id(page), f.key))
                    try:
                        got = normalize(await loc.input_value())
                    except Exception:
                        got = ""
                    if not pick or got != normalize(pick):
                        bad.append(f.key)
                    continue
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
                    want = [normalize(str((exact_choice(o, f.options) if f.options else None) or o))
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
                    if await self._is_group(loc):
                        rows = [r for r in await self._members(loc.locator(_CHOICE), f) if r["checked"]]
                        ticked = [r["text"] for r in rows if r["text"]]
                        vals = {normalize(r["value"]) for r in rows if r["value"] not in ("", "on")}
                        # every wanted option must be ticked (exact); an empty group is never "verified"
                        if not rows or any(exact_choice(one, ticked) is None and normalize(str(one)) not in vals
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
