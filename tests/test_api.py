"""Tests for api.py — HTTP client with mocked transport (respx).

Per DECISIONS #13:
  - lookup_account: silent retry once at 500ms backoff on transient
  - process_payment: never retried on transient
  - Failed process_payment attempts logged with redacted schema
    {account_id, amount, attempted_at, error_class, last4}
  - CVV NEVER logged in any form

Per PDF API spec:
  - lookup-account 200: full LookupResponse shape
  - lookup-account 404: {"error_code": "account_not_found", "message": ...}
  - process-payment 200: {"success": true, "transaction_id": "txn_..."}
  - process-payment 422: {"success": false, "error_code": "..."}
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

import httpx
import pytest

from payment_agent import api, config
from payment_agent.state import (
    LookupOutcome,
    PaymentOutcome,
)


VALID_PAN = "4532015112830366"
VALID_PAN_LAST4 = "0366"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """No-op the silent-retry backoff in every test in this module.

    The 500ms `time.sleep` between lookup retries adds ~3.5s of wall time
    across ~7 retry-firing tests. respx already intercepts all network, so
    real wall-clock sleep adds zero value here. Keeping the patch autouse
    ensures retry-budget tests added later don't silently re-introduce
    runtime drag.
    """
    monkeypatch.setattr(api.time, "sleep", lambda _: None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _success_lookup_body() -> dict:
    return {
        "account_id": "ACC1001",
        "full_name": "Nithin Jain",
        "dob": "1990-05-14",
        "aadhaar_last4": "4321",
        "pincode": "400001",
        "balance": 1250.75,
    }


def _payment_kwargs() -> dict:
    return dict(
        account_id="ACC1001",
        amount=Decimal("500"),
        pan=VALID_PAN,
        cvv="123",
        expiry_month=12,
        expiry_year=2027,
        full_name="Nithin Jain",
    )


# ===========================================================================
# Section 1 — lookup_account: success path
# ===========================================================================


class TestLookupSuccess:
    def test_200_returns_success_with_validated_response(self, respx_mock) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            return_value=httpx.Response(200, json=_success_lookup_body())
        )
        result = api.lookup_account("ACC1001")
        assert result.outcome == LookupOutcome.SUCCESS
        assert result.account_data is not None
        assert result.account_data.account_id == "ACC1001"
        assert result.account_data.balance == Decimal("1250.75")

    def test_request_body_uses_account_id_field(self, respx_mock) -> None:
        route = respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            return_value=httpx.Response(200, json=_success_lookup_body())
        )
        api.lookup_account("ACC1001")
        sent = json.loads(route.calls.last.request.content)
        assert sent == {"account_id": "ACC1001"}

    def test_200_with_extra_field_raises_routes_to_transient(self, respx_mock) -> None:
        # Pydantic strict mode: extra="forbid" rejects unknown fields.
        # ApiUnexpectedResponse → routed via silent retry → TRANSIENT.
        body = _success_lookup_body() | {"extra_field": "surprise"}
        respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            return_value=httpx.Response(200, json=body)
        )
        result = api.lookup_account("ACC1001")
        assert result.outcome == LookupOutcome.TRANSIENT


# ===========================================================================
# Section 2 — lookup_account: 404 account_not_found
# ===========================================================================


class TestLookupAccountNotFound:
    def test_404_with_documented_error_code(self, respx_mock) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            return_value=httpx.Response(
                404,
                json={
                    "error_code": "account_not_found",
                    "message": "No account found with the provided account_id.",
                },
            )
        )
        result = api.lookup_account("ACC9999")
        assert result.outcome == LookupOutcome.ACCOUNT_NOT_FOUND
        assert result.account_data is None

    def test_404_with_undocumented_error_code_routes_to_transient(self, respx_mock) -> None:
        # Per DECISIONS #13: 4xx with undocumented error code is treated as
        # transient (silent retry → TRANSIENT after exhaust).
        respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            return_value=httpx.Response(404, json={"error_code": "rate_limited"})
        )
        result = api.lookup_account("ACC1001")
        assert result.outcome == LookupOutcome.TRANSIENT

    def test_404_does_not_consume_silent_retry(self, respx_mock) -> None:
        # account_not_found is a documented 404 — should be returned on the
        # first attempt without using the silent retry budget.
        route = respx_mock.post(
            f"{config.API_BASE_URL}/api/lookup-account"
        ).mock(
            return_value=httpx.Response(
                404, json={"error_code": "account_not_found"}
            )
        )
        api.lookup_account("ACC9999")
        assert route.call_count == 1


# ===========================================================================
# Section 3 — lookup_account: silent transient retry
# ===========================================================================


class TestLookupSilentRetry:
    def test_500_then_200_succeeds_via_silent_retry(self, respx_mock) -> None:
        responses = [
            httpx.Response(500),
            httpx.Response(200, json=_success_lookup_body()),
        ]
        route = respx_mock.post(
            f"{config.API_BASE_URL}/api/lookup-account"
        ).mock(side_effect=responses)
        result = api.lookup_account("ACC1001")
        assert result.outcome == LookupOutcome.SUCCESS
        assert route.call_count == 2

    def test_500_500_routes_to_transient(self, respx_mock) -> None:
        # Both attempts fail → TRANSIENT (orchestrator consumes one user-
        # visible retry against the lookup cap per DECISIONS #12).
        respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            side_effect=[httpx.Response(500), httpx.Response(500)]
        )
        result = api.lookup_account("ACC1001")
        assert result.outcome == LookupOutcome.TRANSIENT

    def test_503_then_200_succeeds(self, respx_mock) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=_success_lookup_body()),
            ]
        )
        result = api.lookup_account("ACC1001")
        assert result.outcome == LookupOutcome.SUCCESS

    def test_timeout_then_200_succeeds(self, respx_mock) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            side_effect=[
                httpx.TimeoutException("test timeout"),
                httpx.Response(200, json=_success_lookup_body()),
            ]
        )
        result = api.lookup_account("ACC1001")
        assert result.outcome == LookupOutcome.SUCCESS

    def test_connection_error_then_200_succeeds(self, respx_mock) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/lookup-account").mock(
            side_effect=[
                httpx.ConnectError("test connect"),
                httpx.Response(200, json=_success_lookup_body()),
            ]
        )
        result = api.lookup_account("ACC1001")
        assert result.outcome == LookupOutcome.SUCCESS


# ===========================================================================
# Section 4 — process_payment: success path
# ===========================================================================


class TestProcessPaymentSuccess:
    def test_200_returns_success_with_transaction_id(self, respx_mock) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "transaction_id": "txn_1762510325322_l1f14oy",
                },
            )
        )
        result = api.process_payment(**_payment_kwargs())
        assert result.outcome == PaymentOutcome.SUCCESS
        assert result.transaction_id == "txn_1762510325322_l1f14oy"

    def test_request_body_has_nested_payment_method_card_structure(self, respx_mock) -> None:
        route = respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                200, json={"success": True, "transaction_id": "txn_x"}
            )
        )
        api.process_payment(**_payment_kwargs())
        sent = json.loads(route.calls.last.request.content)
        # Verify the spec-locked nested shape.
        assert sent["account_id"] == "ACC1001"
        assert sent["amount"] == 500.0
        assert sent["payment_method"]["type"] == "card"
        card = sent["payment_method"]["card"]
        assert card["cardholder_name"] == "Nithin Jain"  # boundary rename
        assert card["card_number"] == VALID_PAN
        assert card["cvv"] == "123"
        assert card["expiry_month"] == 12
        assert card["expiry_year"] == 2027

    def test_cardholder_name_is_full_name_at_boundary(self, respx_mock) -> None:
        # Boundary rename per design call (b): internal `full_name` becomes
        # API's `cardholder_name`. Verified in request shape.
        route = respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                200, json={"success": True, "transaction_id": "txn_x"}
            )
        )
        kwargs = _payment_kwargs() | {"full_name": "Rajarajeswari Balasubramaniam"}
        api.process_payment(**kwargs)
        sent = json.loads(route.calls.last.request.content)
        assert sent["payment_method"]["card"]["cardholder_name"] == (
            "Rajarajeswari Balasubramaniam"
        )


# ===========================================================================
# Section 5 — process_payment: documented 422 error mapping
# ===========================================================================


class TestProcessPaymentErrors:
    @pytest.mark.parametrize(
        "error_code,expected_outcome",
        [
            ("invalid_amount", PaymentOutcome.INVALID_AMOUNT),
            ("insufficient_balance", PaymentOutcome.INSUFFICIENT_BALANCE),
            ("invalid_card", PaymentOutcome.INVALID_CARD),
            ("invalid_cvv", PaymentOutcome.INVALID_CVV),
            ("invalid_expiry", PaymentOutcome.INVALID_EXPIRY),
        ],
    )
    def test_422_documented_error_codes_map_correctly(
        self, respx_mock, error_code: str, expected_outcome: PaymentOutcome
    ) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                422, json={"success": False, "error_code": error_code}
            )
        )
        result = api.process_payment(**_payment_kwargs())
        assert result.outcome == expected_outcome
        assert result.transaction_id is None

    def test_422_undocumented_error_code_routes_to_unknown(self, respx_mock) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                422,
                json={"success": False, "error_code": "card_blocked_by_issuer"},
            )
        )
        result = api.process_payment(**_payment_kwargs())
        # Per DECISIONS #13: undocumented error codes treated as transient,
        # which on payment maps to UNKNOWN (no retry).
        assert result.outcome == PaymentOutcome.UNKNOWN


# ===========================================================================
# Section 6 — process_payment: NO retry on transient
# ===========================================================================


class TestProcessPaymentNoRetry:
    def test_500_routes_to_unknown_with_one_call(self, respx_mock) -> None:
        # Critical correctness invariant from DECISIONS #13: process_payment
        # is NEVER retried on transient. A 500 must produce exactly one call.
        route = respx_mock.post(
            f"{config.API_BASE_URL}/api/process-payment"
        ).mock(return_value=httpx.Response(500))
        result = api.process_payment(**_payment_kwargs())
        assert result.outcome == PaymentOutcome.UNKNOWN
        assert route.call_count == 1

    def test_timeout_routes_to_unknown_with_one_call(self, respx_mock) -> None:
        # Timeout doesn't tell us if the payment processed — without an
        # idempotency key, retry would risk double-charge.
        route = respx_mock.post(
            f"{config.API_BASE_URL}/api/process-payment"
        ).mock(side_effect=httpx.TimeoutException("test timeout"))
        result = api.process_payment(**_payment_kwargs())
        assert result.outcome == PaymentOutcome.UNKNOWN
        assert route.call_count == 1

    def test_connection_error_routes_to_unknown(self, respx_mock) -> None:
        route = respx_mock.post(
            f"{config.API_BASE_URL}/api/process-payment"
        ).mock(side_effect=httpx.ConnectError("test connect"))
        result = api.process_payment(**_payment_kwargs())
        assert result.outcome == PaymentOutcome.UNKNOWN
        assert route.call_count == 1

    def test_200_without_transaction_id_routes_to_unknown(self, respx_mock) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        result = api.process_payment(**_payment_kwargs())
        assert result.outcome == PaymentOutcome.UNKNOWN

    def test_200_with_success_false_routes_to_unknown(self, respx_mock) -> None:
        # Spec-anomalous: 200 with success=false. Conservative routing.
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                200, json={"success": False, "transaction_id": "txn_x"}
            )
        )
        result = api.process_payment(**_payment_kwargs())
        assert result.outcome == PaymentOutcome.UNKNOWN


# ===========================================================================
# Section 7 — Redacted logging (DECISIONS #13 hard constraints)
# ===========================================================================


class TestRedactedLogging:
    def test_cvv_never_in_log_records(self, respx_mock, caplog) -> None:
        # The hard rule: CVV never written to logs in any form.
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                422, json={"success": False, "error_code": "invalid_card"}
            )
        )
        kwargs = _payment_kwargs() | {"cvv": "987"}  # distinctive sentinel
        with caplog.at_level(logging.WARNING):
            api.process_payment(**kwargs)
        for record in caplog.records:
            assert "987" not in record.getMessage(), (
                f"CVV leaked into log: {record.getMessage()!r}"
            )

    def test_full_pan_never_in_log_records(self, respx_mock, caplog) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                422, json={"success": False, "error_code": "invalid_card"}
            )
        )
        with caplog.at_level(logging.WARNING):
            api.process_payment(**_payment_kwargs())
        for record in caplog.records:
            assert VALID_PAN not in record.getMessage(), (
                f"Full PAN leaked: {record.getMessage()!r}"
            )

    def test_log_contains_last4_account_id_amount_error_class(
        self, respx_mock, caplog
    ) -> None:
        # Schema check: {account_id, amount, attempted_at, error_class, last4}
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                422, json={"success": False, "error_code": "insufficient_balance"}
            )
        )
        with caplog.at_level(logging.WARNING):
            api.process_payment(**_payment_kwargs())
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "ACC1001" in joined
        assert "0366" in joined  # last-4
        assert "insufficient_balance" in joined  # error_class
        assert "500" in joined  # amount

    def test_success_path_does_not_emit_failure_log(self, respx_mock, caplog) -> None:
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(
                200, json={"success": True, "transaction_id": "txn_x"}
            )
        )
        with caplog.at_level(logging.WARNING):
            api.process_payment(**_payment_kwargs())
        # No "payment attempt" or "payment unknown" log on success.
        for record in caplog.records:
            msg = record.getMessage()
            assert "payment attempt" not in msg
            assert "payment unknown" not in msg

    def test_unknown_outcome_logs_concrete_subclass_name(
        self, respx_mock, caplog
    ) -> None:
        # Per the parent-catch-with-subclass-logging rule: the subclass split
        # exists for ops visibility — concrete class name in error_class.
        respx_mock.post(f"{config.API_BASE_URL}/api/process-payment").mock(
            return_value=httpx.Response(503)
        )
        with caplog.at_level(logging.WARNING):
            api.process_payment(**_payment_kwargs())
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "ApiServerError" in joined  # concrete class, not "ApiUnknown"
