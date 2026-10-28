"""Greenhouse: public Job Board API (boards-api.greenhouse.io/v1). Submission needs a per-board API key."""
from __future__ import annotations

import asyncio
import html
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..core.models import ApplicationForm, FieldKind, FormField, Posting, SubmitResult
from ..core.registry import platforms
from .base import AdapterError, BaseAdapter, PostingClosed, iso_date, not_before
from .http import HttpError
from .textflags import html_to_text

_REMOTE_WORD = re.compile(r"\bremote\b", re.IGNORECASE)
API = "https://boards-api.greenhouse.io/v1/boards"
_URL = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)/jobs/(\d+)")
_KINDS = {"input_text": FieldKind.text, "textarea": FieldKind.textarea, "input_file": FieldKind.file,
          "multi_value_single_select": FieldKind.select, "multi_value_multi_select": FieldKind.multiselect,
          "input_hidden": FieldKind.unknown}

# ADR-0072: board token for a company-hosted ?gh_jid= page. Page content is untrusted data: only a strictly formed
# token is ever taken from it, and every candidate is checked against the public jobs API before it is used.
_BOARD_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_PAGE_BOARD = (
    # embed script / iframe: (job-)boards.greenhouse.io/embed/job_board[/js]?for=<board> or embed/job_app?...for=<board>
    re.compile(r"(?:job-)?boards\.greenhouse\.io/embed/job_(?:board(?:/js)?|app)\?(?:[^\"'<>\s]*&)?for=([A-Za-z0-9-]+)"),
    # a link to the hosted posting: (job-)boards.greenhouse.io/<board>/jobs/<id>
    re.compile(r"(?:job-)?boards\.greenhouse\.io/([A-Za-z0-9-]+)/jobs/\d+"),
)
_SITE_NAME = re.compile(r"<meta[^>]+(?:property|name)=[\"']og:site_name[\"'][^>]*>", re.IGNORECASE)
_CONTENT = re.compile(r"content=[\"']([^\"'<>]{1,80})[\"']", re.IGNORECASE)
_HOST_AFFIXES = ("with", "join", "work", "careers", "career", "jobs", "team", "go", "get", "hello", "meet")
_MAX_PAGE_CHARS = 2_000_000
_MAX_CANDIDATES = 6


def _token(raw: str) -> str | None:
    t = raw.strip().lower()
    return t if _BOARD_TOKEN.fullmatch(t) else None


def page_board_candidates(page: str) -> list[str]:
    """Board tokens named by a company careers page (ADR-0072): the Greenhouse embed script or iframe `for=` param,
    a hosted-posting link, then the og:site_name as a slug. Strictly validated; order = confidence."""
    page = html.unescape(page[:_MAX_PAGE_CHARS])
    out: list[str] = []
    for rx in _PAGE_BOARD:
        out += [t for m in rx.finditer(page) if (t := _token(m.group(1)))]
    if (meta := _SITE_NAME.search(page)) and (c := _CONTENT.search(meta.group(0))):
        out += [t for t in (_token(re.sub(r"[^A-Za-z0-9]+", "", c.group(1))),
                            _token(re.sub(r"[^A-Za-z0-9]+", "-", c.group(1)).strip("-"))) if t]
    return list(dict.fromkeys(out))


def host_board_candidates(host: str) -> list[str]:
    """Board guesses from a careers host ("careers.withbrand.example" -> "withbrand", "brand"); the registrable label
    first (the historic guess), then that label without a common careers prefix or suffix."""
    labels = [x for x in host.lower().split(":")[0].split(".") if x]
    base = labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")
    out = [base]
    for a in _HOST_AFFIXES:
        if base.startswith(a) and len(base) > len(a) + 1:
            out.append(base[len(a):].strip("-"))
        if base.endswith(a) and len(base) > len(a) + 1:
            out.append(base[: -len(a)].strip("-"))
    return list(dict.fromkeys(t for x in out if (t := _token(x))))


@platforms.register("greenhouse")
class GreenhouseAdapter(BaseAdapter):
    name = "greenhouse"
    hosts = ("greenhouse.io",)

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._host_boards: dict[str, str] = {}   # company host -> verified board token (in-process, ADR-0072)
        self._host_locks: dict[str, asyncio.Lock] = {}   # get_posting + get_form run concurrently: one lookup per host

    def matches(self, url: str) -> bool:
        return super().matches(url) or "gh_jid=" in url

    @staticmethod
    def _company_gh_page(url: str) -> tuple[str, str] | None:
        """(host, gh_jid) for a company-hosted careers page carrying ?gh_jid=; None for anything else."""
        if not url.startswith(("http://", "https://")):
            return None
        p = urlparse(url)
        host = (p.hostname or "").lower()
        jid = (parse_qs(p.query).get("gh_jid") or [""])[0]
        if not host or host == "greenhouse.io" or host.endswith(".greenhouse.io") or not jid.isdigit():
            return None
        if _URL.search(url) or "for" in parse_qs(p.query):
            return None
        return host, jid

    async def _job_exists(self, board: str, jid: str) -> bool:
        try:
            await self.http.get_json(f"{API}/{board}/jobs/{jid}")
            return True
        except (HttpError, httpx.HTTPError, ValueError):   # 404, other HTTP or transport error: not verified
            return False

    async def _resolve_job(self, url_or_ref: str) -> tuple[str, str]:
        """_need_job, but a company ?gh_jid= page gets its board from the page itself (ADR-0072): candidates from the
        embed script / iframe / og:site_name, then from the host, each verified against the public jobs API; the
        historic host guess only when none verifies. Verified boards are cached per host."""
        page = self._company_gh_page(url_or_ref)
        if not page:
            return self._need_job(url_or_ref)
        host, jid = page
        async with self._host_locks.setdefault(host, asyncio.Lock()):
            if board := self._host_boards.get(host):
                return board, jid
            try:
                found = page_board_candidates(await self.http.get_text(url_or_ref))
            except (HttpError, httpx.HTTPError, UnicodeError):   # unreachable page: the host still yields candidates
                found = []
            for cand in list(dict.fromkeys(found + host_board_candidates(host)))[:_MAX_CANDIDATES]:
                if await self._job_exists(cand, jid):
                    self._host_boards[host] = cand
                    return cand, jid
        return self._need_job(url_or_ref)

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
        """Per office, the ISO-2 of a location that ends in a full country name (ADR-0040), or of a location-less office
        whose name is exactly a full country name (ADR-0053); any non-dict, malformed or country-less office is
        "UNCONFIRMED". No offices -> [] (not confirmed; there is no text fallback)."""
        raw = j.get("offices") or []
        if not isinstance(raw, list):
            return ["UNCONFIRMED"]
        out: list[str] = []
        for o in raw:
            loc = o.get("location") if isinstance(o, dict) else None
            tail = loc.rsplit(",", 1)[-1] if isinstance(loc, str) else ""
            code = self.country_code(tail, allow_codes=False) or (self.country_code(tail) if tail.strip() == "US" else None)
            if not code and isinstance(loc, str) and loc.strip():
                code = self._us_office(loc, str(o.get("name") or ""))
            if not code and isinstance(o, dict) and not (loc or "").strip() and isinstance(o.get("name"), str):
                # ADR-0053: an office with no location whose NAME is exactly a full country name ("United States").
                # ADR-0062: also "Remote - United States" / "United States (Remote)". Codes and labels such as
                # "US-PERM" or "NYC" never confirm.
                code = self.country_code(_REMOTE_WORD.sub(" ", o["name"]).strip(" -,()/"), allow_codes=False)
            out.append(code or "UNCONFIRMED")
        return sorted(set(out))

    @staticmethod
    def _us_office(loc: str, name: str = "") -> str | None:
        """ADR-0062: an office location that is a strictly parsed US place ("Mountain View, CA", or the place after
        an office name: "Acme HQ - Mountain View, CA"). The same strict parser as the mapper; an office whose name or
        location names any other country never confirms."""
        from ..apply.mapper import location_in_country, question_countries
        from ..apply.places import MULTI_REGION, country_iso
        if any(country_iso(c, allow_codes=False) != "US" for t in (name, loc) for c in question_countries(t)[1]):
            return None
        place = loc.rsplit(" - ", 1)[-1].strip()
        return "US" if place and not MULTI_REGION.search(place) and location_in_country(place, "US") else None

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
        board, jid = await self._resolve_job(url_or_ref)
        return self._to_posting(board, await self._get(f"{API}/{board}/jobs/{jid}"), with_description=with_description)

    async def _get(self, url: str, params: dict | None = None) -> Any:
        try:
            return await self.http.get_json(url, params=params)
        except HttpError as e:
            if e.status == 404:
                raise PostingClosed("job not found (closed or moved)") from e
            raise AdapterError(str(e)) from e

    async def _questions(self, board: str, jid: str) -> dict:
        return await self._get(f"{API}/{board}/jobs/{jid}", params={"questions": "true"})

    # ------------------------------------------------------------------ form
    async def get_form(self, url_or_ref: str) -> ApplicationForm:
        board, jid = await self._resolve_job(url_or_ref)
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
