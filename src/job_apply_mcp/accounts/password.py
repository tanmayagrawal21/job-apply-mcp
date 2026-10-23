"""Generated account passwords (ADR-0046): `secrets`-based, >= 20 chars, and acceptable to common ATS rules.

Common rules (Workday, iCIMS, SuccessFactors, Taleo): 8-64 chars; at least one upper, lower, digit and special;
no whitespace; often no more than 2 identical characters in a row; some reject quotes, backslash, angle brackets
or characters outside ASCII. The alphabet below avoids all of those and look-alike characters.
"""
from __future__ import annotations

import re
import secrets

from ..core.secrets import Secret

UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
LOWER = "abcdefghijkmnopqrstuvwxyz"
DIGITS = "23456789"
SPECIAL = "!#$%*+-=?@^_"
ALPHABET = UPPER + LOWER + DIGITS + SPECIAL
MIN_LENGTH, MAX_LENGTH = 20, 64
_TRIPLE = re.compile(r"(.)\1\1")


def meets_policy(pw: str, *, min_length: int = MIN_LENGTH) -> bool:
    return (min_length <= len(pw) <= MAX_LENGTH
            and all(c in ALPHABET for c in pw)
            and any(c in UPPER for c in pw) and any(c in LOWER for c in pw)
            and any(c in DIGITS for c in pw) and any(c in SPECIAL for c in pw)
            and not _TRIPLE.search(pw)
            and pw[0] not in SPECIAL)      # a few forms trim or reject a leading symbol


def generate_password(length: int = 24) -> Secret:
    """Uniformly random over ALPHABET (rejection sampling until the policy holds; expected < 2 draws)."""
    if not MIN_LENGTH <= length <= MAX_LENGTH:
        raise ValueError(f"length must be {MIN_LENGTH}..{MAX_LENGTH}")
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if meets_policy(pw):
            return Secret(pw)
