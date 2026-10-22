import imaplib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_apply_mcp.config import ImapCfg, Settings
from job_apply_mcp.core.models import SubmitResult
from job_apply_mcp.ingest.confirmations import ConfirmationParser, Message, load_patterns
from job_apply_mcp.ingest.sources import ImapSource, MboxSource, build_search, message_from_bytes
from job_apply_mcp.ingest.sync import ingest_messages
from job_apply_mcp.ingest.tools import sync
from job_apply_mcp.store.sqlite import SqliteStore

FIX = Path(__file__).parent / "fixtures" / "mail"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def load(name):
    return message_from_bytes((FIX / f"{name}.eml").read_bytes())


@pytest.fixture
def parser():
    return ConfirmationParser()


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "d")
    yield s
    s.close()


@pytest.mark.parametrize("name,platform,company,title", [
    ("greenhouse", "greenhouse", "Northwind Robotics", "Backend Engineer"),
    ("lever", "lever", "Quillfeather Labs", "Data Analyst"),
    ("ashby", "ashby", "Marlowe Systems", "Platform Engineer"),
    ("workday", "workday", "Tidewater Analytics", "Research Associate"),
    ("icims", "icims", "Cinder Peak Energy", "Support Engineer"),
    ("smartrecruiters", "smartrecruiters", "Orchard Lane Robotics", "Machine Learning Engineer"),
    ("taleo", "taleo", "Fenwick Biologics", "Lab Software Developer"),
    ("successfactors", "successfactors", "Halcyon Freight", "Logistics Data Engineer"),
    ("linkedin", "linkedin", "Brightwell Games", "Gameplay Programmer"),
    ("handshake", "handshake", "Pinecrest Labs", "Software Engineering Intern"),
    ("employer", "employer", "Lumen Harbor", "Site Reliability Engineer"),
])
def test_parse_platforms(parser, name, platform, company, title):
    c = parser.parse(load(name))
    assert c is not None, name
    assert (c.platform, c.company, c.title) == (platform, company, title)
    assert c.confidence >= 0.6
    assert c.date.year == 2026


def test_negatives(parser):
    assert parser.parse(load("not_confirmation_alert")) is None
    assert parser.parse(load("not_confirmation_reject")) is None
    assert parser.parse(Message("a@b.test", "Lunch tomorrow?", None, "see you")) is None


def test_ref_is_stable_and_date_sensitive(parser):
    a, b = parser.parse(load("greenhouse")), parser.parse(load("greenhouse"))
    assert a.ref == b.ref and a.ref.startswith("mail:")
    m = load("greenhouse")
    m.date = m.date + timedelta(days=3)
    assert parser.parse(m).ref != a.ref


def test_user_patterns_extend_without_code(tmp_path):
    f = tmp_path / "p.toml"
    f.write_text('''
[[platform]]
name = "acmehire"
sender_domains = ["acmehire.example"]
subject_positive = ["got your resume"]
[[platform.extract]]
regex = "got your resume for (?P<role>.+?) at (?P<company>.+)$"
''')
    p = ConfirmationParser(load_patterns(f))
    c = p.parse(Message("Jobs <x@mail.acmehire.example>", "We got your resume for Chef at Pie Co",
                        datetime(2026, 9, 1, tzinfo=UTC)))
    assert (c.platform, c.company, c.title) == ("acmehire", "Pie Co", "Chef")
    assert ConfirmationParser().parse(Message("x@y.test", "We got your resume for Chef at Pie Co", None)) is None
    with pytest.raises(FileNotFoundError):
        load_patterns(tmp_path / "missing.toml")


def test_mbox_dir_source_filters_by_date():
    src = MboxSource(FIX)
    assert len(list(src.fetch(EPOCH))) == 13
    assert len(list(src.fetch(datetime(2026, 10, 12, tzinfo=UTC)))) == 3
    with pytest.raises(FileNotFoundError):
        list(MboxSource(FIX / "nope").fetch(EPOCH))


def test_mbox_file_source(tmp_path):
    box = tmp_path / "a.mbox"
    raw = (FIX / "lever.eml").read_text()
    box.write_text("From x@y.test Tue Oct  6 09:30:00 2026\n" + raw + "\n")
    msgs = list(MboxSource(box).fetch(EPOCH))
    assert len(msgs) == 1 and "Quillfeather" in msgs[0].subject


async def test_ingest_idempotent_and_blocks_reapply(store, parser):
    msgs = list(MboxSource(FIX).fetch(EPOCH))
    kw = {"dedupe_days": 3650, "max_new_shown": 3}
    r1 = await ingest_messages(store, parser, msgs, **kw)
    assert (r1["scanned"], r1["matched"], r1["new"]) == (13, 11, 11)
    assert len(r1["new_rows"]) == 3 and r1["more"] == 8
    assert await store.seen("Northwind Robotics Inc", "Backend Engineer (Remote)", within_days=3650)
    row = (await store.list(limit=1))["rows"][0]
    assert row["status"] == "submitted" and row["via"] == "email"
    assert "@" not in str(await store.get(row["ref"]))  # no addresses stored
    r2 = await ingest_messages(store, parser, msgs, **kw)
    assert r2["new"] == 0 and r2["known"] == 11
    assert (await store.list())["total"] == 11


async def test_old_and_preview_and_lowconf(store, parser):
    msgs = list(MboxSource(FIX).fetch(EPOCH))
    r = await ingest_messages(store, parser, msgs, dedupe_days=1)
    assert r["new"] == 0 and r["outside_window"] == 11
    r = await ingest_messages(store, parser, msgs, dedupe_days=3650, preview=True)
    assert r["new"] == 11 and r["preview"] and (await store.list())["total"] == 0
    r = await ingest_messages(store, parser, msgs, dedupe_days=3650, min_confidence=1.01)
    assert r["new"] == 0 and r["low_conf"] == 11


class FakeStore:
    """Only the Store protocol (seen, record): ingestion must not depend on SqliteStore internals."""

    def __init__(self):
        self.rows = []

    async def seen(self, company, title, *, within_days=180):
        return any(c == company and t == title for c, t, _ in self.rows)

    async def record(self, result: SubmitResult, *, company, title):
        self.rows.append((company, title, result))


async def test_protocol_only_store(parser):
    s = FakeStore()
    r = await ingest_messages(s, parser, [load("lever")], dedupe_days=3650)
    assert r["new"] == 1 and s.rows[0][2].via == "email" and s.rows[0][2].status == "submitted"


async def test_client_mode_tool_path(store):
    st = Settings()
    now = datetime.now(UTC).isoformat()
    msgs = [{"sender": "Orbit Cafe Jobs <no-reply@greenhouse.io>", "subject": "Thank you for applying to Orbit Cafe",
             "date": now, "snippet": "Thank you for applying for the Barista Lead position at Orbit Cafe."},
            {"from": "friend@example.test", "subject": "hi", "date": now}]
    out = await sync(store, st, messages=msgs)
    assert out["source"] == "client" and out["new"] == 1 and out["new_rows"][0]["title"] == "Barista Lead"
    assert await store.seen("Orbit Cafe", "Barista Lead")
    assert (await sync(store, st, messages=msgs))["new"] == 0


async def test_sync_mbox_source(store):
    st = Settings.model_validate({"ingest": {"source": "mbox", "path": str(FIX), "since_days": 36500}})
    out = await sync(store, st, since_days=36500)
    assert out["source"] == "mbox" and out["matched"] == 11
    bad = Settings()  # client default cannot be pulled
    with pytest.raises(ValueError):
        await sync(store, bad, source="client-pull")


class FakeImap:
    def __init__(self, raws):
        self.raws, self.calls = raws, []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", readonly))
        return "OK", [b"1"]

    def search(self, charset, *crit):
        self.calls.append(("search", crit))
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raws)))]

    def fetch(self, i, spec):
        self.calls.append(("fetch", spec))
        return "OK", [(b"1 (BODY[] {1}", self.raws[int(i) - 1]), b")"]

    def logout(self):
        self.calls.append(("logout",))

    def __getattr__(self, name):  # any mutating command would land here
        raise AssertionError(f"forbidden IMAP command: {name}")


def test_imap_is_read_only_and_peeks():
    fake = FakeImap([(FIX / "lever.eml").read_bytes()])
    cfg = ImapCfg(host="x", senders=["greenhouse.io"], subjects=["applying"])
    msgs = list(ImapSource(cfg, connect=lambda: fake).fetch(EPOCH))
    assert len(msgs) == 1
    assert ("select", True) in fake.calls
    assert all("PEEK" in c[1] for c in fake.calls if c[0] == "fetch")
    crit = next(c for c in fake.calls if c[0] == "search")[1]
    assert crit[:2] == ("SINCE", "01-Jan-2026") and "OR" in crit and "FROM" in crit


def test_build_search_filters():
    assert build_search(EPOCH, [], []) == ["SINCE", "01-Jan-2026"]
    assert build_search(EPOCH, [], ["a"]) == ["SINCE", "01-Jan-2026", "SUBJECT", '"a"']


def test_imap_credentials_only_from_env(monkeypatch):
    for k in ("JOB_APPLY_IMAP_USER", "JOB_APPLY_IMAP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(PermissionError):
        list(ImapSource(ImapCfg(host="imap.example.test")).fetch(EPOCH))
    assert not hasattr(ImapCfg(), "password") and not hasattr(ImapCfg(), "username")
    assert imaplib  # real client is only constructed when credentials exist
