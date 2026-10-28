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
  ADR-0070 adds contraction folding ("don't" == "do not"), the "(Not Hispanic or Latino)" guard and the closed
  `[demographics].is_veteran` table to demographic_pick.
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
    it, so a page embedding another job's form is refused. A query with a repeated `token` / `gh_jid`, or with both
    naming different ids, is refused with or without `job_id`."""
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
    # A posting id must be unambiguous: one value at most, whether given as `token`, `gh_jid` or both (equal).
    ids = {*(q := parse_qs(t.query, keep_blank_values=True)).get("token", []), *q.get("gh_jid", [])}
    if len(q.get("token", [])) > 1 or len(q.get("gh_jid", [])) > 1 or len(ids) > 1:
        return None
    if job_id and ids != {str(job_id)}:
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


# ADR-0070 amendment (#119): closed rewrites of standard self-ID wording that names the SAME question in more words.
# "race / ethnic group" is the race question; "a veteran or active member of the United States Armed Forces" is the
# plain veteran question (its options then decide; is_veteran answers only the closed table below). Nothing else.
_LABEL_REWRITES: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "race": ((re.compile(r"\b(ethnic|racial|race) groups?\b"), r"\1"),),
    "veteran": ((re.compile(r"\bveteran or (?:an )?active(?: duty)? (?:service )?member(?: of the (?:united states|u s|us)"
                            r" (?:armed forces|military))?\b"), "veteran"),),
}


def self_id_label_ok(label: str, key: str) -> bool:
    """The owner's self-ID value may answer this label: mapper.demographic_label_ok (every word generic self-ID wording
    or the topic's own words), after removing only an answer-instruction parenthetical such as "(please mark all that
    apply)" and applying the closed _LABEL_REWRITES of the same key. A label that mentions the topic inside another
    question (accommodation, language skills) fails."""
    if demographic_label_ok(label, key):
        return True
    t = normalize(_INSTRUCTION.sub(" ", label))
    for rx, rep in _LABEL_REWRITES.get(key, ()):
        t = rx.sub(rep, t)
    return demographic_label_ok(t, key)


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


# ADR-0070: contractions are the same words ("I don't have a disability" == "I do not have a disability").
_CONTRACTIONS = ((re.compile(r"\bdon t\b"), "do not"), (re.compile(r"\bdoesn t\b"), "does not"),
                 (re.compile(r"\bhaven t\b"), "have not"), (re.compile(r"\bi m\b"), "i am"),
                 (re.compile(r"\bisn t\b"), "is not"), (re.compile(r"\baren t\b"), "are not"))


def _canon(text: str) -> str:
    n = normalize(text)
    for rx, rep in _CONTRACTIONS:
        n = rx.sub(rep, n)
    return n


# ADR-0070: an option whose parenthetical asserts a Hispanic/Latino answer ("Asian (Not Hispanic or Latino)") is a
# second fact; it is taken only when the owner's own hispanic answer agrees.
_NOT_HISPANIC = re.compile(r"\bnot (?:of )?hispanic\b|\bnon hispanic\b")     # "(Not of Hispanic Origin)" too (#119)

# ADR-0070: `[demographics].is_veteran` ("Yes"/"No": ever served, protected or not) answers the plain veteran wording.
# Never derived from veteran_status: "not a PROTECTED veteran" does not mean "not a veteran". Not being a veteran does
# imply not being a protected veteran, so "No" also answers the protected-veteran "not" option.
_VETERAN_ANY = {
    "no": frozenset({"no i am not a veteran", "i am not a veteran", "not a veteran", "no not a veteran",
                     "i am not a protected veteran", "no i am not a protected veteran", "not a protected veteran",
                     # #119: never served -> neither a veteran nor an active member
                     "no i am not a veteran or active member", "i am not a veteran or active member",
                     "no i am not a veteran or an active member", "i am not a veteran or an active member"}),
    "yes": frozenset({"yes i am a veteran", "i am a veteran", "yes a veteran",
                      # #119: served (protected or not) -> "a veteran or active member" is true; never protected
                      "yes i am a veteran or active member", "i am a veteran or active member",
                      "yes i am a veteran or an active member", "i am a veteran or an active member"}),
}

# ADR-0070 amendment (#120): the standard disability self-ID answers (OFCCP form CC-305 and its older revision), as
# closed patterns over the contraction-folded text. Four classes, because the revisions state different facts:
#   yes          "I have a disability"                          (now)
#   yes_or_past  "I have a disability, or have had one in the past"  (now OR in the past)
#   no           "I do not have a disability"                   (now; says nothing about the past)
#   no_ever      "I do not have a disability and have not had one in the past"  (neither now nor in the past)
# A value answers an option only when the value implies it (_DISABILITY_IMPLIES): "no" never selects a "no_ever"
# option (that would assert "no past disability", which the owner did not state). A bare "Yes"/"No" value counts
# only on the status question itself (_YESNO_STATUS). Decline options stay the decline table's business.
_DISABILITY_CLASS = {
    "yes_or_past": re.compile(r"^(?:yes )?i have a disability or (?:i )?(?:have had one in the past|previously had a "
                              r"disability|had a disability in the past|have a history(?: or record)? of (?:having )?"
                              r"a disability)$"),
    "yes": re.compile(r"^(?:yes )?i have a disability$"),
    "no_ever": re.compile(r"^(?:no )?i do not have a disability(?: and (?:i )?have not had one in the past| or a "
                          r"history(?: or record)? of (?:having )?(?:one|a disability))$"),
    "no": re.compile(r"^(?:no )?i do not have a disability$"),
}
_DISABILITY_IMPLIES = {"yes": {"yes", "yes_or_past"}, "yes_or_past": {"yes_or_past"},
                       "no": {"no"}, "no_ever": {"no_ever", "no"}}


def disability_class(text: str, *, bare: bool = False) -> str | None:
    """The _DISABILITY_CLASS of a standard disability self-ID answer, else None. `bare`: a lone "Yes"/"No" counts."""
    n = _canon(text)
    if bare and n in ("yes", "no"):
        return n
    return next((c for c, rx in _DISABILITY_CLASS.items() if rx.match(n)), None)


def disability_gap(values: list[str], options: list[str], label: str = "") -> str | None:
    """Why no disability option was taken, when the reason is a missing PAST fact: the owner's "no" (now) answer
    against a form whose only "no" option also states no past disability. None otherwise."""
    status = bool(_YESNO_STATUS["disability"].match(normalize(label)))
    have = {disability_class(str(v), bare=status) for v in values if v and not is_decline_value(v)}
    if "no" in have and any(disability_class(o) == "no_ever" for o in options):
        return ("the only 'No' option also states no disability in the past; [demographics].disability_status does "
                "not say whether you had one in the past (e.g. 'No, I do not have a disability and have not had one "
                "in the past')")
    return None


_PLACEHOLDER = re.compile(r"^(?:please )?(?:select|choose)\b(?: an? (?:option|answer))?$")


def protected_only(options: list[str]) -> bool:
    """Every answer option (decline and placeholder options aside) names PROTECTED veteran status, i.e. the select has
    no plain "veteran" answer for `is_veteran` (ADR-0070 amendment)."""
    real = [o for o in options if not is_decline_value(o) and not _PLACEHOLDER.match(normalize(o))]
    return bool(real) and all(re.search(r"\bprotected\b", normalize(o)) for o in real)


def _paren_ok(option: str, hispanic: str) -> bool:
    tail = _PAREN_TAIL.search(option)
    if not tail or not _NOT_HISPANIC.search(normalize(tail.group(0))):
        return True
    return normalize(hispanic).split()[:1] == ["no"]


def demographic_pick(key: str | None, values: list[str], options: list[str], label: str = "", *,
                     hispanic: str = "", is_veteran: str = "") -> str | None:
    """The single option equal to one of the owner's `values` (tried in order, e.g. race_detail before race), or None.
    Equal means normalised equality, plus ONLY these closed equivalences:
      - gender: Male == Man, Female == Woman (one hit only);
      - an option that is the value followed by an explanatory parenthetical: "East Asian (inclusive of ...)" equals
        "East Asian" (never a prefix or substring: "Asian" never equals "East Asian (...)");
      - disability / hispanic: a profile sentence that starts with "Yes," / "No," equals a bare "Yes" / "No" option,
        ONLY when `label` is the status question itself (_YESNO_STATUS), never a question that mentions the topic.
    ADR-0070 adds: contractions fold ("don't" == "do not"); an option whose parenthetical says "Not Hispanic or
    Latino" is taken only when `hispanic` (the owner's answer) starts with "No"; and for veteran, after the
    veteran_status values, `is_veteran` ("Yes"/"No") answers the plain wording in _VETERAN_ANY.
    Decline values are the caller's business (decline table)."""
    opts = [o for o in options if o and normalize(o) not in ("", "select", "select ...")]
    for v in values:
        nv = _canon(str(v or ""))
        if not nv or is_decline_value(v):
            continue
        hits = [o for o in opts if _canon(o) == nv]
        if len(hits) == 1:
            return hits[0]
        if key == "gender":
            syn = next((g for g in _GENDER_SYN if nv in g), None)
            hits = [o for o in opts if syn and normalize(o) in syn]
            if len(hits) == 1:
                return hits[0]
        hits = [o for o in opts if _PAREN_TAIL.search(o) and _canon(_PAREN_TAIL.sub("", o)) == nv]
        if len(hits) == 1:
            return hits[0] if _paren_ok(hits[0], hispanic) else None
        if key in _YESNO_STATUS and _YESNO_STATUS[key].match(normalize(label)):
            m = re.match(r"^(yes|no) \S", nv)
            hits = [o for o in opts if m and normalize(o) == m.group(1)]
            if len(hits) == 1:
                return hits[0]
        if key == "disability":          # #120: the CC-305 answer family, only an option the value implies
            status = bool(_YESNO_STATUS["disability"].match(normalize(label)))
            ok = _DISABILITY_IMPLIES.get(disability_class(str(v), bare=status) or "", set())
            hits = [o for o in opts if disability_class(o) in ok]
            if len(hits) == 1:
                return hits[0]
    served = normalize(is_veteran).split()[:1]
    if key == "veteran" and served and served[0] in _VETERAN_ANY:
        hits = [o for o in opts if _canon(o) in _VETERAN_ANY[served[0]]]
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
                      r"transfer\w*|marketing|advertis\w*|research|public\w*|government|any|other|others|and|its|their|"
                      # ADR-0070: the name slot never holds a purpose, a method or a processing verb
                      r"ai|artificial|automat\w*|algorithm\w*|machine|to|for|with|via|by|us(?:e|es|ing)|collect\w*|"
                      r"process\w*|stor\w*|retain\w*|record\w*)\b")


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
