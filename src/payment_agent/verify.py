"""Strict-equality primitives for identity verification.

Per DECISIONS hard constraints:
  - "No fuzzy matching on verification (strict full-name match + strict
    secondary factor match)." This module performs canonical equality only —
    Unicode NFC + lowercase + whitespace collapse. Never edit-distance,
    phonetic, or initial-matching.
  - DECISIONS #2: when a verification field fails, the agent names which field
    failed and offers the *remaining* secondary-factor options. The
    :class:`SecondaryFactor` enum + :func:`remaining_factors` helper give
    state.py a single source of truth for that enumeration.

Per BRIEF #2: "The clean split is: LLM extracts the candidate full name,
deterministic code does the exact-string comparison. If the LLM does the
comparison, fuzziness sneaks back in." This module is the deterministic
half of that split.

Format preconditions on secondary factors (Aadhaar last-4, pincode) raise
``ValueError`` rather than silently comparing unequal. A malformed extraction
is *not* a wrong submission per DECISIONS #3 — state.py catches the
``ValueError`` and re-prompts without incrementing the verification retry
counter.

No retry counters live here — those belong to state.py per plan §2.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import date
from enum import StrEnum

# Format preconditions for secondary factors. Submitted side only — the
# expected side comes from the trusted lookup_account API response.
_AADHAAR_LAST4_PATTERN: re.Pattern[str] = re.compile(r"^\d{4}$")
_PINCODE_PATTERN: re.Pattern[str] = re.compile(r"^\d{6}$")


class SecondaryFactor(StrEnum):
    """The secondary factors available for verification (per the spec).

    String values double as slot-store keys so downstream code can
    ``slots[factor.value]`` without a translation layer.
    """

    DOB = "dob"
    AADHAAR_LAST4 = "aadhaar_last4"
    PINCODE = "pincode"


# Canonical ordering — stable for message composition ("try DOB, Aadhaar last 4,
# or pincode") and for deterministic test assertions.
ALL_SECONDARY_FACTORS: tuple[SecondaryFactor, ...] = (
    SecondaryFactor.DOB,
    SecondaryFactor.AADHAAR_LAST4,
    SecondaryFactor.PINCODE,
)


# ---------------------------------------------------------------------------
# Name comparison
# ---------------------------------------------------------------------------

def _canonicalize_name(name: str) -> str:
    """Apply NFC + lowercase + whitespace-collapse + strip.

    Canonicalization, not similarity. ``"  Nithin  Jain  "`` and
    ``"nithin jain"`` canonicalize to the same string; ``"Nithin"`` and
    ``"Nithin Jain"`` do not. Punctuation is preserved (apostrophes and
    hyphens are part of names).
    """
    normalized = unicodedata.normalize("NFC", name)
    return " ".join(normalized.lower().split())


def compare_name(submitted: str, expected: str) -> bool:
    """Strict canonical equality of two names. ``True`` iff they match."""
    return _canonicalize_name(submitted) == _canonicalize_name(expected)


# ---------------------------------------------------------------------------
# Secondary-factor comparisons
# ---------------------------------------------------------------------------

def compare_dob(submitted: date, expected: date) -> bool:
    """Strict equality of two ``date`` objects."""
    return submitted == expected


def compare_aadhaar_last4(submitted: str, expected: str) -> bool:
    """Strict equality after stripping whitespace.

    Raises ``ValueError`` if ``submitted`` is not exactly 4 digits. A
    malformed extraction is not a wrong submission per DECISIONS #3 — the
    caller (state.py) re-prompts without consuming a retry.
    """
    s = submitted.strip()
    if not _AADHAAR_LAST4_PATTERN.fullmatch(s):
        raise ValueError(
            f"submitted aadhaar_last4 is malformed (must be exactly 4 digits): "
            f"{submitted!r}"
        )
    return s == expected.strip()


def compare_pincode(submitted: str, expected: str) -> bool:
    """Strict equality after stripping whitespace.

    Raises ``ValueError`` if ``submitted`` is not exactly 6 digits. A
    malformed extraction is not a wrong submission per DECISIONS #3 — the
    caller (state.py) re-prompts without consuming a retry.
    """
    s = submitted.strip()
    if not _PINCODE_PATTERN.fullmatch(s):
        raise ValueError(
            f"submitted pincode is malformed (must be exactly 6 digits): "
            f"{submitted!r}"
        )
    return s == expected.strip()


# ---------------------------------------------------------------------------
# Factor enumeration helper
# ---------------------------------------------------------------------------

def remaining_factors(
    excluded: SecondaryFactor | Iterable[SecondaryFactor] | None = None,
) -> tuple[SecondaryFactor, ...]:
    """Return the secondary factors *not* in ``excluded``, preserving canonical order.

    Used by state.py to compose the "try one of: X, Y" message after a
    verification failure (DECISIONS #2). With no exclusion, returns all
    secondary factors.
    """
    if excluded is None:
        excluded_set: frozenset[SecondaryFactor] = frozenset()
    elif isinstance(excluded, SecondaryFactor):
        excluded_set = frozenset({excluded})
    else:
        excluded_set = frozenset(excluded)
    return tuple(f for f in ALL_SECONDARY_FACTORS if f not in excluded_set)
