"""Interface tests for extract.py.

Scope per user calibration: 10-15 tests covering the deterministic kernel
of the extractor — schema shape, response parsing, slot conversions, and
fail-soft no-key fallback. Extraction QUALITY (does the prompt extract
DOBs correctly under ambiguity) is the eval's job (eval/extractor_eval.py),
not pytest's. Don't add quality assertions here.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from payment_agent import extract
from payment_agent.state import (
    ExtractionResult,
    Intent,
    SlotStore,
    Stage,
)
from payment_agent.verify import SecondaryFactor


# ---------------------------------------------------------------------------
# Tool schema shape
# ---------------------------------------------------------------------------


def test_tool_schema_shape_locks_contract():
    """Single tool, name matches the dispatch constant, intent required,
    additional properties forbidden, every slot field is nullable."""
    schema = extract._TOOL_SCHEMA
    assert schema["name"] == extract._TOOL_NAME

    input_schema = schema["input_schema"]
    assert input_schema["type"] == "object"
    assert input_schema["required"] == ["intent"]
    assert input_schema["additionalProperties"] is False

    props = input_schema["properties"]
    # Intent is required + enum-restricted to the 3-way classifier values.
    assert props["intent"]["enum"] == ["affirm", "negate", "ambiguous"]
    # Every other slot must accept null so the model can leave it absent.
    for slot in (
        "account_id", "full_name", "dob", "dob_alternate",
        "aadhaar_last4", "pincode", "selected_secondary_factor",
        "amount", "pan", "cvv", "expiry_month", "expiry_year",
        "name_on_card",
    ):
        type_decl = props[slot]["type"]
        assert "null" in type_decl, f"{slot} type must include null: {type_decl}"

    # Cardholder-name guidance is load-bearing per architectural commit
    # (silent-default cases must NOT fill name_on_card with the user's name).
    assert "different cardholder" in props["name_on_card"]["description"]


# ---------------------------------------------------------------------------
# Parsing the LLM tool_use args dict
# ---------------------------------------------------------------------------


def test_parse_happy_path_full_dict():
    """A clean dict with every field set is coerced into typed slots."""
    args: dict[str, Any] = {
        "intent": "affirm",
        "account_id": "ACC1001",
        "full_name": "Nithin Jain",
        "dob": "1990-05-14",
        "dob_alternate": "1990-04-15",
        "aadhaar_last4": "1234",
        "pincode": "560001",
        "selected_secondary_factor": "dob",
        "amount": "500",
        "pan": "4532015112830366",
        "cvv": "123",
        "expiry_month": 12,
        "expiry_year": 27,
        "name_on_card": "Priya Kumar",
    }
    result = extract._parse_tool_input(args)
    assert isinstance(result, ExtractionResult)
    assert result.intent is Intent.AFFIRM
    s = result.slots
    assert s.account_id == "ACC1001"
    assert s.full_name == "Nithin Jain"
    assert s.dob == date(1990, 5, 14)
    assert s.dob_alternate == date(1990, 4, 15)
    assert s.aadhaar_last4 == "1234"
    assert s.pincode == "560001"
    assert s.selected_secondary_factor is SecondaryFactor.DOB
    assert s.amount == Decimal("500")
    assert s.pan == "4532015112830366"
    assert s.cvv == "123"
    assert s.expiry_month == 12
    assert s.expiry_year == 2027  # 2-digit -> 4-digit
    assert s.name_on_card == "Priya Kumar"


@pytest.mark.parametrize(
    "intent_value",
    [None, "", "yes", "AFFIRM_BUT_TYPO", 42, [], {"x": 1}],
)
def test_parse_intent_fallback_to_ambiguous(intent_value):
    """Missing or unrecognized intent falls back to AMBIGUOUS rather than
    raising; downstream re-prompts on ambiguous, which is the safe path."""
    result = extract._parse_tool_input({"intent": intent_value})
    assert result.intent is Intent.AMBIGUOUS


@pytest.mark.parametrize(
    "bad_dob",
    ["1989-02-29", "not-a-date", "29/02/1988", "1990/05/14", "", "  "],
)
def test_parse_invalid_dob_drops_to_none(bad_dob):
    """An ISO-parse failure (invalid leap-year, wrong format, empty) drops
    the DOB to None rather than crashing. Orchestrator re-prompts."""
    result = extract._parse_tool_input({"intent": "ambiguous", "dob": bad_dob})
    assert result.slots.dob is None


def test_parse_dob_alternate_without_dob_is_dropped():
    """An alternate without a primary is meaningless and would confuse the
    orchestrator; cleared so the (dob, dob_alternate) pair is consistent."""
    result = extract._parse_tool_input({
        "intent": "ambiguous",
        "dob": None,
        "dob_alternate": "1990-04-15",
    })
    assert result.slots.dob is None
    assert result.slots.dob_alternate is None


@pytest.mark.parametrize(
    "bad_amount",
    ["", "  ", "abc", "NaN", "Infinity", "-Infinity", True, False, [], {}],
)
def test_parse_invalid_amount_drops_to_none(bad_amount):
    """Non-finite, non-numeric, or non-string amounts drop to None.
    validate.validate_amount catches finite-but-invalid values (zero, neg,
    >2dp) downstream; this layer only screens out garbage that would crash
    Decimal construction or comparison."""
    result = extract._parse_tool_input({"intent": "ambiguous", "amount": bad_amount})
    assert result.slots.amount is None


@pytest.mark.parametrize(
    "pan,expected",
    [
        ("4532015112830366", "4532015112830366"),       # 16 digits, valid len
        ("4532 0151 1283 0366", "4532015112830366"),    # spaces stripped
        ("4532-0151-1283-0366", "4532015112830366"),    # dashes stripped
        ("123456789012", None),                          # 12 digits, too short
        ("12345678901234567890", None),                  # 20 digits, too long
        ("not-a-number", None),                          # no digits
    ],
)
def test_parse_pan_length_and_cleanup(pan, expected):
    """PAN strips non-digit chars; accepts 13-19 digits; otherwise drops."""
    result = extract._parse_tool_input({"intent": "ambiguous", "pan": pan})
    assert result.slots.pan == expected


@pytest.mark.parametrize(
    "cvv,expected",
    [
        ("123", "123"),
        ("1234", "1234"),
        ("12", None),                       # too short
        ("12345", None),                    # too long
        ("ab1", None),                      # 1 digit after strip; too short
    ],
)
def test_parse_cvv_length(cvv, expected):
    """CVV accepts 3 or 4 digits after non-digit cleanup."""
    result = extract._parse_tool_input({"intent": "ambiguous", "cvv": cvv})
    assert result.slots.cvv == expected


@pytest.mark.parametrize(
    "year_in,year_out",
    [(27, 2027), (5, 2005), (2027, 2027), (1999, 1999), ("27", 2027), (None, None)],
)
def test_parse_year_2digit_to_4digit(year_in, year_out):
    """2-digit years (0-99) map to 20YY. 4-digit years pass through.
    Invalid coercions (None, garbage) return None."""
    result = extract._parse_tool_input({
        "intent": "ambiguous", "expiry_year": year_in,
    })
    assert result.slots.expiry_year == year_out


@pytest.mark.parametrize(
    "factor_in,expected",
    [
        ("dob", SecondaryFactor.DOB),
        ("DOB", SecondaryFactor.DOB),
        ("aadhaar_last4", SecondaryFactor.AADHAAR_LAST4),
        ("PINCODE", SecondaryFactor.PINCODE),
        ("garbage", None),
        (None, None),
        (42, None),
    ],
)
def test_parse_secondary_factor_case_insensitive_with_fallback(factor_in, expected):
    """Secondary-factor enum coerce is case-insensitive; unknown drops to None."""
    result = extract._parse_tool_input({
        "intent": "ambiguous", "selected_secondary_factor": factor_in,
    })
    assert result.slots.selected_secondary_factor == expected


@pytest.mark.parametrize(
    "name_in,expected",
    [
        ("Priya Kumar", "Priya Kumar"),
        ("  Priya Kumar  ", "Priya Kumar"),     # whitespace stripped
        ("", None),
        ("   ", None),
        (None, None),
        (42, None),
    ],
)
def test_parse_name_on_card_strip_and_empty(name_in, expected):
    """name_on_card strips whitespace; empty/whitespace-only -> None."""
    result = extract._parse_tool_input({
        "intent": "ambiguous", "name_on_card": name_in,
    })
    assert result.slots.name_on_card == expected


# ---------------------------------------------------------------------------
# Fail-soft no-key path (DECISIONS #18)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_input,expected",
    [
        ("yes please", Intent.AFFIRM),
        ("ok go ahead", Intent.AFFIRM),
        ("confirm", Intent.AFFIRM),
        ("no thanks", Intent.NEGATE),
        ("cancel", Intent.NEGATE),
        ("nevermind", Intent.NEGATE),
        ("I don't want to", Intent.NEGATE),
        ("My DOB is 14-05-1990", Intent.AMBIGUOUS),
        ("ACC1001 please", Intent.AMBIGUOUS),
        ("", Intent.AMBIGUOUS),
    ],
)
def test_fail_soft_intent_classification(user_input, expected, monkeypatch):
    """No-API-key path classifies AFFIRM/NEGATE via regex; everything
    else (including slot-fills) is AMBIGUOUS so the orchestrator re-prompts."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = extract.extract_turn(
        user_input, Stage.AWAITING_PAYMENT_CONFIRMATION, SlotStore(),
    )
    assert result.intent is expected
    # Fail-soft never extracts non-account-id slots.
    assert result.slots.dob is None
    assert result.slots.full_name is None


@pytest.mark.parametrize(
    "stage,expected_account",
    [
        (Stage.GREETING, "ACC1001"),
        (Stage.COLLECTING_ACCOUNT_ID, "ACC1001"),
        (Stage.COLLECTING_IDENTITY, None),                  # not relevant stage
        (Stage.AWAITING_PAYMENT_CONFIRMATION, None),        # off-script here
    ],
)
def test_fail_soft_account_id_extracted_only_in_relevant_stages(
    stage, expected_account, monkeypatch,
):
    """Account-ID regex fires only in the stages where it makes semantic
    sense. In other stages, the same input is intent-only (AMBIGUOUS or
    affirm-via-other-words); no slot fabrication."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = extract.extract_turn(
        "my account is ACC1001", stage, SlotStore(),
    )
    assert result.slots.account_id == expected_account


# ---------------------------------------------------------------------------
# extract_turn end-to-end (with mocked LLM)
# ---------------------------------------------------------------------------


def test_extract_turn_propagates_llm_call_failed(monkeypatch):
    """LlmCallFailed from llm.call_extract is NOT caught in extract_turn;
    it propagates so the orchestrator (agent.py, Step 9) can handle SDK
    failures distinctly from the no-key fail-soft path. DECISIONS #18 only
    covers the absent-key case; SDK errors must surface."""
    from payment_agent.errors import LlmCallFailed

    def boom(**_):
        raise LlmCallFailed("simulated SDK failure")

    monkeypatch.setattr(extract.llm, "call_extract", boom)
    with pytest.raises(LlmCallFailed):
        extract.extract_turn(
            "yes please",
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            SlotStore(),
            api_key="test-key",
        )


@pytest.mark.parametrize("empty_input", ["", "   ", "\n", "\t  \n"])
def test_extract_turn_short_circuits_empty_input_without_llm(empty_input, monkeypatch):
    """Empty/whitespace input returns AMBIGUOUS+empty WITHOUT invoking the
    LLM. Anthropic's Messages API rejects empty user content with HTTP 400;
    deterministically there's nothing to extract from whitespace, so the
    short-circuit avoids a guaranteed-fail API call."""
    def fail_if_called(**_):
        raise AssertionError("llm.call_extract must not be invoked on empty input")
    monkeypatch.setattr(extract.llm, "call_extract", fail_if_called)

    result = extract.extract_turn(
        empty_input, Stage.COLLECTING_IDENTITY, SlotStore(), api_key="test-key",
    )
    assert result.intent is Intent.AMBIGUOUS
    assert result.slots.full_name is None
    assert result.slots.dob is None


def test_extract_turn_passes_stage_aware_prompt_and_parses_response(monkeypatch):
    """End-to-end wiring: extract_turn builds a stage-aware system prompt,
    invokes llm.call_extract with the tool schema + tool name, and parses
    the returned dict into a typed ExtractionResult."""
    captured: dict[str, Any] = {}

    def fake_call_extract(*, system_prompt, user_message, tool, tool_name, api_key):
        captured["system_prompt"] = system_prompt
        captured["user_message"] = user_message
        captured["tool"] = tool
        captured["tool_name"] = tool_name
        captured["api_key"] = api_key
        return {
            "intent": "ambiguous",
            "full_name": "Rajarajeswari Balasubramaniam",
            "dob": "1985-07-22",
        }

    monkeypatch.setattr(extract.llm, "call_extract", fake_call_extract)

    slots = SlotStore(account_id="ACC1001")
    result = extract.extract_turn(
        "you can call me Raja but my full name is Rajarajeswari Balasubramaniam",
        Stage.COLLECTING_IDENTITY,
        slots,
        api_key="test-key",
    )

    # Stage label is in the prompt; already-known slots are listed by name only.
    assert "collecting_identity" in captured["system_prompt"]
    assert "account_id" in captured["system_prompt"]
    # Schema + dispatch hook routed correctly.
    assert captured["tool"] is extract._TOOL_SCHEMA
    assert captured["tool_name"] == extract._TOOL_NAME
    assert captured["api_key"] == "test-key"
    # Returned dict was parsed into typed values.
    assert result.intent is Intent.AMBIGUOUS
    assert result.slots.full_name == "Rajarajeswari Balasubramaniam"
    assert result.slots.dob == date(1985, 7, 22)
