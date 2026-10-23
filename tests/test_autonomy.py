"""ADR-0052: configurable autonomy level (strict | safe_default | infer). Fictional data only (Ada Rivera, Acme)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeProfile, FakeRouter, FakeStore, settings
from test_approvals import _cfg

from job_apply_mcp.apply import autonomy
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.persona.gaps import ProfileDB

YN = ["Yes", "No"]
VET = ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]


def form(ref=POSTING.ref):
    return ApplicationForm(ref=ref, submit_via=["api"], fields=[
        F("first_name", "First Name"), F("email", "Email"),
        F("q_contact", "Can we email or contact you with updates?", FieldKind.select, options=YN),
        F("q_call", "May we call you about other openings?", FieldKind.boolean),
        F("q_cold", "Are you comfortable making cold calls?", FieldKind.select, options=YN),
        F("q_rust", "Do you have experience with Rust?", FieldKind.select, options=YN),
        F("q_vet", "Veteran status", FieldKind.select, options=VET),
        F("q_race", "Race", FieldKind.text, required=False),
        F("q_spons", "Will you now or in the future require visa sponsorship?", FieldKind.select, options=YN),
        F("q_felony", "Have you ever been convicted of a felony?", FieldKind.select, options=YN),
        F("q_true", "I certify that the information provided is true and complete", FieldKind.boolean),
        F("q_nda", "Are you willing to sign a non-compete agreement?", FieldKind.select, options=YN),
        F("q_start", "Are you able to start within two weeks?", FieldKind.select, options=YN),
        F("q_hear", "How did you hear about us?", FieldKind.text),
        F("q_note", "Anything else you would like us to know?", FieldKind.text, required=False),
    ])


HARD = {"q_spons", "q_felony", "q_true", "q_nda", "q_start"}


class Adapter(FakeAdapter):
    async def get_form(self, ref):
        return form(ref)


def engine(level, router=None, gaps=None, tmp_path=None, **cfg):
    s = settings(answers__autonomy=level, **cfg)
    if tmp_path is not None:
        s.paths.data_dir = str(tmp_path)
    e = ApplyEngine(s, resolve_adapter=lambda ref: Adapter(), profile=FakeProfile(), router=router,
                    gates=FakeGates(), store=FakeStore())
    e.gaps = gaps
    return e


def model(**answers):
    return FakeRouter(short={k: {"value": v, "confidence": c, "rationale": "fictional"} for k, (v, c) in answers.items()})


def got(out):
    return {a.key: a for a in out.plan.answers}


# ---- strict ------------------------------------------------------------------------------------------------------
async def test_strict_is_unchanged_nothing_assumed(tmp_path):
    out = await engine("strict", model(q_cold=("Yes", 0.99)), tmp_path=tmp_path).plan(POSTING.ref)
    a = got(out)
    assert not any(x.assumed for x in a.values()) and "assumed" not in out.compact()
    for k in ("q_contact", "q_call", "q_cold", "q_rust", "q_vet", "q_hear", *HARD):
        assert k in out.plan.unresolved and k not in a
    assert not (tmp_path / "evidence").exists()


# ---- safe_default ------------------------------------------------------------------------------------------------
async def test_safe_default_declines_contact_and_eeo_and_uses_referral_default(tmp_path):
    out = await engine("safe_default", model(q_cold=("Yes", 0.99)), tmp_path=tmp_path).plan(POSTING.ref)
    a = got(out)
    assert a["q_contact"].value == "No" and a["q_contact"].assumed
    assert a["q_contact"].source == "assumed:decline_contact_permission"
    assert a["q_call"].value is False and a["q_call"].assumed                 # unticked
    assert a["q_vet"].value == "I don't wish to answer" and a["q_vet"].source == "assumed:eeo_decline"
    assert a["q_hear"].value == "Job board" and a["q_hear"].source == "assumed:referral_default"
    assert "q_race" not in a and "q_note" not in a                            # optional free text stays blank
    for k in ("q_cold", "q_rust", *HARD):                                     # capability is infer-only
        assert k not in a and k in out.plan.unresolved


async def test_safe_default_contact_without_a_declining_option_stays_unresolved():
    f = F("q", "Can we email you about future roles?", FieldKind.select, options=["Email", "Phone"])
    assert autonomy.contact_channels(f) and autonomy.decline_contact(f) is None
    f = F("q", "Can we email you about future roles?", FieldKind.select, options=["Yes, please", "No, thanks"])
    assert autonomy.decline_contact(f) == "No, thanks"


@pytest.mark.parametrize("label", ["Can we email or contact you with updates?", "May we call you about other openings?",
                                   "Would you like to receive updates about future roles?",
                                   "Do you opt-in to receive WhatsApp messages from Acme Recruiting?",
                                   "SMS opt-in", "I agree to receive text messages from Acme Robotics",
                                   "Join our talent community", "Is it OK to text you about interview scheduling?"])
def test_contact_permission_asks_are_recognised(label):
    assert autonomy.contact_channels(F("q", label, FieldKind.select, options=YN), "Acme Robotics")


NOT_ASKS = ["Would you like to receive WhatsApp training?", "Do you have experience writing newsletters?",
            "Would you like to join our marketing team?", "Would you like to own our SMS product?",
            "Can we contact your references?", "Do you want to opt out of marketing emails?",
            "I agree to receive text messages and accept the privacy policy",
            "Would you like to receive updates and agree to binding arbitration?"]


@pytest.mark.parametrize("label", NOT_ASKS)
def test_non_permission_wording_is_never_an_ask(label):
    assert autonomy.contact_channels(F("q", label, FieldKind.select, options=YN), "Acme Robotics") is None


@pytest.mark.parametrize("level", ["safe_default", "infer"])
@pytest.mark.parametrize("label", NOT_ASKS)
async def test_non_permission_wording_is_never_declined(level, label):
    class One(FakeAdapter):
        async def get_form(self, ref):
            return ApplicationForm(ref=ref, submit_via=["api"],
                                   fields=[F("q", label, FieldKind.select, options=YN)])
    e = ApplyEngine(settings(answers__autonomy=level), resolve_adapter=lambda ref: One(), profile=FakeProfile(),
                    router=model(q=("Yes", 0.99)), gates=FakeGates(), store=FakeStore())
    a = got(await e.plan(POSTING.ref)).get("q")
    assert a is None or a.source != "assumed:decline_contact_permission"
    assert a is None or not a.assumed or a.source == "assumed:infer"


@pytest.mark.parametrize("label", ["Are you comfortable making cold calls?", "Do you have experience with Twilio SMS?",
                                   "Do you have a valid email address?"])
def test_capability_or_fact_questions_are_not_permission_asks(label):
    assert autonomy.contact_channels(F("q", label, FieldKind.select, options=YN)) is None


def test_eeo_without_decline_option_is_left_blank():
    f = F("q", "Gender", FieldKind.select, options=["Male", "Female"])
    assert autonomy.eeo_field(f) and autonomy.eeo_decline(f) is None


# ---- infer -------------------------------------------------------------------------------------------------------
async def test_infer_answers_capability_conservatively(tmp_path):
    r = model(q_cold=("Yes", 0.99), q_rust=("Yes", 0.95))
    out = await engine("infer", r, tmp_path=tmp_path).plan(POSTING.ref)
    a = got(out)
    # the resume says nothing about cold calls: a confident Yes still becomes the honest No
    assert a["q_cold"].value == "No" and a["q_cold"].assumed and a["q_cold"].source == "assumed:infer"
    assert "not clearly supported" in a["q_cold"].rationale
    # the resume lists Rust: Yes is kept, with the model's confidence and rationale recorded
    assert a["q_rust"].value == "Yes" and a["q_rust"].confidence == 0.95 and a["q_rust"].rationale == "fictional"
    # everything safe_default does still happens
    assert a["q_contact"].value == "No" and a["q_vet"].value == "I don't wish to answer"
    prompt = next(p for c, p in r.calls if "ONLY when the facts clearly" in p)
    assert "cold calls" in prompt and "sponsorship" not in prompt and "felony" not in prompt
    for k in HARD:                                                              # hard limits: never decided
        assert k not in a and k in out.plan.unresolved


async def test_infer_low_confidence_yes_becomes_no():
    out = await engine("infer", model(q_cold=("No", 0.9), q_rust=("Yes", 0.4))).plan(POSTING.ref)
    a = got(out)
    assert a["q_rust"].value == "No" and a["q_cold"].value == "No"


async def test_infer_without_a_model_leaves_capability_unresolved():
    out = await engine("infer").plan(POSTING.ref)
    assert "q_cold" in out.plan.unresolved and got(out)["q_contact"].value == "No"


@pytest.mark.parametrize("level", ["strict", "safe_default", "infer"])
@pytest.mark.parametrize("label", [
    "Will you now or in the future require visa sponsorship?", "Are you authorized to work in the United States?",
    "Are you a U.S. citizen?", "Have you ever been convicted of a crime?", "Do you consent to a background check?",
    "Are you willing to take a drug test?", "I certify that my answers are true and complete",
    "Do you agree to binding arbitration?", "Are you willing to sign an NDA?", "Do you accept a non-compete?",
    "What are your salary expectations?", "Are you willing to relocate?", "When can you start?",
    "Are you able to work on site in our office?",
])
async def test_hard_limits_never_assumed_at_any_level(level, label):
    f = F("q_hard", label, FieldKind.select, options=YN)
    assert autonomy.hard_limit(f) is not None and not autonomy.capability_question(f)

    class One(FakeAdapter):
        async def get_form(self, ref):
            return ApplicationForm(ref=ref, submit_via=["api"], fields=[f])
    e = ApplyEngine(settings(answers__autonomy=level), resolve_adapter=lambda ref: One(), profile=FakeProfile(),
                    router=model(q_hard=("Yes", 0.99)), gates=FakeGates(), store=FakeStore())
    out = await e.plan(POSTING.ref)
    assert "q_hard" in out.plan.unresolved and not out.plan.answers


# ---- labelling: plan, evidence ------------------------------------------------------------------------------------
async def test_assumed_answers_labelled_in_plan_and_evidence(tmp_path):
    out = await engine("infer", model(q_cold=("Yes", 0.99)), tmp_path=tmp_path).plan(POSTING.ref)
    c = out.compact()
    assert c["assumed"]["q_contact"]["rule"] == "assumed:decline_contact_permission"
    assert c["assumed"]["q_cold"]["confidence"] == 0.99 and "why" in c["assumed"]["q_cold"]
    assert out.plan.audit["assumed"]["q_hear"]["rule"] == "assumed:referral_default"
    ev = list((tmp_path / "evidence").glob("*/assumed.json"))
    rows = {r["key"]: r for r in json.loads(ev[0].read_text())["assumed"]}
    assert rows["q_contact"]["assumed"] is True and rows["q_cold"]["rationale"]
    assert "first_name" not in rows


# ---- pattern promotion -------------------------------------------------------------------------------------------
async def test_kind_promoted_to_profile_gap_after_n_and_stops_once_answered():
    db = ProfileDB(":memory:")
    e = engine("safe_default", gaps=db, answers__promote_after=3)
    for i in (1, 2):
        out = await e.plan(f"greenhouse:acme:{i}")
        assert not [g for g in out.profile_gaps if g.get("gap")]
    assert db.assumed_count("assumed.contact.email") == 2
    await e.plan("greenhouse:acme:1")                                      # re-planning the same posting: no new count
    assert db.assumed_count("assumed.contact.email") == 2
    out = await e.plan("greenhouse:acme:3")
    gaps = {g["gap"] for g in out.profile_gaps if g.get("gap")}
    assert "assumed.contact.email" in gaps and "assumed.referral_source" in gaps
    q = {r["key"]: r for r in db.questions(limit=20)}
    assert "assumed.contact.email" in q and "Yes or No" in q["assumed.contact.email"]["question"]
    assert q["assumed.contact.email"]["default_used"].startswith("assumed:")
    db.set_answer("assumed.contact.email", "Yes")                          # the owner answers once
    a = got(await e.plan("greenhouse:acme:4"))["q_contact"]
    assert a.value == "Yes" and not a.assumed and a.source == "persona:answer"


async def test_eeo_decline_is_never_promoted():
    db = ProfileDB(":memory:")
    e = engine("safe_default", gaps=db, answers__promote_after=1)
    await e.plan("greenhouse:acme:1")
    assert not [r for r in db.questions(limit=50) if "veteran" in r["question"].lower()]


# ---- trust boundary ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("user", "proj", "env", "want"), [
    ("", '[answers]\nautonomy = "infer"\n', {}, "strict"),
    ("", "", {"JOB_APPLY__ANSWERS__AUTONOMY": "safe_default"}, "strict"),
    ('[answers]\nautonomy = "safe_default"\n', '[answers]\nautonomy = "infer"\n', {}, "safe_default"),
    ('[answers]\nautonomy = "infer"\n', '[answers]\nautonomy = "safe_default"\n', {}, "safe_default"),   # stricter ok
    ('[answers]\nautonomy = "infer"\n', "", {"JOB_APPLY__ANSWERS__AUTONOMY": "strict"}, "strict"),
    ('[answers]\nautonomy = "infer"\n', "", {}, "infer"),
])
def test_only_the_trusted_user_config_can_loosen_autonomy(tmp_path, monkeypatch, user, proj, env, want):
    assert _cfg(tmp_path, monkeypatch, user=user, proj=proj, env=env).answers.autonomy == want


def test_untrusted_user_file_cannot_loosen_autonomy(tmp_path, monkeypatch):
    s = _cfg(tmp_path, monkeypatch, user='[answers]\nautonomy = "infer"\n', via_env=True)
    assert s.answers.autonomy == "strict"


def test_project_cannot_raise_promote_after_or_lower_confidence(tmp_path, monkeypatch):
    s = _cfg(tmp_path, monkeypatch, proj="[answers]\npromote_after = 50\ninfer_min_confidence = 0.1\n")
    assert (s.answers.promote_after, s.answers.infer_min_confidence) == (3, 0.8)
    s = _cfg(tmp_path, monkeypatch, proj="[answers]\npromote_after = 1\ninfer_min_confidence = 0.95\n")
    assert (s.answers.promote_after, s.answers.infer_min_confidence) == (1, 0.95)


def test_default_is_strict():
    from job_apply_mcp.config import Settings
    assert Settings().answers.autonomy == "strict"


# ---- control_status, run report, setup wizard --------------------------------------------------------------------
async def test_control_status_shows_autonomy(tmp_path):
    from job_apply_mcp.control.agent import ControlAgent
    s = settings(answers__autonomy="infer")
    s.paths.data_dir = str(tmp_path)
    st = ControlAgent(s).status()
    assert st["autonomy"]["level"] == "infer" and st["autonomy"]["promote_after"] == 3


async def test_run_report_labels_assumed_answers(tmp_path):
    from test_intake import MultiAdapter, make

    class A(MultiAdapter):
        async def get_form(self, ref):
            f = await super().get_form(ref)
            f.fields.append(F("q_contact", "Can we email or contact you with updates?", FieldKind.select, options=YN))
            return f
    r, _, _ = make(tmp_path, adapter=A(), answers__autonomy="safe_default")
    url = "https://boards.greenhouse.io/acme/jobs/1"
    rep = await r.run(links=[url], sources=["links"])
    assert rep["autonomy"] == "safe_default" and rep["counts"] == {"dry_run_ok": 1}
    assert rep["assumed"][url] == [{"key": "q_contact", "value": "No", "rule": "assumed:decline_contact_permission",
                                    "why": "contact permission (email): declined"}]
    again = await r.report(rep["run_id"])
    assert again["assumed"] == rep["assumed"] and again["autonomy"] == "safe_default"
    r2, _, _ = make(tmp_path / "strict", adapter=A())
    rep2 = await r2.run(links=[url], sources=["links"])
    assert "assumed" not in rep2 and rep2["counts"] == {"needs_human": 1}


def test_setup_wizard_asks_and_writes_autonomy(tmp_path):
    import tomllib

    from job_apply_mcp.setup_wizard import run_init
    asked = []

    def ask(prompt, default):
        asked.append(prompt)
        return "safe_default" if "autonomy" in prompt else default
    run_init(config_dir=tmp_path, ask=ask, detect=list, ram_gb=None, out=lambda *_: None)
    assert any("autonomy" in p for p in asked)
    cfg = tomllib.loads(Path(tmp_path / "config.toml").read_text())
    assert cfg["answers"]["autonomy"] == "safe_default"
    run_init(yes=True, force=True, config_dir=tmp_path / "y", detect=list, ram_gb=None, out=lambda *_: None)
    assert tomllib.loads((tmp_path / "y" / "config.toml").read_text())["answers"]["autonomy"] == "strict"


@pytest.mark.parametrize(("level", "kinds"), [
    ("strict", set()),
    ("safe_default", {"assumed.contact.email", "assumed.contact.phone", "assumed.referral_source"}),
    ("infer", {"assumed.contact.email", "assumed.contact.phone", "assumed.referral_source",
               "assumed.q:are you comfortable making cold calls", "assumed.q:do you have experience with rust"}),
])
async def test_pattern_promotion_at_each_level(level, kinds):
    db = ProfileDB(":memory:")
    e = engine(level, model(q_cold=("Yes", 0.99), q_rust=("Yes", 0.9)), gaps=db, answers__promote_after=2)
    for i in (1, 2):
        out = await e.plan(f"greenhouse:acme:{i}")
    assert {g["gap"] for g in out.profile_gaps if g.get("gap")} == kinds
    assert {r["key"] for r in db.questions(limit=50)} >= kinds


@pytest.mark.parametrize("level", ["strict", "safe_default", "infer"])
async def test_run_report_labels_assumed_answers_at_each_level(tmp_path, level):
    from test_intake import MultiAdapter, make

    class A(MultiAdapter):
        async def get_form(self, ref):
            f = await super().get_form(ref)
            f.fields += [F("q_contact", "Can we email or contact you with updates?", FieldKind.select, options=YN),
                         F("q_cold", "Are you comfortable making cold calls?", FieldKind.select, options=YN)]
            return f
    r, _, _ = make(tmp_path, adapter=A(), router=model(q_cold=("Yes", 0.99)), answers__autonomy=level)
    url = "https://boards.greenhouse.io/acme/jobs/1"
    rep = await r.run(links=[url], sources=["links"])
    assert rep["autonomy"] == level
    rules = {x["key"]: x["rule"] for x in (rep.get("assumed") or {}).get(url, [])}
    want = {"strict": {}, "safe_default": {"q_contact": "assumed:decline_contact_permission"},
            "infer": {"q_contact": "assumed:decline_contact_permission", "q_cold": "assumed:infer"}}[level]
    assert rules == want
    assert rep["counts"] == ({"dry_run_ok": 1} if level == "infer" else {"needs_human": 1})
