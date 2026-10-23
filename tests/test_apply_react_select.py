"""react-select / intl-tel / Lever urls[...] behaviour against LOCAL fixtures that mimic the real widgets
(options only commit on a CLICK of the option element; Enter does nothing; async option lists; a country
change resets the phone). Needs a local Chrome/Chromium; skipped otherwise. Fictional data only."""
import http.server
import json
import threading

import pytest

from job_apply_mcp.apply.browser_playwright import PlaywrightDriver, same_value
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import ApplicationForm, FieldKind, FormField

RS_JS = """
function rs(id, opts, o) { o = o || {};
  const root = document.getElementById(id + '_wrap');
  root.className = 'select-shell';
  root.innerHTML = '<div class="select__control"><div class="select__value-container"><div class="select__placeholder">Select...</div>' +
    '<input id="' + id + '" role="combobox" autocomplete="off" aria-expanded="false"></div></div>';
  const inp = root.querySelector('input'), vc = root.querySelector('.select__value-container');
  const ph = root.querySelector('.select__placeholder'); let menu = null, chosen = [];
  function close() { if (menu) { menu.remove(); menu = null; } }
  function render() {
    const q = inp.value.toLowerCase(); close(); if (document.activeElement !== inp) return;
    const draw = () => { close(); menu = document.createElement('div'); menu.className = 'select__menu';
      opts.filter(t => t.toLowerCase().includes(q)).forEach((t, i) => { const d = document.createElement('div');
        d.className = 'select__option' + (i === 0 ? ' select__option--is-focused' : ''); d.setAttribute('role', 'option'); d.textContent = t;
        d.onmouseenter = () => d.classList.add('select__option--is-focused');
        d.onclick = () => { if (o.broken) { close(); return; } if (o.multi) { chosen.push(t); } else { chosen = [t]; }
          vc.querySelectorAll('.select__single-value,.select__multi-value').forEach(x => x.remove());
          chosen.forEach(c => { const e = document.createElement('div'); e.className = o.multi ? 'select__multi-value' : 'select__single-value';
            e.innerHTML = o.multi ? '<span class="select__multi-value__label"></span>' : ''; (o.multi ? e.firstChild : e).textContent = c; vc.insertBefore(e, inp); });
          ph.style.display = 'none'; inp.value = ''; if (!o.multi) close(); if (o.onpick) o.onpick(t); };
        menu.appendChild(d); }); root.appendChild(menu); };
    o.delay ? setTimeout(draw, o.delay) : draw(); }
  inp.addEventListener('focus', render); inp.addEventListener('click', render); inp.addEventListener('input', render);
  inp.addEventListener('keydown', e => { if (e.key === 'Escape') close(); if (e.key === 'Enter') e.preventDefault(); });
}
"""

GH_HTML = """<html><body><form id=f>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<div id=phone_row><label for=country>Country</label><div id=country_wrap></div>
<label for=phone>Phone</label><input id=phone name=phone type=tel></div>
<label for=question_1>Have you ever worked for Acme?</label><div id=question_1_wrap></div>
<label for=question_2>Will you require visa sponsorship?</label><div id=question_2_wrap></div>
<label for=question_3>Preferred offices</label><div id=question_3_wrap></div>
<label for=candidate-location>Location (City)</label><div id=candidate-location_wrap></div>
<button type=submit id=sub>Submit application</button></form>
<script>__RS__
var cn = ['Canada +1', 'United States Minor Outlying Islands +1', 'United States +1', 'India +91'];
rs('country', cn, {onpick: () => { document.getElementById('phone').value = ''; }});
rs('question_1', ['Yes', 'No']); rs('question_2', ['Yes', 'No']);
rs('question_3', ['Springfield', 'Shelbyville', 'Capital City'], {multi: true});
rs('candidate-location', ['Springfield, Illinois, United States', 'Springfield, Missouri, United States'], {delay: 300});
function report() { const v = [...document.querySelectorAll('.select__single-value,.select__multi-value__label')].map(x => x.textContent);
  fetch('/state', {method: 'POST', body: JSON.stringify({vals: v, phone: document.getElementById('phone').value, menus: document.querySelectorAll('.select__menu').length})}); }
new MutationObserver(report).observe(document.body, {subtree: true, childList: true, characterData: true});
document.getElementById('phone').addEventListener('input', report); report();
document.getElementById('f').onsubmit = e => { e.preventDefault(); fetch('/submitted', {method: 'POST'}); };
</script></body></html>""".replace("__RS__", RS_JS)

LEVER_HTML = """<html><body><form id=f><ul>
<li style="display:none"><input name="urls[GitHub]" value=""></li>
<li><label><span>LinkedIn</span><input name="urls[LinkedIn]" type=text></label></li>
<li><label><span>GitHub</span><input name="urls[GitHub]" type=text></label></li>
</ul><button type=submit>Submit application</button></form>
<script>
// controlled-input emulation: only real key events change state; programmatic value sets are reverted
document.querySelectorAll('input[name^="urls["]').forEach(i => { let st = '';
  i.addEventListener('keydown', () => { i.__k = true; });
  i.addEventListener('input', () => { if (i.__k) { st = i.value; i.__k = false; } else { i.value = st; } });
  i.addEventListener('keyup', () => { i.__k = false; }); });
</script></body></html>"""


def _serve(html):
    posts = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            posts.append((self.path, self.rfile.read(n).decode() if n else ""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, posts


@pytest.fixture
async def drv(tmp_path):
    s = Settings()
    s.browser.headless = True
    s.browser.channel = "chrome"
    s.browser.confirmation_wait_s = 2
    d = PlaywrightDriver(s)
    try:
        await d._context()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no local browser: {e}")
    yield d
    await d.aclose()


def test_same_value_tolerates_url_normalisation():
    assert same_value(FieldKind.text, "GitHub", "https://github.com/alex-rivera", "github.com/alex-rivera/")
    assert not same_value(FieldKind.text, "GitHub", "https://github.com/alex-rivera", "github.com/other")


GH_FORM = ApplicationForm(ref="greenhouse:acme:1", fields=[
    FormField(key="first_name", label="First Name", kind=FieldKind.text),
    FormField(key="phone", label="Phone", kind=FieldKind.text),
    FormField(key="question_1", label="Have you ever worked for Acme?", kind=FieldKind.select, options=["Yes", "No"]),
    FormField(key="question_2", label="Will you require visa sponsorship?", kind=FieldKind.boolean, options=["Yes", "No"]),
    FormField(key="question_3", label="Preferred offices", kind=FieldKind.multiselect,
              options=["Springfield", "Shelbyville", "Capital City"]),
    FormField(key="candidate-location", label="Location (City)", kind=FieldKind.location)])
GH_ANS = {"first_name": "Alex", "phone": "555-0101", "question_1": "No", "question_2": True,
          "question_3": ["Springfield", "Capital City"], "candidate-location": "Springfield, Illinois",
          "contact:country": "USA"}


async def test_react_select_options_are_committed_and_country_precedes_phone(drv):
    srv, posts = _serve(GH_HTML)
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/1", GH_FORM, GH_ANS, {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert not [p for p in posts if p[0] == "/submitted"]
    last = json.loads([p for p in posts if p[0] == "/state"][-1][1])
    assert "United States +1" in last["vals"] and last["vals"].count("Yes") == 1 and "No" in last["vals"]
    assert {"Springfield", "Capital City"} <= set(last["vals"]) and last["phone"] == "555-0101"


@pytest.mark.parametrize("country,expect", [("Canada", "Canada +1"), ("India", "India +91"), ("", None)])
async def test_phone_country_comes_from_the_profile(drv, country, expect):
    srv, posts = _serve(GH_HTML)
    form = ApplicationForm(ref="greenhouse:acme:1", fields=GH_FORM.fields[:2])
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/1", form,
                                    {"first_name": "Alex", "phone": "555-0101", "contact:country": country}, {},
                                    dry_run=True)
    assert res.status == "dry_run", res.detail
    last = json.loads([p for p in posts if p[0] == "/state"][-1][1])
    countries = [v for v in last["vals"] if "+" in v]
    assert countries == ([expect] if expect else [])          # no profile country: the widget is never guessed
    assert last["phone"] == "555-0101"


def test_country_helpers():
    from job_apply_mcp.apply.browser_playwright import country_option, phone_country_name
    assert phone_country_name("USA") == phone_country_name("U.S.") == "United States"
    assert phone_country_name("Canada") == "Canada" and phone_country_name("") == ""
    opts = ["United States Minor Outlying Islands +1", "United States +1", "India (+91)"]
    assert country_option("United States", opts) == "United States +1"
    assert country_option("India", opts) == "India (+91)"
    assert country_option("Springfield", opts) is None


TWO_HTML = """<html><body><form id=f>
<label for=question_1>First question</label><div id=question_1_wrap></div>
<label for=question_2>Second question</label><div id=question_2_wrap></div>
<button type=submit>Submit application</button></form>
<script>__RS__
rs('question_1', ['Yes', 'No']); rs('question_2', ['Yes', 'No'], {broken: true});
</script></body></html>""".replace("__RS__", RS_JS)


async def test_commit_readback_is_scoped_to_the_field(drv):
    """question_1 already shows 'Yes'; question_2's option click never commits. Its read-back must not be
    satisfied by question_1's chip."""
    srv, _ = _serve(TWO_HTML)
    ctx = await drv._context()
    page = await ctx.new_page()
    await page.goto(f"http://127.0.0.1:{srv.server_port}/")
    q1 = FormField(key="question_1", label="First question", kind=FieldKind.select, options=["Yes", "No"])
    q2 = FormField(key="question_2", label="Second question", kind=FieldKind.select, options=["Yes", "No"])
    assert await drv._choose(page, q1, await drv._locate(page, q1), "Yes")
    assert not await drv._choose(page, q2, await drv._locate(page, q2), "Yes")
    assert not await drv._committed(page, None, "Yes")        # no container: never committed
    await page.close()


async def test_lever_urls_typed_for_real(drv):
    srv, _ = _serve(LEVER_HTML)
    form = ApplicationForm(ref="lever:acme:1", fields=[
        FormField(key="urls[LinkedIn]", label="LinkedIn", kind=FieldKind.text),
        FormField(key="urls[GitHub]", label="GitHub", kind=FieldKind.text)])
    ans = {"urls[LinkedIn]": "https://www.linkedin.com/in/alex-rivera", "urls[GitHub]": "https://github.com/alex-rivera"}
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/1/apply", form, ans, {}, dry_run=True)
    assert res.status == "dry_run", res.detail


EEO_HTML = """<html><body><form id=f>
<label for=first_name>First Name *</label><input id=first_name name=first_name required>
<label for=why>Why do you want to work here? *</label><textarea id=why aria-required=true></textarea>
<label id=g_l for=gender>Gender *</label><div id=gender_wrap></div>
<label id=v_l for=vet>Veteran Status *</label><div id=vet_wrap></div>
<input aria-hidden=true tabindex=-1 required value="" class=shim>
<button type=submit>Submit application</button></form>
<script>__RS__
rs('gender', ['Male', 'Female', "I don't wish to answer"]);
rs('vet', ['I am not a protected veteran', "I don't wish to answer"]);
document.querySelectorAll('#gender,#vet').forEach(i => i.setAttribute('aria-required', 'true'));
function report() { fetch('/state', {method: 'POST', body: JSON.stringify([...document.querySelectorAll('.select__single-value')].map(x => x.textContent))}); }
new MutationObserver(report).observe(document.body, {subtree: true, childList: true}); report();
</script></body></html>""".replace("__RS__", RS_JS)


async def test_incomplete_reports_required_gaps_and_declines_eeo(drv):
    srv, posts = _serve(EEO_HTML)
    form = ApplicationForm(ref="greenhouse:acme:2", fields=[
        FormField(key="first_name", label="First Name *", kind=FieldKind.text, required=True)])
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/2", form, {"first_name": "Alex"},
                                    {}, dry_run=True)
    assert res.status == "incomplete", res.detail
    assert "Why do you want" in res.detail and "Gender" not in res.detail and "Veteran" not in res.detail
    assert "shim" not in res.detail
    vals = json.loads([p for p in posts if p[0] == "/state"][-1][1])
    assert vals == ["I don't wish to answer", "I don't wish to answer"]


SELFID_HTML = """<html><body><form id=f>
<label for=first_name>First Name *</label><input id=first_name name=first_name required>
<label for=gender>Gender</label><div id=gender_wrap></div>
<label for=race>Race</label><div id=race_wrap></div>
<label for=hispanic_ethnicity>Are you Hispanic/Latino?</label><div id=hispanic_ethnicity_wrap></div>
<label for=veteran_status>Veteran Status *</label><div id=veteran_status_wrap></div>
<label for=disability_status>Disability Status</label><select id=disability_status><option value="">Select...</option>
<option>Yes, I have a disability</option><option>No, I do not have a disability</option><option>I do not want to answer</option></select>
<button type=submit>Submit application</button></form>
<script>__RS__
const D = "I don't wish to answer";
rs('gender', ['Male', 'Female', 'Non-binary', D]); rs('race', ['Asian', 'White', 'Two or More Races', D]);
rs('hispanic_ethnicity', ['Yes', 'No', D]); rs('veteran_status', ['I am a protected veteran', 'I am not a protected veteran', D]);
document.getElementById('veteran_status').setAttribute('aria-required', 'true');
function report() { const o = {}; ['gender', 'race', 'hispanic_ethnicity', 'veteran_status'].forEach(i => {
    const v = document.querySelector('#' + i + '_wrap .select__single-value'); o[i] = v ? v.textContent : ''; });
  const d = document.getElementById('disability_status'); o.disability_status = d.value ? d.selectedOptions[0].text : '';
  fetch('/state', {method: 'POST', body: JSON.stringify(o)}); }
new MutationObserver(report).observe(document.body, {subtree: true, childList: true});
document.getElementById('disability_status').addEventListener('change', report); report();
</script></body></html>""".replace("__RS__", RS_JS)


async def test_dom_only_self_id_uses_profile_or_decline_and_never_invents(drv):
    srv, posts = _serve(SELFID_HTML)
    form = ApplicationForm(ref="greenhouse:acme:5", fields=[
        FormField(key="first_name", label="First Name *", kind=FieldKind.text, required=True)])
    ans = {"first_name": "Alex", "demographic:gender": "Female", "demographic:disability": "No",
           "demographic:hispanic": "Martian"}           # not an offered option -> decline, never a guess
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/5", form, ans, {}, dry_run=True)
    # the native Disability select is DOM-only (no API required:false) and "No" has no equal option: left blank,
    # and under the inverted rule a blank DOM-only native select is a gap
    assert res.status == "incomplete" and "Disability Status" in res.detail, res.detail
    st = json.loads([p for p in posts if p[0] == "/state"][-1][1])
    assert st == {"gender": "Female",                                   # profile value, exact option
                  "race": "",                                           # optional, no profile value: blank
                  "hispanic_ethnicity": "",                             # no equal option: blank, never decline/guess
                  "veteran_status": "I don't wish to answer",           # required, no profile value: decline table
                  "disability_status": ""}                              # "No" is not "No, I do not have ...": blank


SELFID_PAIR_HTML = """<html><body><form id=f>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<label for=gender>__LABEL__</label><div id=gender_wrap></div>
<button type=submit>Submit application</button></form>
<script>__RS__
rs('gender', __OPTS__);
if (__REQ__) document.getElementById('gender').setAttribute('aria-required', 'true');
function report() { const v = document.querySelector('#gender_wrap .select__single-value');
  fetch('/state', {method: 'POST', body: v ? v.textContent : ''}); }
new MutationObserver(report).observe(document.body, {subtree: true, childList: true}); report();
</script></body></html>""".replace("__RS__", RS_JS)


@pytest.mark.parametrize("label,profile,opts", [
    ("Gender", "Female", ["Male", "Non-binary", "I don't wish to answer"]),
    ("Gender identity", "Woman", ["Man", "Non-binary", "I don't wish to answer"]),
    ("Race", "Asian", ["Asian Indian", "White", "I don't wish to answer"]),
])
@pytest.mark.parametrize("required", [False, True])
async def test_self_id_near_miss_is_never_selected(drv, label, profile, opts, required):
    """Female must never select Male, Woman never Man, Asian never Asian Indian: blank, and `incomplete` if required."""
    html = (SELFID_PAIR_HTML.replace("__LABEL__", label).replace("__OPTS__", json.dumps(opts))
            .replace("__REQ__", "true" if required else "false"))
    srv, posts = _serve(html)
    key = "race" if label == "Race" else "gender"
    form = ApplicationForm(ref="greenhouse:acme:6", fields=[FormField(key="first_name", label="First Name", kind=FieldKind.text)])
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/6", form,
                                    {"first_name": "Alex", f"demographic:{key}": profile}, {}, dry_run=True)
    assert [p for p in posts if p[0] == "/state"][-1][1] == ""
    assert res.status == ("incomplete" if required else "dry_run"), res.detail


def test_exact_choice_never_substring():
    from job_apply_mcp.apply.browser_playwright import exact_choice
    assert exact_choice("Female", ["Male", "Female"]) == "Female"
    assert exact_choice("Female", ["Male", "Non-binary"]) is None
    assert exact_choice("Woman", ["Man", "Transgender man"]) is None
    assert exact_choice("Asian", ["Asian Indian", "Asian American"]) is None
    assert exact_choice("asian ", ["ASIAN"]) == "ASIAN"
    assert exact_choice("Decline to self-identify", ["Male", "I don't wish to answer"]) == "I don't wish to answer"
    assert exact_choice("No", ["No, I do not have a disability"]) is None


def test_located_in_us_maps_from_country():
    from job_apply_mcp.apply.mapper import canonical_key
    assert canonical_key("Are you currently located in the US?") == "located_in_us"
    assert canonical_key("Are you currently based in or willing to relocate to the Bay Area?") == "willing_to_relocate"


EDU_HTML = """<html><body><form id=f>
<label for=school--0>School *</label><div id="school--0_wrap"></div>
<label for=degree--0>Degree *</label><div id="degree--0_wrap"></div>
<label for=discipline--0>Discipline *</label><div id="discipline--0_wrap"></div>
<label for=start-year--0>Start date year *</label><input id=start-year--0 type=number required>
<label for=end-year--0>End date year *</label><input id=end-year--0 type=number required>
<button type=button>Add another</button><button type=submit>Submit application</button></form>
<script>__RS__
const SCHOOLS = __SCHOOLS__;
rs('school--0', SCHOOLS, {delay: 200});
rs('degree--0', ["Bachelor's Degree", 'Master of Business Administration (M.B.A.)', "Master's Degree"]);
rs('discipline--0', ['Computer Science', 'Biology', 'Physics']);
['school--0', 'degree--0', 'discipline--0'].forEach(i => document.getElementById(i).setAttribute('aria-required', 'true'));
function report() { fetch('/state', {method: 'POST', body: JSON.stringify({vals: [...document.querySelectorAll('.select__single-value')].map(x => x.textContent),
  sy: document.getElementById('start-year--0').value, ey: document.getElementById('end-year--0').value})}); }
new MutationObserver(report).observe(document.body, {subtree: true, childList: true}); report();
['start-year--0', 'end-year--0'].forEach(i => document.getElementById(i).addEventListener('input', report));
</script></body></html>"""

EDU_ROW = {"school": "Springfield State University", "degree": "Master of Science", "discipline": "Computer Science",
           "start_year": "2022", "end_year": "2024"}


async def _edu_run(drv, schools, row=None):
    html = EDU_HTML.replace("__RS__", RS_JS).replace("__SCHOOLS__", json.dumps(schools))
    srv, posts = _serve(html)
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/3",
                                    ApplicationForm(ref="greenhouse:acme:3", fields=[]), {"education:0": row or EDU_ROW},
                                    {}, dry_run=True)
    st = [p for p in posts if p[0] == "/state"]
    return res, (json.loads(st[-1][1]) if st else {})


async def test_education_block_filled_and_read_back(drv):
    res, st = await _edu_run(drv, ["Springfield University", "Springfield State University", "Other"])
    assert res.status == "dry_run", res.detail
    assert st["vals"] == ["Springfield State University", "Master's Degree", "Computer Science"]
    assert (st["sy"], st["ey"]) == ("2022", "2024")


async def test_education_unknown_school_uses_other_never_a_different_school(drv):
    res, st = await _edu_run(drv, ["Springfield University", "Springfield State University Online", "Other"],
                             {**EDU_ROW, "school": "Springfield State"})
    assert res.status == "dry_run", res.detail
    assert st["vals"][0] == "Other"


async def test_education_unknown_school_without_other_stays_empty(drv):
    res, st = await _edu_run(drv, ["Springfield University", "Shelbyville College"])
    assert res.status == "incomplete" and "School" in res.detail, res.detail
    assert "Springfield University" not in st["vals"] and "Shelbyville College" not in st["vals"]


def test_education_pure_helpers():
    from job_apply_mcp.apply.mapper import pick_degree, pick_school
    opts = ["Associate's Degree", "Bachelor's Degree", "Master of Business Administration (M.B.A.)", "Master's Degree"]
    assert pick_degree("Master of Science", opts) == "Master's Degree"
    assert pick_degree("M.S.", opts) == "Master's Degree"
    assert pick_degree("BS", opts) == "Bachelor's Degree"
    assert pick_degree("MBA", opts) == "Master of Business Administration (M.B.A.)"
    assert pick_degree("Master of Science", ["Master of Business Administration (M.B.A.)"]) is None
    assert pick_school("University of Springfield", ["University of Springfield Global", "Other"]) == "Other"
    assert pick_school("University of Springfield", ["University of Springfield Global"]) is None


def test_profile_education_rows_newest_first():
    from job_apply_mcp.profile import Education, Profile
    p = Profile(education=[Education(school="A", start="2016-08", end="2020-05"),
                           Education(school="B", degree="MS", field="CS", start="2022-08", end="2024-05")])
    r = p.education_rows()
    assert [x["school"] for x in r] == ["B", "A"] and r[0]["start_year"] == "2022" and r[0]["discipline"] == "CS"


OPT_HTML = """<html><body><form id=f>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<label for=question_9>How did you hear about us?</label><div id=question_9_wrap></div>
<button type=submit>Submit application</button></form>
<script>__RS__
rs('question_9', ['Conference', 'News Article'], {multi: true});
</script></body></html>""".replace("__RS__", RS_JS)


@pytest.mark.parametrize("required", [False, True])
async def test_optional_field_left_empty_does_not_block(drv, required):
    srv, _ = _serve(OPT_HTML)
    form = ApplicationForm(ref="greenhouse:acme:3", fields=[
        FormField(key="first_name", label="First Name", kind=FieldKind.text),
        FormField(key="question_9", label="How did you hear about us?", kind=FieldKind.multiselect,
                  options=["Conference", "News Article"], required=required)])
    ans = {"first_name": "Alex", "question_9": ["Company Website"]}   # not an option: the fill fails, field stays empty
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/3", form, ans, {}, dry_run=True)
    assert res.status == ("needs_human" if required else "dry_run"), res.detail


GROUP_HTML = """<html><body><form id=f>
<label for=first_name>First Name</label><input id=first_name name=first_name>
<fieldset id="question_7[]"><legend>How did you hear about us?</legend>
<input type=checkbox id=c1 name="question_7[]" value=1><label for=c1>Conference</label>
<input type=checkbox id=c2 name="question_7[]" value=2><label for=c2>Company Website</label>
<input type=checkbox id=c3 name="question_7[]" value=3><label for=c3>News Article</label></fieldset>
<button type=submit>Submit application</button></form>
<script>document.getElementById('f').onsubmit = e => e.preventDefault();</script></body></html>"""


async def test_checkbox_group_ticks_only_the_matching_box(drv):
    srv, _ = _serve(GROUP_HTML)
    form = ApplicationForm(ref="greenhouse:acme:4", fields=[
        FormField(key="first_name", label="First Name", kind=FieldKind.text),
        FormField(key="question_7[]", label="How did you hear about us?", kind=FieldKind.multiselect,
                  options=["Conference", "Company Website", "News Article"])])
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/4", form,
                                    {"first_name": "Alex", "question_7[]": ["Company Website"]}, {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    page = drv._pages[-1] if getattr(drv, "_pages", None) else None
    if page is not None:
        assert await page.locator("#c2").is_checked() and not await page.locator("#c1").is_checked()
