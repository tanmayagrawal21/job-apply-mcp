from apply_fakes import FORM
from tests_helpers import settings_for_driver

from job_apply_mcp.apply.browser_playwright import (
    ATS,
    detect_ats,
    detect_blocker,
    detect_confirmation,
    fill_order,
    greenhouse_embed_url,
    same_value,
    stop_phrase_hit,
)
from job_apply_mcp.apply.browser_use_driver import RULES, BrowserUseDriver, build_task, parse_result
from job_apply_mcp.core.models import FieldKind


def test_detect_ats_and_embed_url():
    assert detect_ats("https://job-boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_ats("https://jobs.lever.co/acme/1/apply") == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/acme/1/application") == "ashby"
    assert detect_ats("https://careers.acme.com/job?gh_jid=5") == "greenhouse"
    assert detect_ats("https://acme.icims.com/jobs/1") is None
    assert greenhouse_embed_url("https://careers.acme.com/job?gh_jid=5", "greenhouse:acme:5") == \
        "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=5"
    assert greenhouse_embed_url("http://localhost:9/form", "greenhouse:acme:5") == "http://localhost:9/form"


def test_confirmation_needs_url_or_text_with_form_gone():
    gh, ash = ATS["greenhouse"], ATS["ashby"]
    assert detect_confirmation(gh, "https://x.io/acme/jobs/1/confirmation", "", True)
    assert detect_confirmation(ATS["lever"], "https://jobs.lever.co/a/1/thanks", "", True)
    assert not detect_confirmation(ash, "https://jobs.ashbyhq.com/a/1/application",
                                   "Application Submitted! (extension overlay)", True)
    assert detect_confirmation(ash, "https://jobs.ashbyhq.com/a/1/application", "Application submitted", False)
    assert not detect_confirmation(gh, "https://x.io/apply", "Fill in your details", False)


def test_blockers_and_stop_phrases():
    assert detect_blocker("Please verify you are human") == "captcha"
    assert detect_blocker("Enter the 8-character security code we sent") == "verification"
    assert detect_blocker("candidates are eligible to apply once every 60 days") == "duplicate"
    assert detect_blocker("Upload your resume") is None
    assert stop_phrase_hit("This is my own work and no AI was used", ["my own work"]) == "my own work"
    assert stop_phrase_hit("casino AIs", ["no ai"]) is None


def test_fill_order_puts_files_last_and_values_compare():
    rows = fill_order(FORM, {"first_name": "Ada", "q_gender": "x", "q_tool": "t"}, {"resume": "/r.pdf"})
    kinds = [f.kind for f, _ in rows]
    assert kinds[-1] == FieldKind.file and kinds.index(FieldKind.select) > kinds.index(FieldKind.text)
    assert same_value(FieldKind.text, "Phone", "(520) 555-0100", "+1 520 555 0100")
    assert same_value(FieldKind.text, "Email", "Ada@x.com ", "ada@x.com")
    assert not same_value(FieldKind.text, "Email", "a@x.com", "")


def test_agent_driver_prompt_rules_and_result_parsing():
    t = build_task("https://x.test/apply", FORM, {"first_name": "Ada"}, {"resume": "/r.pdf"}, dry_run=False)
    assert "CAPTCHA" in t and "at most ONCE" in t and "First Name" in t and "/r.pdf" in t and RULES in t
    r = parse_result('done\nRESULT: {"status": "submitted", "detail": "ok"}', "ref")
    assert r.status == "submitted" and r.via == "agent"
    assert parse_result("no result", "ref").status == "needs_human"
    assert parse_result('RESULT: {"status": "weird"}', "ref").status == "needs_human"


async def test_agent_driver_refuses_dry_run_by_default():
    drv = BrowserUseDriver(settings_for_driver())
    r = await drv.fill_and_submit("https://x.test", FORM, {}, {}, dry_run=True)
    assert r.status == "dry_run" and "not run" in r.detail
