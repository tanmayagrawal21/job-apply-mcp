"""Ashby: public posting API for discovery; application form via the job board's non-user GraphQL query.
There is no public submission API, so submit() always returns needs_browser."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.models import ApplicationForm, FieldKind, FormField, Posting, SubmitResult
from ..core.registry import platforms
from .base import AdapterError, BaseAdapter, InvalidRef, PostingClosed, iso_date, not_before
from .http import HttpError

BOARD_API = "https://api.ashbyhq.com/posting-api/job-board"
GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
_BOARD_PARAMS = {"includeCompensation": "true"}   # one fixed value: the cache key never varies by call site
_URL = re.compile(r"ashbyhq\.com/([^/?#]+)(?:/([0-9a-f]{8}-[0-9a-f-]{27}))?", re.IGNORECASE)
_QUERY = ("query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { "
          "jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) "
          "{ applicationForm { sections { fieldEntries { ... on FormFieldEntry { isRequired field } } } } } }")
_KINDS = {"string": FieldKind.text, "email": FieldKind.text, "phone": FieldKind.text, "longtext": FieldKind.textarea,
          "file": FieldKind.file, "boolean": FieldKind.boolean, "valueselect": FieldKind.select,
          "multivalueselect": FieldKind.multiselect, "number": FieldKind.number, "date": FieldKind.date,
          "location": FieldKind.location, "sociallink": FieldKind.text}


@platforms.register("ashby")
class AshbyAdapter(BaseAdapter):
    name = "ashby"
    hosts = ("ashbyhq.com",)

    def _parse_url(self, url: str) -> tuple[str, str | None]:
        m = _URL.search(url)
        if not m or m.group(1) in ("posting-api", "api"):
            m = re.search(r"job-board/([^/?#]+)", url)
            if not m:
                raise InvalidRef(f"not an Ashby URL: {url}")
            return m.group(1), None
        return m.group(1), m.group(2)

    def _to_posting(self, board: str, j: dict, *, with_description: bool) -> Posting:
        comp = j.get("compensation") or {}
        pay = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
        url = j.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{j['id']}"
        return self.finish(
            j.get("descriptionPlain") or "", with_description=with_description, api_salary=pay,
            ref=self.ref(board, j["id"]), company=board, title=j.get("title", ""), location=j.get("location") or "",
                 url=url, apply_url=j.get("applyUrl") or url + "/application",
                 posted_at=self._posted(j),
                 countries=self._countries(j), locations=self._locations(j))

    @staticmethod
    def _posted(j: dict) -> str | None:
        return iso_date(j.get("publishedAt"))

    @staticmethod
    def _listed(d: Any) -> list[dict]:
        return [j for j in (d.get("jobs", []) if isinstance(d, dict) else []) if j.get("isListed", True)]

    def _countries(self, j: dict) -> list[str]:
        """addressCountry of the primary and every secondary location (ADR-0040). Every entry must parse: a non-dict,
        malformed or country-less entry is "UNCONFIRMED", never skipped."""
        raw = j.get("secondaryLocations") or []
        entries = [j] + (raw if isinstance(raw, list) else [None])
        out: list[str] = []
        for e in entries:
            addr = e.get("address") if isinstance(e, dict) else None
            post = addr.get("postalAddress") if isinstance(addr, dict) else None
            c = self.country_code(post.get("addressCountry")) if isinstance(post, dict) else None
            out.append(c or "UNCONFIRMED")
        return sorted(set(out))

    @staticmethod
    def _locations(j: dict) -> list[str]:
        raw = j.get("secondaryLocations") or []
        return [str(s.get("location")) for s in (raw if isinstance(raw, list) else [])
                if isinstance(s, dict) and str(s.get("location") or "").strip()]

    async def _board(self, board: str) -> list[dict]:
        try:
            d = await self.http.get_json(f"{BOARD_API}/{board}", params=_BOARD_PARAMS)
        except HttpError as e:
            raise AdapterError("board not found" if e.status == 404 else str(e)) from e
        except httpx.HTTPError as e:   # timeout / transport error after the client's retries (ADR-0074)
            raise AdapterError(f"tool_gap: could not read the Ashby board ({type(e).__name__})") from e
        return self._listed(d)

    async def list_postings(self, board: str, *, since: str | None = None, query: str = "",
                            with_description: bool = False) -> AsyncIterator[Posting]:
        def derive(d: Any) -> list[Posting]:
            return [self._to_posting(board, j, with_description=with_description) for j in self._listed(d)
                    if not_before(self._posted(j), since)]   # date filter first: text flags are the costly part

        try:   # same URL + params as _board, so get_posting reuses the cached response (ADR-0043)
            postings = await self.http.get_derived(f"{BOARD_API}/{board}", derive, params=_BOARD_PARAMS,
                                                   tag=f"{self.name}:{with_description}:{since}")
        except HttpError as e:
            raise AdapterError("board not found" if e.status == 404 else str(e)) from e
        for p in postings:
            yield p

    async def get_posting(self, url_or_ref: str, *, with_description: bool = False) -> Posting:
        board, jid = self._need_job(url_or_ref)
        for j in await self._board(board):
            if j.get("id") == jid:
                return self._to_posting(board, j, with_description=with_description)
        raise PostingClosed("job not found (closed or unlisted)")

    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        board, jid = self._need_job(url_or_ref)
        body = {"operationName": "ApiJobPosting", "query": _QUERY,
                "variables": {"organizationHostedJobsPageName": board, "jobPostingId": jid}}
        try:
            d = await self.http.post_json(GRAPHQL, params={"op": "ApiJobPosting"}, json_body=body,
                                          headers={"content-type": "application/json"})
        except HttpError as e:
            raise AdapterError(f"tool_gap: could not read the Ashby form ({e})") from e
        except httpx.HTTPError as e:   # timeout / transport error after the client's retries (ADR-0074): fail closed
            raise AdapterError(f"tool_gap: could not read the Ashby form ({type(e).__name__})") from e
        if isinstance(d, dict) and not d.get("errors") and isinstance(d.get("data"), dict) \
                and d["data"].get("jobPosting", 0) is None:      # the API answered: no such open posting
            raise PostingClosed("Ashby returned no posting (closed or unlisted)")
        if not isinstance(d, dict) or d.get("errors") or not (d.get("data") or {}).get("jobPosting"):
            raise AdapterError("tool_gap: Ashby form query returned no posting (closed, or the query shape changed)")
        fields: list[FormField] = []
        for sec in d["data"]["jobPosting"]["applicationForm"]["sections"]:
            for e in sec.get("fieldEntries") or []:
                f = e.get("field") or {}
                if not f:
                    continue
                opts = [str(v.get("label", v)) if isinstance(v, dict) else str(v) for v in f.get("selectableValues") or []]
                fields.append(FormField(key=str(f.get("path") or f.get("title")), label=str(f.get("title", "")),
                                        kind=_KINDS.get(str(f.get("type", "")).lower(), FieldKind.unknown),
                                        required=bool(e.get("isRequired")), options=opts))
        return ApplicationForm(ref=self.ref(board, jid), fields=fields, submit_via=["browser"])

    async def submit(self, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str],
                     *, dry_run: bool = True) -> SubmitResult:
        return self.needs_browser(form, "Ashby has no public application API")
