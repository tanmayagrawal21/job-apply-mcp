"""Deterministic prose checks beyond the lexicon lint (ADR-0023). Each returns (kind, detail, sentence) tuples."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date

from ..profile.facts import _STATUS_NOW, _WORK_NOW, check_letter_sentence
from ..profile.resume import Fact, completed_education, is_past, norm

Hit = tuple[str, str, str]
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
_PLACEHOLDER = re.compile(
    r"\[[^\]\n]{1,40}\]|\blorem\b|\bipsum\b|\bX{3,}\b|\byour (?:name|company|address)\b|\bcompany address\b|"
    r"\{\{[^}]*\}\}|<(?:company|name|date|insert)[^>]*>|\bTBD\b|\binsert [a-z ]{2,20} here\b", re.IGNORECASE)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|\]\(\)|^\s{0,3}#{1,6}\s|\*\*|```|`[^`\n]+`|^\s*>\s", re.MULTILINE)
_PUNCT_RUN = re.compile(r"([^\w\s])\1{3,}")
_ORG_WORD = (r"(?:Inc|LLC|Ltd|Corp|Corporation|Co|Company|Labs?|Laboratory|University|Institute|Group|Technologies|"
             r"Systems|Robotics|Analytics|Partners|Foundation|Health|Bank|Studios?)")
_ORG_LINE = re.compile(rf"^\W*([A-Z][\w&.\-]*(?:\s+[A-Z&][\w&.\-]*){{0,4}}\s+{_ORG_WORD})\b\.?\W*$", re.MULTILINE)
_DEAR = re.compile(r"^\s*Dear\s+(.{2,80}?)\s*[,:]?\s*$", re.MULTILINE | re.IGNORECASE)
_GENERIC_ADDR = re.compile(r"hiring|recruit|team|manager|committee|sir|madam|talent|people|human|department", re.IGNORECASE)
_FULL_DATE = re.compile(r"\b(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|"
                        r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b", re.IGNORECASE)


def placeholders(text: str) -> list[Hit]:
    return [("placeholder", f'"{m.group(0)}"', "") for m in _PLACEHOLDER.finditer(text)][:5]


def _tokens(text: str) -> list[str]:
    return text.split()


def degenerate(text: str, *, max_nonalpha: float = 0.25, repeat: int = 3) -> list[Hit]:
    out: list[Hit] = []
    sents = [norm(s) for s in _SENT.split(text) if len(norm(s).split()) >= 4]
    dup = [s for s, n in Counter(sents).items() if n > 1]
    if dup:
        out.append(("degenerate", f"repeated sentence ({len(dup)}x)", dup[0][:100]))
    w = norm(text).split()
    grams = Counter(tuple(w[i:i + 4]) for i in range(max(0, len(w) - 3)))
    loop = [g for g, n in grams.items() if n >= max(2, repeat)]
    if loop and not dup:
        out.append(("degenerate", f"n-gram loop (4-gram repeated {max(grams.values())}x)", " ".join(loop[0])))
    if m := _MD_IMAGE.search(text):
        out.append(("degenerate", "markdown/image syntax or non-text run", m.group(0)[:40]))
    elif m := _PUNCT_RUN.search(text):
        out.append(("degenerate", "non-text punctuation run", m.group(0)[:40]))
    toks = _tokens(text)
    if toks:
        bad = sum(1 for t in toks if not re.search(r"[A-Za-z]", t))
        if bad / len(toks) > max_nonalpha:
            out.append(("degenerate", f"{bad / len(toks):.0%} non-alphabetic tokens (> {max_nonalpha:.0%})", ""))
    return out


def addressee_orgs(text: str) -> list[str]:
    """Organisations named as the letter's addressee (address block or salutation)."""
    head = "\n".join(text.strip().splitlines()[:12])
    orgs = [m.group(1).strip() for m in _ORG_LINE.finditer(head)]
    for m in _DEAR.finditer(head):
        who = m.group(1)
        if m2 := re.search(r"\b(?:at|of|from)\s+(.+)$", who):
            orgs.append(m2.group(1).strip())
        else:
            cand = re.sub(r"\b(?:hiring|recruiting|recruitment|talent|selection|search|committee|team|manager|"
                          r"department|human|resources|people|staff|members?)\b", "", who, flags=re.IGNORECASE).strip(" ,.&-")
            if cand and not _GENERIC_ADDR.fullmatch(cand) and re.match(r"[A-Z]", cand) and len(cand.split()) <= 5 \
                    \
                    and (cand != who.strip(" ,.&-") or re.search(_ORG_WORD + "|[A-Z]{2,}", cand)):
                orgs.append(cand)
    return list(dict.fromkeys(orgs))


def _same_org(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    return bool(na and nb and (na in nb or nb in na))


def addressee(text: str, company: str) -> list[Hit]:
    if not company:
        return []
    return [("addressee", f"letter is addressed to '{o}', not '{company}'", "")
            for o in addressee_orgs(text) if not _same_org(o, company)][:2]


def closing(text: str, patterns: list[str]) -> list[Hit]:
    if not patterns:
        return []
    t = text.strip()
    if any(re.search(p + r"[\s\W]*\Z", t, re.IGNORECASE) for p in patterns):
        return []
    return [("closing", "required closing line template missing", t[-80:])]


def temporal(text: str, resume: list[Fact], today: date) -> list[Hit]:
    """Tense claims that contradict resume dates (graduated vs completing; ended role vs current)."""
    out: list[Hit] = []
    if not resume:
        return out
    done = completed_education(resume, today)
    open_edu = [f for f in resume if f.section == "education" and (f.current or not is_past(f.end, today))]
    for s in (x.strip() for x in _SENT.split(text) if x.strip()):
        if _STATUS_NOW.search(s) and done and not open_edu:
            e = max(done, key=lambda f: f.end)
            out.append(("temporal", f"resume shows {e.title} completed {e.end}", s))
        if _WORK_NOW.search(s):
            for f in resume:
                if f.section == "experience" and f.org and norm(f.org) in norm(s) and not f.current and f.end:
                    out.append(("temporal", f"resume shows {f.title} at {f.org} ended {f.end}", s))
        for f in resume:
            if f.section == "experience" and f.current and f.org and norm(f.org) in norm(s) \
                    and re.search(r"\b(?:formerly|previously|was|left|ended)\b", s, re.IGNORECASE):
                out.append(("temporal", f"resume shows {f.title} at {f.org} is current (no end date)", s))
    return out[:3]


def exemplar_entities(exemplar: str) -> list[str]:
    """Addressees/orgs/full dates that belong to the exemplar letter and must never leak into new text."""
    ents = addressee_orgs(exemplar) + [m.group(0) for m in _FULL_DATE.finditer(exemplar)]
    for m in _DEAR.finditer("\n".join(exemplar.strip().splitlines()[:12])):
        who = m.group(1).strip()
        if not _GENERIC_ADDR.fullmatch(who) and re.match(r"(?:Dr|Prof|Mr|Ms|Mrs)\.?\s+[A-Z]", who):
            ents.append(who)
    return list(dict.fromkeys(e for e in ents if len(e) > 3))


def leakage(text: str, exemplar: str, company: str = "") -> list[Hit]:
    low = text.lower()
    return [("leakage", f"exemplar-specific '{e}' appears in the draft", "")
            for e in exemplar_entities(exemplar) if e.lower() in low and not (company and _same_org(e, company))][:3]


def fact_conflicts(text: str, resume: list[Fact], today: date) -> list[Hit]:
    """Sentences that contradict the resume (title, date span, metric, status). The user-letter admission check
    (ADR-0023) applied to prose the server did not write (ADR-0044)."""
    if not resume:
        return []
    out: list[Hit] = []
    for s in (x.strip() for x in _SENT.split(text) if x.strip()):
        ex = check_letter_sentence(s, resume, today)
        if ex:
            out.append(("fact", ex.reason + (f"; resume: {ex.correction}" if ex.correction else ""), s))
    return out[:3]


# Latin look-alikes from other scripts (Cyrillic, Greek, IPA) that NFKC leaves alone (ADR-0044).
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "һ": "h", "ӏ": "l",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "І": "I", "Ј": "J", "Ѕ": "S", "Ԁ": "D", "Ԛ": "Q", "Ԝ": "W", "Ү": "Y", "Ӏ": "I",
    "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X", "ɑ": "a", "ɡ": "g", "ɩ": "i", "ʟ": "L", "ı": "i",
})
_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def format_chars(text: str) -> list[str]:
    """Unicode format (Cf) characters: zero-width space/joiners, soft hyphen, BOM, bidi controls (ADR-0044)."""
    import unicodedata
    return [f"U+{ord(c):04X}" for c in text if unicodedata.category(c) == "Cf"]


def scan_forms(text: str) -> list[str]:
    """Views of `text` for deterministic phrase scans: NFKC with Cf characters removed and confusables mapped to
    Latin, then the same with every run of punctuation, slug separators and line breaks collapsed to one space."""
    import unicodedata
    t = unicodedata.normalize("NFKC", text)
    t = "".join(c for c in t if unicodedata.category(c) != "Cf").translate(_CONFUSABLES)
    t = unicodedata.normalize("NFKC", t)
    return [t, " ".join(_PUNCT.sub(" ", t).split())]
