"""Tests for tools.py — the five tool wrappers and their schemas.

Covers each tool's preconditions, local validation, stage discriminator,
retry-burn behaviour, terminal routing, and confirmation flag lifecycle.
``api.lookup_account`` and ``api.process_payment`` are monkey-patched so
these tests are pure unit tests with no network contact.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from payment_agent import tools
from payment_agent.api import (
    LookupEffectResult,
    LookupOutcome,
    LookupResponse,
    PaymentEffectResult,
    PaymentOutcome,
)
from payment_agent.session import (
    ConfirmationPending,
    LookupResult,
    SessionState,
    TerminalKind,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


_ACC1001_LOOKUP_RESPONSE = LookupResponse(
    account_id="ACC1001",
    full_name="Nithin Jain",
    dob=date(1990, 5, 14),
    aadhaar_last4="1234",
    pincode="411001",
    balance=Decimal("1250.75"),
)


@pytest.fixture
def session() -> SessionState:
    return SessionState()


@pytest.fixture
def session_with_lookup() -> SessionState:
    s = SessionState()
    s.lookup = LookupResult(
        account_id="ACC1001",
        full_name="Nithin Jain",
        dob=date(1990, 5, 14),
        aadhaar_last4="1234",
        pincode="411001",
        balance=Decimal("1250.75"),
    )
    return s


@pytest.fixture
def verified_session() -> SessionState:
    s = SessionState()
    s.lookup = LookupResult(
        account_id="ACC1001",
        full_name="Nithin Jain",
        dob=date(1990, 5, 14),
        aadhaar_last4="1234",
        pincode="411001",
        balance=Decimal("1250.75"),
    )
    s.verification.locked_name = "nithin jain"
    s.verification.verified = True
    return s


def _patch_lookup(
    monkeypatch: pytest.MonkeyPatch,
    outcome: LookupOutcome,
    account_data: LookupResponse | None = None,
) -> list[str]:
    """Patch ``api.lookup_account`` to return a canned result.

    Returns a list that records the account_id of each call — lets tests
    assert how many API attempts the tool made.
    """
    calls: list[str] = []

    def fake_lookup(account_id: str, *, client: Any = None) -> LookupEffectResult:
        calls.append(account_id)
        return LookupEffectResult(outcome=outcome, account_data=account_data)

    monkeypatch.setattr(tools.api, "lookup_account", fake_lookup)
    return calls


def _patch_lookup_sequence(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[LookupOutcome],
    account_data: LookupResponse | None = None,
) -> list[str]:
    """Patch ``api.lookup_account`` to cycle through a sequence of
    outcomes (one per call). Useful for testing transient → success
    recovery within the tool's retry budget.
    """
    calls: list[str] = []
    it = iter(outcomes)

    def fake_lookup(account_id: str, *, client: Any = None) -> LookupEffectResult:
        calls.append(account_id)
        try:
            outcome = next(it)
        except StopIteration:
            pytest.fail("more calls than scripted outcomes")
        return LookupEffectResult(
            outcome=outcome,
            account_data=account_data if outcome == LookupOutcome.SUCCESS else None,
        )

    monkeypatch.setattr(tools.api, "lookup_account", fake_lookup)
    return calls


def _patch_payment(
    monkeypatch: pytest.MonkeyPatch,
    outcome: PaymentOutcome,
    transaction_id: str | None = None,
) -> list[dict[str, Any]]:
    """Patch ``api.process_payment`` to return a canned result.

    Returns a list that records each call's kwargs.
    """
    calls: list[dict[str, Any]] = []

    def fake_payment(**kwargs: Any) -> PaymentEffectResult:
        calls.append(kwargs)
        return PaymentEffectResult(outcome=outcome, transaction_id=transaction_id)

    monkeypatch.setattr(tools.api, "process_payment", fake_payment)
    return calls


# ===========================================================================
# Tool schemas
# ===========================================================================


class TestToolSchemas:
    def test_five_schemas_registered(self):
        assert len(tools.TOOL_SCHEMAS) == 5

    def test_schema_names_match_handler_registry(self):
        schema_names = {s["name"] for s in tools.TOOL_SCHEMAS}
        handler_names = set(tools.TOOL_HANDLERS.keys())
        assert schema_names == handler_names

    def test_every_schema_has_input_schema_object(self):
        for s in tools.TOOL_SCHEMAS:
            assert s["input_schema"]["type"] == "object"
            assert "properties" in s["input_schema"]
            assert "required" in s["input_schema"]

    def test_render_canonical_message_kinds_match_template_registry(self):
        schema = tools.RENDER_CANONICAL_MESSAGE_SCHEMA
        schema_kinds = set(
            schema["input_schema"]["properties"]["kind"]["enum"]
        )
        template_kinds = set(tools._TEMPLATES.keys())
        assert schema_kinds == template_kinds
        # And exactly the ten kinds from V2-21.
        assert len(schema_kinds) == 10


# ===========================================================================
# lookup_account
# ===========================================================================


class TestLookupAccountInvalidFormat:
    def test_local_validation_returns_stage_local_validation(self, session):
        result = tools.lookup_account_handler(session, {"account_id": "bad"})
        assert result["stage"] == "local_validation"
        assert result["found"] is False
        assert result["error_class"] == "INVALID_ACCOUNT_ID_FORMAT"

    def test_no_api_call_made(self, session, monkeypatch):
        calls = _patch_lookup(monkeypatch, LookupOutcome.SUCCESS)
        tools.lookup_account_handler(session, {"account_id": ""})
        assert calls == []

    def test_terminal_not_set(self, session):
        tools.lookup_account_handler(session, {"account_id": "bad"})
        assert session.terminal is None
        assert session.lookup is None


class TestLookupAccountSuccess:
    def test_returns_found_true_with_data(self, session, monkeypatch):
        _patch_lookup(monkeypatch, LookupOutcome.SUCCESS, _ACC1001_LOOKUP_RESPONSE)
        result = tools.lookup_account_handler(
            session, {"account_id": "ACC1001"}
        )
        assert result["found"] is True
        assert result["stage"] == "api_response"
        assert result["account_id"] == "ACC1001"
        assert result["full_name"] == "Nithin Jain"
        assert result["balance"] == 1250.75
        assert result["currency"] == "INR"
        assert result["error_class"] is None
        assert result["terminal"] is None

    def test_session_lookup_cached_with_all_fields(
        self, session, monkeypatch
    ):
        _patch_lookup(monkeypatch, LookupOutcome.SUCCESS, _ACC1001_LOOKUP_RESPONSE)
        tools.lookup_account_handler(session, {"account_id": "ACC1001"})
        assert session.lookup is not None
        assert session.lookup.account_id == "ACC1001"
        assert session.lookup.full_name == "Nithin Jain"
        assert session.lookup.dob == date(1990, 5, 14)
        assert session.lookup.aadhaar_last4 == "1234"
        assert session.lookup.pincode == "411001"
        assert session.lookup.balance == Decimal("1250.75")

    def test_single_api_attempt_on_success(self, session, monkeypatch):
        calls = _patch_lookup(
            monkeypatch, LookupOutcome.SUCCESS, _ACC1001_LOOKUP_RESPONSE
        )
        tools.lookup_account_handler(session, {"account_id": "ACC1001"})
        assert len(calls) == 1


class TestLookupAccountNotFound:
    def test_sets_terminal_account_not_found(self, session, monkeypatch):
        _patch_lookup(monkeypatch, LookupOutcome.ACCOUNT_NOT_FOUND)
        result = tools.lookup_account_handler(
            session, {"account_id": "ACC9999"}
        )
        assert result["found"] is False
        assert result["stage"] == "api_response"
        assert result["error_class"] == "ACCOUNT_NOT_FOUND"
        assert result["terminal"] == "account_not_found"
        assert session.terminal == TerminalKind.ACCOUNT_NOT_FOUND

    def test_no_retry_on_account_not_found(self, session, monkeypatch):
        calls = _patch_lookup(monkeypatch, LookupOutcome.ACCOUNT_NOT_FOUND)
        tools.lookup_account_handler(session, {"account_id": "ACC9999"})
        assert len(calls) == 1

    def test_lookup_not_cached(self, session, monkeypatch):
        _patch_lookup(monkeypatch, LookupOutcome.ACCOUNT_NOT_FOUND)
        tools.lookup_account_handler(session, {"account_id": "ACC9999"})
        assert session.lookup is None


class TestLookupAccountTransient:
    def test_full_budget_4_attempts_then_terminal(
        self, session, monkeypatch
    ):
        calls = _patch_lookup(monkeypatch, LookupOutcome.TRANSIENT)
        result = tools.lookup_account_handler(
            session, {"account_id": "ACC1001"}
        )
        assert len(calls) == 4  # 1 initial + 3 transparent retries
        assert result["stage"] == "api_call"
        assert result["error_class"] == "TRANSIENT_UNRESOLVABLE"
        assert result["terminal"] == "lookup_unresolvable"
        assert session.terminal == TerminalKind.LOOKUP_UNRESOLVABLE

    def test_transient_then_success_recovers(self, session, monkeypatch):
        calls = _patch_lookup_sequence(
            monkeypatch,
            [LookupOutcome.TRANSIENT, LookupOutcome.SUCCESS],
            account_data=_ACC1001_LOOKUP_RESPONSE,
        )
        result = tools.lookup_account_handler(
            session, {"account_id": "ACC1001"}
        )
        assert len(calls) == 2
        assert result["found"] is True
        assert session.terminal is None


class TestLookupAccountAntiEnumeration:
    def test_second_lookup_returns_preconditions(
        self, session_with_lookup, monkeypatch
    ):
        calls = _patch_lookup(monkeypatch, LookupOutcome.SUCCESS)
        result = tools.lookup_account_handler(
            session_with_lookup, {"account_id": "ACC1002"}
        )
        assert result["stage"] == "preconditions"
        assert result["error_class"] == "ACCOUNT_ALREADY_LOOKED_UP"
        assert calls == []  # no API call made


# ===========================================================================
# submit_verification
# ===========================================================================


class TestSubmitVerificationPreconditions:
    def test_not_looked_up_returns_preconditions(self, session):
        result = tools.submit_verification_handler(
            session,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert result["verified"] is False
        assert result["stage"] == "preconditions"
        assert result["error_class"] == "NOT_LOOKED_UP"
        assert result["retries_remaining"] == 3  # unchanged

    def test_account_mismatch_returns_preconditions(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC9999",  # different
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert result["stage"] == "preconditions"
        assert result["error_class"] == "ACCOUNT_MISMATCH"
        assert result["retries_remaining"] == 3


class TestSubmitVerificationCycleLock:
    def test_first_attempt_locks_name(self, session_with_lookup):
        tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        # locked_name is the canonicalized form
        assert session_with_lookup.verification.locked_name == "nithin jain"

    def test_different_name_returns_cycle_violation(
        self, session_with_lookup
    ):
        # First attempt locks "Nithin Jain"
        tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1980-01-01"},
            },
        )
        # Second attempt: different name
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Priya Mehta",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert result["verified"] is False
        assert result["stage"] == "cycle_lock"
        assert result["error_class"] == "CYCLE_VIOLATION_NAME"
        assert result["failed_fields"] == ["name"]

    def test_cycle_violation_does_not_burn_retry(self, session_with_lookup):
        # Burn one retry first via a real comparison failure.
        tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1980-01-01"},
            },
        )
        counter_after_real_fail = session_with_lookup.verification.counter
        # Now cycle violation
        tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Priya Mehta",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert session_with_lookup.verification.counter == counter_after_real_fail

    def test_whitespace_or_case_difference_does_not_violate(
        self, session_with_lookup
    ):
        tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1980-01-01"},
            },
        )
        # Same name, different case+whitespace
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "  NITHIN   JAIN ",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert result["stage"] != "cycle_lock"


class TestSubmitVerificationLocalValidation:
    def test_invalid_dob_format(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "14-05-1990"},
            },
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "INVALID_DOB_FORMAT"
        assert result["retries_remaining"] == 3  # unchanged

    def test_invalid_aadhaar_format_too_short(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "aadhaar_last4", "value": "12"},
            },
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "INVALID_AADHAAR_FORMAT"

    def test_invalid_pincode_format_letters(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "pincode", "value": "ABC123"},
            },
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "INVALID_PINCODE_FORMAT"

    def test_unknown_factor_type(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "iris_scan", "value": "x"},
            },
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "INVALID_FACTOR_TYPE"

    def test_empty_name(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "   ",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "INVALID_NAME_FORMAT"


class TestSubmitVerificationSuccess:
    def test_correct_dob_succeeds(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert result["verified"] is True
        assert result["stage"] == "comparison"
        assert result["failed_fields"] == []
        assert session_with_lookup.verification.verified is True

    def test_correct_aadhaar_succeeds(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {
                    "type": "aadhaar_last4",
                    "value": "1234",
                },
            },
        )
        assert result["verified"] is True

    def test_correct_pincode_succeeds(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "pincode", "value": "411001"},
            },
        )
        assert result["verified"] is True


class TestSubmitVerificationFailure:
    def test_wrong_dob_decrements_counter(self, session_with_lookup):
        before = session_with_lookup.verification.counter
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1980-01-01"},
            },
        )
        assert result["verified"] is False
        assert result["stage"] == "comparison"
        assert "dob" in result["failed_fields"]
        assert session_with_lookup.verification.counter == before - 1
        assert result["retries_remaining"] == before - 1

    def test_wrong_name_decrements_counter(self, session_with_lookup):
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Notnithin Notjain",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert result["verified"] is False
        assert "name" in result["failed_fields"]

    def test_exhaustion_after_4_failures(self, session_with_lookup):
        # 3 retries shown to user (counter 3→2→1→0), 4th failure terminal.
        for _ in range(3):
            result = tools.submit_verification_handler(
                session_with_lookup,
                {
                    "account_id": "ACC1001",
                    "full_name": "Nithin Jain",
                    "secondary_factor": {"type": "dob", "value": "1980-01-01"},
                },
            )
            assert result["terminal"] is None
        # 4th failure: terminal.
        result = tools.submit_verification_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1980-01-01"},
            },
        )
        assert result["terminal"] == "verification_exhausted"
        assert (
            session_with_lookup.terminal == TerminalKind.VERIFICATION_EXHAUSTED
        )
        assert result["retries_remaining"] == 0


# ===========================================================================
# process_payment
# ===========================================================================


_GOOD_CARD = {
    "number": "4532 0151 1283 0366",
    "cvv": "123",
    "expiry_month": 12,
    "expiry_year": 2099,  # far future to avoid 'now()' coupling
}


def _set_confirmation(
    session: SessionState, *, amount: Decimal, last4: str
) -> None:
    session.confirmation_pending = ConfirmationPending(
        amount=amount, last4=last4
    )


class TestProcessPaymentPreconditions:
    def test_not_verified(self, session_with_lookup):
        result = tools.process_payment_handler(
            session_with_lookup,
            {
                "account_id": "ACC1001",
                "amount": 500,
                "card": _GOOD_CARD,
            },
        )
        assert result["stage"] == "preconditions"
        assert result["error_class"] == "NOT_VERIFIED"
        assert result["retries_remaining"] == 5  # unchanged

    def test_account_mismatch(self, verified_session):
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        result = tools.process_payment_handler(
            verified_session,
            {
                "account_id": "ACC9999",
                "amount": 500,
                "card": _GOOD_CARD,
            },
        )
        assert result["stage"] == "preconditions"
        assert result["error_class"] == "ACCOUNT_MISMATCH"


class TestProcessPaymentConfirmation:
    def test_no_pending_returns_not_confirmed(self, verified_session):
        result = tools.process_payment_handler(
            verified_session,
            {
                "account_id": "ACC1001",
                "amount": 500,
                "card": _GOOD_CARD,
            },
        )
        assert result["stage"] == "preconditions"
        assert result["error_class"] == "NOT_CONFIRMED"
        assert result["retries_remaining"] == 5  # unchanged

    def test_amount_mismatch_returns_confirmation_mismatch(
        self, verified_session
    ):
        _set_confirmation(verified_session, amount=Decimal("600"), last4="0366")
        result = tools.process_payment_handler(
            verified_session,
            {
                "account_id": "ACC1001",
                "amount": 500,
                "card": _GOOD_CARD,
            },
        )
        assert result["error_class"] == "CONFIRMATION_MISMATCH"
        assert verified_session.confirmation_pending is None  # consumed

    def test_last4_mismatch_returns_confirmation_mismatch(
        self, verified_session
    ):
        _set_confirmation(verified_session, amount=Decimal("500"), last4="9999")
        result = tools.process_payment_handler(
            verified_session,
            {
                "account_id": "ACC1001",
                "amount": 500,
                "card": _GOOD_CARD,
            },
        )
        assert result["error_class"] == "CONFIRMATION_MISMATCH"

    def test_confirmation_consumed_on_attempt(
        self, verified_session, monkeypatch
    ):
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        _patch_payment(
            monkeypatch, PaymentOutcome.SUCCESS, transaction_id="txn_xyz"
        )
        tools.process_payment_handler(
            verified_session,
            {
                "account_id": "ACC1001",
                "amount": 500,
                "card": _GOOD_CARD,
            },
        )
        assert verified_session.confirmation_pending is None


class TestProcessPaymentLocalValidation:
    def test_luhn_failure_no_retry_burn(self, verified_session, monkeypatch):
        bad_card = dict(_GOOD_CARD, number="4111 1111 1111 1112")
        _set_confirmation(verified_session, amount=Decimal("500"), last4="1112")
        calls = _patch_payment(monkeypatch, PaymentOutcome.SUCCESS)
        before = verified_session.payment.counter
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": bad_card},
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "INVALID_CARD_LUHN"
        assert verified_session.payment.counter == before
        assert calls == []  # no API call

    def test_cvv_format_failure(self, verified_session):
        bad_card = dict(_GOOD_CARD, cvv="ab")
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": bad_card},
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "INVALID_CVV_FORMAT"

    def test_expiry_past(self, verified_session):
        bad_card = dict(_GOOD_CARD, expiry_year=2000, expiry_month=1)
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": bad_card},
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "EXPIRY_PAST"

    def test_amount_zero_invalid(self, verified_session):
        _set_confirmation(verified_session, amount=Decimal("0"), last4="0366")
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 0, "card": _GOOD_CARD},
        )
        assert result["stage"] == "local_validation"
        assert result["error_class"] == "INVALID_AMOUNT"


class TestProcessPaymentSuccess:
    def test_success_sets_terminal_completed(
        self, verified_session, monkeypatch
    ):
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        _patch_payment(
            monkeypatch, PaymentOutcome.SUCCESS, transaction_id="txn_abc"
        )
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": _GOOD_CARD},
        )
        assert result["success"] is True
        assert result["stage"] == "api_response"
        assert result["transaction_id"] == "txn_abc"
        assert result["terminal"] == "completed"
        assert verified_session.terminal == TerminalKind.COMPLETED
        assert result["remaining_balance"] == 750.75  # 1250.75 - 500
        assert result["last4"] == "0366"

    def test_passes_verified_full_name_as_cardholder(
        self, verified_session, monkeypatch
    ):
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        calls = _patch_payment(
            monkeypatch, PaymentOutcome.SUCCESS, transaction_id="txn_abc"
        )
        tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": _GOOD_CARD},
        )
        # api.process_payment receives the verified user's name
        assert calls[0]["full_name"] == "Nithin Jain"


class TestProcessPaymentApiFailures:
    def test_insufficient_balance_no_retry_burn(
        self, verified_session, monkeypatch
    ):
        _set_confirmation(verified_session, amount=Decimal("2000"), last4="0366")
        _patch_payment(monkeypatch, PaymentOutcome.INSUFFICIENT_BALANCE)
        before = verified_session.payment.counter
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 2000, "card": _GOOD_CARD},
        )
        assert result["error_class"] == "INSUFFICIENT_BALANCE"
        assert result["stage"] == "api_response"
        assert verified_session.payment.counter == before
        assert verified_session.terminal is None

    def test_invalid_card_burns_retry(self, verified_session, monkeypatch):
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        _patch_payment(monkeypatch, PaymentOutcome.INVALID_CARD)
        before = verified_session.payment.counter
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": _GOOD_CARD},
        )
        assert result["error_class"] == "INVALID_CARD"
        assert result["stage"] == "api_response"
        assert verified_session.payment.counter == before - 1
        assert result["retries_remaining"] == before - 1

    def test_invalid_cvv_burns_retry(self, verified_session, monkeypatch):
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        _patch_payment(monkeypatch, PaymentOutcome.INVALID_CVV)
        before = verified_session.payment.counter
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": _GOOD_CARD},
        )
        assert result["error_class"] == "INVALID_CVV"
        assert verified_session.payment.counter == before - 1

    def test_payment_exhausted_after_6_typo_failures(
        self, verified_session, monkeypatch
    ):
        # Counter starts at 5; 5 failures bring it to 0; 6th is terminal.
        _patch_payment(monkeypatch, PaymentOutcome.INVALID_CARD)
        for _ in range(5):
            _set_confirmation(
                verified_session, amount=Decimal("500"), last4="0366"
            )
            result = tools.process_payment_handler(
                verified_session,
                {
                    "account_id": "ACC1001",
                    "amount": 500,
                    "card": _GOOD_CARD,
                },
            )
            assert result["terminal"] is None
        # 6th failure
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": _GOOD_CARD},
        )
        assert result["terminal"] == "payment_exhausted"
        assert verified_session.terminal == TerminalKind.PAYMENT_EXHAUSTED

    def test_unknown_transient_sets_payment_unknown(
        self, verified_session, monkeypatch
    ):
        _set_confirmation(verified_session, amount=Decimal("500"), last4="0366")
        _patch_payment(monkeypatch, PaymentOutcome.UNKNOWN)
        result = tools.process_payment_handler(
            verified_session,
            {"account_id": "ACC1001", "amount": 500, "card": _GOOD_CARD},
        )
        assert result["stage"] == "api_call"
        assert result["error_class"] == "TRANSIENT_NO_RETRY"
        assert result["terminal"] == "payment_unknown"
        assert verified_session.terminal == TerminalKind.PAYMENT_UNKNOWN
        # Counter not decremented (transient is not a typo-class)
        assert verified_session.payment.counter == 5


# ===========================================================================
# cancel_session
# ===========================================================================


class TestCancelSession:
    def test_sets_terminal_cancelled(self, session):
        result = tools.cancel_session_handler(
            session, {"reason": "user_requested_cancellation"}
        )
        assert result["terminated"] is True
        assert result["terminal"] == "cancelled"
        assert session.terminal == TerminalKind.CANCELLED

    def test_already_terminal_preserves_existing_kind(self, session):
        session.terminal = TerminalKind.COMPLETED
        result = tools.cancel_session_handler(
            session, {"reason": "scope_shift"}
        )
        # Original terminal kind survives — cancel_session is a no-op.
        assert session.terminal == TerminalKind.COMPLETED
        assert result["terminal"] == "completed"

    def test_returns_message(self, session):
        result = tools.cancel_session_handler(
            session, {"reason": "out_of_scope_request", "detail": "user asked for refund"}
        )
        assert "cancelled" in result["message"].lower()


# ===========================================================================
# render_canonical_message
# ===========================================================================


class TestRenderCanonicalMessage:
    def test_greeting(self, session):
        result = tools.render_canonical_message_handler(
            session, {"kind": "greeting", "slots": {}}
        )
        assert result["error"] is None
        assert "Hello" in result["message"]
        assert "account ID" in result["message"]

    def test_unknown_kind(self, session):
        result = tools.render_canonical_message_handler(
            session, {"kind": "frobnicate", "slots": {}}
        )
        assert result["error"] == "UNKNOWN_KIND"

    def test_missing_slots(self, session):
        result = tools.render_canonical_message_handler(
            session,
            {"kind": "verify_success_with_balance", "slots": {"balance": 100}},
        )
        assert result["error"] == "MISSING_SLOTS"

    def test_verify_success_with_balance_renders(self, session):
        result = tools.render_canonical_message_handler(
            session,
            {
                "kind": "verify_success_with_balance",
                "slots": {"full_name": "Nithin Jain", "balance": 1250.75},
            },
        )
        assert result["error"] is None
        assert "Thanks, Nithin Jain!" in result["message"]
        assert "₹1,250.75" in result["message"]

    def test_payment_success_recap_renders_with_all_slots(self, session):
        result = tools.render_canonical_message_handler(
            session,
            {
                "kind": "payment_success_recap",
                "slots": {
                    "account_id": "ACC1001",
                    "amount": 500,
                    "transaction_id": "txn_xyz",
                    "last4": "0366",
                    "remaining_balance": 750.75,
                },
            },
        )
        assert result["error"] is None
        assert "Payment successful" in result["message"]
        assert "ACC1001" in result["message"]
        assert "txn_xyz" in result["message"]
        assert "0366" in result["message"]
        assert "₹500.00" in result["message"]
        assert "₹750.75" in result["message"]


class TestConfirmationPromptSideEffect:
    def test_sets_pending_with_binding_values(self, session):
        result = tools.render_canonical_message_handler(
            session,
            {
                "kind": "confirmation_prompt",
                "slots": {
                    "amount": 500,
                    "last4": "0366",
                    "expiry_month": 12,
                    "expiry_year": 2027,
                },
            },
        )
        assert result["error"] is None
        assert session.confirmation_pending == ConfirmationPending(
            amount=Decimal("500"), last4="0366"
        )

    def test_re_emission_overwrites_pending(self, session):
        tools.render_canonical_message_handler(
            session,
            {
                "kind": "confirmation_prompt",
                "slots": {
                    "amount": 500,
                    "last4": "0366",
                    "expiry_month": 12,
                    "expiry_year": 2027,
                },
            },
        )
        # User changes their mind; LLM re-emits with new values.
        tools.render_canonical_message_handler(
            session,
            {
                "kind": "confirmation_prompt",
                "slots": {
                    "amount": 250,
                    "last4": "0366",
                    "expiry_month": 12,
                    "expiry_year": 2027,
                },
            },
        )
        assert session.confirmation_pending.amount == Decimal("250")

    def test_message_includes_amount_and_last4(self, session):
        result = tools.render_canonical_message_handler(
            session,
            {
                "kind": "confirmation_prompt",
                "slots": {
                    "amount": 500,
                    "last4": "0366",
                    "expiry_month": 12,
                    "expiry_year": 2027,
                },
            },
        )
        assert "₹500.00" in result["message"]
        assert "ending 0366" in result["message"]
        assert "12/2027" in result["message"]
        assert "yes" in result["message"].lower()


class TestRenderTerminalKinds:
    @pytest.mark.parametrize(
        "kind",
        [
            "account_not_found",
            "verification_exhausted",
            "payment_exhausted",
            "payment_unknown",
            "cancelled",
            "session_closed",
        ],
    )
    def test_terminal_kinds_render_without_slots(self, session, kind):
        result = tools.render_canonical_message_handler(
            session, {"kind": kind, "slots": {}}
        )
        assert result["error"] is None
        assert result["message"]  # non-empty
        # Each terminal-flavored message must invite support contact OR
        # mention session ending/cancellation — they're all closure copy.
        lower = result["message"].lower()
        assert any(
            phrase in lower
            for phrase in (
                "session has ended",
                "ended",
                "cancelled",
                "contact our support",
                "start a new session",
            )
        ), f"unexpected message for kind={kind}: {result['message']!r}"


# ===========================================================================
# Integration: happy path through the tools end-to-end
# ===========================================================================


class TestHappyPathToolChain:
    """Drives the 5 tools through the happy-path sequence to confirm the
    cross-tool state transitions hold together: lookup → verify → render
    confirmation → process_payment success → render recap.
    """

    def test_full_chain_completes(self, session, monkeypatch):
        # 1. Lookup
        _patch_lookup(monkeypatch, LookupOutcome.SUCCESS, _ACC1001_LOOKUP_RESPONSE)
        r1 = tools.lookup_account_handler(session, {"account_id": "ACC1001"})
        assert r1["found"] is True
        assert session.lookup is not None

        # 2. Verify
        r2 = tools.submit_verification_handler(
            session,
            {
                "account_id": "ACC1001",
                "full_name": "Nithin Jain",
                "secondary_factor": {"type": "dob", "value": "1990-05-14"},
            },
        )
        assert r2["verified"] is True
        assert session.verification.verified is True

        # 3. Render confirmation prompt (sets pending)
        r3 = tools.render_canonical_message_handler(
            session,
            {
                "kind": "confirmation_prompt",
                "slots": {
                    "amount": 500,
                    "last4": "0366",
                    "expiry_month": 12,
                    "expiry_year": 2099,
                },
            },
        )
        assert r3["error"] is None
        assert session.confirmation_pending is not None

        # 4. Process payment
        _patch_payment(monkeypatch, PaymentOutcome.SUCCESS, transaction_id="txn_z")
        r4 = tools.process_payment_handler(
            session,
            {"account_id": "ACC1001", "amount": 500, "card": _GOOD_CARD},
        )
        assert r4["success"] is True
        assert session.terminal == TerminalKind.COMPLETED
        assert session.confirmation_pending is None  # consumed

        # 5. Render recap
        r5 = tools.render_canonical_message_handler(
            session,
            {
                "kind": "payment_success_recap",
                "slots": {
                    "account_id": "ACC1001",
                    "amount": 500,
                    "transaction_id": "txn_z",
                    "last4": "0366",
                    "remaining_balance": 750.75,
                },
            },
        )
        assert r5["error"] is None
        assert "Payment successful" in r5["message"]
