"""Tests for validate.py — pure validation primitives.

Per DECISIONS hard constraints:
  - All inputs validated before any API call.
  - Leap-year strict-parse via ``datetime.date(y, m, d)`` — not ``dateutil.parser``
    (BRIEF #3, ACC1004 = 1988-02-29 valid; 1989-02-29 invalid).
  - DOB defaults to DD-MM-YYYY (Indian context per DECISIONS #15);
    ambiguous strings return both readings for orchestrator-level disambiguation.
  - Amount validation must be complete client-side (DECISIONS #4):
    zero / negative / >2 decimals must never reach the API.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payment_agent import validate


# ---------------------------------------------------------------------------
# Luhn
# ---------------------------------------------------------------------------

class TestLuhnCheck:
    def test_valid_visa_test_pan_passes(self) -> None:
        # Standard Visa test PAN, Luhn-valid.
        assert validate.luhn_check("4532015112830366") is True

    def test_valid_mastercard_test_pan_passes(self) -> None:
        assert validate.luhn_check("5500000000000004") is True

    def test_valid_amex_test_pan_passes(self) -> None:
        assert validate.luhn_check("378282246310005") is True  # 15 digits

    def test_invalid_pan_one_digit_changed_fails(self) -> None:
        # Last digit changed from 6 to 5.
        assert validate.luhn_check("4532015112830365") is False

    def test_all_zeros_passes_luhn_arithmetically(self) -> None:
        # luhn_check is pure arithmetic; length check lives in validate_pan.
        assert validate.luhn_check("0000000000000000") is True

    def test_empty_string_returns_false(self) -> None:
        assert validate.luhn_check("") is False

    def test_non_digit_input_returns_false(self) -> None:
        assert validate.luhn_check("abcd") is False


# ---------------------------------------------------------------------------
# validate_pan
# ---------------------------------------------------------------------------

class TestValidatePan:
    def test_returns_digit_only_pan_for_valid_input(self) -> None:
        assert validate.validate_pan("4532015112830366") == "4532015112830366"

    def test_strips_spaces_before_validating(self) -> None:
        assert validate.validate_pan("4532 0151 1283 0366") == "4532015112830366"

    def test_strips_dashes_before_validating(self) -> None:
        assert validate.validate_pan("4532-0151-1283-0366") == "4532015112830366"

    def test_raises_on_luhn_failure(self) -> None:
        with pytest.raises(ValueError, match="Luhn"):
            validate.validate_pan("4532015112830365")

    def test_raises_on_too_short(self) -> None:
        with pytest.raises(ValueError, match="length"):
            validate.validate_pan("411111")  # 6 digits

    def test_raises_on_too_long(self) -> None:
        with pytest.raises(ValueError, match="length"):
            validate.validate_pan("4" * 20)  # 20 digits

    def test_accepts_15_digit_amex(self) -> None:
        assert validate.validate_pan("378282246310005") == "378282246310005"

    def test_raises_on_non_digit_content(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_pan("XYZX 0151 1283 0366")

    def test_raises_on_empty_input(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_pan("")


# ---------------------------------------------------------------------------
# validate_cvv
# ---------------------------------------------------------------------------

class TestValidateCvv:
    def test_accepts_3_digit_cvv(self) -> None:
        assert validate.validate_cvv("123") == "123"

    def test_accepts_4_digit_cvv(self) -> None:
        assert validate.validate_cvv("1234") == "1234"

    def test_strips_whitespace(self) -> None:
        assert validate.validate_cvv("  123 ") == "123"

    def test_raises_on_too_short(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_cvv("12")

    def test_raises_on_too_long(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_cvv("12345")

    def test_raises_on_non_digit_content(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_cvv("12a")

    def test_raises_on_empty(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_cvv("")


# ---------------------------------------------------------------------------
# parse_expiry
# ---------------------------------------------------------------------------

class TestParseExpiry:
    def test_two_digit_year_expands_to_4_digit(self) -> None:
        assert validate.parse_expiry("12/27") == (12, 2027)

    def test_four_digit_year_passes_through(self) -> None:
        assert validate.parse_expiry("12/2027") == (12, 2027)

    def test_single_digit_month(self) -> None:
        assert validate.parse_expiry("1/27") == (1, 2027)

    def test_strips_whitespace(self) -> None:
        assert validate.parse_expiry(" 12 / 27 ") == (12, 2027)

    def test_dash_separator_accepted(self) -> None:
        assert validate.parse_expiry("12-27") == (12, 2027)

    def test_raises_on_invalid_month_zero(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_expiry("0/27")

    def test_raises_on_invalid_month_thirteen(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_expiry("13/27")

    def test_raises_on_no_separator(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_expiry("1227")

    def test_raises_on_empty(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_expiry("")

    def test_raises_on_non_numeric(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_expiry("ab/cd")


# ---------------------------------------------------------------------------
# validate_expiry (parse + must-be-in-future)
# ---------------------------------------------------------------------------

class TestValidateExpiry:
    def test_future_expiry_passes(self) -> None:
        # Today is in the past relative to 2030-12 cutoff.
        today = date(2026, 5, 9)
        assert validate.validate_expiry("12/30", today=today) == (12, 2030)

    def test_current_month_passes(self) -> None:
        # Card expires at end of expiry month — this month is still valid.
        today = date(2026, 5, 9)
        assert validate.validate_expiry("05/26", today=today) == (5, 2026)

    def test_last_day_of_expiry_month_passes(self) -> None:
        today = date(2026, 5, 31)
        assert validate.validate_expiry("05/26", today=today) == (5, 2026)

    def test_first_day_after_expiry_month_fails(self) -> None:
        today = date(2026, 6, 1)
        with pytest.raises(ValueError, match="expired"):
            validate.validate_expiry("05/26", today=today)

    def test_past_year_fails(self) -> None:
        today = date(2026, 5, 9)
        with pytest.raises(ValueError, match="expired"):
            validate.validate_expiry("12/24", today=today)


# ---------------------------------------------------------------------------
# parse_iso_date
# ---------------------------------------------------------------------------

class TestParseIsoDate:
    def test_parses_iso_format(self) -> None:
        assert validate.parse_iso_date("1990-05-14") == date(1990, 5, 14)

    def test_leap_year_valid(self) -> None:
        # ACC1004 case from BRIEF #3.
        assert validate.parse_iso_date("1988-02-29") == date(1988, 2, 29)

    def test_leap_year_invalid_raises(self) -> None:
        # The canary: dateutil.parser would silently coerce; datetime.date raises.
        with pytest.raises(ValueError):
            validate.parse_iso_date("1989-02-29")

    def test_invalid_month_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_iso_date("1990-13-01")

    def test_non_iso_format_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_iso_date("14-05-1990")


# ---------------------------------------------------------------------------
# parse_dob_strict
# ---------------------------------------------------------------------------

class TestParseDobStrict:
    def test_constructs_date_from_components(self) -> None:
        assert validate.parse_dob_strict(year=1990, month=5, day=14) == date(1990, 5, 14)

    def test_leap_year_valid(self) -> None:
        assert validate.parse_dob_strict(year=1988, month=2, day=29) == date(1988, 2, 29)

    def test_leap_year_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_dob_strict(year=1989, month=2, day=29)

    def test_invalid_month_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_dob_strict(year=1990, month=13, day=1)

    def test_invalid_day_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_dob_strict(year=1990, month=4, day=31)


# ---------------------------------------------------------------------------
# parse_dob_with_ambiguity (DD-MM-YYYY default; ambiguous when both readings valid)
# ---------------------------------------------------------------------------

class TestParseDobWithAmbiguity:
    def test_unambiguous_when_only_dd_mm_parses(self) -> None:
        # 13 cannot be a month; only DD-MM is valid → unambiguous.
        primary, alt = validate.parse_dob_with_ambiguity("13-04-1990")
        assert primary == date(1990, 4, 13)
        assert alt is None

    def test_unambiguous_when_only_mm_dd_parses(self) -> None:
        # day=4 month=13 fails DD-MM; MM-DD reads as Apr 13 → return as primary.
        primary, alt = validate.parse_dob_with_ambiguity("04-13-1990")
        assert primary == date(1990, 4, 13)
        assert alt is None

    def test_ambiguous_when_both_readings_valid(self) -> None:
        # 05-04: DD-MM = April 5; MM-DD = May 4. Both plausible → return both.
        primary, alt = validate.parse_dob_with_ambiguity("05-04-1990")
        assert primary == date(1990, 4, 5)  # DD-MM is primary per DECISIONS #15
        assert alt == date(1990, 5, 4)

    def test_leap_year_acc1004(self) -> None:
        # 29-02-1988: DD-MM = Feb 29 1988 (valid leap); MM-DD = month 29 invalid.
        primary, alt = validate.parse_dob_with_ambiguity("29-02-1988")
        assert primary == date(1988, 2, 29)
        assert alt is None

    def test_leap_year_invalid_year_raises(self) -> None:
        # 29-02-1989: DD-MM invalid (1989 not leap); MM-DD invalid (month 29).
        with pytest.raises(ValueError):
            validate.parse_dob_with_ambiguity("29-02-1989")

    def test_slash_separator_accepted(self) -> None:
        primary, alt = validate.parse_dob_with_ambiguity("13/04/1990")
        assert primary == date(1990, 4, 13)
        assert alt is None

    def test_dot_separator_accepted(self) -> None:
        primary, _alt = validate.parse_dob_with_ambiguity("13.04.1990")
        assert primary == date(1990, 4, 13)

    def test_year_before_plausible_range_raises(self) -> None:
        # 1900 is before plausible DOB range (~1925).
        with pytest.raises(ValueError):
            validate.parse_dob_with_ambiguity("01-01-1900")

    def test_future_date_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_dob_with_ambiguity("01-01-2099", today=date(2026, 5, 9))

    def test_wrong_component_count_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_dob_with_ambiguity("13-04")

    def test_non_numeric_components_raise(self) -> None:
        with pytest.raises(ValueError):
            validate.parse_dob_with_ambiguity("ab-cd-efgh")

    def test_mixed_separators_rejected(self) -> None:
        # "29-02.1988" — separator changes mid-string. Previously silently
        # accepted (split on any of [-/.]); now rejected by the consistent-
        # separator regex, since DOB is in the critical-subset pass-bar.
        with pytest.raises(ValueError, match="consistent separator"):
            validate.parse_dob_with_ambiguity("29-02.1988")

    def test_mixed_separators_slash_dash_rejected(self) -> None:
        with pytest.raises(ValueError, match="consistent separator"):
            validate.parse_dob_with_ambiguity("13/04-1990")


# ---------------------------------------------------------------------------
# validate_amount
# ---------------------------------------------------------------------------

class TestValidateAmount:
    def test_string_integer_amount(self) -> None:
        assert validate.validate_amount("500") == Decimal("500")

    def test_string_with_decimals(self) -> None:
        assert validate.validate_amount("1500.50") == Decimal("1500.50")

    def test_int_amount(self) -> None:
        assert validate.validate_amount(500) == Decimal("500")

    def test_strips_whitespace(self) -> None:
        assert validate.validate_amount("  500  ") == Decimal("500")

    def test_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            validate.validate_amount("0")

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            validate.validate_amount("-100")

    def test_more_than_two_decimals_raises(self) -> None:
        with pytest.raises(ValueError, match="decimal"):
            validate.validate_amount("100.005")

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_amount("abc")

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_amount("")

    def test_currency_symbol_raises(self) -> None:
        # validate sees clean numbers; symbol stripping is the extractor's job.
        with pytest.raises(ValueError):
            validate.validate_amount("₹500")

    def test_bool_rejected(self) -> None:
        # bool is a subclass of int in Python; ``True`` would otherwise become
        # ``Decimal(1)`` — silent, wrong, and exactly the kind of footgun the
        # "validators must catch this client-side" rule (DECISIONS #4) exists for.
        with pytest.raises(ValueError, match="bool"):
            validate.validate_amount(True)  # type: ignore[arg-type]

    def test_unsupported_type_rejected(self) -> None:
        # Defensive: anything outside str | int | float | Decimal is rejected loudly.
        with pytest.raises(ValueError, match="Unsupported"):
            validate.validate_amount(object())  # type: ignore[arg-type]

    # --- Special Decimal values: Infinity / NaN must all raise ValueError ---
    # These are real bugs caught by review: Decimal('Infinity') passes d > 0 and
    # the exponent guard short-circuits because as_tuple().exponent == 'F' (str,
    # not int). Decimal('NaN') constructs cleanly but crashes ``d <= 0`` with
    # InvalidOperation because NaN is not orderable.

    def test_decimal_infinity_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount("Infinity")

    def test_decimal_negative_infinity_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount("-Infinity")

    def test_float_inf_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount(float("inf"))

    def test_float_negative_inf_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount(float("-inf"))

    def test_decimal_infinity_object_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount(Decimal("Infinity"))

    def test_decimal_nan_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount("NaN")

    def test_float_nan_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount(float("nan"))

    def test_decimal_nan_object_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount(Decimal("NaN"))

    def test_decimal_snan_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate.validate_amount(Decimal("sNaN"))


# ---------------------------------------------------------------------------
# validate_account_id
# ---------------------------------------------------------------------------

class TestValidateAccountId:
    def test_valid_acc_id_passes(self) -> None:
        assert validate.validate_account_id("ACC1001") == "ACC1001"

    def test_strips_whitespace(self) -> None:
        assert validate.validate_account_id("  ACC1001  ") == "ACC1001"

    def test_lowercase_rejected(self) -> None:
        # Strict format; the extractor normalizes case before reaching here.
        with pytest.raises(ValueError):
            validate.validate_account_id("acc1001")

    def test_too_few_digits_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_account_id("ACC123")

    def test_too_many_digits_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_account_id("ACC12345")

    def test_wrong_prefix_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_account_id("BCC1234")

    def test_no_prefix_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_account_id("1234")

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate.validate_account_id("")
