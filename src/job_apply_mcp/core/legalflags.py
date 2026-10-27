"""Citizenship / U.S.-person requirement wording in posting text (ADR-0062). Pure regex text, no I/O: used only by the
citizenship policy gate (policy.gates), which alone decides; the posting flagger (platforms.textflags) stays broad.

A requirement sentence is flagged in every common form ("must be a U.S. citizen", "must be a citizen of the United
States", "U.S. citizenship is a requirement", "citizenship required", "requires U.S. citizenship", "U.S. citizens and permanent residents only",
"must be a U.S. national", "must be a lawful permanent resident or U.S. citizen", "... or green card holder").
Exactly one alternative is exempt: "citizen OR <work eligibility>" ("or eligible to work", "or authorized to work",
"or have work authorization"), which asks for the right to work, not for citizenship. EEO lines ("citizenship
status") and bare export-control mentions contain no requirement form and are never flagged."""
from __future__ import annotations

import re

_US = r"(?:u\.?\s?s\.?(?:a\.?)?|united states(?: of america)?|american)"
_PR = r"(?:green card holders?|(?:lawful |legal )?permanent resident(?:s|cy)?)"
# the one exemption: the clause after "or" grants work eligibility instead of citizenship
_WORK_OK = (r"(?!\w*,? *(?:or|and/or) (?:(?:be|are|is) )?(?:otherwise )?(?:legally )?(?:eligible|authori[sz]ed|permitted)"
            r" to work|\w*,? *(?:or|and/or) (?:have|has|hold|possess) (?:a )?(?:valid |current )?(?:work|employment) "
            r"authori[sz]ation|\w*,? *(?:or|and/or) (?:have |hold )?(?:the )?(?:legal )?right to work)")

CITIZENSHIP_REQUIREMENT = (
    rf"must (?:be|hold|have|possess) (?:an? )?(?:{_PR} or (?:an? )?)?{_US} (?:citizen(?:ship)?|national(?:ity)?)s?"
    rf"(?:,? (?:or|and/or|and) (?:an? )?{_PR})?{_WORK_OK}"
    rf"|must (?:be|hold|have|possess) (?:an? )?(?:citizen(?:ship)?|national(?:ity)?) of (?:the )?{_US}{_WORK_OK}"
    rf"|{_US} (?:citizen(?:ship)?|national(?:ity)?)s? (?:is |are )?(?:required|mandatory|a (?:requirement|must))"
    rf"|{_US} citizens?(?: (?:and|or|&) {_PR})? only\b"
    rf"|only (?:open to )?{_US} citizens"
    r"|(?<!no )(?<!not )citizenship (?:is )?(?:required|mandatory|a requirement)"
    rf"|requires? (?:{_US} )?citizenship(?! status| information| details)")

# a requirement that U.S. permanent residents also meet (the U.S.-person gate, not the citizenship gate)
_US_PERSON_FORM = re.compile(rf"{_PR}", re.IGNORECASE)


def requirement_kind(snippet: str) -> str:
    """'us_person' when the requirement also admits permanent residents, else 'citizenship'."""
    return "us_person" if _US_PERSON_FORM.search(snippet) else "citizenship"
