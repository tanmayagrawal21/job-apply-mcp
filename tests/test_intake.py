"""run_applications, run reports and feedback (ADR-0045). Offline: fake adapter/store, fictional fixtures."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from apply_fakes import F, FakeProfile, FakeRouter, settings

from job_apply_mcp.apply.adapters import PolicyGates
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.core.models import ApplicationForm, FieldKind, Posting, SubmitResult
from job_apply_mcp.intake.links import (
    ImapLinkSource,
    MailFilter,
    MboxLinkSource,
    extract_urls,
    link_mail_from_dict,
    resolve_filter,
)
from job_apply_mcp.intake.runner import IntakeRunner
from job_apply_mcp.intake.store import IntakeStore
from job_apply_mcp.platforms import AdapterError

FIX = Path(__file__).parent / "fixtures" / "intake"
ME = "avery.tester@example.com"
HOSTS = {"boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com"}


class MultiAdapter:
    """One posting per target; company/title derived from the URL. Tracks per-host submit concurrency."""
    name = "greenhouse"

    def __init__(self, delay=0.0, prose_for=()):
        self.delay, self.prose_for = delay, set(prose_for)
        self.submits: list[str] = []
        self.active: dict[str, int] = {}
        self.peak_host: dict[str, int] = {}
        self.peak = self._now = 0

    def matches(self, url):
        return True

    @staticmethod
    def _co(ref):
        if ref.startswith("http"):
            return urlsplit(ref).path.strip("/").split("/")[0].title() or "Acme"
        return ref.split(":")[1].title()

    async def get_posting(self, ref, *, with_description=False):
        url = ref if ref.startswith("http") else f"https://boards.greenhouse.io/{ref.split(':')[1]}/jobs/{ref.split(':')[2]}"
        return Posting(ref=ref, platform="greenhouse", company=self._co(ref), title=f"Engineer {abs(hash(ref)) % 97}",
                       url=url, description="<p>Build things.</p>")

    async def get_form(self, ref):
        fields = [F("first_name", "First Name"), F("email", "Email")]
        if ref in self.prose_for:
            fields.append(F("q_why", "Why do you want to work here?", FieldKind.textarea))
        return ApplicationForm(ref=ref, submit_via=["api"], fields=fields)

    async def submit(self, form, answers, files, *, dry_run=True):
        host = urlsplit(form.ref).hostname or form.ref.split(":")[1]
        self.active[host] = self.active.get(host, 0) + 1
        self._now += 1
        self.peak_host[host] = max(self.peak_host.get(host, 0), self.active[host])
        self.peak = max(self.peak, self._now)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active[host] -= 1
            self._now -= 1
        self.submits.append(form.ref)
        return SubmitResult(ref=form.ref, status="dry_run" if dry_run else "submitted", via="api")


class Store:
    def __init__(self):
        self.records = []
        self._locks: dict[str, asyncio.Lock] = {}

    async def seen(self, company, title, *, within_days=180, **_):
        return any(r.status == "submitted" and c == company and t == title for r, c, t in self.records)

    async def record(self, result, *, company, title, **_):
        self.records.append((result, company, title))

    def company_lock(self, company):
        return self._locks.setdefault(company, asyncio.Lock())


class _P:
    def __init__(self):
        self.answers: dict[str, str] = {}
        self.salary = SimpleNamespace(expected=None)


class Profile(FakeProfile):
    def __init__(self):
        self.p = _P()


def resolve(target):
    if target.startswith("http"):
        if (urlsplit(target).hostname or "") in HOSTS:
            return object()
        raise AdapterError("no adapter")
    if target.startswith("greenhouse:"):
        return object()
    raise AdapterError("no adapter")


def make(tmp_path, *, adapter=None, search=None, router=None, live=False, connect=None, sink=None, **cfg):
    s = settings(**cfg)
    s.paths.data_dir = str(tmp_path)
    s.dry_run = not live
    s.apply.auto_approve = live
    s.intake.mail.source = "mbox"
    s.intake.mail.path = str(FIX / "eml")
    s.intake.mail.self_address = ME
    s.intake.mail.since = "2026-10-01"
    s.intake.sources = ["links", "mail", "discovery"]
    adapter = adapter or MultiAdapter()
    eng = ApplyEngine(s, resolve_adapter=lambda ref: adapter, profile=Profile(), router=router,
                      gates=PolicyGates(s), store=Store())
    r = IntakeRunner(s, engine=eng, store=IntakeStore(tmp_path), resolve=resolve, search=search, mail_connect=connect,
                     profile_answer_sink=None if sink is False else sink)
    if sink is False:
        r._sink = None
    return r, adapter, eng


# ------------------------------------------------------------------------------------------ link extraction
def test_extract_urls_unwraps_cleans_and_dedupes():
    text = ("see https://boards.greenhouse.io/northwind/jobs/4001?utm_source=x&gh_src=abc. and "
            "<a href=\"https://www.google.com/url?q=https%3A%2F%2Fjobs.lever.co%2Fglobex%2Fabc-123&amp;sa=D\">x</a> "
            "again https://boards.greenhouse.io/northwind/jobs/4001?gh_src=abc#apply")
    assert extract_urls(text) == ["https://jobs.lever.co/globex/abc-123",
                                  "https://boards.greenhouse.io/northwind/jobs/4001?gh_src=abc"]


def test_eml_fixtures_self_only_since_date_and_html():
    s = settings()
    s.intake.mail.self_address = ME
    s.intake.mail.since = "2026-10-01"
    flt, err = resolve_filter(s.intake.mail, s.ingest.imap)
    assert not err and flt.sender == ME and flt.recipient == ME
    msgs = list(MboxLinkSource(FIX / "eml").fetch(flt, 50))
    assert [m.key for m in msgs] == ["<links-001@fixture.test>", "<links-002@fixture.test>"]   # other sender, old: out
    assert msgs[0].urls[:3] == ["https://boards.greenhouse.io/northwind/jobs/4001?gh_src=abc",
                                "https://jobs.lever.co/globex/abc-123", "https://careers.example.com/jobs/77"]
    assert "https://jobs.ashbyhq.com/initech/9f8e7d6c?src=mail" in msgs[1].urls


def test_mbox_label_and_query_filters():
    flt = MailFilter(sender=ME, recipient=ME, label="Jobs")
    assert [m.key for m in MboxLinkSource(FIX / "labeled.mbox").fetch(flt, 10)] == ["<mbox-001@fixture.test>"]
    flt = MailFilter(sender=ME, query="dinner")
    assert [m.key for m in MboxLinkSource(FIX / "labeled.mbox").fetch(flt, 10)] == ["<mbox-002@fixture.test>"]


def test_self_unknown_fails_closed(monkeypatch):
    s = settings()
    monkeypatch.delenv(s.ingest.imap.user_env, raising=False)
    _, err = resolve_filter(s.intake.mail, s.ingest.imap)
    assert "self_address" in err


def test_imap_is_read_only():
    raw = (FIX / "eml" / "01_self_plain.eml").read_bytes()

    class Conn:
        def __init__(self):
            self.calls = []

        def select(self, folder, readonly=False):
            self.calls.append(("select", folder, readonly))
            return "OK", [b"1"]

        def search(self, charset, *crit):
            self.calls.append(("search", crit))
            return "OK", [b"1"]

        def fetch(self, i, what):
            self.calls.append(("fetch", what))
            return "OK", [(b"1", raw)]

        def logout(self):
            self.calls.append(("logout",))

        def __getattr__(self, name):   # store / copy / move / expunge / append would land here
            raise AssertionError(f"mailbox-modifying call {name}")

    c = Conn()
    s = settings()
    flt = MailFilter(sender=ME, recipient=ME, label="Jobs", since=None)
    out = list(ImapLinkSource(s.ingest.imap, connect=lambda: c).fetch(flt, 10))
    assert out and out[0].urls[0].startswith("https://boards.greenhouse.io/")
    assert c.calls[0] == ("select", '"Jobs"', True)
    crit = c.calls[1][1]
    assert "FROM" in crit and "TO" in crit and all("PEEK" in x[1] for x in c.calls if x[0] == "fetch")


def test_client_messages():
    m = link_mail_from_dict({"id": "m1", "from": ME, "to": [ME], "subject": "x",
                             "body": "https://jobs.lever.co/globex/zz-1"})
    assert m.key == "m1" and m.urls == ["https://jobs.lever.co/globex/zz-1"]


# ------------------------------------------------------------------------------------------ runs
async def test_explicit_links_and_unsupported_site(tmp_path):
    r, ad, _ = make(tmp_path)
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1", "https://careers.example.com/jobs/9",
                             "https://boards.greenhouse.io/acme/jobs/1?utm_source=dup"], sources=["links"])
    assert rep["counts"] == {"dry_run_ok": 1, "unsupported_site": 1}
    assert rep["unsupported_site"][0]["ref"] == "https://careers.example.com/jobs/9"
    assert "careers.example.com" in rep["unsupported_site"][0]["why"]
    assert ad.submits == ["https://boards.greenhouse.io/acme/jobs/1"]
    again = await r.report(rep["run_id"])
    assert again["counts"] == rep["counts"] and again["unsupported_site"][0]["src"] == "links"


async def test_links_file(tmp_path):
    f = tmp_path / "links.txt"
    f.write_text("# saved\nhttps://boards.greenhouse.io/acme/jobs/1\n\nhttps://jobs.lever.co/globex/abc\n")
    r, ad, _ = make(tmp_path)
    rep = await r.run(links_file=str(f), sources=["links"])
    assert rep["counts"] == {"dry_run_ok": 2} and len(ad.submits) == 2


async def test_max_applications_respected(tmp_path):
    r, ad, _ = make(tmp_path)
    links = [f"https://boards.greenhouse.io/co{i}/jobs/{i}" for i in range(5)]
    rep = await r.run(links=links, sources=["links"], max_applications=2)
    assert rep["counts"] == {"dry_run_ok": 2, "skipped": 3} and len(ad.submits) == 2
    assert all("max_applications=2" in x["why"] for x in rep["skipped"])
    assert [x["ref"] for x in rep["dry_run_ok"]] == links[:2]         # priority order within the source


async def test_priority_order_links_then_mail_then_discovery(tmp_path):
    calls = []

    async def search(**kw):
        calls.append(kw)
        return {"rows": [{"ref": "greenhouse:disco:1", "co": "Disco", "title": "Engineer"}]}

    r, _ad, _ = make(tmp_path, search=search, intake__plan_factor=1.0)
    rep = await r.run(links=["https://boards.greenhouse.io/explicit/jobs/1"], max_applications=2)
    got = [(x["src"], x["ref"]) for x in rep["dry_run_ok"]]
    assert got[0] == ("links", "https://boards.greenhouse.io/explicit/jobs/1") and got[1][0] == "mail"
    assert calls == []                                    # the pool was full before discovery
    assert all(x["src"] == "mail" for x in rep["skipped"])
    rep2 = await r.run(sources=["discovery"], max_applications=3)
    assert calls[0]["limit"] == 3 and rep2["dry_run_ok"][0]["ref"] == "greenhouse:disco:1"


async def test_mail_ids_recorded_and_deferred_mail_kept(tmp_path):
    r, _ad, _ = make(tmp_path)
    r.s.intake.plan_factor = 1.0
    rep = await r.run(sources=["mail"], max_applications=1)
    # message 1: greenhouse + lever + unsupported; only one planned, the rest deferred -> message stays unprocessed
    assert rep["counts"]["dry_run_ok"] == 1 and rep["counts"]["unsupported_site"] == 1
    assert await r.store.processed(["<links-001@fixture.test>", "<links-002@fixture.test>"]) == set()
    rep = await r.run(sources=["mail"], max_applications=10)
    assert await r.store.processed(["<links-001@fixture.test>", "<links-002@fixture.test>"]) == {
        "<links-001@fixture.test>", "<links-002@fixture.test>"}
    rep = await r.run(sources=["mail"], max_applications=10)
    assert rep["counts"] == {}                              # nothing reprocessed
    assert (FIX / "eml" / "01_self_plain.eml").read_bytes().startswith(b"From: Avery")   # mailbox untouched


async def test_feedback_applied_in_next_run(tmp_path):
    r, _ad, eng = make(tmp_path)
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    rid = rep["run_id"]
    assert (await r.give_feedback(rid, "https://boards.greenhouse.io/acme/jobs/1", {"kind": "deny_company"}))["id"]
    assert (await r.give_feedback(rid, None, {"kind": "deny_title", "pattern": "intern"}))["id"]
    ans = await r.give_feedback(rid, None, {"kind": "answer", "pattern": "favorite editor", "value": "Vim"})
    assert ans["needs_confirmation"] and ans["confirm"].endswith(str(ans["id"]))
    assert (await r.give_feedback(rid, None, {"kind": "salary", "salary_floor": 90000}))["id"]
    assert (await r.give_feedback(rid, None, {"kind": "run_size", "delta": -8}))["id"]
    assert (await r.give_feedback(rid, None, "prefer robotics companies"))["kind"] == "note"
    routed = await r.give_feedback(rid, None, {"kind": "answer", "pattern": "authorized to work", "value": "Yes"})
    assert routed["kind"] == "profile_answer" and "answer_profile_question" in routed["when"]
    assert "salary" in (await r.give_feedback(rid, None, {"kind": "answer", "pattern": "desired pay",
                                                           "value": "1"}))["error"]
    assert "error" in await r.give_feedback(rid, "https://nope.example.com/x", "hi")

    links = ["https://boards.greenhouse.io/acme/jobs/2"] + [f"https://boards.greenhouse.io/co{i}/jobs/{i}"
                                                           for i in range(4)]
    rep2 = await r.run(links=links, sources=["links"])
    ctx = rep2["plan_context"]
    assert ctx["feedback_notes"] == ["prefer robotics companies"]
    assert ctx["applied_feedback"][0] == "deny company ^Acme$" and len(ctx["applied_feedback"]) == 4
    assert [a["id"] for a in ctx["awaiting_confirmation"]] == [ans["id"]]
    assert rep2["skipped"][0]["ref"] == links[0] and "excluded employer" in rep2["skipped"][0]["why"]
    assert rep2["counts"]["dry_run_ok"] == 2                       # run_size 10 - 8
    assert "favorite editor" not in eng.profile.p.answers      # a client answer is not owner authority
    assert r.s.policy.salary_floor == 90000 and "intern" in r.s.policy.exclude_title_patterns
    assert (await r.confirm_feedback(ans["id"], "human:cli"))["confirmed_by"] == "human:cli"
    rep3 = await r.run(links=links[1:2], sources=["links"])
    assert rep3["plan_context"]["applied_feedback"] == ["answer for 'favorite editor' set"]
    assert eng.profile.p.answers["favorite editor"] == "Vim"
    rep3 = await r.run(links=links[1:2], sources=["links"])
    assert "plan_context" not in rep3 or not rep3["plan_context"].get("applied_feedback")
    assert r.s.policy.exclude_employer_patterns.count("^Acme$") == 1      # replay is idempotent
    rep_saved = await r.report(rep2["run_id"])
    assert rep_saved["plan_context"]["feedback_notes"] == ["prefer robotics companies"]


async def test_submit_parallel_across_hosts_one_per_host(tmp_path):
    ad = MultiAdapter(delay=0.1)
    r, ad, _ = make(tmp_path, adapter=ad, live=True)
    links = [f"https://boards.greenhouse.io/g{i}/jobs/{i}" for i in range(3)] + \
            [f"https://jobs.lever.co/l{i}/x{i}" for i in range(3)]
    t0 = time.monotonic()
    rep = await r.run(links=links, sources=["links"])
    took = time.monotonic() - t0
    assert rep["counts"] == {"submitted": 6}
    assert max(ad.peak_host.values()) == 1 and ad.peak == 2
    assert took < 6 * 0.1                                   # serial would be 0.6 s


async def test_live_human_mode_hands_off_for_approval(tmp_path):
    r, ad, _ = make(tmp_path, live=True)
    r.s.apply.approval_mode = "human"
    r.s.apply.auto_approve = False
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    assert rep["counts"] == {"needs_human": 1} and "approve" in rep["needs_human"][0]["why"]
    assert rep["needs_human"][0]["approval_id"] and ad.submits == []


async def test_deferred_prose_skipped_without_blocking(tmp_path):
    prose = "https://boards.greenhouse.io/wordy/jobs/1"
    router = FakeRouter(short={}, prose="never called")
    r, _ad, _ = make(tmp_path, adapter=MultiAdapter(prose_for=[prose]), router=router)
    assert r.s.writing.mode == "inline"                     # the run defers on its own
    rep = await r.run(links=[prose, "https://boards.greenhouse.io/plain/jobs/2"], sources=["links"])
    assert rep["counts"] == {"dry_run_ok": 1, "skipped": 1}
    assert "prose not approved" in rep["skipped"][0]["why"]
    assert not [c for c, _ in router.calls if c in ("narrative", "review")]


async def test_dry_run_cannot_be_relaxed(tmp_path):
    r, _, _ = make(tmp_path)
    assert "dry_run" in (await r.run(links=["https://boards.greenhouse.io/a/jobs/1"], dry_run=False))["error"]


async def test_report_marks_confirmed(tmp_path):
    r, _, eng = make(tmp_path, live=True)
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    assert rep["counts"] == {"submitted": 1}
    eng.approvals.set_state("https://boards.greenhouse.io/acme/jobs/1", "confirmed")
    assert (await r.report())["counts"] == {"confirmed": 1}


@pytest.mark.parametrize("fb", [{"kind": "nope"}, {"kind": "salary"}, {"kind": "run_size", "delta": "x"},
                                {"kind": "deny_title", "pattern": "("}, ""])
async def test_bad_feedback_rejected(tmp_path, fb):
    r, _, _ = make(tmp_path)
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    assert "error" in await r.give_feedback(rep["run_id"], None, fb)


def test_cli_parses_run_report_feedback():
    from job_apply_mcp.cli import build_parser

    a = build_parser().parse_args(["run", "--links", "jobs.txt", "--max", "3", "--source", "links",
                                   "--mail-since", "2026-10-01", "--dry-run"])
    assert (a.cmd, a.links_file, a.max_applications, a.source, a.mail_since, a.dry_run) == (
        "run", "jobs.txt", 3, ["links"], "2026-10-01", True)
    assert build_parser().parse_args(["report"]).run_id == ""
    assert build_parser().parse_args(["feedback", "r1", "too many startups", "--ref", "x"]).ref == "x"


async def test_salary_feedback_raises_but_never_below_floor(tmp_path):
    r, _, eng = make(tmp_path)
    pol = r.s.policy
    pol.salary_floor, pol.default_ask = 80000, 85000
    eng.profile.p.salary.expected = 90000
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    rid = rep["run_id"]
    assert "id" in await r.give_feedback(rid, None, {"kind": "salary", "delta": 5000})        # "raise salary"
    await r.run(links=[], sources=["links"])
    assert (pol.default_ask, eng.profile.p.salary.expected) == (90000, 95000)
    await r.run(links=[], sources=["links"])                                                  # replay: not doubled
    assert (pol.default_ask, eng.profile.p.salary.expected) == (90000, 95000)
    await r.give_feedback(rid, None, {"kind": "salary", "salary_floor": 50000, "default_ask": 60000,
                                      "expected": 70000})
    rep = await r.run(links=[], sources=["links"])
    assert pol.salary_floor == 80000                                                          # floor never lowered
    assert (pol.default_ask, eng.profile.p.salary.expected) == (80000, 80000)                 # clamped to the floor
    assert any("floor 80000" in n for n in rep["plan_context"]["notes"])
    await r.give_feedback(rid, None, {"kind": "salary", "salary_floor": 100000, "percent": 10})
    await r.run(links=[], sources=["links"])
    assert pol.salary_floor == 100000 and pol.default_ask == 100000    # 60000 * 1.1, clamped to the new floor
    await r.give_feedback(rid, None, {"kind": "salary", "percent": 10})
    await r.run(links=[], sources=["links"])
    assert pol.default_ask == 110000


async def test_profile_answers_pending_until_builder_then_delivered(tmp_path):
    r, _, eng = make(tmp_path, sink=False)   # sink=False: no profile builder, rows stay pending
    assert r._sink is None
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    rid = rep["run_id"]
    fb = await r.give_feedback(rid, "https://boards.greenhouse.io/acme/jobs/1",
                               {"kind": "profile_answer", "question": "Are you legally authorized to work here?",
                                "value": "Yes", "scope": "company"})
    assert fb["kind"] == "profile_answer"
    await r.give_feedback(rid, None, {"kind": "answer", "pattern": "veteran status", "value": "I am not a veteran"})
    rep = await r.run(links=[], sources=["links"])
    pend = rep["plan_context"]["pending_profile_answers"]
    assert [p["scope"] for p in pend] == ["company", "global"] and pend[0]["scope_value"] == "Acme"
    assert pend[0]["key"] == "work_authorized" and pend[1]["key"] == "veteran"
    assert "veteran status" not in eng.profile.p.answers           # never an in-memory overlay
    assert (await r.report())["plan_context"]["pending_profile_answers"] == pend
    assert all("--confirm" in p["confirm"] for p in pend)
    got = []
    r._sink = lambda *a: got.append(a)
    rep = await r.run(links=[], sources=["links"])
    assert got == []                                      # unconfirmed client answers are never delivered
    for p in pend:
        await r.confirm_feedback(p["id"], "human:cli")
    rep = await r.run(links=[], sources=["links"])
    assert got[0] == ("work_authorized", "Yes", "company", "Acme") and len(got) == 2
    assert sum("answer_profile_question" in x for x in rep["plan_context"]["applied_feedback"]) == 2
    rep = await r.run(links=[], sources=["links"])
    assert "pending_profile_answers" not in rep["plan_context"] and len(got) == 2


RESTRICTED = ["work permit", "legally permitted to be employed", "green card", "permanent resident", "ITAR",
              "US person", "U.S. person", "export control", "background check", "agree to the terms", "acknowledge",
              "consent", "SMS updates", "drug test", "arbitration", "true and complete", "nationality",
              "military service", "Did you use AI to complete this application", "right to work",
              "residence permit", "national insurance number", "date of birth", "marital status", "religion",
              "criminal record", "re:visa.*status", "notice period under my contract terms"]


@pytest.mark.parametrize("pat", RESTRICTED)
async def test_restricted_answers_never_overlays(tmp_path, pat):
    r, _, eng = make(tmp_path)
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    out = await r.give_feedback(rep["run_id"], None, {"kind": "answer", "pattern": pat, "value": "Yes"})
    assert out["kind"] == "profile_answer" and out["needs_confirmation"]
    await r.run(links=[], sources=["links"])
    assert eng.profile.p.answers == {}


async def test_plain_answer_and_stop_phrase(tmp_path):
    from job_apply_mcp.intake.feedback import is_restricted_question
    assert not is_restricted_question("favorite editor") and not is_restricted_question("preferred team size")
    assert is_restricted_question("my secret phrase", ["secret phrase"])


async def test_human_mode_client_salary_waits_for_confirmation(tmp_path):
    r, _, _ = make(tmp_path)
    r.s.apply.approval_mode = "human"
    r.s.policy.salary_floor = 70000
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    fb = await r.give_feedback(rep["run_id"], None, {"kind": "salary", "salary_floor": 75000})
    assert fb["needs_confirmation"]
    deny = await r.give_feedback(rep["run_id"], None, {"kind": "deny_title", "pattern": "manager"})
    assert "needs_confirmation" not in deny                   # tightening needs no confirmation
    rep = await r.run(links=[], sources=["links"])
    assert r.s.policy.salary_floor == 70000 and rep["plan_context"]["awaiting_confirmation"][0]["id"] == fb["id"]
    user = await r.give_feedback(rep["run_id"], None, {"kind": "salary", "salary_floor": 72000}, origin="user")
    assert "needs_confirmation" not in user
    await r.run(links=[], sources=["links"])
    assert r.s.policy.salary_floor == 72000


async def test_salary_ceiling(tmp_path):
    r, _, _e = make(tmp_path)
    r.s.policy.salary_floor, r.s.policy.default_ask = 50000, 60000
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    assert "percent" in (await r.give_feedback(rep["run_id"], None, {"kind": "salary", "percent": 1000}))["error"]
    for _ in range(4):
        await r.give_feedback(rep["run_id"], None, {"kind": "salary", "percent": 100})
    rep = await r.run(links=[], sources=["links"])
    assert r.s.policy.default_ask == 180000                       # 3 x the largest configured value
    assert any("capped at 180000" in n for n in rep["plan_context"]["notes"])


def test_mail_filter_fails_closed():
    s = settings()
    s.intake.mail.self_address = ME
    for o in ({"sender": ""}, {"recipient": ""}, {"self_address": "x@example.org"}, {"label": "Other"},
              {"sender": "someone@example.org"}):
        assert "not allowed per call" in resolve_filter(s.intake.mail, s.ingest.imap, overrides=o)[1]
    flt, err = resolve_filter(s.intake.mail, s.ingest.imap, overrides={"query": "jobs", "since_days": 3})
    assert not err and flt.query == "jobs" and flt.sender == ME
    for bad in ("", "example.com", "someone@example.org"):
        s.intake.mail.sender = bad
        assert "intake.mail.sender" in resolve_filter(s.intake.mail, s.ingest.imap)[1]
    s.intake.mail.self_aliases = ["avery.alt@example.net"]
    s.intake.mail.sender = "avery.alt@example.net"
    assert resolve_filter(s.intake.mail, s.ingest.imap)[0].sender == "avery.alt@example.net"
    m = link_mail_from_dict({"id": "x", "from": "someone@example.com", "to": [ME], "body": ""})
    assert not MailFilter(sender="example.com", recipient=ME)(m)       # no substring/domain matching


async def test_concurrent_runs_are_serialized(tmp_path):
    r, _, _ = make(tmp_path, adapter=MultiAdapter(delay=0.05), live=True)
    links = ["https://boards.greenhouse.io/a/jobs/1"]
    a, b = await asyncio.gather(r.run(links=links, sources=["links"]), r.run(links=links, sources=["links"]))
    assert sum("error" in x for x in (a, b)) == 1


def test_site_key():
    from job_apply_mcp.intake.runner import site_key
    assert site_key("boards.greenhouse.io") == site_key("job-boards.greenhouse.io") == "greenhouse.io"
    assert site_key("careers.example.co.uk") == "example.co.uk"


def test_mail_identity_only_from_trusted_user_config(tmp_path, monkeypatch, caplog):
    """A project file or JOB_APPLY__* must not point "self" at someone else's mail (ADR-0045)."""
    from test_approvals import _cfg
    user = '[intake.mail]\nself_address = "avery.tester@example.com"\n'
    proj = ('[intake.mail]\nsender = "intruder@example.org"\nrecipient = "intruder@example.org"\n'
            'self_aliases = ["intruder@example.org"]\nquery = "jobs"\n[ingest.imap]\nuser_env = "OTHER_USER"\n')
    env = {"JOB_APPLY__INTAKE__MAIL__SELF_ADDRESS": "intruder@example.org",
           "JOB_APPLY__INTAKE__MAIL__SENDER": "intruder@example.org"}
    with caplog.at_level("WARNING"):
        s = _cfg(tmp_path, monkeypatch, user=user, proj=proj, env=env)
    m = s.intake.mail
    assert (m.sender, m.recipient, m.self_address, m.self_aliases) == ("self", "self", ME, [])
    assert s.ingest.imap.user_env == "JOB_APPLY_IMAP_USER" and m.query == "jobs"    # other keys still layer
    assert "intake.mail.self_aliases" in caplog.text and "IGNORED" in caplog.text
    s = _cfg(tmp_path, monkeypatch, user='[intake.mail]\nself_aliases = ["avery.alt@example.net"]\n'
                                         'sender = "avery.alt@example.net"\n', proj="", env={})
    assert s.intake.mail.self_aliases == ["avery.alt@example.net"] and s.intake.mail.sender == "avery.alt@example.net"


def test_untrusted_user_file_cannot_set_mail_identity(tmp_path, monkeypatch):
    from test_approvals import _cfg
    s = _cfg(tmp_path, monkeypatch, user='[intake.mail]\nself_aliases = ["intruder@example.org"]\n', via_env=True)
    assert s.intake.mail.self_aliases == []


async def test_confirmed_profile_answers_reach_builder_as_user_written(tmp_path):
    """With the profile builder installed (ADR-0047), confirmed rows are stored as the user's answers; unconfirmed
    client rows never reach it; demographic keys are refused by the builder and stay pending with a note."""
    from job_apply_mcp.persona.tools import db_for
    r, _, _ = make(tmp_path)
    assert r._sink is not None
    rep = await r.run(links=["https://boards.greenhouse.io/acme/jobs/1"], sources=["links"])
    fb = await r.give_feedback(rep["run_id"], None, {"kind": "profile_answer", "key": "work_authorized",
                                                     "value": "Yes"})
    vet = await r.give_feedback(rep["run_id"], None, {"kind": "answer", "pattern": "veteran status", "value": "No"})
    await r.run(links=[], sources=["links"])
    db = db_for(r.s)
    assert db.answer("work_authorized") is None                    # not confirmed: never delivered
    await r.confirm_feedback(fb["id"], "human:cli")
    await r.confirm_feedback(vet["id"], "human:cli")
    rep = await r.run(links=[], sources=["links"])
    assert db.answer("work_authorized") == "Yes"
    origins = {row[0] for row in db._db.execute("SELECT origin FROM answers WHERE key='work_authorized'")}
    assert origins == {"user_written"}
    assert [p["key"] for p in rep["plan_context"]["pending_profile_answers"]] == ["veteran"]
    assert any("veteran" in n for n in rep["plan_context"]["notes"])


async def test_builder_sink_passes_origin_when_supported(tmp_path, monkeypatch):
    import job_apply_mcp.persona.tools as pt
    seen = {}

    def fake(settings, key, value, scope="global", scope_value="", origin="client"):
        seen.update(key=key, origin=origin)
        return {"ok": True}

    monkeypatch.setattr(pt, "store_answer", fake)
    r, _, _ = make(tmp_path)
    r._sink("work_authorized", "Yes", "global", "")
    assert seen == {"key": "work_authorized", "origin": "user_written"}
