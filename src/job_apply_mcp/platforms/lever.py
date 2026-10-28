"""Lever: public Postings API (api.lever.co/v0). Submission through the apply endpoint needs an API key."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from ..core.models import ApplicationForm, FieldKind, Posting, SubmitResult
from ..core.registry import platforms
from .base import AdapterError, BaseAdapter, PostingClosed, iso_date, not_before
from .http import MAX_TEXT_BYTES, HttpError
from .lever_cards import parse_cards
from .textflags import html_to_text

APPLY_PAGE_MAX_BYTES = MAX_TEXT_BYTES   # the apply page is read up to this cap; a longer page fails get_form

API = "https://api.lever.co/v0/postings"
_URL = re.compile(r"lever\.co/([^/?#]+)(?:/([0-9a-f]{8}-[0-9a-f-]{27}))?", re.IGNORECASE)


def _plain(d: dict) -> str:
    lists = " ".join(f"{l.get('text', '')}: {html_to_text(l.get('content', ''))}" for l in d.get("lists") or [])
    return " ".join(x for x in (d.get("descriptionPlain"), lists, d.get("additionalPlain")) if x)


@platforms.register("lever")
class LeverAdapter(BaseAdapter):
    name = "lever"
    hosts = ("lever.co",)

    def _parse_url(self, url: str) -> tuple[str, str | None]:
        host = urlparse(url).netloc
        m = _URL.search(url)
        if not m or not host.startswith(("jobs.", "api.")):
            raise AdapterError(f"not a Lever URL: {url}")
        board = m.group(1)
        if board in ("v0", "postings"):  # api.lever.co/v0/postings/<co>/<id>
            m2 = re.search(r"/postings/([^/?#]+)(?:/([0-9a-f-]{36}))?", url)
            if not m2:
                raise AdapterError(f"not a Lever URL: {url}")
            return m2.group(1), m2.group(2)
        return board, m.group(2)

    def _to_posting(self, board: str, d: dict, *, with_description: bool) -> Posting:
        cat = d.get("categories") or {}
        sr = d.get("salaryRange")
        pay = None
        if sr and sr.get("min") and sr.get("max"):
            cur = {"USD": "$"}.get(sr.get("currency", "USD"), sr.get("currency", "") + " ")
            unit = "/hr" if "hour" in str(sr.get("interval", "")) else ""
            pay = f"{cur}{sr['min']:,.0f}-{cur}{sr['max']:,.0f}{unit}"
        return self.finish(
            _plain(d), with_description=with_description, api_salary=pay,
            ref=self.ref(board, d["id"]), company=board, title=d.get("text", ""),
                 location=cat.get("location") or "", url=d.get("hostedUrl") or f"https://jobs.lever.co/{board}/{d['id']}",
                 apply_url=d.get("applyUrl") or f"https://jobs.lever.co/{board}/{d['id']}/apply",
                 posted_at=self._posted(d),
                 countries=self._countries(d, cat), locations=self._locations(cat))

    @staticmethod
    def _posted(d: dict) -> str | None:
        return iso_date(d.get("createdAt"))

    def _countries(self, d: dict, cat: dict) -> list[str]:
        """Lever's `country` covers the primary location only; any other location makes the set unconfirmed, so
        job_country() is None (deny-by-default, ADR-0040)."""
        c = self.country_code(d.get("country"))
        raw = cat.get("allLocations") or []
        if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
            return ["UNCONFIRMED"]                       # malformed entry: never confirms
        primary = str(cat.get("location") or "").strip().lower()
        others = {x.strip().lower() for x in raw if x.strip()} - {primary}
        if others or (raw and not primary):              # more than one location: `country` covers the primary only
            return [c, "UNCONFIRMED"] if c else ["UNCONFIRMED"]
        return [c] if c else []

    @staticmethod
    def _locations(cat: dict) -> list[str]:
        raw = cat.get("allLocations") or []
        return [x for x in raw if isinstance(x, str) and x.strip()] if isinstance(raw, list) else []

    async def list_postings(self, board: str, *, since: str | None = None, query: str = "",
                            with_description: bool = False) -> AsyncIterator[Posting]:
        def derive(data: Any) -> list[Posting]:
            return [self._to_posting(board, d, with_description=with_description)
                    for d in (data if isinstance(data, list) else []) if not_before(self._posted(d), since)]

        try:   # finished postings are memoized per response digest (ADR-0043)
            postings = await self.http.get_derived(f"{API}/{board}", derive, params={"mode": "json"},
                                                   tag=f"{self.name}:{with_description}:{since}")
        except HttpError as e:
            raise AdapterError("posting not found (closed or moved)" if e.status == 404 else str(e)) from e
        for p in postings:
            yield p

    async def get_posting(self, url_or_ref: str, *, with_description: bool = False) -> Posting:
        board, pid = self._need_job(url_or_ref)
        return self._to_posting(board, await self._get(f"{API}/{board}/{pid}"), with_description=with_description)

    async def _get(self, url: str, params: dict | None = None) -> Any:
        try:
            return await self.http.get_json(url, params=params)
        except HttpError as e:
            if e.status == 404:
                raise PostingClosed("posting not found (closed or moved)") from e
            raise AdapterError(str(e)) from e

    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        """Lever exposes no public schema; this is the standard apply payload. Custom questions are only
        visible on the hosted apply page, so `submit_via` lists the browser as the complete path."""
        board, pid = self._need_job(url_or_ref)
        F = FieldKind
        fields = self.std_fields(("name", "Full name", F.text, True), ("email", "Email", F.text, True),
                                 ("phone", "Phone", F.text, False), ("org", "Current company", F.text, False),
                                 ("urls[LinkedIn]", "LinkedIn", F.text, False), ("urls[GitHub]", "GitHub", F.text, False),
                                 ("comments", "Additional information", F.textarea, False),
                                 ("resume", "Resume", F.file, True))
        html, truncated = await self._apply_page(board, pid)
        if truncated:
            # ADR-0072: cards past the text cap would vanish silently and an API submit would omit a required
            # answer; fail closed rather than report a partial form as complete
            raise AdapterError(f"tool_gap: the Lever apply page is larger than the {APPLY_PAGE_MAX_BYTES} byte text "
                               "cap, so custom questions may be missing; the form is not reported")
        if html:                       # optional standard inputs this posting's page does not render are dropped
            fields = [f for f in fields if f.required or f'name="{f.key}"' in html]
        fields += [f for f in parse_cards(html) if f.key not in {x.key for x in fields}]
        return ApplicationForm(ref=self.ref(board, pid), fields=fields,
                               submit_via=["api", "browser"] if self.api_key(board) else ["browser"])

    async def _apply_page(self, board: str, pid: str) -> tuple[str, bool]:
        """(HTML, truncated) of the hosted apply page (custom-question cards, ADR-0031). A page cut at the text cap
        is reported (ADR-0072). A failed fetch fails closed (ADR-0074): a 404 means the posting is gone
        (PostingClosed); any other HTTP error after the client's bounded retries, a timeout or any other exception
        is a tool_gap AdapterError, because the custom questions (some may be required) could not be read."""
        url = f"https://jobs.lever.co/{board}/{pid}/apply"
        try:
            return await self.http.get_text_capped(url, max_bytes=APPLY_PAGE_MAX_BYTES)
        except HttpError as e:
            if e.status in (404, 410):
                raise PostingClosed("apply page not found (closed or moved)") from e
            why = f"HTTP {e.status}"
        except Exception as e:  # noqa: BLE001 - transport errors, timeouts, decoding: never a complete form
            why = type(e).__name__
        raise AdapterError(f"tool_gap: could not read the Lever apply page ({why}), so its custom questions are "
                           "unknown; the form is not reported")

    async def submit(self, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str],
                     *, dry_run: bool = True) -> SubmitResult:
        board, pid = self._need_job(form.ref)
        key = self.api_key(board)
        if not key:
            return self.needs_browser(form, "no Lever API key configured for this board")
        if missing := self.missing_required(form, answers, files):
            return SubmitResult(ref=form.ref, status="needs_human", via="api", detail=f"missing required: {missing}")
        if dry_run:
            return SubmitResult(ref=form.ref, status="dry_run", via="api",
                                detail=f"would POST {len(answers)} fields, {len(files)} files")
        try:
            resp = await self.http.post_form(f"{API}/{board}/{pid}", params={"key": key},
                                             data={k: str(v) for k, v in answers.items()}, files=self.read_files(files))
        except HttpError as e:
            return SubmitResult(ref=form.ref, status="error", via="api", detail=f"{e}: {e.body[:200]}")
        except OSError as e:
            return SubmitResult(ref=form.ref, status="error", via="api", detail=f"file error: {e}")
        conf = resp.get("applicationId") if isinstance(resp, dict) else None
        return SubmitResult(ref=form.ref, status="submitted", via="api", confirmation=str(conf or resp)[:200])
