"""ADR-0031: Lever custom-question cards become FormFields. Fictional data only; offline."""
import respx
from lever_card_fixtures import CARD_ID, key, page
from platform_fixtures import LEVER_ID

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import map_fields
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.platforms.lever import LeverAdapter
from job_apply_mcp.platforms.lever_cards import parse_cards
from job_apply_mcp.profile import Authorization, Preferences, Profile

K = FieldKind


def shape(fields):
    return [(f.key, f.label, f.kind, f.required, f.options) for f in fields]


EXPECT = [
    (key(0), "Why Springfield Robotics?", K.textarea, True, []),
    (key(1), "Preferred name", K.text, False, []),
    (key(2), "Do you require visa sponsorship?", K.select, True, ["Yes", "No"]),
    (key(3), "Willing to relocate?", K.select, True, ["Yes", "No"]),
    (key(4), "How did you hear about us?", K.multiselect, False, ["LinkedIn", "Referral", "Other"]),
]


def test_parse_from_json():
    assert shape(parse_cards(page())) == EXPECT


def test_dom_fallback_matches_json_result():
    assert shape(parse_cards(page(with_json=False))) == EXPECT


def test_malformed_json_falls_back_to_dom():
    broken = page(with_json=False).replace(
        "<ul><li", f'<input type="hidden" value="{{nope" name="cards[{CARD_ID}][baseTemplate]"><ul><li', 1)
    assert shape(parse_cards(broken)) == EXPECT


def test_no_cards():
    assert parse_cards("<html><body><form><input name='name'></form></body></html>") == []


def _view(**auth):
    return ProfileView(Profile(authorization=Authorization(work_authorized=True, **auth),
                               preferences=Preferences(willing_to_relocate=True)), Settings())


def test_mapper_fills_standard_questions_and_defers_prose():
    r = map_fields(parse_cards(page()), _view(requires_sponsorship=True), company="Springfield Robotics")
    got = {a.key: a.value for a in r.answers}
    assert got[key(2)] == "Yes" and got[key(3)] == "Yes"             # exact input names, option values as rendered
    assert key(0) in [u.key for u in r.unresolved]                    # prose is left for the client task


@respx.mock
async def test_get_form_appends_cards_after_standard_fields(http):
    respx.get(f"https://jobs.lever.co/democo/{LEVER_ID}/apply").respond(text=page().replace("<form>", '<form><input name="phone"><input name="org"><input name="urls[LinkedIn]">'))
    form = await LeverAdapter(http, env={}).get_form(f"lever:democo:{LEVER_ID}")
    keys = [f.key for f in form.fields]
    assert "urls[GitHub]" not in keys                               # page renders no GitHub input
    assert keys[:3] == ["name", "email", "phone"] and keys[-5:] == [e[0] for e in EXPECT]


@respx.mock
async def test_get_form_survives_apply_page_failure(http):
    respx.get(f"https://jobs.lever.co/democo/{LEVER_ID}/apply").respond(status_code=503)
    form = await LeverAdapter(http, env={}).get_form(f"lever:democo:{LEVER_ID}")
    assert {"name", "email", "resume"} <= {f.key for f in form.fields} and len(form.fields) == 8


def test_llm_null_string_is_never_an_answer():
    from apply_fakes import F, FakeAdapter, FakeGates, FakeProfile, FakeStore, settings

    from job_apply_mcp.apply.engine import ApplyEngine
    e = ApplyEngine(settings(), resolve_adapter=lambda r: FakeAdapter(), profile=FakeProfile(), router=None,
                    gates=FakeGates(), store=FakeStore(), control=None, driver_factory=lambda n: None)
    f = F("q", "Which event did you attend?", K.text)
    assert e._accept(f, {"value": "null", "confidence": 1.0}) is None
    assert e._accept(f, {"value": "None", "confidence": 1.0}) is None


def test_saved_yesno_answer_never_lands_in_preferred_name_field():
    """Regression: saved answer 'referred' -> 'No' matched inside the word 'preferred'."""
    label = ("What is your preferred first name and last name? If your legal first and last name is your "
             "preferred name, you do not need to respond.")
    prof = Profile(answers={"referred": "No"}, authorization=Authorization(work_authorized=True))
    f = parse_cards(page())[0].model_copy(update={"label": label, "required": False})
    r = map_fields([f], ProfileView(prof, Settings()), company="Springfield Robotics")
    assert r.answers == []
    assert prof.answer_for("Were you referred by an employee?") == "No"
    assert prof.answer_for("What is your preferred name?") is None
