"""Tests for redact.py — sensitive data scrubbing helpers.

Per DECISIONS.md hard constraints:
  - CVV never written to logs in any form.
  - PAN logged only as last-4.
  - DOB, Aadhaar last-4, pincode never exposed in agent messages or logs.

Per #13 implications, the API log schema is explicitly:
  {account_id, amount, attempted_at, error_class, last4}
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from payment_agent import redact


# ---------------------------------------------------------------------------
# last4
# ---------------------------------------------------------------------------

class TestLast4:
    def test_returns_last_four_digits_of_pan(self) -> None:
        assert redact.last4("4532015112830366") == "0366"

    def test_strips_spaces_before_taking_last_four(self) -> None:
        assert redact.last4("4532 0151 1283 0366") == "0366"

    def test_strips_dashes_before_taking_last_four(self) -> None:
        assert redact.last4("4532-0151-1283-0366") == "0366"

    def test_raises_on_pan_shorter_than_four_digits(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            redact.last4("123")

    def test_raises_on_empty_input(self) -> None:
        with pytest.raises(ValueError):
            redact.last4("")

    def test_raises_on_non_digit_only(self) -> None:
        with pytest.raises(ValueError):
            redact.last4("abcd")


# ---------------------------------------------------------------------------
# mask_pan
# ---------------------------------------------------------------------------

class TestMaskPan:
    def test_masks_a_16_digit_pan_to_last_four_visible(self) -> None:
        assert redact.mask_pan("4532015112830366") == "**** **** **** 0366"

    def test_handles_spaces_in_input(self) -> None:
        assert redact.mask_pan("4532 0151 1283 0366") == "**** **** **** 0366"

    def test_raises_on_short_pan(self) -> None:
        with pytest.raises(ValueError):
            redact.mask_pan("123")


# ---------------------------------------------------------------------------
# scrub_sensitive
# ---------------------------------------------------------------------------

class TestScrubSensitive:
    def test_removes_cvv_field(self) -> None:
        out = redact.scrub_sensitive({"cvv": "123", "amount": 500})
        assert "cvv" not in out
        assert out["amount"] == 500

    def test_removes_cvv_field_case_insensitively(self) -> None:
        out = redact.scrub_sensitive({"CVV": "123"})
        assert "CVV" not in out
        assert "cvv" not in out

    def test_replaces_pan_with_last_four(self) -> None:
        out = redact.scrub_sensitive({"pan": "4532015112830366"})
        assert out["pan"] == "0366"

    def test_replaces_card_number_with_last_four(self) -> None:
        out = redact.scrub_sensitive({"card_number": "4532015112830366"})
        assert out["card_number"] == "0366"

    def test_replaces_pan_with_last_four_case_insensitively(self) -> None:
        out = redact.scrub_sensitive({"PAN": "4532015112830366"})
        assert out["PAN"] == "0366"

    def test_replaces_card_number_with_last_four_case_insensitively(self) -> None:
        out = redact.scrub_sensitive({"Card_Number": "4532015112830366"})
        assert out["Card_Number"] == "0366"

    def test_pan_key_with_non_string_int_value_is_redacted_not_passed_through(
        self,
    ) -> None:
        """A non-string value under a PAN key (e.g. an int from an
        upstream type bug) MUST NOT pass through to the output. The
        scrubber is the defensive backstop; api.py's types are not the
        contract redact.py defends. Replace with REDACTED_PLACEHOLDER —
        preserves the 'key was present' signal without leaking content."""
        out = redact.scrub_sensitive({"card_number": 1234567890123456})
        assert out["card_number"] == redact.REDACTED_PLACEHOLDER
        assert out["card_number"] != 1234567890123456

    def test_pan_key_with_none_value_is_redacted_not_recursed(self) -> None:
        """None under a PAN key is also not a string. Without the
        defensive branch, scrub_sensitive(None) returned None and the key
        was emitted unchanged — making 'pan: null' visible in logs and
        masking that the upstream forgot to populate the field."""
        out = redact.scrub_sensitive({"pan": None})
        assert out["pan"] == redact.REDACTED_PLACEHOLDER

    def test_removes_dob(self) -> None:
        out = redact.scrub_sensitive({"dob": "1990-05-14"})
        assert "dob" not in out

    def test_removes_aadhaar_last4(self) -> None:
        out = redact.scrub_sensitive({"aadhaar_last4": "4321"})
        assert "aadhaar_last4" not in out

    def test_removes_pincode(self) -> None:
        out = redact.scrub_sensitive({"pincode": "560001"})
        assert "pincode" not in out

    def test_removes_full_name(self) -> None:
        out = redact.scrub_sensitive({"full_name": "Nithin Jain"})
        assert "full_name" not in out

    def test_does_not_mutate_input(self) -> None:
        payload = {"cvv": "123", "amount": 500}
        redact.scrub_sensitive(payload)
        assert payload == {"cvv": "123", "amount": 500}

    def test_recursively_scrubs_nested_dicts(self) -> None:
        out = redact.scrub_sensitive({
            "request": {"cvv": "123", "pan": "4532015112830366", "amount": 500},
            "meta": {"dob": "1990-05-14"},
        })
        assert "cvv" not in out["request"]
        assert out["request"]["pan"] == "0366"
        assert out["request"]["amount"] == 500
        assert "dob" not in out["meta"]

    def test_recursively_scrubs_lists_of_dicts(self) -> None:
        out = redact.scrub_sensitive({"items": [{"cvv": "123"}, {"cvv": "456"}]})
        for item in out["items"]:
            assert "cvv" not in item

    def test_property_no_sensitive_field_appears_in_serialized_output(self) -> None:
        import json

        # CVV / pincode / Aadhaar values are chosen so they cannot accidentally
        # appear as substrings of the kept last-4 values ("0366", "8821").
        payload = {
            "cvv": "987",
            "pan": "4532015112830366",
            "dob": "1988-02-29",
            "aadhaar_last4": "5544",
            "pincode": "751002",
            "full_name": "Nithin Jain",
            "card_number": "4111111111118821",
            "nested": {"cvv": "654", "dob": "1990-01-01"},
        }
        out = redact.scrub_sensitive(payload)
        serialized = json.dumps(out)
        # The sensitive *values* must not survive.
        for forbidden in (
            "987", "654", "1988-02-29", "1990-01-01", "751002", "Nithin", "5544",
        ):
            assert forbidden not in serialized, f"{forbidden!r} leaked into log"
        # PAN values must appear only as last-4.
        assert "4532015112830366" not in serialized
        assert "4111111111118821" not in serialized
        assert "0366" in serialized
        assert "8821" in serialized


# ---------------------------------------------------------------------------
# build_api_log_record
# ---------------------------------------------------------------------------

class TestBuildApiLogRecord:
    def test_constructs_documented_schema(self) -> None:
        ts = datetime(2026, 5, 9, 12, 34, 56, tzinfo=timezone.utc)
        record = redact.build_api_log_record(
            account_id="ACC1001",
            amount=500,
            attempted_at=ts,
            error_class=None,
            last4="0366",
        )
        assert record == {
            "account_id": "ACC1001",
            "amount": 500,
            "attempted_at": "2026-05-09T12:34:56+00:00",
            "error_class": None,
            "last4": "0366",
        }

    def test_includes_error_class_when_set(self) -> None:
        ts = datetime(2026, 5, 9, tzinfo=timezone.utc)
        record = redact.build_api_log_record(
            account_id="ACC1003",
            amount=500,
            attempted_at=ts,
            error_class="insufficient_balance",
            last4="0366",
        )
        assert record["error_class"] == "insufficient_balance"

    def test_record_contains_only_documented_keys(self) -> None:
        ts = datetime(2026, 5, 9, tzinfo=timezone.utc)
        record = redact.build_api_log_record(
            account_id="ACC1001",
            amount=500,
            attempted_at=ts,
            error_class=None,
            last4="0366",
        )
        assert set(record.keys()) == {
            "account_id", "amount", "attempted_at", "error_class", "last4",
        }

    def test_naive_datetime_raises(self) -> None:
        # Naive datetimes produce offset-less ISO strings, which are ambiguous
        # in distributed logs. Fail loud rather than silently emit an ambiguous
        # timestamp.
        ts_naive = datetime(2026, 5, 9, 12, 34, 56)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            redact.build_api_log_record(
                account_id="ACC1001",
                amount=500,
                attempted_at=ts_naive,
                error_class=None,
                last4="0366",
            )
