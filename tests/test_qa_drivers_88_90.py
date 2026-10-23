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


# ---------------------------------------------------------------- Ashby required markup (Lambda follow-up, ADR-0056)
# Shape observed on a public Ashby form: each [data-field-path] entry has a .ashby-application-form-question-title;
# a required one carries a hashed `_required_<hash>` class. Voluntary EEO and recording-consent groups have none.

def _ashby_entry(fp, title, required, inner, fieldset=False):
    cls = "_heading_x1 _required_ab12_91 ashby-application-form-question-title" if required else \
          "_heading_x1  ashby-application-form-question-title"
    tag = "fieldset" if fieldset else "div"
    return (f'<div data-field-path="{fp}"><{tag} class="_fieldEntry_x ashby-application-form-field-entry">'
            f'<label class="{cls}">{title}</label>{inner}</{tag}></div>')


def _radios(name, opts, checked=None):
    return "".join(f'<span><input type="radio" id="{name}-{i}" name="{name}" value="{o}"{" checked" if o == checked else ""}>'
                   f'<label for="{name}-{i}">{o}</label></span>' for i, o in enumerate(opts))


def ashby_form(*, onsite=True, untitled=False):
    p = "c7512b4c__"
    parts = [
        _ashby_entry("_systemfield_name", "Name", True, '<input id="_systemfield_name" name="_systemfield_name" value="Alex Rivera">'),
        _ashby_entry("_systemfield_recording_consent", "Interview Recording Consent", False,
                     _radios(p + "_systemfield_recording_consent", ["Yes, I consent to be recorded"]), fieldset=True),
        _ashby_entry("q-onsite", "Are you able and willing to work onsite?", True,
                     '<input type="checkbox" name="q-onsite" id="q-onsite-yes"' + (" checked" if onsite else "") +
                     '><label for="q-onsite-yes">Yes</label><input type="checkbox" name="q-onsite" id="q-onsite-no">'
                     '<label for="q-onsite-no">No</label>'),
        _ashby_entry("_systemfield_eeoc_gender", "Gender", False,
                     _radios(p + "_systemfield_eeoc_gender", ["Male", "Female", "Decline to self-identify"]), fieldset=True),
        _ashby_entry("_systemfield_eeoc_veteran_status", "Veteran Status", False,
                     _radios(p + "_systemfield_eeoc_veteran_status", ["I am not a protected veteran",
                                                                      "I decline to self-identify"]), fieldset=True)]
    if untitled:
        parts.append('<fieldset><legend>Untitled group</legend>' + _radios("mystery", ["A", "B"]) + '</fieldset>')
    return "<html><body><form>" + "".join(parts) + "</form></body></html>"


async def test_ashby_unmarked_eeo_and_consent_groups_are_optional(drv):
    pg = await _page(drv, ashby_form(), ats="ashby", api={"req": ["_systemfield_name", "q-onsite"]})
    assert await drv._empty_required(pg) == []


async def test_ashby_required_group_still_blocks(drv):
    pg = await _page(drv, ashby_form(onsite=False), ats="ashby")      # marked by the _required_ class alone
    assert [r["label"] for r in await drv._empty_required(pg)] == ["Are you able and willing to work onsite?"]


async def test_ashby_api_required_overrides_missing_markup(drv):
    pg = await _page(drv, ashby_form(), ats="ashby", api={"req": ["_systemfield_eeoc_gender"]})
    assert [r["label"] for r in await drv._empty_required(pg)] == ["Gender"]


async def test_ashby_group_without_a_title_keeps_inverted_rule(drv):
    pg = await _page(drv, ashby_form(untitled=True), ats="ashby")
    assert [r["label"] for r in await drv._empty_required(pg)] == ["Untitled group"]


async def test_ashby_yes_no_with_hidden_checkbox_is_fast(drv):
    html = ('<html><body><form><div data-field-path="q1"><div role="group" aria-label="Willing to work onsite?">'
            '<label class="_required_ab12_91 ashby-application-form-question-title">Willing to work onsite?</label>'
            '<input type="checkbox" name="q1" style="display:none">'
            '<button type="button" aria-pressed="false" id="y">Yes</button>'
            '<button type="button" aria-pressed="false" id="n">No</button></div></div></form>'
            "<script>document.querySelectorAll('button').forEach(b => b.onclick = () => {"
            "document.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));"
            "document.querySelector('[name=q1]').checked = b.id === 'y'; });</script></body></html>")
    pg = await _page(drv, html, ats="ashby")
    f = FormField(key="q1", label="Willing to work onsite?", kind=FieldKind.boolean, required=True)
    t0 = time.monotonic()
    assert await drv._fill(pg, ATS["ashby"], f, True)
    assert time.monotonic() - t0 < 5                    # was the full 30 s default timeout per question
    assert await drv._verify(pg, [(f, True)], ATS["ashby"]) == []


# ---------------------------------------------------------------- control review on #91: unrecognised required
# markup must FAIL CLOSED. Each shape has one empty required question; every one must block.

def _lever_q(label_html="<div class=\"application-label\">Which apply?</div>", extra="", q_attrs="", ul_attrs="",
             style=""):
    boxes = "".join(f'<li><label><input type="checkbox" name="cards[c1][field0]" value="{o}"><span>{o}</span></label></li>'
                    for o in ("A", "B"))
    return (f'<html><head><style>{style}</style></head><body><form><ul><li class="application-question"{q_attrs}>'
            f'{label_html}{extra}<div class="application-field"><ul{ul_attrs}>{boxes}</ul></div></li></ul></form>'
            '</body></html>')


LEVER_BLOCK = {
    "asterisk-outside-label": _lever_q(extra="<span>✱</span>"),
    "required-text": _lever_q('<div class="application-label">Which apply? (Required)</div>'),
    "required-badge": _lever_q('<div class="application-label">Which apply? <span>Required</span></div>'),
    "css-after-asterisk": _lever_q('<div class="application-label lab-x">Which apply?</div>',
                                   style=".lab-x::after{content:'*'}"),
    "aria-required-ul": _lever_q(ul_attrs=' aria-required="true"'),
    "aria-required-question": _lever_q(q_attrs=' aria-required="true"'),
    "no-application-label": _lever_q("<div>Which apply?</div>"),
    "select-required-text": ('<html><body><form><ul><li class="application-question"><label><div class="application-label">'
                             'How did you hear? (required)</div><select name="cards[c1][field1]"><option value="">Select'
                             '</option><option>Web</option></select></label></li></ul></form></body></html>'),
}


@pytest.mark.parametrize("shape", sorted(LEVER_BLOCK))
async def test_lever_unrecognised_required_markup_blocks(drv, shape):
    pg = await _page(drv, LEVER_BLOCK[shape], ats="lever")
    assert len(await drv._empty_required(pg)) == 1, shape


def _ashby_g(*, wrapper_attrs="", entry_cls="_fieldEntry_x", fs_attrs="", title="Pick one",
             title_cls="_heading_x1  ashby-application-form-question-title", extra_title=""):
    radios = _radios("e1__q_pick", ["A", "B"])
    return (f'<html><body><form><div data-field-path="q_pick"{wrapper_attrs}><fieldset class="{entry_cls} '
            f'ashby-application-form-field-entry"{fs_attrs}>{extra_title}<label class="{title_cls}">{title}</label>'
            f'{radios}</fieldset></div></form></body></html>')


ASHBY_BLOCK = {
    "aria-required-fieldset": _ashby_g(fs_attrs=' aria-required="true"'),
    "data-required-wrapper": _ashby_g(wrapper_attrs=' data-required="true"'),
    "required-class-on-entry": _ashby_g(entry_cls="_fieldEntry_x _required_zz_9"),
    "two-titles": _ashby_g(extra_title='<label class="_heading_x1 ashby-application-form-question-title">Section</label>'),
    "required-text-in-title": _ashby_g(title="Pick one (required)"),
}


@pytest.mark.parametrize("shape", sorted(ASHBY_BLOCK))
async def test_ashby_unrecognised_required_markup_blocks(drv, shape):
    pg = await _page(drv, ASHBY_BLOCK[shape], ats="ashby")
    assert len(await drv._empty_required(pg)) == 1, shape


async def test_unmarked_single_title_groups_stay_optional(drv):
    pg = await _page(drv, _lever_q(), ats="lever")
    assert await drv._empty_required(pg) == []
    pg = await _page(drv, _ashby_g(), ats="ashby")
    assert await drv._empty_required(pg) == []


async def test_prose_mentioning_required_is_not_a_marker(drv):
    html = _ashby_g().replace('<label class="_heading_x1  ashby-application-form-question-title">Pick one</label>',
                              '<label class="_heading_x1  ashby-application-form-question-title">Pick one</label>'
                              '<p>Recordings are kept unless retention is required by applicable law.</p>')
    pg = await _page(drv, html, ats="ashby")
    assert await drv._empty_required(pg) == []
