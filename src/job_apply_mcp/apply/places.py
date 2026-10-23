"""Country and place data for deny-by-default location matching (ADR-0040).

Everything here is DATA: country names, first-level subdivisions (names and codes), extra code collisions, a small
city table for ambiguous codes, shared city names, region words and per-country visa lexicons. Adding a country means
adding rows, not code. No network, no LLM.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

# ISO 3166-1 alpha-2 (plus "UK", which postings use for GB)
ISO2 = frozenset((  # noqa: SIM905
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ "
    "CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO "
    "FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE "
    "JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO "
    "MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW "
    "PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM "
    "TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW").split())
_ISO_ALIASES = {"UK": "GB"}

# country -> name pattern on normalized text (lowercase, punctuation -> spaces). Used as a location marker and to map a
# structured country name to ISO-2. "georgia" is deliberately absent (US state and country).
COUNTRY_NAMES: dict[str, str] = {
    "US": r"united states(?: of america)?|usa|u s(?: a)?|us",
    "CA": r"canada", "GB": r"united kingdom|uk|u k|great britain|britain|gb", "IE": r"(?<!northern )ireland",
    "IN": r"india", "AU": r"australia", "NZ": r"new zealand", "DE": r"germany|deutschland", "FR": r"france",
    "NL": r"netherlands|the netherlands|holland", "ES": r"spain", "IT": r"italy", "PT": r"portugal", "PL": r"poland",
    "SE": r"sweden", "NO": r"norway", "DK": r"denmark", "FI": r"finland", "CH": r"switzerland", "AT": r"austria",
    "BE": r"belgium", "LU": r"luxembourg", "CZ": r"czechia|czech republic", "RO": r"romania", "UA": r"ukraine",
    "GR": r"greece", "HU": r"hungary", "EE": r"estonia", "BG": r"bulgaria", "TR": r"turkey|turkiye",
    "IL": r"israel", "AE": r"united arab emirates|uae", "PK": r"pakistan", "CN": r"china", "HK": r"hong kong",
    "TW": r"taiwan", "JP": r"japan", "KR": r"south korea|korea", "SG": r"singapore", "PH": r"philippines",
    "VN": r"vietnam|viet nam", "ID": r"indonesia", "MY": r"malaysia", "TH": r"thailand", "MX": r"(?<!new )mexico",
    "BR": r"brazil|brasil", "AR": r"argentina", "CO": r"colombia", "CL": r"chile", "PE": r"peru", "UY": r"uruguay",
    "CR": r"costa rica", "HN": r"honduras", "JM": r"jamaica", "ZA": r"south africa", "NG": r"nigeria",
    "KE": r"kenya", "EG": r"egypt", "LR": r"liberia",
}
_NAME_RES = {c: re.compile(rf"\b(?:{p})\b") for c, p in COUNTRY_NAMES.items()}

# first-level subdivisions: full names and short codes (lowercase)
SUBDIVISIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "US": {"names": ("alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
                     "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
                     "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
                     "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
                     "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
                     "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
                     "washington", "west virginia", "wisconsin", "wyoming", "district of columbia"),
           "codes": ("al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks",
                     "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny",
                     "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
                     "wi", "wy", "dc")},
    "CA": {"names": ("alberta", "british columbia", "manitoba", "new brunswick", "newfoundland and labrador",
                     "newfoundland", "nova scotia", "northwest territories", "nunavut", "ontario",
                     "prince edward island", "quebec", "saskatchewan", "yukon"),
           "codes": ("ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt")},
    "AU": {"names": ("new south wales", "victoria", "queensland", "western australia", "south australia", "tasmania",
                     "australian capital territory", "northern territory"),
           "codes": ("nsw", "vic", "qld", "wa", "sa", "tas", "act", "nt")},
    "IN": {"names": ("andhra pradesh", "karnataka", "kerala", "tamil nadu", "telangana", "maharashtra", "gujarat",
                     "rajasthan", "west bengal", "uttar pradesh", "delhi", "new delhi", "haryana", "punjab",
                     "madhya pradesh", "bihar", "odisha", "goa", "assam"),
           "codes": ("ap", "ka", "kl", "tn", "tg", "ts", "mh", "gj", "rj", "wb", "up", "dl", "hr", "pb", "mp", "br",
                     "od", "ga", "as")},
    "GB": {"names": ("england", "scotland", "wales", "northern ireland"), "codes": ()},
}
# collisions not visible from the tables above (Italian provinces, older Indian codes)
EXTRA_COLLISIONS: dict[str, frozenset[str]] = {"US": frozenset(["ct", "mi", "or", "ri", "ut", "vt"])}
# an ambiguous code counts only with one of these cities in front of it
CITY_CODES: dict[str, frozenset[tuple[str, str]]] = {
    "US": frozenset([("san francisco", "ca"), ("los angeles", "ca"), ("san diego", "ca"), ("san jose", "ca"),
                     ("palo alto", "ca"), ("mountain view", "ca"), ("sunnyvale", "ca"), ("long beach", "ca"),
                     ("chicago", "il"), ("boston", "ma"), ("atlanta", "ga"), ("denver", "co"), ("boulder", "co"),
                     ("philadelphia", "pa"), ("pittsburgh", "pa"), ("baltimore", "md"), ("indianapolis", "in"),
                     ("new orleans", "la"), ("minneapolis", "mn"), ("arlington", "va"), ("phoenix", "az"),
                     ("nashville", "tn"), ("charlotte", "nc"), ("raleigh", "nc"), ("louisville", "ky"),
                     ("st louis", "mo"), ("boise", "id"), ("seattle", "wa"), ("redmond", "wa"), ("bellevue", "wa"),
                     ("spokane", "wa"), ("tacoma", "wa"), ("hartford", "ct"), ("stamford", "ct"),
                     ("new haven", "ct"), ("detroit", "mi"), ("ann arbor", "mi"), ("grand rapids", "mi"),
                     ("portland", "or"), ("eugene", "or"), ("salt lake city", "ut"), ("provo", "ut"), ("lehi", "ut"),
                     ("burlington", "vt"), ("providence", "ri")]),
    "CA": frozenset([("st john s", "nl"), ("saskatoon", "sk"), ("regina", "sk"), ("whitehorse", "yt"),
                     ("yellowknife", "nt"), ("iqaluit", "nu"), ("charlottetown", "pe")]),
    "AU": frozenset([("adelaide", "sa"), ("darwin", "nt"), ("fremantle", "wa"), ("perth", "wa")]),
    "IN": frozenset([("chennai", "tn"), ("coimbatore", "tn"), ("hyderabad", "tg"), ("mumbai", "mh"), ("pune", "mh"),
                     ("gurugram", "hr"), ("gurgaon", "hr"), ("bhopal", "mp"), ("indore", "mp"), ("patna", "br"),
                     ("panaji", "ga"), ("guwahati", "as")]),
}
# well-known city names that exist in several countries -> the country they usually mean (None: no single one). A
# "City, XX" code decides for them only in that home country ("Sydney, NSW" is AU; "Paris, TX" needs "USA").
SHARED_CITY_NAMES: dict[str, str | None] = {"perth": "AU", "melbourne": "AU", "sydney": "AU", "london": "GB",
                                            "paris": "FR", "birmingham": "GB", "cambridge": "GB", "hamilton": None,
                                            "victoria": "CA", "vancouver": "CA", "kingston": "JM"}
# regions or several places: a location naming one is never a single country
MULTI_REGION = re.compile(r"\b(?:americas?|north america|latam|latin america|emea|apac|asia|europe|global|worldwide|"
                          r"international|anywhere|multiple|several|various|or|and|also|plus|possible)\b|[;/|&+]",
                          re.IGNORECASE)
# parentheticals that carry no place and may be dropped ("(HQ)", "(Hybrid)", "(PST)")
_NO_PLACE_PAREN = re.compile(r"hq|hybrid|remote|on ?site|in office|office|headquarters|est|pst|cst|mst|et|pt|ct|mt|"
                             r"main office|head office")

# per-country visa lexicons (normalized tokens). Example clauses made only of these words describe that country's
# sponsorship ("(e.g., H-1B, TN)"); they never make a question about another country answerable.
_VISA_COMMON = frozenset(["visa", "visas", "status", "etc", "and", "or", "a", "an", "the", "holder", "holders",
                          "permit", "permits", "work", "sponsorship", "s"])
VISA_LEXICON: dict[str, frozenset[str]] = {
    "US": frozenset(["h1b", "1b", "h4", "opt", "cpt", "tn", "f1", "l1", "o1", "e3", "j1", "stem", "ead", "h", "4", "f",
                     "1", "l", "o", "e", "3", "j"]),
    "CA": frozenset(["lmia", "pgwp", "owp", "cusma", "iec", "open", "closed", "post", "graduation"]),
    "GB": frozenset(["skilled", "worker", "tier", "2", "graduate", "hpi", "global", "talent"]),
    "AU": frozenset(["482", "485", "186", "189", "190", "tss", "subclass", "temporary", "skill", "shortage"]),
    "IN": frozenset(["employment", "e"]),
}
# the visa words that mark an example clause as a visa list at all
VISA_MARKERS: dict[str, frozenset[str]] = {
    "US": frozenset(["h1b", "1b", "h4", "opt", "cpt", "tn", "f1", "l1", "o1", "e3", "j1", "stem", "ead", "h"]),
    "CA": frozenset(["lmia", "pgwp", "owp", "cusma", "iec"]),
    "GB": frozenset(["skilled", "tier", "hpi"]),
    "AU": frozenset(["482", "485", "186", "189", "190", "tss", "subclass"]),
    "IN": frozenset(),
}


def _norm(s: str) -> str:
    s = str(s or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def country_iso(value: object, *, allow_codes: bool = True) -> str | None:
    """A structured country value ("US", "USA", "United States", "Canada", "gb") -> ISO-2, or None if unknown.
    `allow_codes=False` for free text, where a two-letter tail may be a state ("CA" = California)."""
    v = str(value or "").strip()
    if not v:
        return None
    up = re.sub(r"[^A-Za-z]", "", v).upper()
    if allow_codes and len(up) == 2 and (up in ISO2 or up in _ISO_ALIASES):
        return _ISO_ALIASES.get(up, up)
    n = _norm(v)
    for code, rx in _NAME_RES.items():
        if rx.fullmatch(n):
            return code
    return None


def _codes(country: str) -> frozenset[str]:
    return frozenset(SUBDIVISIONS.get(country, {}).get("codes", ()))


def ambiguous_codes(country: str) -> frozenset[str]:
    """Subdivision codes of `country` that are also an ISO-2 code or another country's subdivision code."""
    others = {c for k, v in SUBDIVISIONS.items() if k != country for c in v.get("codes", ())}
    iso = {c.lower() for c in ISO2}
    return frozenset(c for c in _codes(country) if c in iso or c in others) | EXTRA_COLLISIONS.get(country, frozenset())


def markers_in(text: str) -> set[str]:
    """Countries whose name appears anywhere in `text` (normalized)."""
    n = _norm(text)
    return {c for c, rx in _NAME_RES.items() if rx.search(n)}


def _part_in(part: str, country: str, foreign_names: Sequence[str] = ()) -> bool:
    """Only the LAST comma component decides: the country's name, a full subdivision name, or "City, XX" with a code of
    that country (an ambiguous code needs a listed city; a shared city name never decides by code)."""
    if foreign_names or (markers_in(part) - {country}):
        return False
    comps = [_norm(c) for c in part.split(",") if _norm(c)]
    if not comps:
        return False
    tail = comps[-1]
    if _NAME_RES[country].search(tail) if country in _NAME_RES else False:
        return True
    if tail in SUBDIVISIONS.get(country, {}).get("names", ()):
        return True
    if len(comps) < 2 or not re.fullmatch(r"[a-z]{2,3}", tail) or tail not in _codes(country):
        return False
    city = comps[-2]
    if city in SHARED_CITY_NAMES and SHARED_CITY_NAMES[city] != country:
        return False
    return tail not in ambiguous_codes(country) or (city, tail) in CITY_CODES.get(country, frozenset())


def _parenthetical(m: re.Match[str]) -> str:
    """"(US)" / "(Canada)" become a trailing component; an allowlisted no-place note ("(HQ)") is dropped; anything else
    ("(also London)", "(Toronto)") becomes its own location part, which must itself be in the country."""
    inner = m.group(1)
    if country_iso(inner) == "US" or country_iso(inner, allow_codes=False):
        return f", {inner}"
    if not _norm(inner) or _NO_PLACE_PAREN.fullmatch(_norm(inner)):
        return " "
    return f" ; {inner} ; "


def strip_own_subdivisions(text: str, country: str) -> str:
    """Normalized text without `country`'s own subdivision names ("British Columbia" is not Britain)."""
    n = _norm(text)
    for name in sorted(SUBDIVISIONS.get(country, {}).get("names", ()), key=len, reverse=True):
        n = re.sub(rf"\b{re.escape(name)}\b", " ", n)
    return n


def split_location(location: str) -> list[str]:
    text = re.sub(r"\(([^)]*)\)", _parenthetical, location or "")
    return [p for p in re.split(r"[;|]|\s/\s|\bor\b", text) if p.strip()]
