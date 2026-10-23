"""ADR-0047: persona policies, gap collector, stored answers, MCP tools and CLI. Fictional profile only."""
import json
import tomllib

import pytest
from apply_fakes import POSTING, F, FakeAdapter, FakeGates, FakeStore, settings

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.engine import ApplyEngine
from job_apply_mcp.apply.mapper import map_fields
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.persona import Persona, PersonaPolicy, ProfileDB, Role, posting_scope, role_family
from job_apply_mcp.persona import cli as pcli
from job_apply_mcp.persona import tools as ptools
from job_apply_mcp.persona.keys import gap_key
from job_apply_mcp.persona.models import pick_project
from job_apply_mcp.profile import Profile, Work

TOML = """
[contact]
first_name = "Alex"
last_name = "Rivera"
email = "alex.rivera@example.com"

[[work]]
company = "Initech"
title = "Software Engineer"
start = "2018-01"
end = "2020-12"

[[work]]
company = "Hooli"
title = "Software Engineering Intern"
start = "2021-06"
end = "2021-08"

[[work]]
company = "Springfield College"
title = "Teaching Assistant"
start = "2021-09"
end = "2022-05"

[persona]
experience_counting = "Only full-time roles count; internships do not."
salary_policy = "Midpoint of the posted range, never below my floor."

[persona.exceptional_work]
projects = [
  {name = "Fleet scheduler", summary = "Dispatch optimizer for 40 warehouse robots.", keywords = ["robots", "scheduling", "python"]},
  {name = "Query planner", summary = "Cost-based planner for a toy SQL engine.", keywords = ["database", "sql", "compiler"]},
]

[persona.answers]
ai_tools = "I use an AI coding assistant for boilerplate and review every line myself."
"""


def profile(extra: str = "") -> Profile:
    return Profile.model_validate(tomllib.loads(TOML + extra))


def view(p: Profile | None = None, db: ProfileDB | None = None) -> ProfileView:
    v = ProfileView(p or profile(), Settings())
    v.persona = PersonaPolicy((p or v.p).persona, db, v.resume_projects)
    return v


# ---- persona section parsing: existing string keys are kept verbatim, nothing overwritten -----------------------
def test_string_policies_parse_as_notes_and_keep_defaults():
    p = profile().persona
    assert p.experience_counting.note.startswith("Only full-time") and p.experience_counting.mode == "full_time_only"
    assert p.salary_policy.note and p.salary_policy.mode == "midpoint"
    assert [x.name for x in p.exceptional_work.projects] == ["Fleet scheduler", "Query planner"]
    assert p.configured("experience_counting") and p.configured("salary_policy") and p.configured("exceptional_work")
    assert not Persona().configured("salary_policy")


def test_table_policies_parse_and_exceptional_work_string():
    p = Profile.model_validate({"persona": {"exceptional_work": "my best work is the scheduler",
                                           "experience_counting": {"mode": "all", "count_from": "2019-01"}}}).persona
    assert p.exceptional_work.note == "my best work is the scheduler" and p.exceptional_work.projects == []
    assert p.experience_counting.mode == "all"


# ---- experience counting -------------------------------------------------------------------------------------
def test_years_experience_counts_full_time_only_by_default():
    v = view()
    assert v.lookup("years_experience") == pytest.approx(3.0)       # Initech 2018-01..2020-12; intern + TA excluded


def test_include_internships_and_all_and_count_from():
    toml = "\n[persona.experience_counting]\nmode = \"include_internships\"\n"
    p = Profile.model_validate(tomllib.loads(TOML.split("[persona]")[0] + toml))
    assert round(view(p).lookup("years_experience"), 2) == round(3 + 3 / 12, 2)
    p2 = Profile.model_validate(tomllib.loads(TOML.split("[persona]")[0] + '\n[persona.experience_counting]\nmode="all"\n'))
    assert view(p2).lookup("years_experience") > 3.5
    p3 = Profile.model_validate(tomllib.loads(TOML.split("[persona]")[0]
                                              + '\n[persona.experience_counting]\nmode="all"\ncount_from="2021-01"\n'))
    assert view(p3).lookup("years_experience") == pytest.approx(1.0)


def test_explicit_kind_and_org_overrides():
    p = Profile(work=[Work(company="Initech", title="Research Assistant", start="2019-01", end="2019-12", kind="full_time"),
                      Work(company="Hooli", title="Engineer", start="2020-01", end="2021-01")],
                persona=Persona.model_validate({"experience_counting": {"exclude_orgs": ["Hooli"]}}))
    assert view(p).lookup("years_experience") == pytest.approx(1.0)
    assert PersonaPolicy(Persona()).counted_spans([Role("2020-01", "", "Engineer", "X")]) == [("2020-01", "")]


# ---- salary policy -----------------------------------------------------------------------------------------
def _posting(salary):
    return POSTING.model_copy(update={"salary": salary})


def test_salary_midpoint_never_below_floor_and_modes():
    cfg = Settings().policy
    pol = PersonaPolicy(Persona.model_validate({"salary_policy": {"floor": 130000}}))
    assert pol.salary(cfg, _posting("$100,000 - $160,000")).ask == 130000      # midpoint 130k, floor 130k
    assert pol.salary(cfg, _posting("$100,000 - $120,000")).ask == 120000      # floor above the top: capped at top
    mid = PersonaPolicy(Persona())
    assert mid.salary(cfg, _posting("$100,000 - $120,000")).ask == 110000
    top = PersonaPolicy(Persona.model_validate({"salary_policy": {"mode": "top"}}))
    assert top.salary(cfg, _posting("$100,000 - $120,000")).ask == 120000
    low = PersonaPolicy(Persona.model_validate({"salary_policy": {"mode": "floor", "floor": 105000}}))
    assert low.salary(cfg, _posting("$100,000 - $120,000")).ask == 105000


def test_salary_no_range_uses_configured_figure_else_none():
    cfg = Settings().policy
    assert PersonaPolicy(Persona()).salary(cfg, _posting("")) is None
    assert PersonaPolicy(Persona()).salary(cfg, _posting(""), expected=118000).ask == 118000
    fixed = PersonaPolicy(Persona.model_validate({"salary_policy": {"fixed": 121000}}))
    assert fixed.salary(cfg, _posting(""), expected=118000).ask == 121000


# ---- exceptional work ----------------------------------------------------------------------------------------
def test_exceptional_work_picks_most_relevant_project_per_posting():
    ps = profile().persona.exceptional_work.projects
    assert pick_project(ps, "Backend engineer for warehouse robots and scheduling").name == "Fleet scheduler"
    assert pick_project(ps, "SQL database compiler engineer").name == "Query planner"
    assert pick_project([], "x") is None
    v = view()
    with posting_scope("Acme", "Database Engineer", "We build a SQL query planner and compiler."):
        facts = v.facts("Describe your most impressive project", 6)
    assert facts[0].startswith("EXCEPTIONAL WORK") and "Query planner" in facts[0]
    with posting_scope("Acme", "Robotics Engineer", "Warehouse robots scheduling in python"):
        assert "Fleet scheduler" in v.facts("Exceptional work", 6)[0]
    assert not any(f.startswith("EXCEPTIONAL") for f in v.facts("Why this company?", 6))


# ---- keys & families -------------------------------------------------------------------------------------------
def test_gap_keys_and_role_families():
    assert gap_key("What AI tools do you use?", None) == "ai_tools"
    assert gap_key("Favorite build tool", None) == "q:favorite build tool"
    assert gap_key("Gender", "gender") == "gender"
    assert role_family("Senior Machine Learning Engineer") == "ml" and role_family("Chef") == "other"


# ---- DB: gaps ranking, idempotency, answers with scopes ------------------------------------------------------------
def test_gap_ranking_frequency_times_impact_and_idempotent_rows():
    db = ProfileDB(":memory:")
    for i in range(2):
        db.record_hit("q:often", "Often asked", ref=f"r{i}", kind="unresolved", company="A", title="T")
    db.record_hit("q:blocker", "Blocks one", ref="r0", kind="unresolved", blocked=True, company="B", title="U")
    db.record_hit("q:blocker", "Blocks one", ref="r1", kind="unresolved", blocked=True, company="C", title="U")
    db.record_hit("gender", "Gender", ref="r0", kind="default", default_used="Decline", company="A", title="T")
    db.record_hit("gender", "Gender", ref="r0", kind="default", default_used="Decline", company="A", title="T")
    rows = db.questions(5)
    assert [r["key"] for r in rows] == ["q:blocker", "q:often", "gender"]      # 2x3=6 > 2x2=4 > 1
    assert rows[0]["blocked"] == 2 and rows[2]["n"] == 1 and rows[2]["default_used"] == "Decline"
    assert len(rows[1]["examples"]) == 2
    db.clear_ref("r0", keep={"gender"})
    assert {r["key"]: r["n"] for r in db.questions(5)} == {"q:blocker": 1, "q:often": 1, "gender": 1}


def test_answers_scopes_most_specific_wins_and_validation():
    db = ProfileDB(":memory:")
    db.record_hit("q:x", "X", ref="r", kind="unresolved")
    db.set_answer("q:x", "global answer")
    db.set_answer("q:x", "acme answer", "company", "Acme Robotics, Inc.")
    db.set_answer("q:x", "ml answer", "role_family", "ML")
    assert db.answer("q:x") == "global answer"
    assert db.answer("q:x", company="ACME Robotics") == "acme answer"
    assert db.answer("q:x", company="Other", family="ml") == "ml answer"
    assert db.questions(5) == []                                  # answered gaps leave the queue
    assert db.all_answers()[0]["origin"] == "user_written"
    with pytest.raises(ValueError):
        db.set_answer("q:x", "v", "company", "")
    with pytest.raises(ValueError):
        db.set_answer("q:x", "v", "planet", "z")


# ---- mapper + ProfileView enforcement -------------------------------------------------------------------------------
def test_mapper_claims_free_form_question_from_stored_and_persona_answers():
    db = ProfileDB(":memory:")
    v = view(db=db)
    db.set_answer("q:favorite build tool", "Bazel")
    flds = [F("a", "Favorite build tool", max_length=40), F("b", "What AI tools do you use?", FieldKind.textarea)]
    r = map_fields(flds, v, persona=v.persona)
    got = {a.key: (a.value, a.source) for a in r.answers}
    assert got["a"] == ("Bazel", "persona:answer") and "AI coding assistant" in got["b"][0]
    assert map_fields(flds, v).unresolved                               # without the persona hook: unchanged behavior


def test_demographics_come_from_profile_only_even_for_human_answers():
    db = ProfileDB(":memory:")
    v = view(db=db)
    db.set_answer("gender", "Woman", origin="user_written")
    assert v.lookup("gender") == "Decline to self-identify"             # default and deliberate decline are untouched
    p = profile('\n[demographics]\ngender = "Man"\n')
    assert view(p, db).lookup("gender") == "Man"


def test_scoped_stored_answer_uses_posting_context():
    db = ProfileDB(":memory:")
    v = view(db=db)
    db.set_answer("q:favorite build tool", "Bazel", "company", "Acme Robotics")
    f = F("t", "Favorite build tool")
    assert map_fields([f], v, persona=v.persona).unresolved
    with posting_scope("Acme Robotics Inc", "Engineer"):
        assert map_fields([f], v, persona=v.persona).answers[0].value == "Bazel"


def test_persona_question_answers_overlay_the_policy():
    db = ProfileDB(":memory:")
    bare = Profile(work=[Work(company="Initech", title="Intern", start="2020-01", end="2021-01")])
    v = ProfileView(bare, Settings())
    v.persona = PersonaPolicy(bare.persona, db, v.resume_projects)
    assert v.lookup("years_experience") is None                           # intern excluded by default
    assert not v.persona.configured("experience_counting")
    db.set_answer("persona.experience_counting", "include_internships")
    assert v.lookup("years_experience") == pytest.approx(1.08, abs=0.01) and v.persona.configured("experience_counting")
    db.set_answer("persona.salary_no_range", "$125,000")
    assert v.persona.salary(Settings().policy, _posting("")).ask == 125000
    db.set_answer("persona.exceptional_work", "Tiny OS: a toy kernel in Rust")
    assert v.persona.projects()[0].name == "Tiny OS"


# ---- engine: gap collection end to end --------------------------------------------------------------------------------
def _engine(db, fields, posting=POSTING, p=None):
    pv = view(p or profile(), db)
    pv.resume_path = None
    ad = FakeAdapter(form=ApplicationForm(ref=posting.ref, submit_via=["api"], fields=fields), posting=posting)
    e = ApplyEngine(settings(), resolve_adapter=lambda r: ad, profile=pv, router=None, gates=FakeGates(), store=FakeStore())
    e.persona, e.gaps = pv.persona, db
    return e


async def test_plan_records_unresolved_and_default_used_gaps():
    db = ProfileDB(":memory:")
    fields = [F("fn", "First Name"), F("tool", "Favorite build tool", max_length=40),
              F("g", "Gender", FieldKind.select, options=["Male", "Female", "Decline To Self Identify"]),
              F("yrs", "Years of professional experience", FieldKind.number),
              F("sal", "Desired salary", FieldKind.number)]
    no_salary = Profile.model_validate(tomllib.loads(TOML.replace(
        'salary_policy = "Midpoint of the posted range, never below my floor."', "")))
    e = _engine(db, fields, p=no_salary)
    out = await e.plan(POSTING.ref)
    rows = {r["key"]: r for r in db.questions(10)}
    assert "q:favorite build tool" in rows and rows["q:favorite build tool"]["examples"] == ["Acme Robotics - Software Engineer"]
    assert "gender" not in rows                                          # EEO defaults are never collected
    assert "persona.salary_no_range" in rows and "persona.experience_counting" not in rows   # experience is configured
    assert out.plan.unresolved == ["tool"] or "tool" in out.plan.unresolved
    again = await e.plan(POSTING.ref)                                    # re-plan: no double counting
    assert {r["key"]: r["n"] for r in db.questions(10)}["q:favorite build tool"] == 1 and again


async def test_blocked_required_field_outranks_and_answers_apply_to_next_plan():
    db = ProfileDB(":memory:")
    fields = [F("tool", "Favorite build tool", required=True, max_length=40),
              F("opt", "Preferred editor", required=False, max_length=40)]
    e = _engine(db, fields)
    await e.plan(POSTING.ref)
    rows = db.questions(5)
    assert rows[0]["key"] == "q:favorite build tool" and rows[0]["blocked"] == 1
    ptools.store_answer(_settings_for(db), "q:favorite build tool", "Bazel")
    out = await e.plan(POSTING.ref)
    assert {a.key: a.value for a in out.plan.answers}["tool"] == "Bazel"
    assert all(r["key"] != "q:favorite build tool" for r in db.questions(5))


def _settings_for(db, monkeypatch=None):
    s = Settings()
    s.paths.data_dir = ":memory:"
    s.apply.approval_mode = "agent"
    ptools.shared = lambda _d: db            # route the tools to the test database
    return s


async def test_salary_field_uses_persona_policy_and_range():
    db = ProfileDB(":memory:")
    e = _engine(db, [F("sal", "Desired salary", FieldKind.number)], posting=_posting("$100,000 - $120,000"))
    out = await e.plan(POSTING.ref)
    assert {a.key: a.value for a in out.plan.answers}["sal"] == "110000"
    assert db.questions(5) == []


# ---- MCP tools, elicitation and CLI -----------------------------------------------------------------------------------
class _Svc:
    def __init__(self, s):
        self.settings = s


class _Res:
    def __init__(self, action, data=None):
        self.action, self.data = action, data


class _Ctx:
    def __init__(self, answers=None, fail=False):
        self.answers, self.fail, self.calls = answers or {}, fail, 0

    async def elicit(self, message, schema):
        self.calls += 1
        if self.fail:
            raise RuntimeError("no elicitation")
        return _Res("accept", schema(**self.answers))


@pytest.fixture
def seeded(monkeypatch):
    db = ProfileDB(":memory:")
    s = _settings_for(db)
    for i in range(2):
        db.record_hit("q:favorite build tool", "Favorite build tool", ref=f"r{i}", kind="unresolved", blocked=True,
                      company="Acme", title="Engineer")
    db.record_hit("work_authorized", "Authorized to work?", ref="r0", kind="unresolved", company="Acme", title="Engineer")
    monkeypatch.setattr(ptools, "_asked", False)
    return db, s


async def test_tools_questions_and_answer(seeded):
    from mcp.server.fastmcp import FastMCP
    _db, s = seeded
    m = FastMCP("t")
    ptools.register(m, lambda: _Svc(s))
    names = {t.name for t in await m.list_tools()}
    assert {"profile_questions", "answer_profile_question"} <= names
    q = json.loads((await m.call_tool("profile_questions", {"limit": 1}))[0][0].text)["questions"]
    assert len(q) == 1 and q[0]["key"] == "q:favorite build tool" and q[0]["n"] == 2


async def test_ask_at_start_runs_once_and_saves_answers(seeded):
    db, s = seeded
    ctx = _Ctx({"a0": "Bazel", "a1": ""})
    assert await ptools.ask_at_start(ctx, lambda: _Svc(s)) == 1
    assert await ptools.ask_at_start(ctx, lambda: _Svc(s)) == 0 and ctx.calls == 1       # once per session
    assert db.answer("q:favorite build tool") == "Bazel"
    assert db.gap("work_authorized")["status"] == "skipped"
    assert db.questions(5) == []


async def test_ask_at_start_without_elicitation_or_when_disabled(seeded, monkeypatch):
    db, s = seeded
    assert await ptools.ask_at_start(_Ctx(fail=True), lambda: _Svc(s)) == 0
    assert db.questions(5)                                                   # nothing lost
    monkeypatch.setattr(ptools, "_asked", False)
    s.persona.ask_on_start = False
    ctx = _Ctx({})
    assert await ptools.ask_at_start(ctx, lambda: _Svc(s)) == 0 and ctx.calls == 0


def test_cli_ask_list_answer(seeded, capsys):
    db, s = seeded
    assert pcli.cli(["list"], s) == 0 and "Favorite build tool" in capsys.readouterr().out
    assert pcli.cli(["ask"], s, is_tty=False) == 2
    replies = iter(["Bazel", ""])
    assert pcli.cli(["ask"], s, input_fn=lambda _p: next(replies), is_tty=True) == 0
    assert db.answer("q:favorite build tool") == "Bazel" and db.gap("work_authorized")["status"] == "skipped"
    assert pcli.cli(["answer", "work_authorized", "Yes"], s, is_tty=True) == 0
    assert db.answer_row("work_authorized") == ("Yes", "user_written")
    assert pcli.cli(["answer", "work_authorized", "Yes"], s, is_tty=False) == 2        # not from a script
    assert pcli.cli(["answer", "k", "x", "--scope", "company"], s, is_tty=True) == 2


def test_cli_routes_profile_subcommand():
    from job_apply_mcp.cli import COMMANDS, build_parser
    assert "profile" in COMMANDS and build_parser().parse_args(["profile", "list"]).cmd == "profile"


def test_owner_style_profile_roundtrip_is_not_rewritten(tmp_path):
    """Seeding = parsing: loading a profile never rewrites the file, and the persona strings survive intact."""
    f = tmp_path / "profile.toml"
    f.write_text(TOML, encoding="utf-8")
    before = f.read_bytes()
    from job_apply_mcp.profile import load_profile
    p = load_profile(f)
    assert p.persona.experience_counting.note == "Only full-time roles count; internships do not."
    assert f.read_bytes() == before


# ---- review rounds: only the human writes answers; the hook has a positive allowlist ---------------------------------
async def _call(s, name, args):
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("t")
    ptools.register(m, lambda: _Svc(s))
    return json.loads((await m.call_tool(name, args))[0][0].text)


async def test_answer_tool_takes_no_value_and_stores_nothing_without_elicitation(seeded):
    db, s = seeded
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("t")
    ptools.register(m, lambda: _Svc(s))
    tool = next(t for t in await m.list_tools() if t.name == "answer_profile_question")
    assert "value" not in tool.inputSchema["properties"]
    r = await _call(s, "answer_profile_question", {"key": "work_authorized"})
    assert r["ok"] is False and db.all_answers() == []
    assert (await _call(s, "answer_profile_question", {"key": "race"}))["ok"] is False
    assert db.all_answers() == []


async def test_answer_tool_stores_what_the_human_typed(seeded, monkeypatch):
    db, s = seeded

    async def typed(ctx, key, question, scope, sv):
        return "typed by the human"
    monkeypatch.setattr(ptools, "_ask_value", typed)
    r = await _call(s, "answer_profile_question", {"key": "work_authorized", "scope": "company:Acme"})
    assert r["origin"] == "user_written" and db.answer_row("work_authorized", company="acme") == ("typed by the human", "user_written")

    async def blank(ctx, key, question, scope, sv):
        return ""
    monkeypatch.setattr(ptools, "_ask_value", blank)
    assert (await _call(s, "answer_profile_question", {"key": "x"}))["ok"] is False


def test_db_refuses_non_human_origin():
    with pytest.raises(ValueError):
        ProfileDB(":memory:").set_answer("k", "v", origin="client")


BLOCKED = ["Have you ever been charged with a crime?", "I understand that false statements may lead to termination",
           "Do you confirm the above is accurate?", "Do you accept our code of conduct?",
           "Do you give permission for a background screen?", "Can we contact you by WhatsApp?",
           "Do you allow us to keep your data for 2 years?", "Did a chatbot help you write this?",
           "Can we call your references?", "Are you willing to relocate?",
           "I certify that the information provided is true and complete", "Do you agree to the terms and conditions?",
           "Do you consent to receive SMS messages?", "Was AI used to write this application?",
           "Have you ever been convicted of a felony?", "Are you a U.S. person as defined by ITAR?",
           "Do you identify as transgender?", "What is your race?", "Do you agree to binding arbitration?",
           "I attest that my Python answers are accurate", "Do you certify you have 5 years of Python?",
           "Do you consent to remote monitoring?", "Do you identify as LGBTQ+?"]


@pytest.mark.parametrize("label", BLOCKED)
def test_stored_and_persona_answers_never_answer_blocked_labels(label):
    from job_apply_mcp.persona.keys import normalize_question
    db = ProfileDB(":memory:")
    p = Profile(persona=Persona(answers={"python": "Yes", "remote": "Yes", normalize_question(label): "Yes"}))
    v = view(p, db)
    db.set_answer("q:" + normalize_question(label)[:120], "Yes", origin="user_written")
    for ctype in (FieldKind.text, FieldKind.textarea):
        r = map_fields([F("f", label, ctype)], v, persona=v.persona, stop_phrases=["certify"])
        assert not r.answers, label


def test_hook_allowlist_and_whole_question_match():
    p = Profile(persona=Persona(answers={"favorite build tool": "Bazel", "favorite color": "Blue",
                                         "what is your favorite build tool": "Bazel too"}))
    db = ProfileDB(":memory:")
    v = view(p, db)
    db.set_answer("q:favorite color", "Blue", origin="user_written")
    assert not map_fields([F("f", "Favorite color")], v, persona=v.persona).answers          # not on the allowlist
    assert not map_fields([F("f", "Describe your favorite build tool experience")], v, persona=v.persona).answers
    got = map_fields([F("f", "Favorite build tool", max_length=40)], v, persona=v.persona).answers
    assert got and got[0].value == "Bazel" and got[0].source == "persona:answer"


def test_hook_runs_after_mapper_guards_consent_rescoping():
    """A privacy-notice label reaches the mapper's own rules first (ADR-0038); a stored q: answer cannot pre-empt it."""
    db = ProfileDB(":memory:")
    v = view(db=db)
    lbl = "Gender (see our Privacy Notice)"
    db.set_answer("q:" + "gender see our privacy notice", "Yes", origin="user_written")
    r = map_fields([F("f", lbl, FieldKind.select, options=["Male", "Female", "Decline to self-identify"])], v, persona=v.persona)
    base = map_fields([F("f", lbl, FieldKind.select, options=["Male", "Female", "Decline to self-identify"])], v)
    assert [a.value for a in r.answers] == [a.value for a in base.answers]     # identical with and without the hook
    assert "Yes" not in [a.value for a in r.answers]


def test_salary_profile_floor_cannot_lower_policy_floor():
    cfg = Settings().policy.model_copy(update={"salary_floor": 120000})
    pol = PersonaPolicy(Persona.model_validate({"salary_policy": {"floor": 50000}}))
    assert pol.salary(cfg, _posting("$100,000 - $140,000")).ask == 120000


def test_collector_skips_blocked_and_demographic_gaps():
    from job_apply_mcp.persona.collector import collect_gaps
    db = ProfileDB(":memory:")
    fl = [F("a", "I certify the information is true", FieldKind.text), F("b", "What is your race?", FieldKind.text),
          F("c", "Favorite build tool", FieldKind.text), F("d", "Will you require visa sponsorship?", FieldKind.text)]
    collect_gaps(db, None, None, ref="r", posting=POSTING, fields=fl, answers=[], unresolved=["a", "b", "c", "d"])
    assert {r["key"] for r in db.questions(10)} == {"q:favorite build tool", "requires_sponsorship"}
    collect_gaps(db, None, None, ref="r2", posting=POSTING, fields=[F("t", "Which build tool do you use?")],
                 answers=[], unresolved=["t"])
    assert "q:which build tool do you use" in {r["key"] for r in db.questions(10)}
