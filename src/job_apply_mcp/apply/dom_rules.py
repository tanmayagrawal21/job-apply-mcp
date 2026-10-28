"""Pure rules for DOM-only fields the Playwright driver fills itself (ADR-0063). Offline-testable; no browser.

  * greenhouse_frame_target / app_form_missing: a company page embedding the form in an iframe is replaced by the
    frame (ADR-0069); a page holding none of the posting's field keys is never filled.
  * greenhouse_offsite_embed: a job-boards.greenhouse.io URL that REDIRECTED to a company careers site is reopened as
    the Greenhouse embed, so the driver never fills a company page (job-alert boxes, iframes it cannot reach).
  * dom_demographic_key / demographic_pick: voluntary self-ID selects in the DOM (EEOC block, custom demographic
    survey). Matching stays exact (ADR-0039/0040); the only equivalences are listed here and are closed tables.
  * is_demographic_consent: the narrowly-worded "I consent to <company> processing my responses to the demographic
    survey above" box. Not a privacy notice (ADR-0034/0038); its own rule, see ADR-0063.
  * is_pronoun_label: a pronouns choice group (optional; filled only from the profile).
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from .mapper import demographic_label_ok, normalize

_DECLINE_WORDS = re.compile(r"^(decline|prefer not|i prefer not|i don t wish|i do not wish|i don t want|"
                            r"i do not want|i choose not|choose not|i wish not)")


def _gh_host(host: str) -> bool:
    return host == "greenhouse.io" or host.endswith(".greenhouse.io")


def greenhouse_offsite_embed(requested: str, landed: str, ref: str) -> str | None:
    """The embed URL when a Greenhouse board URL redirected OFF greenhouse.io (company-hosted board); else None.
    Only a redirect that started on greenhouse.io counts, so a page the caller deliberately opened is never replaced."""
    parts = ref.split(":")
    if len(parts) < 3 or parts[0] != "greenhouse" or not parts[1] or not parts[2]:
        return None
    req = (urlparse(requested).hostname or "").lower()
    got = (urlparse(landed).hostname or "").lower()
    if not _gh_host(req) or not got or _gh_host(got):
        return None
    return f"https://job-boards.greenhouse.io/embed/job_app?for={parts[1]}&token={parts[2]}"


def _unambiguous(u: str) -> bool:
    """False for any string a WHATWG parser (the browser) and urllib could read differently: a backslash (WHATWG
    treats it as '/' in http(s)), ASCII control/space characters including tab and newline (WHATWG strips them),
    DEL, or any non-ASCII character (IDNA)."""
    return bool(u) and all(0x21 <= ord(c) < 0x7f and c != "\\" for c in u)


def _authority_ok(u: str) -> bool:
    """No userinfo ('@') and no percent-encoding in the authority, so the checked host is the host that is opened."""
    netloc = urlparse(u).netloc
    return bool(netloc) and "@" not in netloc and "%" not in netloc


def greenhouse_frame_target(page_url: str, src: str, *, resolved: str | None = None,
                            job_id: str | None = None) -> str | None:
    """The URL to open for a Greenhouse application iframe found on a company page (ADR-0069), or None.

    `src` is the raw iframe src (or a frame URL); `resolved` is the browser's own WHATWG resolution of it
    (`new URL(src, document.baseURI).href`). Both must be unambiguous (no backslash, control/space or non-ASCII
    characters, no userinfo or percent-encoding in the authority), and when `resolved` is given its scheme and host
    must equal urllib's reading of `src`; `resolved` is then what is opened. Only http(s) on greenhouse.io or on the
    page's own origin is accepted. With `job_id` (the ref's posting id) the target's `token` or `gh_jid` must equal
    it, so a page embedding another job's form is refused."""
    if not src or not page_url or not _unambiguous(src.strip()) or not _unambiguous(page_url):
        return None
    target = urljoin(page_url, src.strip())
    if not _authority_ok(target):
        return None
    if resolved is not None:
        r, t0 = urlparse(resolved), urlparse(target)
        if (not _unambiguous(resolved) or not _authority_ok(resolved) or r.scheme != t0.scheme
                or (r.hostname or "").lower() != (t0.hostname or "").lower() or r.port != t0.port):
            return None
        target = resolved
    t, p = urlparse(target), urlparse(page_url)
    if t.scheme not in ("http", "https") or not t.hostname:
        return None
    if not (_gh_host(t.hostname.lower()) or (t.scheme, t.netloc.lower()) == (p.scheme, p.netloc.lower())):
        return None
    if target.split("#")[0] == page_url.split("#")[0]:
        return None
    if job_id:
        q = parse_qs(t.query)
        if str(job_id) not in (q.get("token", []) + q.get("gh_jid", [])):
            return None
    return target


def app_form_missing(api_keys: list[str], found: int) -> bool:
    """ADR-0069: the page holds none of the posting's own field ids/names, so the form in view is not the application
    form (a job-alert box, a newsletter, an unreached iframe). Needs at least two keys to judge; never fill then."""
    return len(api_keys) >= 2 and found == 0


# ---------------------------------------------------------------- voluntary self-identification

_TOPICS = (
    ("sexual_orientation", re.compile(r"\bsexual orientation\b")),
    ("pronouns", re.compile(r"\bpronouns?\b")),
    ("hispanic", re.compile(r"\bhispanic\b|\blatin[oax]\b|\blatine\b")),
    ("race", re.compile(r"\brace\b|\bracial\b|\bethnicity\b|\bethnic\b")),
    ("veteran", re.compile(r"\bveterans?\b|\barmed forces\b|\bmilitary\b")),
    ("disability", re.compile(r"\bdisabilit(y|ies)\b|\bdisabled\b")),
    ("gender", re.compile(r"\bgender\b")),
)
_NOT_PROFILE = re.compile(r"\btrans(gender)?\b|\bcisgender\b|\bnon binary\b|\bintersex\b")


def dom_demographic_key(label: str) -> str | None:
    """The [demographics] key for a DOM self-ID question the mapper's canonical_key does not classify, e.g. "I identify
    my gender as:". Exactly one topic must match; a label touching two topics (or trans/cis wording, which the
    profile has no key for) gets None, i.e. the decline/blank path, never another topic's value."""
    t = normalize(label)
    if _NOT_PROFILE.search(t):
        return None
    hits = {k for k, rx in _TOPICS if rx.search(t)}
    return hits.pop() if len(hits) == 1 else None


_INSTRUCTION = re.compile(r"\(\s*(?:please\s+)?(?:mark|select|check|choose|tick)\s+(?:all|any|one|only one)"
                          r"(?:\s+that\s+apply)?\s*\)", re.IGNORECASE)


def self_id_label_ok(label: str, key: str) -> bool:
    """The owner's self-ID value may answer this label: mapper.demographic_label_ok (every word generic self-ID wording
    or the topic's own words), after removing only an answer-instruction parenthetical such as "(please mark all that
    apply)". A label that mentions the topic inside another question (accommodation, language skills) fails."""
    return demographic_label_ok(label, key) or demographic_label_ok(_INSTRUCTION.sub(" ", label), key)


def is_decline_value(v: object) -> bool:
    n = normalize(str(v or ""))
    return not n or bool(_DECLINE_WORDS.match(n))


_GENDER_SYN = (frozenset({"male", "man"}), frozenset({"female", "woman"}))
_PAREN_TAIL = re.compile(r"\s*\([^()]*\)\s*$")


# The Yes/No lead equivalence applies ONLY to the status question itself, never to a question that merely mentions the
# topic (accommodation, work limitations, language skills ...). Matched against the normalised label.
_YESNO_STATUS = {
    "disability": re.compile(r"^(?:disability(?: status)?|i have a disability|do you (?:have|identify as "
                             r"(?:a person with|having)) a disability|are you (?:disabled|a person with a disability))$"),
    "hispanic": re.compile(r"^(?:hispanic(?: or)? latin[oax](?: ethnicity)?|are you(?: of)? hispanic(?: or)? latin[oax]"
                           r"(?: origin| ethnicity)?|do you identify as hispanic(?: or)? latin[oax])$"),
}


def demographic_pick(key: str | None, values: list[str], options: list[str], label: str = "") -> str | None:
    """The single option equal to one of the owner's `values` (tried in order, e.g. race_detail before race), or None.
    Equal means normalised equality, plus ONLY these closed equivalences:
      - gender: Male == Man, Female == Woman (one hit only);
      - an option that is the value followed by an explanatory parenthetical: "South Asian (inclusive of ...)" equals
        "South Asian" (never a prefix or substring: "Asian" never equals "South Asian (...)");
      - disability / hispanic: a profile sentence that starts with "Yes," / "No," equals a bare "Yes" / "No" option,
        ONLY when `label` is the status question itself (_YESNO_STATUS), never a question that mentions the topic.
    Decline values are the caller's business (decline table)."""
    opts = [o for o in options if o and normalize(o) not in ("", "select")]
    for v in values:
        nv = normalize(str(v or ""))
        if not nv or is_decline_value(v):
            continue
        hits = [o for o in opts if normalize(o) == nv]
        if len(hits) == 1:
            return hits[0]
        if key == "gender":
            syn = next((g for g in _GENDER_SYN if nv in g), None)
            hits = [o for o in opts if syn and normalize(o) in syn]
            if len(hits) == 1:
                return hits[0]
        hits = [o for o in opts if _PAREN_TAIL.search(o) and normalize(_PAREN_TAIL.sub("", o)) == nv]
        if len(hits) == 1:
            return hits[0]
        if key in _YESNO_STATUS and _YESNO_STATUS[key].match(normalize(label)):
            m = re.match(r"^(yes|no) \S", nv)
            hits = [o for o in opts if m and normalize(o) == m.group(1)]
            if len(hits) == 1:
                return hits[0]
    return None


# ---------------------------------------------------------------- demographic-data processing consent (ADR-0063)

_VERB = r"(?:(?:and |or )?(?:the )?(?:collect\w*|stor\w*|process\w*|us(?:e|ing)|retain\w*|retention|record\w*)(?: of)? )"
_DEMO_CONSENT = re.compile(
    r"^(?:by (?:checking|ticking|selecting) (?:this|the) box )?"
    r"(?:i (?:hereby )?(?:consent|agree) to |i give (?:my )?consent (?:to|for) )"
    r"(?P<who>(?:[a-z0-9&]+ ){0,6}?)"
    rf"{_VERB}+"
    r"my (?:responses|answers) (?:to|in) (?:the|these|this) (?:voluntary |optional )?"
    r"(?:demographic|self identification|eeo|diversity)(?: data)? (?:surveys?|questions?|questionnaires?|section)"
    r"(?: above| below)?$")
_WHO_BAD = re.compile(r"\b(share|sharing|shared|third|parties|party|partners?|affiliates?|vendors?|sell|sale|"
                      r"transfer\w*|marketing|advertis\w*|research|public\w*|government|any|other|others|and|its|their)\b")


def is_demographic_consent(label: str) -> bool:
    """True only for a label that consents to the posting company collecting / storing / processing the answers to
    the demographic survey on the same form. The whole label must fit the template; the only free slot is a short
    data-controller name (at most 6 words) that names no sharing, third party, marketing or other purpose."""
    if "&" in label:                       # "Acme & Initech": a second controller (fail closed)
        return False
    t = normalize(label)
    m = _DEMO_CONSENT.match(t)
    who = m.group("who") if m else ""
    return bool(m) and not _WHO_BAD.search(who or "")     # exactly one controller, no other purpose


# ---------------------------------------------------------------- pronouns

_PRONOUN_LABEL = re.compile(r"^(?:(?:what are |please (?:select|share|indicate|list) )?(?:your |my )?"
                            r"(?:preferred |personal )?pronouns?(?: \(optional\))?(?: optional)?)$")


def is_pronoun_label(label: str) -> bool:
    """A short label that only asks for pronouns ("Pronouns", "Preferred pronouns", "What are your pronouns")."""
    return bool(_PRONOUN_LABEL.match(normalize(label)))
