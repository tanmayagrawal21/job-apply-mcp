"""ADR-0050 (#70): messaging/marketing opt-ins fail closed; flat [answers] never answer authorization or sponsorship
on a foreign or unconfirmed-country job. Fictional profile only (Alex Rivera, Springfield)."""
import pytest
from apply_fakes import F

from job_apply_mcp.apply.adapters import ProfileView
from job_apply_mcp.apply.mapper import map_fields, optin_channels
from job_apply_mcp.config import Settings
from job_apply_mcp.core.models import FieldKind
from job_apply_mcp.profile import Authorization, Contact, Profile

YN = ["Yes", "No"]
# flat patterns a real profile may hold: "opt" is the US work program, "now or in the future" a sponsorship answer
FLAT = {"opt": "Yes", "now or in the future": "Yes", "require sponsorship": "Yes", "sms": "No",
        "text message": "No", "recruiting": "Yes", "receive": "Yes", "whatsapp": "Yes"}


def view(answers=None, country="United States", **auth):
    a = {"work_authorized": True, "requires_sponsorship": True} | auth
    return ProfileView(Profile(contact=Contact(first_name="Alex", last_name="Rivera", city="Springfield",
                                               country=country),
                               authorization=Authorization.model_validate(a), answers=dict(FLAT) | (answers or {})),
                       Settings())


def run(label, kind=FieldKind.select, options=YN, required=True, p=None, job="US", company="Acme Robotics"):
    f = F("q", label, kind, required=required, options=list(options) if options else None)
    r = map_fields([f], p or view(), company=company, job_country=job)
    got = {a.key: (a.value, a.source) for a in r.answers}.get("q")
    run.gaps = r.profile_gaps
    return got, [x.key for x in r.unresolved], [x.key for x in r.omitted]


WHATSAPP = [
    "Do you opt-in to receive WhatsApp messages from Acme Recruiting?",
    "Do you opt in to receive Whatsapp messages from Acme Recruiting?",
    "Opt-in to WhatsApp updates",
    "I would like to receive updates via WhatsApp",
    "Can we contact you on Whats App about your application?",
    "WHATSAPP: may we message you about this role?",
]
OTHER_CHANNELS = [
    "Do you consent to receive SMS messages about your application?",
    "I agree to receive text messages from Acme Robotics",
    "Can we send you text alerts about interview scheduling?",
    "Would you like to receive recruiting updates by text?",
    "Opt in to mobile messaging from our recruiting team",
    "Sign me up for the Acme newsletter",
    "Would you like to receive marketing communications from Acme?",
    "Do you opt-in to receive Telegram messages from Acme?",
]


@pytest.mark.parametrize("label", WHATSAPP + OTHER_CHANNELS)
@pytest.mark.parametrize("job", ["US", "CA", None])
def test_required_optin_is_no_never_yes(label, job):
    assert run(label, job=job)[0] == ("No", "rule:messaging_optin")
    assert run(label, FieldKind.boolean, options=None, job=job)[0] == (False, "rule:messaging_optin")
    assert run(label, options=["Yes, opt me in", "No, thank you"], job=job)[0] == ("No, thank you",
                                                                                   "rule:messaging_optin")


@pytest.mark.parametrize("label", WHATSAPP + OTHER_CHANNELS)
def test_optional_optin_is_left_empty(label):
    assert run(label, required=False) == (None, [], ["q"])
    assert run(label, FieldKind.boolean, options=None, required=False) == (None, [], ["q"])


def test_only_an_exact_channel_yes_opts_in():
    yes_wa = view({"optin:whatsapp": "Yes"})
    assert run(WHATSAPP[0], p=yes_wa)[0] == ("Yes", "rule:messaging_optin")
    assert run(WHATSAPP[0], FieldKind.boolean, options=None, required=False, p=yes_wa)[0] == (True,
                                                                                              "rule:messaging_optin")
    # a Yes for one channel never opts in to another, or to a question naming two channels
    assert run(OTHER_CHANNELS[0], p=yes_wa)[0] == ("No", "rule:messaging_optin")
    assert run("Do you agree to receive WhatsApp and SMS messages?", p=yes_wa)[0] == ("No", "rule:messaging_optin")
    # flat patterns that merely contain the channel word are not that key
    assert run(WHATSAPP[0], p=view({"opt in whatsapp": "Yes", "re:whatsapp": "Yes"}))[0] == ("No",
                                                                                          "rule:messaging_optin")


def test_optout_and_unclear_options_stay_with_the_owner():
    assert run("Do you want to opt out of WhatsApp messages?") == (None, ["q"], [])
    assert run("Do you opt-in to receive WhatsApp messages?", options=["Sure", "Maybe later"]) == (None, ["q"], [])


@pytest.mark.parametrize("label,kind", [
    ("WhatsApp number", FieldKind.text),
    ("Do you have marketing experience?", FieldKind.select),
    ("How many years of SMS gateway experience do you have?", FieldKind.select),
])
def test_channel_word_alone_is_not_an_optin(label, kind):
    assert optin_channels(F("q", label, kind, options=YN if kind == FieldKind.select else None)) is None


def test_flat_opt_pattern_never_matches_opt_in():
    p = view()
    assert p.p.answer_for("Do you opt-in to the Acme talent survey?") is None
    assert p.p.answer_for("Are you on OPT?") == "Yes"


# ---------------------------------------------------------------- sponsorship on a non-home-country job
SPONSOR = ("Will you require Acme to sponsor you for a work permit now or in the future for the location(s) you "
           "selected in your previous response?")


@pytest.mark.parametrize("label", [
    SPONSOR,
    "Will you now or in the future require sponsorship to work in this role?",
    "Do you need a visa to work here, now or in the future?",
    "Would you require Acme to sponsor you?",
])
@pytest.mark.parametrize("job", ["CA", None])
def test_flat_answers_never_answer_sponsorship_on_foreign_or_unconfirmed_job(label, job):
    got, unresolved, _ = run(label, job=job)
    assert got is None and unresolved == ["q"]


def test_canada_job_uses_only_canada_facts():
    p = view(CA={"work_authorized": True, "requires_sponsorship": False})
    assert run("Will you now or in the future require sponsorship to work in this role?", p=p, job="CA")[0] == \
        ("No", "rule:requires_sponsorship")
    assert run("Would you require Acme to sponsor you?", p=p, job="CA")[:2] == (None, ["q"])   # company name: owner


def test_non_yes_no_sponsorship_options_on_a_foreign_job_stay_unresolved():
    opts = ["I will need sponsorship", "I hold an open work permit"]
    assert run("Visa sponsorship status", options=opts, job="CA")[:2] == (None, ["q"])


STRIPE_STYLE = ("Will you require Acme Robotics to sponsor you for a work permit now or in the future for the "
                "location(s) you selected in in your previous response?")


def test_us_job_answers_sponsor_you_wording_from_the_structured_fact():
    for auth in ({"requires_sponsorship": True}, {"US": {"requires_sponsorship": True}}):
        p = view(**auth)
        assert run(STRIPE_STYLE, p=p, job="US")[0] == ("Yes", "rule:requires_sponsorship")
        assert run("Will you now or in the future require sponsorship to work in this role?", p=p, job="US")[0] == \
            ("Yes", "rule:requires_sponsorship")
    p = view(requires_sponsorship=False)
    assert run(STRIPE_STYLE, p=p, job="US")[0] == ("No", "rule:requires_sponsorship")
    assert run(STRIPE_STYLE, FieldKind.boolean, options=None, p=p, job="US")[0] == (False, "rule:requires_sponsorship")


def test_us_job_without_a_structured_fact_is_unresolved_despite_flat_answers():
    p = view(requires_sponsorship=None)               # FLAT still holds "now or in the future" / "require sponsorship"
    assert run(STRIPE_STYLE, p=p, job="US")[:2] == (None, ["q"])
    assert run("Will you now or in the future require sponsorship?", p=p, job="US")[:2] == (None, ["q"])


def test_ca_job_with_only_a_us_fact_is_unresolved():
    p = view(requires_sponsorship=False)
    assert run(STRIPE_STYLE, p=p, job="CA")[:2] == (None, ["q"])
    assert run(STRIPE_STYLE, p=p, job=None)[:2] == (None, ["q"])


def test_home_job_relaxation_never_covers_another_named_country():
    p = view(requires_sponsorship=False)
    assert run("Will you require Acme Robotics to sponsor you for a work permit in Canada?", p=p, job="US")[:2] == \
        (None, ["q"])


# ---------------------------------------------------------------- home country: no US default (ADR-0050)
@pytest.mark.parametrize("country", ["Canada", "CA", "canada"])
def test_contact_country_sets_the_home_country(country):
    p = view(country=country, requires_sponsorship=False)
    assert p.lookup("home_country") == "CA"
    assert run("Will you now or in the future require sponsorship?", p=p, job="CA")[0] == \
        ("No", "rule:requires_sponsorship")
    assert run("Will you now or in the future require sponsorship?", p=p, job="US")[:2] == (None, ["q"])
    assert run.gaps == [{"country": "US", "fact": "requires_sponsorship", "key": "q"}]


def test_explicit_home_country_wins_over_contact_country():
    p = view(country="Canada", home_country="United States", requires_sponsorship=False)
    assert p.lookup("home_country") == "US"
    assert run(STRIPE_STYLE, p=p, job="US")[0] == ("No", "rule:requires_sponsorship")


@pytest.mark.parametrize("country", ["", "Springfield", "Atlantis"])
def test_no_confirmed_country_means_no_home_and_unresolved(country):
    p = view(country=country, requires_sponsorship=False, work_authorized=True)
    assert p.lookup("home_country") is None
    for job in ("US", "CA"):
        assert run("Will you now or in the future require sponsorship?", p=p, job=job)[:2] == (None, ["q"])
        assert run.gaps == [{"country": job, "fact": "requires_sponsorship", "key": "q"}]
        assert run("Are you authorized to work in this country?", p=p, job=job)[:2] == (None, ["q"])
    assert p.lookup("requires_sponsorship") is None and p.lookup("work_authorized") is None
    # a per-country table still answers for its own country
    q = view(country=country, requires_sponsorship=None, US={"requires_sponsorship": False})
    assert run("Will you now or in the future require sponsorship?", p=q, job="US")[0] == \
        ("No", "rule:requires_sponsorship")


# ---------------------------------------------------------------- control round (#73): deny-by-default contact asks
# flat patterns that answered these Yes before (word-boundary hits): each is a channel or contact word
CONTROL_FLAT = {"text": "Yes", "whatsapp": "Yes", "signal": "Yes", "contact": "Yes", "email": "Yes", "call": "Yes",
                "updates": "Yes", "future roles": "Yes", "texted": "Yes", "reach": "Yes"}
CONTROL_LABELS = [
    ("Is it OK to text you?", ["sms"]),
    ("May we text your mobile?", ["sms"]),
    ("Do you agree to be texted?", ["sms"]),
    ("Can we send you a text?", ["sms"]),
    ("Can we text you about interview scheduling?", ["sms"]),
    ("Can recruiters call or text you?", ["sms", "phone"]),
    ("Okay to WhatsApp you?", ["whatsapp"]),
    ("Would you like Signal updates?", ["signal"]),
    ("Can we reach you on Signal?", ["signal"]),
    ("Can we contact you with updates?", ["contact"]),
    ("Can we email you about future roles?", ["email", "marketing"]),
]


@pytest.mark.parametrize("label,chans", CONTROL_LABELS)
def test_control_labels_fail_closed(label, chans):
    p = view(CONTROL_FLAT)
    f = F("q", label, FieldKind.select, options=YN)
    assert optin_channels(f) == chans
    assert run(label, p=p)[0] == ("No", "rule:messaging_optin")
    assert run(label, FieldKind.boolean, options=None, p=p)[0] == (False, "rule:messaging_optin")
    assert run(label, p=p, required=False) == (None, [], ["q"])
    assert run(label, FieldKind.boolean, options=None, p=p, required=False) == (None, [], ["q"])
    yes = view(CONTROL_FLAT | {f"optin:{c}": "Yes" for c in chans})
    assert run(label, p=yes)[0] == ("Yes", "rule:messaging_optin")
    if len(chans) > 1:                  # every named channel needs its own Yes
        part = view(CONTROL_FLAT | {f"optin:{chans[0]}": "Yes"})
        assert run(label, p=part)[0] == ("No", "rule:messaging_optin")


@pytest.mark.parametrize("label", [
    "Do you have experience with Twilio SMS?",
    "Have you built SMS gateways?",
    "How many years of email marketing experience do you have?",
    "Can we contact your references?",
    "Can we contact your current employer?",
    "Have you previously been contacted by one of our recruiters?",
    "Are you comfortable being on call?",
    "Are you available for a phone screen this week?",
    "Do you know anyone who works at Acme Robotics?",
])
def test_skills_facts_and_other_rules_are_not_optins(label):
    assert optin_channels(F("q", label, FieldKind.select, options=YN)) is None


def test_open_to_receiving_newsletter_optional_is_empty():
    assert run("Are you open to receiving our newsletter?", required=False) == (None, [], ["q"])


@pytest.mark.parametrize("tail", ["for the location you selected", "for the locations you selected",
                                  "for the location(s) you selected in your previous response",
                                  "in the locations that you have selected"])
def test_selected_places_wording_on_a_us_job(tail):
    p = view(requires_sponsorship=False)
    assert run(f"Will you require Acme Robotics to sponsor you {tail}?", p=p, job="US")[0] == \
        ("No", "rule:requires_sponsorship")
    assert run(f"Will you require Acme Robotics to sponsor you {tail}?", p=p, job="CA")[:2] == (None, ["q"])


def test_engine_guard_covers_every_wording():
    from job_apply_mcp.apply.mapper import contact_permission_channels
    for label, chans in CONTROL_LABELS:
        assert contact_permission_channels(label) == chans
    assert contact_permission_channels("WhatsApp number") == []
    assert contact_permission_channels("Have you built SMS gateways?") == []
    assert contact_permission_channels("WhatsApp updates", choice=True) == ["whatsapp"]
