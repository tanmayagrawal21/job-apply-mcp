"""Location autocomplete helpers (geocoder-backed selects such as Greenhouse's "Location (City)").

Geocoders often return nothing for "City, ST", so the driver retries with progressively shorter queries and then
picks the suggestion that agrees with the remaining parts (state/country). Pure functions; no I/O."""
from __future__ import annotations

import re

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}


def _parts(value: str) -> list[str]:
    return [p.strip() for p in str(value).split(",") if p.strip()]


def location_queries(value: str) -> list[str]:
    """Queries to try, most specific first: as given, state spelled out, then city alone (deduplicated)."""
    parts = _parts(value)
    out = [str(value).strip()]
    if len(parts) >= 2 and parts[1].upper() in US_STATES:
        out.append(", ".join([parts[0], US_STATES[parts[1].upper()], *parts[2:]]))
    if parts:
        out.append(parts[0])
    seen: set[str] = set()
    return [q for q in out if q and not (q.lower() in seen or seen.add(q.lower()))]


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def pick_location(value: str, options: list[str]) -> str | None:
    """The suggestion that starts with the city and agrees with every later part (state abbreviation or name,
    country). Falls back to the first suggestion starting with the city only when no qualifier was given."""
    parts = _parts(value)
    if not parts or not options:
        return None
    city = parts[0].lower()
    starts = [o for o in options if o.strip().lower().startswith(city)]
    quals = parts[1:]
    if not quals:
        return starts[0] if starts else None
    for o in starts:
        ot = _tokens(o)
        if all(_tokens(q) <= ot or (q.upper() in US_STATES and _tokens(US_STATES[q.upper()]) <= ot) for q in quals):
            return o
    return None
