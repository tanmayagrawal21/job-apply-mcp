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
        d.onclick = () => { if (o.multi) { chosen.push(t); } else { chosen = [t]; }
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
          "question_3": ["Springfield", "Capital City"], "candidate-location": "Springfield, Illinois"}


async def test_react_select_options_are_committed_and_country_precedes_phone(drv):
    srv, posts = _serve(GH_HTML)
    res = await drv.fill_and_submit(f"http://127.0.0.1:{srv.server_port}/acme/jobs/1", GH_FORM, GH_ANS, {}, dry_run=True)
    assert res.status == "dry_run", res.detail
    assert not [p for p in posts if p[0] == "/submitted"]
    last = json.loads([p for p in posts if p[0] == "/state"][-1][1])
    assert "United States +1" in last["vals"] and last["vals"].count("Yes") == 1 and "No" in last["vals"]
    assert {"Springfield", "Capital City"} <= set(last["vals"]) and last["phone"] == "555-0101"


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
