"""HTTP client for /api/lookup-account and /api/process-payment.

Wire-format adapter: this is the only module that knows the API's JSON
shapes and field names. State.py and templates.py work with the internal
slot vocabulary; api.py translates at the boundary.

Per DECISIONS #13:
  - lookup_account: silent retry once at 500ms backoff on transient
    failures (5xx / timeout / connection error / 4xx with undocumented
    error code). Per-attempt timeout 4s, total budget 10s. If still
    failing after the silent retry, surfaces as
    LookupOutcome.TRANSIENT — the orchestrator consumes one user-visible
    retry against the lookup cap (DECISIONS #12).
  - process_payment: never retried on transient failures. A timeout
    doesn't tell us whether the payment processed; without an
    idempotency key, retry risks double-charge. Routes to
    PaymentOutcome.UNKNOWN, which the state machine maps to
    terminal_payment_unknown with the conservative "couldn't confirm"
    copy from DECISIONS #13. Per-call timeout 30s.
  - Failed process_payment attempts logged with the redacted schema
    {account_id, amount, attempted_at, error_class, last4}. CVV NEVER
    logged in any form. PAN logged only as last-4.

Exception handling: ApiUnknown is the parent class for the routing-
identical transient/unknown failures (ApiServerError, ApiUnexpectedResponse).
ApiTimeout and ApiTransport are routing-identical too — we catch the
tuple. The subclass split exists for log/ops visibility (the concrete
class name lands in error_class), not for state-machine behavior.

Architecture commit: this module never sees the LLM and never holds
slot state across calls. It is a pure I/O adapter — caller passes
primitives, callee returns a state.py result type.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from pydantic import ValidationError

from payment_agent import config, redact
from payment_agent.errors import (
    ApiServerError,
    ApiTimeout,
    ApiTransport,
    ApiUnexpectedResponse,
    ApiUnknown,
)
from payment_agent.state import (
    LookupEffectResult,
    LookupOutcome,
    LookupResponse,
    PaymentEffectResult,
    PaymentOutcome,
)

_LOG = logging.getLogger(__name__)

# Maps the API's process-payment error_code strings to PaymentOutcome enum.
# The strings are spec-locked from the PDF "API Error Codes" table.
_PAYMENT_ERROR_MAP: dict[str, PaymentOutcome] = {
    "invalid_amount": PaymentOutcome.INVALID_AMOUNT,
    "insufficient_balance": PaymentOutcome.INSUFFICIENT_BALANCE,
    "invalid_card": PaymentOutcome.INVALID_CARD,
    "invalid_cvv": PaymentOutcome.INVALID_CVV,
    "invalid_expiry": PaymentOutcome.INVALID_EXPIRY,
}


def make_client() -> httpx.Client:
    """Construct an httpx.Client with the spec base URL and per-call timeouts.

    Per-call timeouts are passed at the request site (different per endpoint
    per DECISIONS #13), so the client-level default is generous; the request
    site overrides it.
    """
    return httpx.Client(
        base_url=config.API_BASE_URL,
        timeout=httpx.Timeout(timeout=config.PAYMENT_CALL_TIMEOUT_S),
    )


# ===========================================================================
# lookup_account — silent retry once on transient
# ===========================================================================


def lookup_account(
    account_id: str, *, client: httpx.Client | None = None
) -> LookupEffectResult:
    """POST /api/lookup-account with the silent-retry policy from DECISIONS #13.

    Returns a LookupEffectResult. Never raises — all errors are mapped to
    outcomes the state machine can route on.
    """
    own_client = client is None
    if client is None:
        client = make_client()
    try:
        return _lookup_with_retry(client, account_id)
    finally:
        if own_client:
            client.close()


def _lookup_with_retry(client: httpx.Client, account_id: str) -> LookupEffectResult:
    """Two-attempt path: first attempt, optional 500ms backoff + one retry."""
    deadline = time.monotonic() + config.LOOKUP_TOTAL_BUDGET_S

    # First attempt.
    try:
        return _lookup_once(client, account_id)
    except (ApiTimeout, ApiTransport, ApiUnknown):
        pass  # silent retry on the documented transient classes

    # Backoff before retry, but only if we have budget left.
    if time.monotonic() + config.LOOKUP_SILENT_RETRY_BACKOFF_S > deadline:
        return LookupEffectResult(outcome=LookupOutcome.TRANSIENT)
    time.sleep(config.LOOKUP_SILENT_RETRY_BACKOFF_S)

    # Second attempt.
    try:
        return _lookup_once(client, account_id)
    except (ApiTimeout, ApiTransport, ApiUnknown):
        return LookupEffectResult(outcome=LookupOutcome.TRANSIENT)


def _lookup_once(
    client: httpx.Client, account_id: str
) -> LookupEffectResult:
    """Single lookup attempt.

    Returns a LookupEffectResult on a documented response (200 success or
    404 account_not_found). Raises ApiTimeout / ApiTransport / ApiServerError
    / ApiUnexpectedResponse on the routing-identical transient classes.
    """
    try:
        resp = client.post(
            "/api/lookup-account",
            json={"account_id": account_id},
            timeout=config.LOOKUP_PER_ATTEMPT_TIMEOUT_S,
        )
    except httpx.TimeoutException as e:
        raise ApiTimeout(f"lookup timeout: {e}") from e
    except httpx.RequestError as e:
        raise ApiTransport(f"lookup transport: {e}") from e

    if resp.status_code == 200:
        try:
            data = LookupResponse.model_validate(resp.json())
        except (ValueError, ValidationError) as e:
            raise ApiUnexpectedResponse(
                f"lookup 200 schema mismatch: {e}"
            ) from e
        return LookupEffectResult(
            outcome=LookupOutcome.SUCCESS, account_data=data
        )

    if resp.status_code == 404:
        body = _safe_json(resp)
        if body.get("error_code") == "account_not_found":
            return LookupEffectResult(outcome=LookupOutcome.ACCOUNT_NOT_FOUND)
        raise ApiUnexpectedResponse(
            f"lookup 404 with unexpected body: {body!r}"
        )

    if 500 <= resp.status_code < 600:
        raise ApiServerError(f"lookup {resp.status_code}")

    # Any other 4xx is an undocumented error code — treated as transient
    # per DECISIONS #13.
    raise ApiUnexpectedResponse(
        f"lookup unexpected status {resp.status_code}"
    )


# ===========================================================================
# process_payment — never retried on transient
# ===========================================================================


def process_payment(
    *,
    account_id: str,
    amount: Decimal,
    pan: str,
    cvv: str,
    expiry_month: int,
    expiry_year: int,
    full_name: str,
    client: httpx.Client | None = None,
) -> PaymentEffectResult:
    """POST /api/process-payment. Single attempt, no retry on transient.

    Returns a PaymentEffectResult; never raises. Logs every attempt with
    the redacted schema from DECISIONS #13. CVV is captured here only for
    the request body and is never persisted, logged, or referenced after
    this function returns.

    Boundary rename (path b per cardholder-name design call): the API
    field `cardholder_name` is populated from our internal `full_name`
    slot — the verified user's name. Per the PDF "Important API Notes",
    `cardholder_name` is accepted as-is and not validated against the
    account holder, so passing the verified name straight through is
    correct and saves a slot/turn.
    """
    own_client = client is None
    if client is None:
        client = make_client()

    payload = _build_payment_payload(
        account_id=account_id,
        amount=amount,
        pan=pan,
        cvv=cvv,
        expiry_month=expiry_month,
        expiry_year=expiry_year,
        full_name=full_name,
    )

    last4 = redact.last4(pan)
    attempted_at = datetime.now(timezone.utc)

    try:
        try:
            resp = client.post(
                "/api/process-payment",
                json=payload,
                timeout=config.PAYMENT_CALL_TIMEOUT_S,
            )
        except httpx.TimeoutException as e:
            return _payment_unknown(
                account_id=account_id,
                amount=amount,
                attempted_at=attempted_at,
                last4=last4,
                error_class=type(e).__name__,
                detail=f"timeout: {e}",
            )
        except httpx.RequestError as e:
            return _payment_unknown(
                account_id=account_id,
                amount=amount,
                attempted_at=attempted_at,
                last4=last4,
                error_class=type(e).__name__,
                detail=f"transport: {e}",
            )

        return _classify_payment_response(
            resp,
            account_id=account_id,
            amount=amount,
            attempted_at=attempted_at,
            last4=last4,
        )
    finally:
        if own_client:
            client.close()


def _build_payment_payload(
    *,
    account_id: str,
    amount: Decimal,
    pan: str,
    cvv: str,
    expiry_month: int,
    expiry_year: int,
    full_name: str,
) -> dict[str, Any]:
    """Construct the nested payment_method.card request payload per PDF spec."""
    return {
        "account_id": account_id,
        # API spec example shows `"amount": 500.00` (number). Decimal.float
        # avoids JSON-serialization surprises with Pydantic-y Decimal types.
        "amount": float(amount),
        "payment_method": {
            "type": "card",
            "card": {
                "cardholder_name": full_name,
                "card_number": pan,
                "cvv": cvv,
                "expiry_month": expiry_month,
                "expiry_year": expiry_year,
            },
        },
    }


def _classify_payment_response(
    resp: httpx.Response,
    *,
    account_id: str,
    amount: Decimal,
    attempted_at: datetime,
    last4: str,
) -> PaymentEffectResult:
    """Map HTTP response → PaymentEffectResult; log non-success outcomes."""
    if resp.status_code == 200:
        body = _safe_json(resp)
        if body.get("success") is True and isinstance(
            body.get("transaction_id"), str
        ):
            return PaymentEffectResult(
                outcome=PaymentOutcome.SUCCESS,
                transaction_id=body["transaction_id"],
            )
        return _payment_unknown(
            account_id=account_id,
            amount=amount,
            attempted_at=attempted_at,
            last4=last4,
            error_class=ApiUnexpectedResponse.__name__,
            detail=f"200 without success+transaction_id: {body!r}",
        )

    if resp.status_code == 422:
        body = _safe_json(resp)
        code = body.get("error_code")
        outcome = _PAYMENT_ERROR_MAP.get(code) if isinstance(code, str) else None
        if outcome is None:
            return _payment_unknown(
                account_id=account_id,
                amount=amount,
                attempted_at=attempted_at,
                last4=last4,
                error_class=ApiUnexpectedResponse.__name__,
                detail=f"422 with unknown error_code: {code!r}",
            )
        # Documented payment failure — log with the documented error_code
        # as error_class for ops visibility.
        _log_payment_record(
            account_id=account_id,
            amount=amount,
            attempted_at=attempted_at,
            last4=last4,
            error_class=code,
        )
        return PaymentEffectResult(outcome=outcome)

    if 500 <= resp.status_code < 600:
        return _payment_unknown(
            account_id=account_id,
            amount=amount,
            attempted_at=attempted_at,
            last4=last4,
            error_class=ApiServerError.__name__,
            detail=f"server error {resp.status_code}",
        )

    return _payment_unknown(
        account_id=account_id,
        amount=amount,
        attempted_at=attempted_at,
        last4=last4,
        error_class=ApiUnexpectedResponse.__name__,
        detail=f"unexpected status {resp.status_code}",
    )


def _payment_unknown(
    *,
    account_id: str,
    amount: Decimal,
    attempted_at: datetime,
    last4: str,
    error_class: str,
    detail: str,
) -> PaymentEffectResult:
    """Log + return UNKNOWN. Conservative routing per DECISIONS #13."""
    _log_payment_record(
        account_id=account_id,
        amount=amount,
        attempted_at=attempted_at,
        last4=last4,
        error_class=error_class,
    )
    _LOG.warning("payment unknown: %s", detail)
    return PaymentEffectResult(outcome=PaymentOutcome.UNKNOWN)


def _log_payment_record(
    *,
    account_id: str,
    amount: Decimal,
    attempted_at: datetime,
    last4: str,
    error_class: str | None,
) -> None:
    """Emit the redacted log record per DECISIONS #13."""
    record = redact.build_api_log_record(
        account_id=account_id,
        amount=float(amount),
        attempted_at=attempted_at,
        error_class=error_class,
        last4=last4,
    )
    _LOG.warning("payment attempt: %s", record)


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    """Return the JSON body as a dict, or {} if it isn't a JSON object."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
