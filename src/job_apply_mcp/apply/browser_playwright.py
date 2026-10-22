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
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from ..core import registry
from ..core.models import ApplicationForm, FieldKind, FormField, SubmitResult
from .mapper import best_option, canonical_key, normalize

# ------------------------------------------------------------------ pure helpers (unit-tested offline)


@dataclass(frozen=True)
class AtsProfile:
    name: str
    typing: str = "fill"                      # "fill" | "keys" (React state ignores programmatic setters)
    confirm_urls: tuple[str, ...] = ()
    confirm_text: tuple[str, ...] = ("thank you for applying", "application submitted", "submitted successfully",
                                     "thanks for applying", "your application has been")
    upload_settle_extra_s: float = 0.0


ATS: dict[str, AtsProfile] = {
    "greenhouse": AtsProfile("greenhouse", confirm_urls=("/confirmation",),
                             confirm_text=("thank you for applying", "thank you for your interest")),
    "lever": AtsProfile("lever", confirm_urls=("/thanks",), confirm_text=("application submitted",),
                        upload_settle_extra_s=3.0),  # resume parse re-renders the form
    "ashby": AtsProfile("ashby", typing="keys", confirm_urls=("/success", "/confirmation"),
                        confirm_text=("application submitted", "thanks for applying", "successfully submitted")),
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
    return normalize(str(want)) == normalize(got)


# ------------------------------------------------------------------ Playwright layer (live-tested only)

_MENU_OPTS = "[class*=select__menu] [role=option], [role=listbox] [role=option], [class*=option]"
_SELECT_VALUES = "[class*=select__single-value], [class*=select__multi-value__label]"
_SUBMIT_NAME = re.compile(r"^\s*(submit( application)?|send application|apply( now)?)\s*$", re.IGNORECASE)


class PlaywrightDriver:
    name = "playwright"

    def __init__(self, settings: Settings):
        self.s = settings
        self.b = settings.browser
        self._pw: Any = None
        self._ctx: Any = None
        self._lock = asyncio.Lock()

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
        try:
            return await self._run(page, ats, greenhouse_embed_url(apply_url, ref) if ats_name == "greenhouse"
                                   else apply_url, form, answers, files, dry_run)
        except Exception as e:
            return SubmitResult(ref=ref, status="error", via="browser", detail=f"{type(e).__name__}: {e}"[:200])
        finally:
            if not self.b.keep_pages_open:
                await page.close()

    async def _run(self, page, ats: AtsProfile, url: str, form: ApplicationForm, answers: dict[str, Any],
                   files: dict[str, str], dry_run: bool) -> SubmitResult:
        ref = form.ref
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("form input, form textarea, form select, input[type=file]",
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
        for f, v in texty:
            if not await self._fill(page, ats, f, v):
                failed.append(f.key)
        for f, v in uploads:                       # resume LAST
            if not await self._upload(page, f, v):
                failed.append(f.key)
        if uploads:
            await self._settle_upload(page, ats)
        bad = await self._verify(page, texty)
        if bad:                                     # uploads/parsers can wipe fields: one refill pass
            for f, v in texty:
                if f.key in bad:
                    await self._fill(page, ats, f, v)
            bad = await self._verify(page, [(f, v) for f, v in texty if f.key in bad])
        problems = sorted(set(failed) | set(bad))
        if problems:
            return SubmitResult(ref=ref, status="needs_human", via="browser",
                                detail=f"could not fill/verify: {problems[:8]}")
        if dry_run:
            return SubmitResult(ref=ref, status="dry_run", via="browser",
                                detail=f"filled and verified {len(steps)} fields; not submitted")
        return await self._submit(page, ats, ref)

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
                if await c.count():
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
            if f.kind in (FieldKind.select, FieldKind.multiselect, FieldKind.location) or f.options:
                vals = value if isinstance(value, list) else [value]
                ok = True
                for v in vals:
                    ok &= await self._choose(page, f, loc, str(v))
                return ok
            text = str(value)
            if ats.typing == "keys":
                await loc.click()
                await loc.press("ControlOrMeta+a")
                await loc.press("Backspace")
                await loc.press_sequentially(text, delay=5)
            else:
                await loc.fill(text)
            return True
        except Exception:
            return False

    async def _choose(self, page, f: FormField, loc, value: str) -> bool:
        tag = (await loc.evaluate("e => e.tagName")).upper()
        if tag == "SELECT":
            for kw in ({"label": value}, {"value": value}):
                try:
                    await loc.select_option(**kw, timeout=2000)
                    return True
                except Exception:
                    continue
            return False
        await loc.click()
        want = value
        async_lookup = f.kind == FieldKind.location or not f.options
        if async_lookup:   # geocoded / searchable lists: type then wait for options
            await loc.press_sequentially(value, delay=15)
        opts = page.locator(_MENU_OPTS)
        try:
            await opts.first.wait_for(state="visible", timeout=3500 if async_lookup else 1500)
        except Exception:
            if not f.options:
                return False
            await loc.press_sequentially(value[:6], delay=15)   # filter a long fixed list
            try:
                await opts.first.wait_for(state="visible", timeout=1500)
            except Exception:
                return False
        texts = [t.strip() for t in await opts.all_inner_texts()]
        pick = best_option(want, texts) if texts else None
        if pick is None and async_lookup and texts:
            pick = texts[0]
        if pick is None:
            await page.keyboard.press("Escape")
            return False
        await opts.get_by_text(pick, exact=True).first.click()
        if f.kind == FieldKind.multiselect:
            await page.keyboard.press("Escape")
        return True

    async def _set_boolean(self, page, f: FormField, loc, want: bool) -> bool:
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
                await btn.first.click()
                return True
        return False

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

    async def _verify(self, page, rows: list[tuple[FormField, Any]]) -> list[str]:
        bad: list[str] = []
        shown = " | ".join(normalize(t) for t in await page.locator(_SELECT_VALUES).all_inner_texts())
        for f, v in rows:
            if f.kind == FieldKind.boolean:
                continue
            if f.kind in (FieldKind.select, FieldKind.multiselect, FieldKind.location) or f.options:
                loc = await self._locate(page, f)
                try:
                    if loc is not None and (await loc.evaluate("e => e.tagName")).upper() == "SELECT":
                        got = await loc.evaluate("e => e.selectedOptions[0] ? e.selectedOptions[0].text : ''")
                        if not got or (f.options and not best_option(v if isinstance(v, str) else v[0], [got])):
                            bad.append(f.key)
                        continue
                except Exception:
                    bad.append(f.key)
                    continue
                for one in (v if isinstance(v, list) else [v]):
                    want = normalize(str(one))
                    if f.kind == FieldKind.location:
                        want = want.split(" ")[0]
                    if want not in shown:
                        bad.append(f.key)
                        break
                continue
            loc = await self._locate(page, f)
            try:
                got = await loc.input_value() if loc is not None else ""
            except Exception:
                got = ""
            if not same_value(f.kind, f.label, v, got):
                bad.append(f.key)
        return bad

    # -- submit ---------------------------------------------------------------------------------
    async def _submit(self, page, ats: AtsProfile, ref: str) -> SubmitResult:
        btn = page.get_by_role("button", name=_SUBMIT_NAME)
        if not await btn.count():
            btn = page.locator("button[type=submit], input[type=submit]")
        vis = [i for i in range(await btn.count()) if await btn.nth(i).is_visible()]
        if len(vis) != 1:
            return SubmitResult(ref=ref, status="needs_human", via="browser",
                                detail=f"{len(vis)} submit buttons found; refusing to guess")
        if (blk := detect_blocker(await page.locator("body").inner_text())) in ("captcha", "verification"):
            return SubmitResult(ref=ref, status="roadblock", via="browser", detail=f"{blk} challenge visible")
        target = btn.nth(vis[0])
        await target.scroll_into_view_if_needed()
        await target.click()                                   # the ONE submit click; never repeated
        waited, step = 0.0, 0.5
        while waited <= self.b.confirmation_wait_s:
            await asyncio.sleep(step)
            waited += step
            text = await page.locator("body").inner_text()
            still = await page.get_by_role("button", name=_SUBMIT_NAME).count() > 0
            if detect_confirmation(ats, page.url, text, still):
                return SubmitResult(ref=ref, status="submitted", via="browser", confirmation=page.url[:200])
            blk = detect_blocker(text)
            if blk == "duplicate":
                return SubmitResult(ref=ref, status="duplicate", via="browser", detail="site reports a prior application")
            if blk in ("captcha", "verification"):
                return SubmitResult(ref=ref, status="roadblock", via="browser", detail=f"{blk} after submit")
        return SubmitResult(ref=ref, status="needs_human", via="browser",
                            detail="submit clicked but no confirmation detected; do NOT retry, check manually")


@registry.browser_drivers.register("playwright")
def _make(settings: Settings, **_: Any) -> PlaywrightDriver:
    return PlaywrightDriver(settings)
