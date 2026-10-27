"""Consent-window attestations (ADR-0065): inside an owner-minted consent window, tick COMMON attestation and
legal-terms checkboxes of the classes the owner opted into in the trusted user config.

Safety argument (each condition is necessary; any one failing leaves the field to the owner, exactly as before):

1. **Opt-in, trusted config only.** `[consent] auto_attest = [...]` (default empty) is honoured only from the trusted
   user config (config._RELAXES + `Settings._auto_attest_trusted`, fail closed for programmatic Settings).
2. **Active window.** `consent.active(settings, "approve_submissions")` must return an owner-minted, unexpired,
   correctly scoped consent, at plan time AND again at submit time (`submit_check`).
3. **Required field only.** An optional attestation (an optional arbitration agreement, say) is never ticked.
4. **Deny-by-default classification.** The label must (a) trigger at least one allowed class, every triggered class
   must be enabled, (b) contain nothing from `_EXCLUDE` (AI/authorship, export control/citizenship, criminal history,
   restrictive covenants, specific facts, messaging opt-ins, privacy notices, signatures, ...), and (c) consist ONLY
   of words in the generic vocabulary plus the triggered classes' vocabularies (after removing the employer's name and
   a fixed ATS name list), modelled on mapper._ACK_VOCAB. Anything left over -> unresolved.
5. **Unambiguous value.** A checkbox (boolean) gets True; a choice field needs exactly one affirmative option whose
   words are all acknowledgement words; anything else -> unresolved.
6. **Logged.** Each tick records class, exact label, posting ref and consent id in the plan audit, the evidence
   folder (`attested.json`, append-only) and the approvals audit log, and is listed in the run report.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.models import Answer, FieldKind, FormField
from .mapper import canonical_key, eeo_topics, normalize, optin_channels

CLASSES: tuple[str, ...] = ("accuracy", "policy_ack", "background_check", "arbitration_terms")
SOURCE = "consent:auto_attest"          # Answer.source prefix: "consent:auto_attest:<class>[+<class>]"
SCOPE = "approve_submissions"

def _words(text: str) -> list[str]:
    return text.split()


# ---- (a) class triggers: what makes a label an attestation of that class ------------------------------------------
_TRIGGER: dict[str, re.Pattern[str]] = {
    "accuracy": re.compile(
        r"\b(?:true|truthful|accurate|accuracy|correct|complete|completeness)\b|\bbest of my knowledge\b|"
        r"\bmisrepresent\w*|\bfalsif\w*"),
    "policy_ack": re.compile(
        r"\bat will\b|\bequal (?:employment )?opportunity\b|\beeo\b|\bjob description\b|\bjob posting\b|"
        r"\bpolic(?:y|ies)\b"),
    "background_check": re.compile(
        r"\b(?:background|reference|references) (?:check|checks|checking|investigation|investigations|screening|"
        r"verification)\b|\bcheck (?:of )?(?:my )?references\b"),
    "arbitration_terms": re.compile(
        r"\barbitrat\w*|\bterms of (?:service|use)\b|\bterms and conditions\b|\buser agreement\b"),
}
# every triggered class also needs an attestation verb somewhere in the label
_VERB = re.compile(r"\b(?:certif(?:y|ies)|attest|affirm|declare|confirm|acknowledge|acknowledged|understand|agree|"
                   r"accept|consent|authori[sz]e|read|reviewed)\b")

# ---- (b) exclusions: never auto-ticked, whatever else the label says ----------------------------------------------
_EXCLUDE = re.compile(
    # AI use, authorship, "my own work", authenticity
    r"\bai\b|\ba i\b|artificial intelligence|\bllms?\b|chat ?gpt|\bgpt\b|copilot|generative|\bmachine\b|\bbots?\b|"
    r"\bautomat\w*|\bown (?:work|words|writing|effort)\b|\bmy own\b|personally|\bmyself\b|\bauthentic\w*|"
    r"\boriginal\w*|plagiar\w*|\bwrote\b|\bwritten\b|\bauthored\b|\bassist\w*|\bhelp\b|\bhuman\b|"
    # export control, ITAR, citizenship, national status, sanctions
    r"\bexport\w*|\bitar\b|\bear\b|\bcitizen\w*|\bnational\w*|\bus person\b|\bu s person\b|\bdeemed\b|sanction\w*|"
    r"embargo\w*|\bforeign\b|\bcountr\w*|\bimmigra\w*|\bvisa\b|\bsponsor\w*|\bauthori[sz]ed\b|\bauthori[sz]ation\b|"
    r"\beligib\w*|\bpermanent resident\b|\bgreen card\b|"
    # criminal history
    r"\bcriminal\b|\bconvict\w*|\bfelon\w*|\barrest\w*|\bmisdemeanou?r\w*|\boffen[cs]e\w*|\bguilty\b|\bplead\w*|"
    r"\bcharges?\b|\bcharged\b|\bprobation\b|\bparole\b|\bsex offender\b|\bexpunge\w*|"
    # restrictive covenants
    r"\bcompet\w*|\bnon ?compete\b|\brestrictive\b|\bcovenant\w*|\bsolicit\w*|\bnda\b|non ?disclosure|"
    r"\bconfidential\w*|\binvention\w*|intellectual property|\bproprietary\b|\bgarden leave\b|"
    # certification of a specific fact (degree, license, clearance, years, ...)
    r"\bdegree\w*|\bdiploma\w*|\blicen[cs]\w*|\bclearance\w*|\byears?\b|\bmonths?\b|\bgpa\b|\bgraduat\w*|"
    r"\btranscript\w*|\bbar\b|\bcpa\b|\bregistered\b|\b18\b|\bage\b|\bover the age\b|\bsalary\b|\bcompensation\b|"
    r"\bpay\b|\brelocat\w*|\bstart date\b|\bavailab\w*|\bdrug\w*|\balcohol\b|\bmedical\b|\bhealth\b|\bvaccin\w*|"
    r"\bcovid\b|\bphysical\b|\bcredit\b|\bdriv\w*|\bmotor vehicle\b|\bfingerprint\w*|\bsecurity\b|\bpolygraph\b|"
    r"\bconflicts? of interest\b|\brelative\w*|\bfamily\b|\bformer employee\b|\bpreviously (?:employed|worked)\b|"
    r"\bcurrently employed\b|\bfrom (?:my|the) resume\b|"
    # messaging / marketing / talent-community opt-ins (ADR-0050: owner-only) and contact permission
    r"\bmarketing\b|\bsms\b|\bmms\b|\btext\w*|\bmessag\w*|\bnewsletter\w*|\btalent (?:community|network|pool)\b|"
    r"\bfuture (?:opportunit\w*|roles|openings|positions|jobs)\b|\bjob alerts?\b|\bsubscri\w*|\bopt\w* ?in\b|"
    r"\bopt\w* ?out\b|\bpromot\w*|\be ?mails?\b|\bcall\w*|\bphone\w*|\bwhats ?app\b|\bcontact\w*|\bupdates?\b|"
    r"\bnotif\w*|\bkeep in touch\b|\bstay in touch\b|"
    # privacy notices keep their own owner preference (ADR-0034); data sharing, recordings, signatures, waivers
    # outside an arbitration clause, employment contracts
    r"\bprivacy\b|\bgdpr\b|\bccpa\b|\bpersonal (?:data|information)\b|\bdata\b|\bthird part\w*|\bshar\w*|"
    r"\btransfer\w*|\bsell\b|\brecord\w*|\btranscri\w*|\bmonitor\w*|\bvideo\b|\baudio\b|\bphoto\w*|\blikeness\b|"
    r"\bsocial media\b|\bsignature\w*|\bsign\b|\bsigned\b|\binitials?\b|\bfull name\b|\btype your\b|"
    r"\bterms (?:and conditions )?of employment\b|\bconditions of employment\b|"
    r"\bemployment (?:agreement|contract|offer|terms)\b|\boffer letter\b|\bunpaid\b|\btrial\b|\bovertime\b|"
    r"\bshifts?\b|\bweekends?\b|\btravel\b|\bfirearm\w*|\bweapon\w*|\bgovernment\b|\bclassified\b|"
    # negations, refusals and anything that is not an affirmative statement
    r"\bnot\b|\bno\b|\bnever\b|\bdecline\w*|\brefus\w*|\bwithdraw\w*|\brevok\w*|\bunless\b|\bexcept\b")

# ---- (c) vocabulary allowlists (deny-by-default, like mapper._ACK_VOCAB) ------------------------------------------
_GENERIC = frozenset(_words("""
i me my we our us you your the a an and or of to by in on for with that this these those it its is are am be been
being was were have has had will shall would may might can could should do does did any all each as at from under
upon such other herein hereby above below here following provided submitted given application applications form
applying apply applicant applicants candidate candidates process hiring yes please check box tick click selecting
select checking by understand understood acknowledge acknowledged acknowledges acknowledging agree agreed agrees
accept accepted accepts confirm confirmed confirms certify certifies certified attest attests affirm affirms declare
declares consent consents consenting read reviewed review that s if required mandatory required company employer
position role job also further meaning including includes
"""))
_VOCAB: dict[str, frozenset[str]] = {
    "accuracy": frozenset(_words("""
    information info statements statement answers answer responses response details contained included given
    true truthful accurate accuracy correct complete completeness best knowledge belief misrepresentation
    misrepresentations misrepresent misleading false falsification falsified omission omissions omit omitted
    incomplete inaccurate untrue material facts fact grounds ground result results resulting lead disqualification
    disqualify disqualified rejection rejected refusal removal consideration elimination termination terminated
    dismissal dismissed discharge employment hire hired immediate immediately later discovered discovery time
    point subsequent subsequently made making supplied supporting documents documentation resume cv certification
    attestation applicant s
    """)),
    "policy_ack": frozenset(_words("""
    at will employment employer employee either party parties terminate terminated termination relationship time
    reason cause notice without equal opportunity eeo statement statements policy policies job description posting
    position role requirements responsibilities duties details summary listed described company s handbook code
    conduct guidelines practices does discriminate basis law laws applicable protected status nothing contract
    guarantee guaranteed duration specified period
    """)),
    "background_check": frozenset(_words("""
    background reference references check checks checking investigation investigations screening screenings
    verification verify verified conduct conducted conducting perform performed performing obtain obtained
    authorize authorise authorizes authorises authorization future prior pre offer extended contingent contingency
    upon successful completion satisfactory part employment history previous past employers employer education
    educational professional may be will required necessary agent agents third provider providers company s
    """)),
    "arbitration_terms": frozenset(_words("""
    arbitration arbitrate arbitrated arbitrator mandatory binding mutual agreement agreements program policy
    dispute disputes resolution claims claim individual basis waive waiver waiving jury trial class collective
    action actions representative court rights right resolve resolved terms service use conditions user account
    candidate portal platform site website career careers profile create creating created
    """)),
}
# applicant-tracking systems that own a candidate-account Terms of Service (fixed list, not configurable)
_ATS_NAMES = frozenset(_words("""greenhouse lever ashby workday smartrecruiters icims taleo successfactors jobvite bamboohr
breezy workable recruitee teamtailor oracle sap"""))

_LEGAL_SUFFIX = frozenset(["inc", "llc", "ltd", "corp", "co", "corporation", "gmbh", "plc", "limited", "company"])

# options: the one affirmative choice, made only of acknowledgement words
_OPT_WORDS = frozenset(_words("""yes i agree accept acknowledge acknowledged confirm certify consent understand understood
have read and do the above terms i m am"""))
_OPT_NEG = _OPT_WORDS | {"no", "not", "don", "t", "decline", "do"}
_OPT_AFFIRM = re.compile(r"\b(?:yes|agree|accept|acknowledge\w*|confirm|certify|consent|understand\w*)\b")


def _strip_names(n: str, company: str, aliases: Sequence[str]) -> str:
    """Remove the employer's own name (full name, with and without a legal suffix) and its aliases."""
    names: set[str] = set()
    for c in (company, *aliases):
        words = normalize(c or "").split()
        if words:
            names.add(" ".join(words))
        while words and words[-1] in _LEGAL_SUFFIX:
            words = words[:-1]
            if words:
                names.add(" ".join(words))
    for name in sorted(names, key=len, reverse=True):
        n = re.sub(rf"\b{re.escape(name)}(?: s)?\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def classify(label: str, company: str = "", aliases: Sequence[str] = ()) -> tuple[str, ...]:
    """The attestation classes `label` belongs to, or () when it is not a pure, common attestation.
    Deny-by-default: any exclusion term, any word outside the vocabularies, or no attestation verb -> ()."""
    raw = (label or "").strip()
    if not raw or len(raw) > 600:
        return ()
    n = normalize(raw)
    if _EXCLUDE.search(n) or "?" in raw or eeo_topics(raw):
        return ()
    if canonical_key(raw) not in (None, "privacy_ack"):     # a canonical question has its own rule
        return ()
    if not _VERB.search(n):
        return ()
    stripped = _strip_names(n, company, aliases)
    hits = tuple(c for c in CLASSES if _TRIGGER[c].search(stripped))
    if not hits:
        return ()
    vocab = _GENERIC.union(*(_VOCAB[c] for c in hits))
    if "arbitration_terms" in hits:
        vocab = vocab | _ATS_NAMES
    if any(t not in vocab for t in stripped.split()):
        return ()
    return hits


def tick_value(f: FormField) -> Any | None:
    """The affirmative value for an attestation field, or None when the field shape is not an unambiguous tick."""
    if f.kind == FieldKind.boolean and not f.options:
        return True
    if f.kind not in (FieldKind.boolean, FieldKind.select, FieldKind.multiselect) or not f.options:
        return None
    aff = []
    for o in f.options:
        n = normalize(o)
        if _EXCLUDE.search(n):
            if not all(t in _OPT_NEG for t in n.split()):
                return None                         # an option naming something else: the owner decides
            continue                                # a plain "No" / "I do not agree"
        if _OPT_AFFIRM.search(n) and all(t in _OPT_WORDS for t in n.split()):
            aff.append(o)
    if len(aff) != 1:
        return None
    return [aff[0]] if f.kind == FieldKind.multiselect else aff[0]


# ---- settings / consent ----------------------------------------------------------------------------------------
def enabled_classes(settings: Any) -> frozenset[str]:
    """Opted-in classes; empty unless load_settings verified they come from the trusted user config."""
    cfg = getattr(settings, "consent", None)
    if cfg is None or not getattr(settings, "_auto_attest_trusted", False):
        return frozenset()
    return frozenset(c for c in cfg.auto_attest if c in CLASSES)


def active_window(settings: Any) -> Any | None:
    """The owner-minted consent covering approve_submissions right now (read-only, consumes nothing), or None."""
    from .. import consent
    with contextlib.suppress(Exception):            # any store problem fails closed
        return consent.active(settings, SCOPE)
    return None


def eligible(f: FormField, enabled: frozenset[str], company: str = "",
             aliases: Sequence[str] = ()) -> tuple[tuple[str, ...], Any] | None:
    """(classes, value) when `f` may be auto-ticked under `enabled`, else None."""
    if not enabled or not f.required or f.kind == FieldKind.file:
        return None
    if optin_channels(f) is not None:              # ADR-0050: messaging opt-ins stay owner-only
        return None
    cls = classify(f.label, company, aliases)
    if not cls or not set(cls) <= enabled:
        return None
    v = tick_value(f)
    return None if v is None else (cls, v)


def apply_auto_attest(mapped: Any, settings: Any, *, ref: str, company: str = "",
                      aliases: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Hook after map_fields: move eligible unresolved attestation fields into `mapped.answers`. Returns the log rows
    (also the plan audit's `auto_attest`). No opt-in or no active window -> [] and `mapped` is untouched."""
    enabled = enabled_classes(settings)
    if not enabled:
        return []
    c = active_window(settings)
    if c is None:
        return []
    rows: list[dict[str, Any]] = []
    keep = []
    for f in mapped.unresolved:
        got = eligible(f, enabled, company, aliases)
        if got is None:
            keep.append(f)
            continue
        cls, v = got
        mapped.answers.append(Answer(key=f.key, value=v, source=f"{SOURCE}:{'+'.join(cls)}",
                                     rationale=f"consent {c.id}: {'+'.join(cls)}"))
        rows.append({"key": f.key, "class": "+".join(cls), "label": f.label, "value": v, "ref": ref,
                     "consent_id": c.id})
    if rows:
        mapped.unresolved = keep
        ticked = {r["key"] for r in rows}
        mapped.skipped_voluntary = [f for f in mapped.skipped_voluntary if f.key not in ticked]
    return rows


def auto_keys(answers: Sequence[Answer]) -> set[str]:
    return {a.key for a in answers if (a.source or "").startswith(SOURCE)}


def reclassify(fields: Sequence[FormField], eff: dict[str, Any], company: str = "", aliases: Sequence[str] = (),
               owner_answer: Any = None) -> set[str]:
    """Stateless submit path (no cached plan): fields that hold an auto-attest-shaped tick and have no explicit
    owner answer. Uses every class, so a stale tick is caught even when the opt-in has since been removed."""
    out: set[str] = set()
    for f in fields:
        v = eff.get(f.key)
        if v in (None, "", False, []) or not classify(f.label, company, aliases):
            continue
        if owner_answer is not None:
            with contextlib.suppress(Exception):
                if owner_answer(f) is not None:
                    continue
        out.add(f.key)
    return out


def submit_check(settings: Any, fields: Sequence[FormField], eff: dict[str, Any], keys: set[str],
                 company: str = "", aliases: Sequence[str] = ()) -> tuple[str | None, Any | None, list[dict[str, Any]]]:
    """Re-verify every auto-ticked field at submit time. Returns (block reason or None, consent, log rows)."""
    keys = {k for k in keys if eff.get(k) not in (None, "", False, [])}
    if not keys:
        return None, None, []
    by = {f.key: f for f in fields}
    enabled = enabled_classes(settings)
    c = active_window(settings)
    if c is None:
        return (f"auto-attested fields {sorted(keys)[:6]} need an active owner consent window "
                f"({SCOPE}); re-plan or let the owner answer them"), None, []
    rows = []
    for k in sorted(keys):
        f = by.get(k)
        got = eligible(f, enabled, company, aliases) if f is not None else None
        if got is None or got[1] != eff.get(k):
            return f"auto-attested field {k!r} is no longer eligible (opt-in or wording changed); re-plan", None, []
        rows.append({"key": k, "class": "+".join(got[0]), "label": f.label, "value": eff.get(k),  # type: ignore[union-attr]
                     "consent_id": c.id})
    return None, c, rows


def write_evidence(data_dir: str | Path, ref: str, stage: str, rows: list[dict[str, Any]]) -> str | None:
    """Append one event to attested.json in the ref's evidence folder. Never raises."""
    if not rows:
        return None
    try:
        d = Path(data_dir).expanduser() / "evidence" / hashlib.sha256(ref.encode()).hexdigest()[:12]
        d.mkdir(parents=True, exist_ok=True)
        p = d / "attested.json"
        events: list[Any] = []
        if p.is_file():
            with contextlib.suppress(ValueError, OSError, AttributeError, TypeError):
                events = list(json.loads(p.read_text(encoding="utf-8")).get("events") or [])
        events.append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "stage": stage,
                       "auto_attested": [{**r, "ref": ref} for r in rows]})
        p.write_text(json.dumps({"ref": ref, "events": events}, indent=1, default=str), encoding="utf-8")
        return str(p)
    except OSError:
        return None
