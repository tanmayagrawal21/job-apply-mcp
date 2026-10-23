"""Greenhouse: public Job Board API (boards-api.greenhouse.io/v1). Submission needs a per-board API key."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core.models import ApplicationForm, FieldKind, FormField, Posting, SubmitResult
from ..core.registry import platforms
from .base import AdapterError, BaseAdapter, iso_date, not_before
from .http import HttpError
from .textflags import html_to_text

API = "https://boards-api.greenhouse.io/v1/boards"
_URL = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)/jobs/(\d+)")
_KINDS = {"input_text": FieldKind.text, "textarea": FieldKind.textarea, "input_file": FieldKind.file,
          "multi_value_single_select": FieldKind.select, "multi_value_multi_select": FieldKind.multiselect,
          "input_hidden": FieldKind.unknown}


@platforms.register("greenhouse")
class GreenhouseAdapter(BaseAdapter):
    name = "greenhouse"
    hosts = ("greenhouse.io",)

    def matches(self, url: str) -> bool:
        return super().matches(url) or "gh_jid=" in url

    def _parse_url(self, url: str) -> tuple[str, str | None]:
        p = urlparse(url)
        q = parse_qs(p.query)
        if m := _URL.search(url):
            return m.group(1), m.group(2)
        if "for" in q:  # embed/job_app?for=<board>&token=<id>
            return q["for"][0], (q.get("token") or [None])[0]
        if "greenhouse.io" in p.netloc:
            parts = [x for x in p.path.split("/") if x]
            if parts:
                return parts[0], None
        if "gh_jid" in q:  # company career page embedding Greenhouse: best-effort board guess from the host
            labels = p.netloc.split(".")
            return (labels[-2] if len(labels) >= 2 else labels[0]), q["gh_jid"][0]
        raise AdapterError(f"not a Greenhouse URL: {url}")

    # ------------------------------------------------------------------ discovery
    def _to_posting(self, board: str, j: dict, *, with_description: bool) -> Posting:
        jid = str(j["id"])
        text = html_to_text(j.get("content"))
        return self.finish(
            text, with_description=with_description,
            ref=self.ref(board, jid), company=j.get("company_name") or board, title=j.get("title", ""),
                 location=(j.get("location") or {}).get("name", ""),
                 url=j.get("absolute_url") or f"https://job-boards.greenhouse.io/{board}/jobs/{jid}",
                 apply_url=f"https://job-boards.greenhouse.io/{board}/jobs/{jid}",
                 posted_at=self._posted(j),
                 countries=self._countries(j), locations=self._locations(j))

    @staticmethod
    def _posted(j: dict) -> str | None:
        return iso_date(j.get("updated_at") or j.get("first_published"))

    def _countries(self, j: dict) -> list[str]:
        """"US" per office only when its location ends in an explicit United States name (ADR-0040); any non-dict,
        malformed or other office is "UNCONFIRMED". No offices -> [] (not confirmed; there is no text fallback)."""
        raw = j.get("offices") or []
        if not isinstance(raw, list):
            return ["UNCONFIRMED"]
        out: list[str] = []
        for o in raw:
            loc = o.get("location") if isinstance(o, dict) else None
            tail = loc.rsplit(",", 1)[-1] if isinstance(loc, str) else ""
            out.append("US" if self.country_code(tail) == "US" else "UNCONFIRMED")
        return sorted(set(out))

    @staticmethod
    def _locations(j: dict) -> list[str]:
        raw = j.get("offices") or []
        return [str(o.get("location")) for o in (raw if isinstance(raw, list) else [])
                if isinstance(o, dict) and str(o.get("location") or "").strip()]

    async def list_postings(self, board: str, *, since: str | None = None, query: str = "",
                            with_description: bool = False) -> AsyncIterator[Posting]:
        params = {"content": "true"} if (self.cfg.list_with_descriptions or with_description) else None

        def derive(data: Any) -> list[Posting]:
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            return [self._to_posting(board, j, with_description=with_description) for j in jobs
                    if not_before(self._posted(j), since)]   # date filter first: text flags are the costly part

        try:   # finished postings are memoized per response digest (ADR-0043)
            postings = await self.http.get_derived(f"{API}/{board}/jobs", derive, params=params,
                                                   tag=f"{self.name}:{with_description}:{since}")
        except HttpError as e:
            raise AdapterError("job not found (closed or moved)" if e.status == 404 else str(e)) from e
        for p in postings:
            yield p

    async def get_posting(self, url_or_ref: str, *, with_description: bool = False) -> Posting:
        board, jid = self._need_job(url_or_ref)
        return self._to_posting(board, await self._get(f"{API}/{board}/jobs/{jid}"), with_description=with_description)

    async def _get(self, url: str, params: dict | None = None) -> Any:
        try:
            return await self.http.get_json(url, params=params)
        except HttpError as e:
            raise AdapterError("job not found (closed or moved)" if e.status == 404 else str(e)) from e

    async def _questions(self, board: str, jid: str) -> dict:
        return await self._get(f"{API}/{board}/jobs/{jid}", params={"questions": "true"})

    # ------------------------------------------------------------------ form
    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        board, jid = self._need_job(url_or_ref)
        d = await self._questions(board, jid)
        fields: list[FormField] = []
        for q in (d.get("questions") or []) + (d.get("location_questions") or []):
            fs = q.get("fields") or []
            for i, f in enumerate(fs):
                kind = _KINDS.get(f.get("type"), FieldKind.unknown)
                opts = [v.get("label", "") for v in f.get("values") or []]
                if kind == FieldKind.select and sorted(o.lower() for o in opts) == ["no", "yes"]:
                    kind = FieldKind.boolean
                # a question with several inputs (e.g. resume file + pasted text) is satisfied by the first
                fields.append(FormField(key=f["name"], label=q.get("label", f["name"]), kind=kind,
                                        required=bool(q.get("required")) and i == 0, options=opts))
        via = ["api", "browser"] if self.api_key(board) else ["browser"]
        return ApplicationForm(ref=self.ref(board, jid), fields=fields, submit_via=via)

    # ------------------------------------------------------------------ submit
    async def submit(self, form: ApplicationForm, answers: dict[str, Any], files: dict[str, str],
                     *, dry_run: bool = True) -> SubmitResult:
        board, jid = self._need_job(form.ref)
        key = self.api_key(board)
        if not key:
            return self.needs_browser(form, "no Job Board API key configured for this board")
        if missing := self.missing_required(form, answers, files):
            return SubmitResult(ref=form.ref, status="needs_human", via="api", detail=f"missing required: {missing}")
        if dry_run:
            return SubmitResult(ref=form.ref, status="dry_run", via="api",
                                detail=f"would POST {len(answers)} fields, {len(files)} files")
        try:
            # map option labels to the value ids Greenhouse expects
            d = await self._questions(board, jid)
            vmap = {f["name"]: {v["label"].lower(): str(v["value"]) for v in f.get("values") or []}
                    for q in (d.get("questions") or []) + (d.get("location_questions") or []) for f in q.get("fields") or []}
            data: dict[str, Any] = {}
            for k, v in answers.items():
                m = vmap.get(k) or {}
                data[k] = [m.get(str(x).lower(), str(x)) for x in v] if isinstance(v, list) else m.get(str(v).lower(), str(v))
            upload = {k: (n, b) for k, (n, b) in self.read_files(files).items()}
            resp = await self.http.post_form(f"{API}/{board}/jobs/{jid}", data=data, files=upload, auth=(key, ""))
        except HttpError as e:
            return SubmitResult(ref=form.ref, status="error", via="api", detail=f"{e}: {e.body[:200]}")
        except OSError as e:
            return SubmitResult(ref=form.ref, status="error", via="api", detail=f"file error: {e}")
        return SubmitResult(ref=form.ref, status="submitted", via="api", confirmation=str(resp)[:200])
