"""Tests for verify.py — strict-equality primitives.

Per DECISIONS hard constraints:
  - "No fuzzy matching on verification (strict full-name match + strict
    secondary factor match)" — no edit-distance, no phonetic, no typo tolerance.
  - Per #2: when a verification field fails, the agent names which field failed
    and offers the remaining secondary-factor options. ALL_SECONDARY_FACTORS
    is the canonical enumeration.
  - Per BRIEF #2: "The clean split is: LLM extracts the candidate full name,
    deterministic code does the exact-string comparison. If the LLM does the
    comparison, fuzziness sneaks back in." This module is that deterministic
    code; canonicalization is whitespace + case + Unicode NFC only — never
    similarity.

verify.py owns no retry counters (those live in state.py per plan §2).
"""
from __future__ import annotations

import unicodedata
from datetime import date

import pytest

from payment_agent import verify


# ---------------------------------------------------------------------------
# compare_name
# ---------------------------------------------------------------------------

class TestCompareName:
    def test_exact_match(self) -> None:
        assert verify.compare_name("Nithin Jain", "Nithin Jain") is True

    def test_case_insensitive(self) -> None:
        assert verify.compare_name("nithin jain", "Nithin Jain") is True

    def test_uppercase_match(self) -> None:
        assert verify.compare_name("NITHIN JAIN", "Nithin Jain") is True

    def test_collapses_internal_whitespace(self) -> None:
        assert verify.compare_name("Nithin   Jain", "Nithin Jain") is True

    def test_leading_trailing_whitespace_stripped(self) -> None:
        assert verify.compare_name("  Nithin Jain  ", "Nithin Jain") is True

    def test_tabs_treated_as_whitespace(self) -> None:
        assert verify.compare_name("Nithin\tJain", "Nithin Jain") is True

    def test_different_name_no_match(self) -> None:
        assert verify.compare_name("Nithin Jain", "Rahul Sharma") is False

    def test_partial_name_no_match(self) -> None:
        # Strict: "Nithin" alone is not the full name on file.
        assert verify.compare_name("Nithin", "Nithin Jain") is False

    def test_extra_middle_name_no_match(self) -> None:
        # Strict: middle-name addition is a different name.
        assert verify.compare_name("Nithin Kumar Jain", "Nithin Jain") is False

    def test_typo_no_match(self) -> None:
        # The whole point of "no fuzzy matching": one missing letter fails.
        assert verify.compare_name("Nithn Jain", "Nithin Jain") is False

    def test_transliteration_variant_no_match(self) -> None:
        # The BRIEF's primary motivating example. Same name in real life,
        # different romanizations — strict comparison must reject. The
        # extractor is responsible for producing the canonical form on file;
        # this layer never absorbs transliteration drift as a "match."
        assert verify.compare_name(
            "Rajarajeshwari Balasubramaniam",
            "Rajarajeswari Balasubramaniam",
        ) is False

    def test_initial_expansion_no_match(self) -> None:
        # Common in Indian contexts: speech uses initials, the database holds
        # the expanded form. Strict comparison rejects.
        assert verify.compare_name(
            "R. Balasubramaniam",
            "Rajarajeswari Balasubramaniam",
        ) is False

    def test_honorific_prefix_no_match(self) -> None:
        # Extractor should strip honorifics before this layer sees the name.
        # Documenting the boundary: verify does not absorb the cost of an
        # extractor bug — "Mr. Nithin Jain" is literally a different string.
        assert verify.compare_name("Mr. Nithin Jain", "Nithin Jain") is False

    def test_punctuation_matters(self) -> None:
        # Apostrophe is part of the name; stripping it would be fuzzy.
        assert verify.compare_name("OBrien", "O'Brien") is False

    def test_unicode_nfc_normalization(self) -> None:
        # Two encodings of "café": precomposed (NFC) vs decomposed (NFD).
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        assert nfc != nfd  # bytes differ
        assert verify.compare_name(nfc, nfd) is True  # canonicalized equal

    def test_empty_strings_match(self) -> None:
        # Edge case: both empty canonicalize to empty. Documented behavior.
        assert verify.compare_name("", "") is True

    def test_empty_vs_nonempty_no_match(self) -> None:
        assert verify.compare_name("", "Nithin Jain") is False


# ---------------------------------------------------------------------------
# compare_dob
# ---------------------------------------------------------------------------

class TestCompareDob:
    def test_match(self) -> None:
        assert verify.compare_dob(date(1990, 5, 14), date(1990, 5, 14)) is True

    def test_off_by_one_day_no_match(self) -> None:
        assert verify.compare_dob(date(1990, 5, 14), date(1990, 5, 15)) is False

    def test_off_by_one_year_no_match(self) -> None:
        assert verify.compare_dob(date(1990, 5, 14), date(1991, 5, 14)) is False

    def test_leap_year_match(self) -> None:
        # ACC1004 case from BRIEF #3.
        assert verify.compare_dob(date(1988, 2, 29), date(1988, 2, 29)) is True


# ---------------------------------------------------------------------------
# compare_aadhaar_last4
# ---------------------------------------------------------------------------

class TestCompareAadhaarLast4:
    def test_match(self) -> None:
        assert verify.compare_aadhaar_last4("1234", "1234") is True

    def test_strips_whitespace(self) -> None:
        assert verify.compare_aadhaar_last4(" 1234 ", "1234") is True

    def test_mismatch(self) -> None:
        assert verify.compare_aadhaar_last4("1234", "5678") is False

    def test_leading_zero_preserved_in_match(self) -> None:
        # Aadhaar last-4 can start with 0. String comparison preserves the
        # zero — guards against a future regression to int-coercion.
        assert verify.compare_aadhaar_last4("0123", "0123") is True

    def test_leading_zero_not_collapsed_on_mismatch(self) -> None:
        # "0123" must NOT match "0124" (or any other 4-digit string) by
        # accident.
        assert verify.compare_aadhaar_last4("0123", "0124") is False

    # --- Format precondition: malformed extraction raises rather than
    # silently consuming a verification retry (DECISIONS #3 + reviewer HIGH).

    def test_3_digit_submitted_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            verify.compare_aadhaar_last4("123", "1234")

    def test_5_digit_submitted_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            verify.compare_aadhaar_last4("12345", "1234")

    def test_non_digit_submitted_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            verify.compare_aadhaar_last4("12ab", "1234")

    def test_empty_submitted_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            verify.compare_aadhaar_last4("", "1234")


# ---------------------------------------------------------------------------
# compare_pincode
# ---------------------------------------------------------------------------

class TestComparePincode:
    def test_match(self) -> None:
        assert verify.compare_pincode("560001", "560001") is True

    def test_strips_whitespace(self) -> None:
        assert verify.compare_pincode(" 560001 ", "560001") is True

    def test_mismatch(self) -> None:
        assert verify.compare_pincode("560001", "560002") is False

    # --- Format precondition: 6-digit Indian pincode required ---

    def test_5_digit_submitted_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            verify.compare_pincode("56000", "560001")

    def test_7_digit_submitted_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            verify.compare_pincode("5600012", "560001")

    def test_non_digit_submitted_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            verify.compare_pincode("ABCDEF", "560001")

    def test_empty_submitted_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            verify.compare_pincode("", "560001")


# ---------------------------------------------------------------------------
# SecondaryFactor enum + enumeration
# ---------------------------------------------------------------------------

class TestSecondaryFactor:
    def test_three_factors_defined(self) -> None:
        # The spec offers DOB, Aadhaar last-4, and pincode as secondary factors.
        assert set(verify.SecondaryFactor) == {
            verify.SecondaryFactor.DOB,
            verify.SecondaryFactor.AADHAAR_LAST4,
            verify.SecondaryFactor.PINCODE,
        }

    def test_all_secondary_factors_tuple_matches_enum(self) -> None:
        assert set(verify.ALL_SECONDARY_FACTORS) == set(verify.SecondaryFactor)

    def test_factor_values_are_slot_keys(self) -> None:
        # The string value must match the slot-store key for downstream code
        # that does ``slots[factor.value]`` lookups.
        assert verify.SecondaryFactor.DOB.value == "dob"
        assert verify.SecondaryFactor.AADHAAR_LAST4.value == "aadhaar_last4"
        assert verify.SecondaryFactor.PINCODE.value == "pincode"

    def test_remaining_factors_after_one_excluded(self) -> None:
        # Helper used by state.py to compose "try one of: X, Y" messages.
        remaining = verify.remaining_factors(excluded=verify.SecondaryFactor.DOB)
        assert verify.SecondaryFactor.DOB not in remaining
        assert verify.SecondaryFactor.AADHAAR_LAST4 in remaining
        assert verify.SecondaryFactor.PINCODE in remaining
        assert len(remaining) == 2

    def test_remaining_factors_with_multiple_exclusions(self) -> None:
        remaining = verify.remaining_factors(
            excluded={verify.SecondaryFactor.DOB, verify.SecondaryFactor.PINCODE}
        )
        assert remaining == (verify.SecondaryFactor.AADHAAR_LAST4,)

    def test_remaining_factors_with_no_exclusion_returns_all_in_canonical_order(self) -> None:
        # state.py iterates this to compose ordered messages ("try DOB,
        # Aadhaar last 4, or pincode"). Order is part of the contract;
        # tuple equality (not set equality) catches a future reorder.
        assert verify.remaining_factors() == verify.ALL_SECONDARY_FACTORS
