"""ADR-0064: the candidate journal. Fictional candidate only (Alex Rivera, Springfield, Acme Robotics)."""
import asyncio
import json
import os
import stat

import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import map_fields
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.journal import CandidateJournal, redact_secrets, risk_class
from job_apply_mcp.journal.overlay import JournalView, journal_view
from job_apply_mcp.journal.tools import CLIENT_INSTRUCTIONS, list_facts, record
from job_apply_mcp.profile import Authorization, Contact, Preferences, Profile

YN = ["Yes", "No"]
RELOC = "Are you willing to relocate for this role?"
SPONS = "Will you now or in the future require sponsorship to work in the United States?"


def jr(tmp_path):
    return CandidateJournal(tmp_path / "state")


def view(prefs=None, **auth):
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city="Springfield", region="IL",
                                               country="United States"),
                               preferences=prefs or Preferences(), authorization=Authorization.model_validate(auth)),
                       Settings())


def mapped(j, label, p=None, job="US", company="Acme Robotics", title="Engineer", options=YN, kind=FieldKind.select):
    f = F("q", label, kind, options=list(options) if options else None)
    r = map_fields([f], p or view(), company=company, job_country=job,
                   journal=journal_view(j, company, title, job))
    return {a.key: (a.value, a.source) for a in r.answers}.get("q"), r


# ---------------------------------------------------------------- append-only store
def test_record_is_dated_append_only_and_private(tmp_path):
    j = jr(tmp_path)
    e, warn = j.record("I would relocate for the right team.", key="preferences.relocate", value="yes",
                       quote="yes, I'd relocate", client="test-client")
    assert not warn and e.key == "preferences.willing_to_relocate" and e.date == e.ts[:10]
    assert e.risk == "everyday" and e.usable_for_forms()
    if os.name == "posix":
        assert stat.S_IMODE(j.path.stat().st_mode) == 0o600
    lines = j.path.read_text().splitlines()
    t = j.retract(e.id, "changed my mind")
    after = j.path.read_text().splitlines()
    assert after[:len(lines)] == lines and len(after) == len(lines) + 1        # nothing rewritten
    assert json.loads(after[-1])["op"] == "retract" and json.loads(after[-1])["retracts"] == e.id
    assert j.entries() == [] and j.entries(include_retracted=True)[0].retracted_at == t.ts
    with pytest.raises(ValueError):
        j.retract(e.id)
    with pytest.raises(ValueError):
        j.retract("cj_missing")


def test_capture_is_never_blocked_by_bad_metadata(tmp_path):
    e, warn = jr(tmp_path).record("Prefers mornings for interviews", key="Not A Key!", scope="planet:mars",
                                  source="carrier pigeon", confidence="sure")
    assert e.key == "" and e.scope.startswith("invalid:") and e.source == "chat" and e.confidence == "inferred"
    assert len(warn) == 4 and not e.usable_for_forms()


# ---------------------------------------------------------------- secrets
CARD = "4111" + " 1111" * 3                     # the well-known test card number (Luhn-valid)


@pytest.mark.parametrize("text,kind", [
    ("my password is Tr0ub4dor", "password"),
    ("the verification code was 482913", "otp"),
    ("token: " + "ghp_" + "a1" * 18, "token"),
    ("SSN 123-45-6789 for the background form", "ssn"),
    (f"pay with {CARD}", "card"),
])
def test_secrets_are_redacted(text, kind):
    out, kinds = redact_secrets(text)
    assert kind in kinds and "[redacted:" in out
    for secret in ("Tr0ub4dor", "482913", "a1a1a1", "6789", "1111 1111"):
        assert secret not in out


def test_ordinary_facts_are_not_redacted():
    for text in ("I have 5 years of Python and lived in Springfield since 2019.",
                 "Phone screens work best on 2026-11-03; my zip is 62701.",
                 "See https://github.com/example/project for the demo"):
        assert redact_secrets(text) == (text, [])


def test_secret_keys_and_secret_only_facts_are_refused(tmp_path):
    j = jr(tmp_path)
    with pytest.raises(ValueError):
        j.record("my login", key="accounts.password", value="x")
    with pytest.raises(ValueError):
        j.record("password: hunter22")
    e, _ = j.record("Workday login for Acme uses password: hunter22", value="password: hunter22")
    assert "hunter22" not in j.path.read_text() and e.redacted == ["password"] and e.value == ""


# ---------------------------------------------------------------- risk classes and confirmation
def test_risk_classes():
    assert risk_class("preferences.willing_to_relocate") == "everyday"
    assert risk_class("authorization.US.requires_sponsorship") == "sensitive"
    assert risk_class("demographics.veteran") == "sensitive"
    assert risk_class("", "I have a security clearance") == "sensitive"
    assert risk_class("preferences.willing_to_relocate", "only if the visa transfers") == "sensitive"
    assert risk_class("attestation.truthful", "") == "attestation"
    assert risk_class("", "I certify my answers are true and complete") == "attestation"


def test_sensitive_gate(tmp_path):
    j = jr(tmp_path)
    k = "authorization.us.requires_sponsorship"
    quoted, _ = j.record("Needs sponsorship in the US", key=k, value="yes", quote="I will need H-1B sponsorship")
    assert quoted.key == "authorization.US.requires_sponsorship" and quoted.confirmed_via == "client_quote"
    no_quote, _ = j.record("Needs sponsorship in the US", key=k, value="yes")
    inferred, _ = j.record("Probably needs sponsorship", key=k, value="yes", quote="I'm on a visa",
                           confidence="inferred")
    doc, _ = j.record("Needs sponsorship", key=k, value="yes", quote="x", source="document")
    declined, _ = j.record("Needs sponsorship", key=k, value="yes", quote="x", elicited=False)
    dialog, _ = j.record("Needs sponsorship", key=k, value="yes", source="document", elicited=True)
    assert [x.usable_for_forms() for x in (quoted, no_quote, inferred, doc, declined, dialog)] == \
        [True, False, False, False, False, True]
    assert dialog.confirmed_via == "elicitation"
    att, _ = j.record("I certify everything is true", key="attestation.truthful", value="yes", quote="yes",
                      elicited=True)
    assert not att.usable_for_forms()


# ---------------------------------------------------------------- mapper: after the profile, with provenance
def test_journal_fills_after_profile_with_provenance(tmp_path):
    j = jr(tmp_path)
    assert mapped(j, RELOC)[0] is None                       # nothing known: unresolved
    e, _ = j.record("Alex will relocate", key="preferences.willing_to_relocate", value="yes", quote="sure, I'd move")
    got, _ = mapped(j, RELOC)
    assert got == ("Yes", f"journal:{e.id}@{e.date}")


def test_profile_value_wins_when_equal_and_conflict_is_flagged(tmp_path):
    j = jr(tmp_path)
    j.record("Alex will relocate", key="preferences.willing_to_relocate", value="yes", quote="yes")
    same = view(prefs=Preferences(willing_to_relocate=True))
    assert mapped(j, RELOC, p=same)[0] == ("Yes", "rule:willing_to_relocate")
    other = view(prefs=Preferences(willing_to_relocate=False))
    got, r = mapped(j, RELOC, p=other)
    assert got is None and [f.key for f in r.unresolved] == ["q"]
    c = r.hints["q"]["conflict"]["values"][0]
    assert c["profile"] == "No" and c["journal"] == "Yes" and c["entry"].startswith("cj_")


def test_newest_entry_wins_and_retraction_removes_it(tmp_path):
    j = jr(tmp_path)
    j.record("Alex will relocate", key="preferences.willing_to_relocate", value="yes", quote="yes")
    no, _ = j.record("Correction: no relocation", key="preferences.willing_to_relocate", value="no", quote="no",
                     source="correction")
    assert mapped(j, RELOC)[0][0] == "No"
    j.retract(no.id)
    assert mapped(j, RELOC)[0][0] == "Yes"


def test_sensitive_journal_facts_need_confirmation_to_fill(tmp_path):
    j = jr(tmp_path)
    j.record("Needs sponsorship", key="authorization.US.requires_sponsorship", value="yes",
             quote="I'm on OPT", confidence="inferred")
    j.record("Authorized in the US", key="authorization.US.work_authorized", value="yes")   # no quote: unconfirmed
    assert mapped(j, SPONS)[0] is None
    e, _ = j.record("Needs sponsorship", key="authorization.US.requires_sponsorship", value="yes",
                    quote="yes, I will need sponsorship")
    assert mapped(j, SPONS)[0] == ("Yes", f"journal:{e.id}@{e.date}")
    # a country-scoped fact never answers for another country's job
    assert mapped(j, "Will you now or in the future require sponsorship to work in Canada?", job="CA")[0] is None


def test_scoped_entries(tmp_path):
    j = jr(tmp_path)
    j.record("Would relocate only for Acme", key="preferences.willing_to_relocate", value="yes",
             scope="company:Acme Robotics Inc", quote="for Acme, yes")
    j.record("Generally not relocating", key="preferences.willing_to_relocate", value="no", quote="no")
    assert mapped(j, RELOC)[0][0] == "Yes"                     # company scope beats global
    assert mapped(j, RELOC, company="Globex")[0][0] == "No"


def test_attestation_wording_is_never_filled_from_journal(tmp_path):
    j = jr(tmp_path)
    j.record("Alex will relocate", key="preferences.willing_to_relocate", value="yes", quote="yes")
    assert mapped(j, "I certify that I am willing to relocate")[0] is None


def test_view_ignores_unmapped_and_empty(tmp_path):
    j = jr(tmp_path)
    assert journal_view(j) is None                           # no file yet
    j.record("Worked at Example Corp 2019-2021", key="employment.prior_employers", value="Example Corp")
    v = journal_view(j)
    assert isinstance(v, JournalView) and v.index == {}


# ---------------------------------------------------------------- stories
def test_stories_are_essay_material(tmp_path):
    j = jr(tmp_path)
    j.record("Led the migration of a billing system to event sourcing; cut reconciliation time in half.",
             kind="story", value="ignored")
    j.record("Mentored three interns on robotics testing.", kind="story", scope="company:Globex")
    rows = j.stories("Describe a migration you led", company="Acme Robotics")
    assert len(rows) == 1 and "billing" in rows[0]["text"]
    assert j.entries()[0].value == "" and not j.entries()[0].usable_for_forms()
    assert len(j.stories("robotics", company="Globex")) == 2


# ---------------------------------------------------------------- tools
class _Res:
    def __init__(self, action, correct=None):
        self.action = action
        self.data = None if correct is None else type("D", (), {"correct": correct})()


class _Ctx:
    def __init__(self, res=None, boom=False):
        self.res, self.boom, self.asked = res, boom, []

    async def elicit(self, message, schema):
        self.asked.append(message)
        if self.boom:
            raise RuntimeError("elicitation not supported")
        return self.res


def _settings(tmp_path):
    s = Settings()
    s.paths.state_dir = str(tmp_path / "state")
    return s


def test_record_tool_elicits_only_sensitive_facts(tmp_path):
    s = _settings(tmp_path)
    ctx = _Ctx(_Res("accept", True))
    out = asyncio.run(record(s, ctx, fact="Lives in Springfield", key="contact.city", value="Springfield"))
    assert out["ok"] and out["usable"] and ctx.asked == []
    out = asyncio.run(record(s, ctx, fact="US citizen", key="authorization.US.citizen", value="yes",
                             source="document"))
    assert out["confirmed_via"] == "elicitation" and out["usable"] and len(ctx.asked) == 1
    no_dialog = _Ctx(boom=True)
    out = asyncio.run(record(s, no_dialog, fact="US citizen", key="authorization.US.citizen", value="yes",
                             quote="I'm a US citizen"))
    assert out["confirmed_via"] == "client_quote"
    out = asyncio.run(record(s, _Ctx(_Res("decline")), fact="US citizen", key="authorization.US.citizen",
                             value="yes", quote="I'm a US citizen"))
    assert not out["usable"] and "note" in out
    out = asyncio.run(record(s, None, fact="password: hunter22"))
    assert out["ok"] is False and "hunter22" not in json.dumps(out)
    lst = list_facts(s, key_prefix="authorization.", limit=2)
    assert lst["total"] == 3 and len(lst["rows"]) == 2 and lst["next"] == 2


def test_server_registers_tools_and_instructions():
    from job_apply_mcp import server
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"record_candidate_fact", "list_candidate_facts", "retract_candidate_fact"} <= names
    assert CLIENT_INSTRUCTIONS in (server.mcp.instructions or "")
    assert "record_candidate_fact" in CLIENT_INSTRUCTIONS
