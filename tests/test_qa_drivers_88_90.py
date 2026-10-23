"""QA issues #88 (Lever required-field verifier), #89 (Ashby location autocomplete commit) and #90 (closed postings,
comprehension checks). ADR-0056. Local fixtures only: the browser tests load fixture HTML into a local Chrome and
are skipped when none is installed; nothing touches the network."""
import time
import urllib.parse

import pytest
import respx
from apply_fakes import POSTING, F, FakeAdapter, FakeDriver
from test_apply_engine import engine

from job_apply_mcp.apply.browser_playwright import ATS, PlaywrightDriver
from job_apply_mcp.apply.closed import challenge_label, closed_reason, closed_text
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, Decision, FieldKind, FormField
from job_apply_mcp.platforms import PostingClosed
from job_apply_mcp.platforms.greenhouse import GreenhouseAdapter
from job_apply_mcp.platforms.lever import LeverAdapter

# ---------------------------------------------------------------- #90 pure detection


@pytest.mark.parametrize("label", ["Name (NOT USED; APPLICATIONS CLOSED)", "Email (NOT USED)",
                                   "We are no longer accepting applications for this role"])
def test_closed_markers_in_labels(label):
    assert closed_reason(labels=["Phone", label]).startswith("closed:")


def test_closed_markers_in_text_title_and_description():
    assert closed_text("APPLICATIONS CLOSED. Thank you for your interest.")
    assert closed_text("This position is no longer available")
    assert closed_reason(title="[CLOSED] Robotics Engineer")
    assert closed_reason(description="<p>We are no longer accepting applications.</p>")


@pytest.mark.parametrize("title,label", [("Closed-loop Controls Engineer", "Full name"),
                                         ("Software Engineer", "Have you used closed-source tools?"),
                                         ("Software Engineer", "Is this field used in your current role?")])
def test_open_postings_are_not_closed(title, label):
    assert closed_reason(title=title, description="Applications are reviewed weekly.", labels=[label]) is None


@pytest.mark.parametrize("label,hit", [("Comprehension Check", True), ("Attention check: pick the third option", True),
                                       ("Are you a human?", True), ("Are you authorized to work in the US?", False),
                                       ("I consent under applicable anti-spam laws", False)])
def test_challenge_labels(label, hit):
    assert challenge_label(label) is hit


# ---------------------------------------------------------------- #90 engine / adapters

CLOSED_FORM = ApplicationForm(ref=POSTING.ref, submit_via=["browser"], fields=[
    F("name", "Name (NOT USED; APPLICATIONS CLOSED)"), F("email", "Email (NOT USED; APPLICATIONS CLOSED)"),
    F("check", "Comprehension Check", FieldKind.select, options=["A", "B", "C"])])
CHALLENGE_FORM = ApplicationForm(ref=POSTING.ref, submit_via=["browser"], fields=[
    F("first_name", "First Name"), F("email", "Email"),
    F("check", "Comprehension Check", FieldKind.select, options=["A", "B", "C"])])


async def test_closed_form_is_skipped_not_an_owner_action():
    e, _ = engine(adapter=FakeAdapter(form=CLOSED_FORM), drivers={"playwright": FakeDriver()})
    out = await e.plan(POSTING.ref)
    assert out.plan.gate.decision == Decision.skip and out.plan.gate.reasons[0].startswith("closed:")
    assert not out.client_tasks and not out.plan.answers and not out.plan.unresolved
    r = await e.submit(POSTING.ref, {}, dry_run=True)
    assert r.status == "skipped" and r.detail.startswith("closed:")
    rows = await e.apply_batch([POSTING.ref], dry_run=True)
    assert rows[0]["status"] == "skipped"


async def test_comprehension_check_is_a_visible_challenge_never_answered():
    drv = FakeDriver()
    e, _ = engine(adapter=FakeAdapter(form=CHALLENGE_FORM), drivers={"playwright": drv})
    out = await e.plan(POSTING.ref)
    assert out.plan.gate.decision == Decision.needs_human
    assert "visible challenge" in out.plan.gate.reasons[0] and "Comprehension Check" in out.plan.gate.reasons[0]
    assert not out.client_tasks and "check" not in {a.key for a in out.plan.answers}
    r = await e.submit(POSTING.ref, {"check": "A"}, dry_run=True)       # even a supplied answer is never used
    assert r.status == "needs_human" and "visible challenge" in r.detail and not drv.calls


class _ClosedAdapter(FakeAdapter):
    async def get_posting(self, ref, *, with_description=False):
        raise PostingClosed("posting not found (closed or moved)")


async def test_api_404_closed_is_skipped():
    e, _ = engine(adapter=_ClosedAdapter())
    out = await e.plan(POSTING.ref)
    assert out.plan.gate.decision == Decision.skip and "closed" in out.plan.gate.reasons[0]
    r = await e.submit(POSTING.ref, {}, dry_run=True)
    assert r.status == "skipped" and "closed" in r.detail


@respx.mock
async def test_lever_and_greenhouse_404_raise_posting_closed(http):
    respx.get("https://api.lever.co/v0/postings/democo/abc").respond(404)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmeco/jobs/9").respond(404)
    with pytest.raises(PostingClosed):
        await LeverAdapter(http, env={}).get_posting("lever:democo:abc")
    with pytest.raises(PostingClosed):
        await GreenhouseAdapter(http, env={}).get_posting("greenhouse:acmeco:9")


# ---------------------------------------------------------------- browser fixtures


@pytest.fixture
async def drv(tmp_path):
    s = Settings()
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.captcha_handoff = False
    s.browser.hydrate_wait_s = 1
    d = PlaywrightDriver(s)
    try:
        await d._context()
    except Exception as e:  # noqa: BLE001  # no browser available
        pytest.skip(f"no local browser: {e}")
    yield d
    await d.aclose()


async def _page(drv, html, ats="lever", api=None):
    pg = await (await drv._context()).new_page()
    await pg.set_content(html)
    drv._api_keys[id(pg)] = {"req": [], "opt": [], "keys": [], "stop": [], "ats": ats, **(api or {})}
    return pg


# ---------------------------------------------------------------- #88 Lever verifier (Spotify form shape, fictional)

CARD = "cards[11111111-aaaa-bbbb-cccc-222222222222][field0]"


def lever_form(*, name="Alex Rivera", location="Portland, Oregon, United States", prior=True, org="Initech"):
    v = lambda x: f' value="{x}"' if x else ""
    return f"""<html><body><form id="application-form"><div class="section application-form"><ul>
<li class="application-question resume"><label><div class="application-label">Resume/CV <span class="required">✱</span></div>
 <div class="application-field"><input type="file" name="resume"></div></label></li>
<li class="application-question"><label><div class="application-label">Full name<span class="required">✱</span></div>
 <div class="application-field"><input type="text" name="name" required{v(name)}></div></label></li>
<li class="application-question"><div class="application-label multiple-select">Pronouns</div><div class="application-field">
 <ul id="candidatePronounsCheckboxes"><div class="column-wrapper"><div class="table-row">
  <li class="column"><label><input type="checkbox" name="pronouns" value="He/him"><span>He/him</span></label></li>
  <li class="column"><label><input type="checkbox" name="pronouns" value="She/her"><span>She/her</span></label></li>
  <li class="column"><label><input type="checkbox" name="pronouns" value="They/them"><span>They/them</span></label></li></div></div>
  <hr><li><label><input type="checkbox" name="pronouns" value="Use name only"><span>Use name only</span></label></li>
  <hr><li><label><input type="checkbox" value="Custom" id="customPronounsOption"><span>Custom</span></label>
   <input style="display:none" id="customPronounsTextField" type="text" name="pronouns"></li>
  <p class="description">Let the employer know what pronouns you use.</p></ul></div></li>
<li class="application-question"><label><div class="application-label">Email<span class="required">✱</span></div>
 <div class="application-field"><input name="email" type="email" required value="alex.rivera@example.com"></div></label></li>
<li class="application-question"><label><div class="application-label">Current location <span class="required">✱</span></div>
 <div class="application-field"><input class="location-input" id="location-input" type="text" name="location" required{v(location)}>
 <input id="selected-location" type="hidden" name="selectedLocation"></div></label></li>
<li class="application-question"><label><div class="application-label">Current company <span class="required">✱</span></div>
 <div class="application-field"><input type="text" name="org" required{v(org)}></div></label></li></ul></div>
<div class="section application-form"><h4>Previous Employment</h4><ul><li class="application-question custom-question"><div>
 <div class="application-label full-width multiple-select"><div class="text">Have you ever been employed by Acme Audio?<span class="required">✱</span></div></div>
 <div class="application-field full-width required-field"><ul data-qa="checkboxes">
  <li><label><input type="checkbox" name="{CARD}" value="No"{' checked' if prior else ''}><span>No</span></label></li>
  <li><label><input type="checkbox" name="{CARD}" value="Yes"><span>Yes</span></label></li></ul></div></div></li></ul></div>
<div class="section application-form"><h4>Demographic Survey</h4><div class="application-question"><label>
 <div class="application-label">What is your location?</div><div class="application-field"><select class="candidate-location">
 <option value="">Select...</option><option value="US">United States</option></select></div></label></div></div>
<div class="section application-form hidden" style="display:none"><ul><li class="application-question">
 <div class="application-label"><div class="text">Survey question<span class="required">✱</span></div></div>
 <ul><li><label><input type="radio" name="survey0" value="A"><span>A</span></label></li></ul></li></ul></div>
<div class="section application-form" data-qa="consent-section"><ul><li class="application-question"><div class="application-field full-width">
 <p>By applying for this position, your data will be processed as per Acme Audio's <a href="#">Privacy Policy</a>.</p>
 <ul><li><label><span>Acme Audio has my consent to contact me about future job opportunities.</span>
 <input type="hidden" name="consent[marketing]" value="0"><input type="checkbox" name="consent[marketing]" value="1"></label></li></ul>
</div></li></ul></div></form></body></html>"""


async def test_lever_fully_filled_form_has_no_required_gaps(drv):
    pg = await _page(drv, lever_form())
    t0 = time.monotonic()
    assert await drv._empty_required(pg) == []        # pronouns, Custom, privacy text, survey select: all optional
    assert time.monotonic() - t0 < 2
    assert await drv._native_invalid(pg) == []


@pytest.mark.parametrize("kw,label", [({"name": ""}, "Full name"), ({"location": ""}, "Current location"),
                                      ({"org": ""}, "Current company"),
                                      ({"prior": False}, "Have you ever been employed by Acme Audio?")])
async def test_lever_empty_required_field_still_blocks(drv, kw, label):
    pg = await _page(drv, lever_form(**kw))
    labels = [r["label"] for r in await drv._empty_required(pg)]
    assert labels == [label]                            # fail closed, with the question's own label


async def test_lever_api_required_group_without_markup_still_blocks(drv):
    pg = await _page(drv, lever_form(), api={"req": ["pronouns"]})
    assert [r["label"] for r in await drv._empty_required(pg)] == ["Pronouns"]


async def test_other_ats_keep_inverted_rule_for_unmarked_groups(drv):
    pg = await _page(drv, lever_form(), ats="ashby")      # ADR-0039 rule unchanged outside Lever
    assert await drv._empty_required(pg)


# ---------------------------------------------------------------- #89 Ashby location autocomplete
# Imitates the live widget (observed with Playwright on a public Ashby form): an id-less input[role=combobox]
# [aria-autocomplete=list] inside [data-field-path]; typing fires a debounced geo lookup (ApiAutocompleteGeoLocation);
# options render in a floating [role=listbox] named by aria-controls; clicking one sets the input to the option text,
# closes the list (aria-expanded=false) and saves it (ApiSetFormValue); blurring without a pick clears the text.

ASHBY_COMBO = """<html><body><div id=root><form>
<div class="_fieldEntry ashby-application-form-field-entry" data-field-path="_systemfield_location">
 <label class="_heading _required" for="_systemfield_location">Location</label>
 <div class="_inputContainer"><input class="ashby-application-form-input-autocomplete" placeholder="Start typing..."
  aria-autocomplete="list" aria-expanded="false" aria-haspopup="listbox" role="combobox" value=""></div></div>
<div class="ashby-application-form-field-entry" data-field-path="_systemfield_name"><label for="_systemfield_name">Name</label>
 <input id="_systemfield_name" name="_systemfield_name"></div>
</form></div>
<script>
const PLACES = __PLACES__, COMMIT = __COMMIT__;
const inp = document.querySelector('input[role=combobox]'); let timer = null, lb = null, committed = '';
inp.addEventListener('input', () => { clearTimeout(timer); if (lb) { lb.remove(); lb = null; }
  timer = setTimeout(() => { const q = inp.value.toLowerCase(); const hits = PLACES.filter(p => p.toLowerCase().startsWith(q));
    if (!hits.length) return;
    lb = document.createElement('div'); lb.id = ':r0:'; lb.setAttribute('role', 'listbox'); lb.style.position = 'absolute';
    hits.forEach((h, i) => { const o = document.createElement('div'); o.setAttribute('role', 'option'); o.id = ':r' + (i + 1) + ':';
      o.textContent = h; o.addEventListener('click', () => { if (COMMIT) { inp.value = h; committed = h; }
        inp.setAttribute('aria-expanded', 'false'); lb.remove(); lb = null; }); lb.appendChild(o); });
    document.body.appendChild(lb); inp.setAttribute('aria-controls', ':r0:'); inp.setAttribute('aria-expanded', 'true');
  }, 300); });
// observed on the live widget: leaving it without picking an option drops the typed text
inp.addEventListener('blur', () => setTimeout(() => { if (lb) { lb.remove(); lb = null; } inp.setAttribute('aria-expanded', 'false');
  if (inp.value !== committed) inp.value = committed; }, 400));
</script></body></html>"""
LOC = FormField(key="_systemfield_location", label="Location", kind=FieldKind.location, required=True)
PLACES = '["Portland, Oregon, United States", "Portlaoise, Laois, Ireland"]'


def ashby(places=PLACES, commit=True):
    return ASHBY_COMBO.replace("__PLACES__", places).replace("__COMMIT__", "true" if commit else "false")


async def _value(pg):
    return await pg.locator('[data-field-path="_systemfield_location"] input').input_value()


@pytest.mark.parametrize("answer", ["Portland, OR", "Portland, Oregon, United States", "Portland, Oregon"])
async def test_ashby_location_commits_best_match_and_verifies(drv, answer):
    pg = await _page(drv, ashby(), ats="ashby", api={"req": ["_systemfield_location"]})
    assert await drv._fill(pg, ATS["ashby"], LOC, answer)
    assert await _value(pg) == "Portland, Oregon, United States"
    assert await drv._verify(pg, [(LOC, answer)], ATS["ashby"]) == []
    assert await drv._empty_required(pg) == []


async def test_ashby_location_with_no_matching_option_fails_closed_and_clears(drv):
    drv.s.browser.combobox_commit_s = 4
    pg = await _page(drv, ashby('["Portlaoise, Laois, Ireland"]'), ats="ashby", api={"req": ["_systemfield_location"]})
    t0 = time.monotonic()
    assert not await drv._fill(pg, ATS["ashby"], LOC, "Portland, OR")
    assert time.monotonic() - t0 < 6                    # bounded by browser.combobox_commit_s
    assert await _value(pg) == ""                       # no uncommitted text left behind
    assert await drv._verify(pg, [(LOC, "Portland, OR")], ATS["ashby"]) == ["_systemfield_location"]
    assert [r["label"] for r in await drv._empty_required(pg)] == ["Location"]
    t1 = time.monotonic()                               # the refill pass does not spend the budget again
    assert not await drv._fill(pg, ATS["ashby"], LOC, "Portland, OR") and time.monotonic() - t1 < 0.5


async def test_ashby_location_click_that_does_not_commit_fails_closed(drv):
    drv.s.browser.combobox_commit_s = 4
    pg = await _page(drv, ashby(commit=False), ats="ashby")
    assert not await drv._fill(pg, ATS["ashby"], LOC, "Portland, OR")
    assert await drv._verify(pg, [(LOC, "Portland, OR")], ATS["ashby"]) == ["_systemfield_location"]


def test_combobox_budget_capped_at_20s():
    s = Settings()
    assert s.browser.combobox_commit_s <= 20
    with pytest.raises(ValueError):
        s.browser.__class__(combobox_commit_s=60)


# ---------------------------------------------------------------- #90 driver: closed page / comprehension check


def _data_url(html):
    return "data:text/html," + urllib.parse.quote(html)


async def test_driver_reports_closed_page_as_skipped(drv):
    html = ("<html><body><form><h2>APPLICATIONS CLOSED</h2><label for=n>Name (NOT USED; APPLICATIONS CLOSED)</label>"
            "<input id=n name=n></form></body></html>")
    form = ApplicationForm(ref="ashby:acme:job1", fields=[FormField(key="n", label="Name", kind=FieldKind.text)])
    r = await drv.fill_and_submit(_data_url(html), form, {"n": "x"}, {}, dry_run=True)
    assert r.status == "skipped" and r.detail.startswith("closed:")


async def test_driver_hands_off_comprehension_check(drv):
    html = ("<html><body><form><label for=n>Name</label><input id=n name=n>"
            "<label for=c>Comprehension Check</label><select id=c name=c><option>A</option></select></form></body></html>")
    form = ApplicationForm(ref="ashby:acme:job1", fields=[FormField(key="n", label="Name", kind=FieldKind.text)])
    r = await drv.fill_and_submit(_data_url(html), form, {"n": "x"}, {}, dry_run=True)
    assert r.status == "needs_human" and "visible challenge" in r.detail, r
