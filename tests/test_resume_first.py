"""ADR-0023: resume-first fact precedence, new lint classes, quality gate, tuning hard gates. All data fictional."""
from datetime import date

import pytest
from apply_fakes import POSTING, F, FakeAdapter, FakeRouter
from test_apply_engine import GOOD as GOOD_LETTER
from test_apply_engine import engine
from test_tuning import FakeTrainer, body, many, rec
from test_tuning import settings as tune_settings

from job_apply_mcp.apply.writing import lint, write_text
from job_apply_mcp.config import Settings, WritingCfg
from job_apply_mcp.core.models import ApplicationForm, FieldKind
from job_apply_mcp.profile import Education, Profile, Work
from job_apply_mcp.profile.facts import admit_letter_facts, facts_report, resolve_facts, resume_consistency
from job_apply_mcp.profile.resume import load_resume, parse_resume_text, temporal_phrasing
from job_apply_mcp.tuning import evalgate
from job_apply_mcp.writing import quality

TODAY = date(2026, 10, 22)
RESUME = """Jordan Lee
EXPERIENCE
Data Engineer, Sample Analytics Co        Jun 2024 - Present
- Built pipelines that cut report latency by 40% across 12 teams
- Automated nightly QA with Python and SQL
Research Assistant | Example State University   Jan 2022 - Aug 2023
- Published a 3-author study on fleet routing

EDUCATION
MS in Data Science, Example State University   Aug 2024 - May 2026
BS in Computer Science, Example State University   2020 - 2024

SKILLS
Python, SQL, Rust
"""
CFG = WritingCfg()


@pytest.fixture
def resume():
    return parse_resume_text(RESUME)


def test_parse_resume_structure_and_provenance(resume):
    exp = [f for f in resume if f.section == "experience"]
    assert exp[0].title == "Data Engineer" and exp[0].org == "Sample Analytics Co" and exp[0].current
    assert exp[0].start == "2024-06" and exp[0].end == "" and "40%" in exp[0].metrics
    assert exp[1].end == "2023-08" and not exp[1].current
    ms = next(f for f in resume if f.title.startswith("MS"))
    assert ms.end == "2026-05" and ms.org == "Example State University"
    assert all((f.origin, f.source, f.precedence) == ("user_written", "resume", 1) for f in resume)
    assert any(f.section == "skills" for f in resume)


def test_load_resume_txt_and_pdf_needs_extra(tmp_path, monkeypatch):
    p = tmp_path / "r.txt"
    p.write_text(RESUME)
    assert len(load_resume(p)) >= 4
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "pypdf":
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    (tmp_path / "r.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(RuntimeError, match=r"job-apply-mcp\[resume\]"):
        load_resume(tmp_path / "r.pdf")


def test_resume_consistency_current_student(resume):
    prof = Profile(education=[Education(school="Example State University", degree="MS", field="Data Science",
                                        end="2027-05")],
                   work=[Work(company="Sample Analytics Co", title="Data Intern", start="2024-06")])
    kinds = {c.kind for c in resume_consistency(resume, prof, TODAY)}
    assert {"status", "date", "title"} <= kinds
    clean = Profile(work=[Work(company="Sample Analytics Co", title="Data Engineer", start="2024-06")])
    assert resume_consistency(resume, clean, TODAY) == []


def test_letter_facts_additive_admitted_conflicts_excluded(resume):
    letter = (
        "Dear Hiring Team,\n"
        "I built a warehouse slotting optimizer called SlotWise that saved 18 percent of travel time.\n"
        "I worked as Data Intern at Sample Analytics Co during 2021.\n"
        "I am currently completing my Master of Science in Data Science.\n"
        "I automated nightly QA and reduced report latency by 15% across 12 teams.\n")
    ok, bad = admit_letter_facts(letter, resume, TODAY)
    assert any("SlotWise" in f.text for f in ok) and all(f.precedence == 2 for f in ok)
    reasons = {b.reason.split(":")[0] for b in bad}
    assert {"title conflict", "outdated status", "metric conflict"} <= reasons or "date conflict" in reasons
    status = next(b for b in bad if "outdated status" in b.reason)
    assert "completed 2026-05" in status.reason and "2026-05" in status.correction
    title = next(b for b in bad if b.reason.startswith("title"))
    assert "Data Engineer" in title.correction
    assert not any("SlotWise" in b.text for b in bad)


def test_resolve_facts_only_resume_first_lines_and_report(resume):
    prof = Profile(work=[Work(company="Sample Analytics Co", title="Data Intern", start="2024-06"),
                         Work(company="Other Org", title="Tutor", start="2019-01", end="2020-01")])
    letter = "I am currently pursuing my master degree in data science at Example State University."
    fs = resolve_facts(resume, prof, [letter, "I built SlotWise, a slotting optimizer that saved 18 percent."], TODAY)
    text = "\n".join(fs.lines)
    assert "Data Intern" not in text and "Tutor at Other Org" in text      # resume supersedes; unrelated profile fact kept
    assert "my master degree" not in text and "SlotWise" in text
    assert "TIMELINE" in text and "never 'currently pursuing/completing'" in text
    rep = facts_report(fs)
    assert rep["letter_facts_excluded"] == 1 and "completed" in rep["excluded"][0]["reason"]
    assert rep["conflicts"] and rep["letter_facts_admitted"] == 1


def test_temporal_phrasing(resume):
    t = "\n".join(temporal_phrasing(resume, TODAY))
    assert "current role is Data Engineer" in t and "ended 2023-08" in t and "completed 2026-05" in t


# ----------------------------------------------------------------------------------------------- lint
def kinds(text, **kw):
    return {v.kind for v in lint(text, CFG, **kw)}


LONG = GOOD_LETTER


def test_placeholders_flagged():
    for bad in ("Dear [Your Name], I write.", "Date: [Date]", "[Company Address]", "Lorem ipsum dolor sit amet.",
                "Signed XXX"):
        assert "placeholder" in kinds(bad), bad
    assert "placeholder" not in kinds("I built a scheduler.")


def test_addressee_mismatch():
    letter = "Globex Corp\n\nDear Hiring Team,\n" + LONG
    assert "addressee" in kinds(letter, company="Acme Robotics")
    assert "addressee" not in kinds("Acme Robotics\n\nDear Acme Robotics Hiring Team,\n" + LONG, company="Acme Robotics")
    assert "addressee" in kinds("Dear Initech Recruiting Team,\n" + LONG, company="Acme Robotics")
    assert "addressee" not in kinds("Dear Hiring Manager,\n" + LONG, company="Acme Robotics")


def test_temporal_contradiction_vs_resume(resume):
    bad = LONG + "\n\nI am currently completing my Master of Science in Data Science."
    assert "temporal" in kinds(bad, company="Acme Robotics", resume=resume, today=TODAY)
    assert "temporal" not in kinds(LONG + "\n\nI completed my Master of Science in May 2026.", company="Acme Robotics",
                                   resume=resume, today=TODAY)
    old = "In my current role at Example State University I lead research."
    assert "temporal" in kinds(old, resume=resume, today=TODAY)


def test_degenerate_outputs():
    assert "degenerate" in kinds("I build robust tools. I build robust tools. I build robust tools for you.")
    assert "degenerate" in kinds("Hello ![]() world of text here")
    assert "degenerate" in kinds("# Heading\n**bold** letter")
    loop = "the robots move the pallets and the robots move the pallets and the robots move the pallets again"
    assert "degenerate" in kinds(loop)
    assert "degenerate" in kinds("1 2 3 4 5 6 7 8 word " * 1 + "-- == ## 12 34")
    assert "degenerate" not in kinds(LONG, company="Acme Robotics")


def test_required_closing_and_inflation():
    cfg = WritingCfg(required_closing=[r"Sincerely,\s*Jordan Lee"])
    assert any(v.kind == "closing" for v in lint(LONG, cfg, company="Acme Robotics"))
    assert not any(v.kind == "closing" for v in lint(LONG + "\n\nSincerely,\nJordan Lee", cfg, company="Acme Robotics"))
    for ph in ("track record", "extensive experience", "high-stakes", "proven ability", "I possess"):
        assert "banned_phrase" in kinds(f"My {ph} is shown here."), ph


def test_exemplar_leakage_lint():
    ex = "Globex Corp\nMarch 3, 2024\n\nDear Hiring Team,\nI built things."
    assert "leakage" in kinds("I would join Globex Corp and ship fast.", exemplar=ex, company="Acme Robotics")
    assert "leakage" not in kinds("I would join Acme Robotics.", exemplar=ex, company="Acme Robotics")


# ----------------------------------------------------------------------------- writer prompt / never pass
async def test_writer_prompt_style_only_and_degenerate_never_pass(resume):
    ex = "Globex Corp\n\nDear Hiring Team,\nI built a thing."
    r = FakeRouter(prose=LONG)
    res = await write_text(r, CFG, kind="free_text", label="q", company="Acme Robotics", title="SWE", summary="",
                           facts=["f"], exemplar=ex, resume=resume, today=TODAY)
    prompt = r.calls[0][1]
    assert "style only" in prompt and "do not copy facts, names, addressees, dates" in prompt
    assert "completed 2026-05" in prompt and res.status == "needs_review"
    bad = "![]() " + LONG
    r2 = FakeRouter(prose=bad)
    res2 = await write_text(r2, WritingCfg(reviewer="router", max_rounds=2), kind="free_text", label="q",
                            company="Acme Robotics", title="t", summary="", facts=[])
    assert res2.status == "needs_human" and any(v.kind == "degenerate" for v in res2.violations)
    assert not any(c == "review" for c, _ in r2.calls)


# ---------------------------------------------------------------------------------------- quality gate
STRONG = (
    "I am writing about the Systems Engineer role at Acme Robotics.\n\n"
    "Your fleets lose time when dispatch is unpredictable. At Sample Analytics Co I built pipelines in Python and "
    "SQL that cut report latency by 40% across 12 teams, which is the same routing problem.\n\n"
    "I automated nightly QA with Python, so regressions reach reviewers before customers do. "
    "That discipline applies directly to your control software.\n\n"
    "Thank you for your consideration.")
WEAK = (
    "I am a hard-working team player with a wide range of skills. I am a fast learner and results-driven. "
    "I would be a dynamic value add.\n\nI have various experience and strong communication, and I work on many "
    "different things in a fast-paced way and bring best practices.\n\nI hope to make a difference.")
POSTING_SUMMARY = "We are building warehouse robots that cut order errors. You will design dispatch and routing software."
FACTS = [f.line() for f in parse_resume_text(RESUME)]


def test_scorecard_strong_passes_weak_fails():
    corpus = [STRONG, STRONG.replace("Systems Engineer", "Platform Engineer").replace("40%", "35%")]
    good = quality.scorecard(STRONG, CFG, corpus=corpus, grounding=" ".join(FACTS), posting=POSTING_SUMMARY)
    assert good["ok"], good
    weak = quality.scorecard(WEAK, CFG, corpus=corpus, grounding=" ".join(FACTS), posting=POSTING_SUMMARY)
    assert not weak["ok"] and any("evidence_density" in f or "specificity" in f for f in weak["fails"])
    assert weak["metrics"]["specificity"]["v"] < good["metrics"]["specificity"]["v"]
    assert quality.baseline(corpus)["evidence_density"]["mean"] > 0


async def test_weak_draft_fails_gate_strong_passes_with_rubric():
    cfg = WritingCfg(reviewer="router", max_rounds=2, required_closing=[])
    corpus = [STRONG]
    kw = {"kind": "cover_letter", "label": "Cover letter", "company": "Acme Robotics", "title": "SWE",
          "summary": POSTING_SUMMARY, "facts": FACTS, "corpus": corpus}
    weak = await write_text(FakeRouter(prose=WEAK), cfg, **kw)
    assert weak.status == "needs_human" and any(v.kind == "quality" for v in weak.violations) and weak.scorecard
    good_review = {"scores": {"argument": 5, "voice": 4, "precision": 5, "specificity": 4}, "at_least_as_strong": True,
                   "why": "ok", "edits": []}
    r = FakeRouter(prose=STRONG, review=good_review)
    ok = await write_text(r, cfg, **kw)
    assert ok.status == "pass" and ok.scorecard["metrics"] and ok.scorecard["rubric"]["argument"] == 5
    review_prompt = next(p for c, p in r.calls if c == "review")
    assert "STYLE ONLY" in review_prompt and "at least as strong" in review_prompt
    low = dict(good_review, scores={"argument": 2, "voice": 4, "precision": 5, "specificity": 4}, edits=["tie para 2"])
    bad = await write_text(FakeRouter(prose=STRONG, review=low), cfg, **kw)
    assert bad.status == "needs_human" and any("argument 2 < 4" in v.detail for v in bad.violations)
    # client reviewer: lint+metrics clean -> needs_review carrying the scorecard and the quality bar
    cl = await write_text(FakeRouter(prose=STRONG), WritingCfg(), **kw)
    assert cl.status == "needs_review" and "QUALITY BAR" in cl.rubric and cl.scorecard["ok"]


# --------------------------------------------------------------------------- cover-letter policy (engine)
def _form(required: bool):
    return ApplicationForm(ref=POSTING.ref, fields=[
        F("first_name", "First Name"), F("cover", "Cover Letter", FieldKind.textarea, required=required)])


@pytest.mark.parametrize(("policy", "required", "drafted"), [
    ("required_only", False, False), ("required_only", True, True),
    ("always", False, True), ("never", True, False), ("never", False, False)])
async def test_cover_letter_policy(policy, required, drafted):
    r = FakeRouter(prose=LONG)
    e, _ = engine(adapter=FakeAdapter(form=_form(required)), router=r, writing__cover_letter_policy=policy)
    out = await e.plan(POSTING.ref)
    wrote = any(c == "narrative" for c, _ in r.calls)
    assert wrote is drafted
    tasks = [t for t in out.client_tasks if t["key"] == "cover"]
    if not required and policy != "always":
        assert tasks == [] and "cover" not in out.plan.unresolved          # no draft, no client task
    if drafted:
        assert tasks and tasks[0]["draft"]
    if required and policy == "never":
        assert tasks and "draft" not in tasks[0]


def test_default_policy_is_required_only():
    assert Settings().writing.cover_letter_policy == "required_only"


# ------------------------------------------------------------------------------------- tuning hard gates
def test_degeneration_and_leakage_counters():
    assert evalgate.degenerate_count(["fine text with many distinct words here", "Ok ok ok ![]() x"]) == 1
    rows = [{"messages": [{"content": "s"}, {"content": "Write for Acme"},
                          {"content": "Globex Corp\nMarch 3, 2024\nDear Hiring Team,\nI built it."}]},
            {"messages": [{"content": "s"}, {"content": "Write for Initech"}, {"content": "Initech\nDear Team,\nHi"}]}]
    assert evalgate.leakage_count(["clean", "I admire Globex Corp deeply"], rows) == 1
    assert evalgate.leakage_count(["clean", "clean"], rows) == 0


class DegenerateTrainer(FakeTrainer):
    def generate(self, base, adapter, prompts, max_tokens):
        if adapter is None:
            return super().generate(base, adapter, prompts, max_tokens)
        return ["Great letter. Great letter. Great letter. ![]()"] * len(prompts)


class LeakyTrainer(FakeTrainer):
    def generate(self, base, adapter, prompts, max_tokens):
        if adapter is None:
            return super().generate(base, adapter, prompts, max_tokens)
        return ["Letter to Globex Corp about work done."] * len(prompts)


@pytest.mark.parametrize(("trainer", "word"), [(DegenerateTrainer(), "degenerate"), (LeakyTrainer(), "leakage")])
def test_gate_rejects_regardless_of_loss(tmp_path, trainer, word):
    from job_apply_mcp.tuning.manager import TuneManager
    src_rows = [rec(f"r{i}", ("Globex Corp\nMarch 3, 2024\nDear Hiring Team,\n" + body(40, chr(97 + i)))) for i in range(6)]

    class Src:
        def items(self, kinds=None):
            return src_rows
    m = TuneManager(tune_settings(tmp_path), Src(), trainer, battery=lambda: False, resident=lambda s: [])
    out = m.run("manual")
    assert out["status"] == "reject" and word in str(out["gate"]["reasons"]), out
    assert out["gate"]["nll_gain"] > 0.2      # loss improved a lot; still rejected


def test_many_helper_still_promotes(tmp_path):
    from job_apply_mcp.tuning.manager import TuneManager
    m = TuneManager(tune_settings(tmp_path), many(), FakeTrainer(), battery=lambda: False, resident=lambda s: [])
    assert m.run("manual")["status"] == "promote"


def test_profile_view_serves_only_resolved_facts(tmp_path):
    from job_apply_mcp.apply.adapters import ProfileView
    (tmp_path / "resume.txt").write_text(RESUME)
    (tmp_path / "letter.txt").write_text("Dear Hiring Team,\nI built SlotWise, a slotting optimizer that saved 18 percent.\n"
                                         "I am currently completing my Master of Science in Data Science.\n")
    s = Settings.model_validate({"paths": {"resume": str(tmp_path / "resume.txt"), "data_dir": str(tmp_path)},
                                 "writing": {"style_exemplar": str(tmp_path / "letter.txt")}})
    pv = ProfileView(Profile(work=[Work(company="Sample Analytics Co", title="Data Intern", start="2024-06")]), s)
    facts = "\n".join(pv.facts("slotting", 20))
    assert "SlotWise" in facts and "Data Intern" not in facts and "currently completing" not in facts
    assert "Data Engineer" in facts and pv.corpus and pv.resume_facts


def test_doctor_resume_check(tmp_path):
    from job_apply_mcp.doctor import _resume_consistency_check
    p = tmp_path / "r.txt"
    p.write_text(RESUME)
    prof = Profile(education=[Education(school="Example State University", degree="MS", end="2027-05")])
    c = _resume_consistency_check(p, prof)
    assert c.status == "warn" and "conflict" in c.detail
