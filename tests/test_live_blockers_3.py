"""ADR-0059: e2e blockers - unconfirmed US job country, graduation and citizenship false gates, "based in, or willing
to relocate to" questions and owner corrections. Fictional data only (Jane Doe, Springfield IL, Acme Robotics)."""
import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import PolicyGates, ProfileView
from job_apply_mcp.apply.mapper import job_country, map_fields
from job_apply_mcp.config import PolicyCfg, Settings
from job_apply_mcp.core.models import ApplicationForm, Decision, FieldKind, Posting
from job_apply_mcp.platforms import textflags as tf
from job_apply_mcp.platforms.greenhouse import GreenhouseAdapter
from job_apply_mcp.policy import gates as g
from job_apply_mcp.policy.gates import GateContext
from job_apply_mcp.profile import Authorization, Contact, Education, Preferences, Profile, Work

YN = ["Yes", "No"]
S = Decision.skip


class _Stored:
    """A persona stand-in holding the owner's answers to recorded profile gaps."""

    def __init__(self, answers):
        self.answers = answers

    def stored(self, key):
        return self.answers.get(key)


def view(city="Springfield", region="IL", reloc=None, persona=None, **auth):
    a = {"work_authorized": True, "requires_sponsorship": False} | auth
    v = ProfileView(Profile(contact=Contact(first_name="Jane", last_name="Doe", city=city, region=region,
                                            country="United States"),
                            work=[Work(company="Initech", title="Engineer", start="2024-01")],
                            preferences=Preferences(willing_to_relocate=reloc),
                            authorization=Authorization.model_validate(a)), Settings())
    v.persona = persona
    return v


def run(label, kind=FieldKind.boolean, options=YN, p=None, job="US", locs=(), company="Acme Robotics"):
    f = F("q", label, kind, options=list(options) if options else None)
    r = map_fields([f], p or view(), company=company, job_country=job, job_locations=list(locs))
    run.gaps = [x.get("fact") for x in r.profile_gaps]
    return {a.key: (a.value, a.source) for a in r.answers}.get("q")


# ---------------------------------------------------------------- explicit country vs. abbreviations (ADR-0059 #54)
def test_explicit_country_rule_needs_the_full_country_name():
    full = "Are you authorized to work lawfully in the United States for Acme Robotics?"
    assert run(full, job=None) == (True, "rule:work_authorized")
    assert run("Are you authorized to work in the USA for Acme Robotics?", job=None) == (True, "rule:work_authorized")
    for short in ("Are you authorized to work in the U.S. for Acme Robotics?",
                  "Will Acme Robotics need to sponsor you to work in the US?"):
        assert run(short, job=None) is None              # ADR-0053: abbreviations keep needing a confirmed US job
        assert run(short, job="US") is not None
    assert run(full, job="CA") is None


# ---------------------------------------------------------------- "the country in which you are applying"
@pytest.mark.parametrize("label,key", [
    ("Are you authorized to work in the country in which you are applying?", "work_authorized"),
    (("Do you now, or will you in the future, require sponsorship for employment in the country which you are "
      "applying?"), "requires_sponsorship"),
])
def test_country_you_are_applying_in_is_the_job_country(label, key):
    assert run(label)[1] == f"rule:{key}"
    assert run(label, job=None) is None and run(label, job="CA") is None


# ---------------------------------------------------------------- job country confirmation
def test_a_list_of_us_cities_confirms_a_us_job():
    assert job_country("San Francisco, CA; Seattle, WA; New York, NY", ["US"]) == "US"
    assert job_country("San Francisco, CA; Toronto, ON", ["US"]) is None
    assert job_country("Seattle, WA; Remote", ["US"]) is None
    assert job_country("San Francisco, CA; EMEA", ["US"]) is None
    assert job_country("San Francisco, CA; Seattle, WA", ["US", "CA"]) is None


def test_greenhouse_office_locations_and_remote_country_names():
    gh = GreenhouseAdapter.__new__(GreenhouseAdapter)
    assert gh._countries({"offices": [{"name": "HQ", "location": "Acme HQ - Mountain View, CA"}]}) == ["US"]
    assert gh._countries({"offices": [{"name": "Remote - United States", "location": None}]}) == ["US"]
    assert gh._countries({"offices": [{"name": "United States (Remote)", "location": None}]}) == ["US"]
    assert gh._countries({"offices": [{"name": "US - Remote Zone 1", "location": None}]}) == ["UNCONFIRMED"]
    assert gh._countries({"offices": [{"name": "Canada", "location": "Seattle, WA"}]}) == ["UNCONFIRMED"]
    assert gh._countries({"offices": [{"name": "X", "location": "Toronto, ON"}]}) == ["UNCONFIRMED"]


# ---------------------------------------------------------------- graduation gate: clear mismatch only
def gate(name, text, cfg, **kw):
    return g.GATES[name](GateContext(text=text, **kw), cfg)


@pytest.mark.parametrize("text", [
    "Open to the class of 2026, 2027, or 2028.",
    "For students graduating by June 2028.",
    "Class of 2028 or earlier.",
    "Graduates from 2024 onwards are welcome.",
    "We love hiring graduates.",
])
def test_ambiguous_or_open_graduation_wording_never_gates(text):
    assert gate("graduation", text, PolicyCfg(graduation="2026-05")) is None
    assert not tf.grad_window_conflict(tf.grad_windows(text), "2026-05")


def test_graduation_gate_uses_the_profile_date_and_flex():
    cfg = PolicyCfg(graduation="2024-05", graduation_flex_months=2)
    text = "Software Engineer, new grad: class of 2028."
    assert gate("graduation", text, cfg, graduation="2028-05") is None       # the profile's date wins
    assert gate("graduation", text, cfg)[0] == S                               # policy.graduation fallback
    assert gate("graduation", "Class of 2027.", PolicyCfg(graduation_flex_months=2), graduation="2026-11") is None
    assert gate("graduation", "Class of 2027.", PolicyCfg(), graduation="2026-11")[0] == S
    assert tf.grad_window_conflict(["Class of 2027"], "2026-11", 2) is False


async def test_policy_gates_read_the_profile_graduation():
    prof = Profile(contact=Contact(first_name="Jane", last_name="Doe"),
                   education=[Education(school="Springfield State", degree="BS", end="2028-05")])
    pg = PolicyGates(Settings(), ProfileView(prof, Settings()))
    post = Posting(platform="greenhouse", ref="greenhouse:acme:1", company="Acme Robotics", title="Intern",
                   location="", url="https://example.invalid/1", description="Open to the class of 2028.")
    res = await pg.evaluate(post, ApplicationForm(ref="greenhouse:acme:1", fields=[]))
    assert res.decision == Decision.submit


# ---------------------------------------------------------------- citizenship gate: explicit requirement only
@pytest.mark.parametrize("text", [
    "We consider all applicants without regard to race, sex, national origin or citizenship status.",
    "Applicants must be U.S. citizens or eligible to work in the United States.",
    "Must be a U.S. citizen or authorized to work in the U.S.",
    "This role may be subject to export control regulations; we may ask about your citizenship.",
])
def test_boilerplate_citizenship_mentions_do_not_gate(text):
    assert gate("citizenship", text, PolicyCfg()) is None
    assert "citizenship" not in tf.flag_text(text)


def test_explicit_citizenship_requirement_still_gates():
    for text in ("U.S. citizenship is required for this role.", "Must be a U.S. citizen.", "US citizens only."):
        assert gate("citizenship", text, PolicyCfg())[0] == S
        assert gate("citizenship", text, PolicyCfg(us_citizen=True)) is None


def test_posting_flag_snippets_are_sorted_to_their_own_gate():
    ctx = {"flags": {"citizenship": ["ITAR"]}}
    assert gate("citizenship", "", PolicyCfg(us_person=True), **ctx) is None    # an ITAR snippet is not citizenship
    assert gate("citizenship", "", PolicyCfg(), **ctx)[1] == "US-person/ITAR restriction"
    assert gate("citizenship", "", PolicyCfg(), flags={"citizenship": ["export-control"]}) is None


# ---------------------------------------------------------------- "based in, or willing to relocate to, <place>"
BASED = "Are you based in, or willing to relocate to, the Denver metro area?"


def test_based_or_relocate_from_residence_and_relocation():
    assert run(BASED, p=view(reloc=True)) == (True, "rule:based_or_relocate")
    assert run(BASED, p=view(city="Denver", region="CO", reloc=False)) == (True, "rule:based_or_relocate")
    assert run(BASED, p=view(reloc=False)) == (False, "rule:based_or_relocate")       # elsewhere and not relocating
    assert run(BASED, p=view()) is None and run.gaps == ["preferences.willing_to_relocate"]   # ask the owner
    assert run("Are you based in or willing to relocate to the Chicago area?", p=view(reloc=False)) is None
    generic = "Are you based in, or willing to relocate to, this location?"
    assert run(generic, p=view(reloc=False), locs=["Springfield, IL"]) == (True, "rule:based_or_relocate")
    assert run(BASED, FieldKind.select, YN, p=view(reloc=True)) == ("Yes", "rule:based_or_relocate")


def test_owner_correction_for_a_relocation_gap_is_used():
    owner = _Stored({"profile.preferences.willing_to_relocate": "Yes"})
    assert run(BASED, p=view(persona=owner)) == (True, "rule:based_or_relocate")
    no = _Stored({"profile.preferences.willing_to_relocate": "No"})
    assert run(BASED, p=view(persona=no)) == (False, "rule:based_or_relocate")
    assert run(BASED, p=view(reloc=False, persona=owner)) == (False, "rule:based_or_relocate")   # the profile wins
