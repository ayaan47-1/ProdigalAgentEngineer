"""Tool wrappers — the kernel-mediated guardrail layer.

The LLM emits tool calls; this module receives them, runs the kernel
function with the provided arguments, mutates SessionState as needed,
and returns a structured result the LLM consumes on the next inference
pass.

Five tools per DECISIONS_V2 V2-3:

  1. ``lookup_account`` — wraps ``api.lookup_account`` with anti-enumeration
     preconditions and the kernel-internal retry budget (V2-9).
  2. ``submit_verification`` — strict-equality compare via ``verify.py``;
     enforces cycle-name lock (V2-10) and the per-cycle retry counter.
  3. ``process_payment`` — wraps ``api.process_payment`` with the
     confirmation-pending gate (V2-11), local validation, and the
     asymmetric retry policy.
  4. ``cancel_session`` — explicit terminal exit by user intent.
  5. ``render_canonical_message`` — owns the 10 spec-required canonical
     strings. The ``confirmation_prompt`` kind also sets the
     ``confirmation_pending`` flag on the session.

Single-writer discipline: these handlers are the ONLY mutators of
``SessionState`` (orchestrator reads ``terminal`` for short-circuit but
never writes). Counter-burn rules:

  - ``submit_verification`` decrements ``session.verification.counter``
    only on ``stage=comparison + verified=false``.
  - ``process_payment`` decrements ``session.payment.counter`` only on
    documented typo-class API responses (INVALID_CARD / INVALID_CVV /
    INVALID_EXPIRY).
  - Local-validation, preconditions, and cycle-lock failures never burn.
  - Transient/unknown payment outcomes are immediate-terminal (never
    retried per DECISIONS #13).

Counter semantics: counters start at the cap value (3 for verification,
5 for payment) and count down. When a failing event arrives with the
counter already at 0, that event is the (cap+1)th and triggers the
terminal route (verification_exhausted / payment_exhausted).
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from payment_agent import api, validate, verify
from payment_agent.api import (
    LookupEffectResult,
    LookupOutcome,
    PaymentEffectResult,
    PaymentOutcome,
)
from payment_agent.session import (
    ConfirmationPending,
    LookupResult,
    SessionState,
    TerminalKind,
)

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# api.lookup_account already does 1 silent retry internally per DECISIONS
# #13. V2-9 adds up to 3 transparent retries on top of that, all inside
# one tool call. Total: 1 initial + 3 retries = 4 attempts of
# api.lookup_account from the tool's perspective.
_LOOKUP_USER_VISIBLE_RETRIES: int = 3


# Tool name string constants — used for the schema 'name' field, the
# dispatcher registry, and the orchestrator's lookup.
TOOL_LOOKUP_ACCOUNT: str = "lookup_account"
TOOL_SUBMIT_VERIFICATION: str = "submit_verification"
TOOL_PROCESS_PAYMENT: str = "process_payment"
TOOL_CANCEL_SESSION: str = "cancel_session"
TOOL_RENDER_CANONICAL_MESSAGE: str = "render_canonical_message"


# Canonical kind string constants — matched against the schema enum and
# the templates dict.
KIND_GREETING: str = "greeting"
KIND_ACCOUNT_NOT_FOUND: str = "account_not_found"
KIND_VERIFY_SUCCESS_WITH_BALANCE: str = "verify_success_with_balance"
KIND_CONFIRMATION_PROMPT: str = "confirmation_prompt"
KIND_PAYMENT_SUCCESS_RECAP: str = "payment_success_recap"
KIND_VERIFICATION_EXHAUSTED: str = "verification_exhausted"
KIND_PAYMENT_EXHAUSTED: str = "payment_exhausted"
KIND_PAYMENT_UNKNOWN: str = "payment_unknown"
KIND_CANCELLED: str = "cancelled"
KIND_SESSION_CLOSED: str = "session_closed"

ALL_KINDS: list[str] = [
    KIND_GREETING,
    KIND_ACCOUNT_NOT_FOUND,
    KIND_VERIFY_SUCCESS_WITH_BALANCE,
    KIND_CONFIRMATION_PROMPT,
    KIND_PAYMENT_SUCCESS_RECAP,
    KIND_VERIFICATION_EXHAUSTED,
    KIND_PAYMENT_EXHAUSTED,
    KIND_PAYMENT_UNKNOWN,
    KIND_CANCELLED,
    KIND_SESSION_CLOSED,
]


# ===========================================================================
# Tool schemas (Anthropic SDK input_schema format)
# ===========================================================================

LOOKUP_ACCOUNT_SCHEMA: dict[str, Any] = {
    "name": TOOL_LOOKUP_ACCOUNT,
    "description": (
        "Look up an account by ID. Returns the account holder's name and "
        "outstanding balance on success. Call this once, after the user has "
        "provided their account ID. The kernel handles transient API "
        "retries internally; the LLM should not loop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": (
                    "Account identifier in the form ACC followed by 4 digits "
                    "(e.g., ACC1001). Capture exactly as the user stated it; "
                    "do not silently correct typos."
                ),
            },
        },
        "required": ["account_id"],
    },
}

SUBMIT_VERIFICATION_SCHEMA: dict[str, Any] = {
    "name": TOOL_SUBMIT_VERIFICATION,
    "description": (
        "Submit a single identity-verification attempt. Provide the user's "
        "full name AND one secondary factor (DOB / Aadhaar last-4 / "
        "pincode). Both must match exactly for verification to succeed. A "
        "failed attempt decrements the retry budget; local-format failures "
        "and cycle-lock rejections do not. Within a verification cycle, "
        "the first full_name submitted locks the name; submitting a "
        "different name returns CYCLE_VIOLATION_NAME."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": (
                    "Must match the account_id of the successful lookup."
                ),
            },
            "full_name": {
                "type": "string",
                "description": (
                    "Full name as the user stated it. The kernel "
                    "canonicalizes whitespace and case before strict compare."
                ),
            },
            "secondary_factor": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["dob", "aadhaar_last4", "pincode"],
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "For 'dob': strict ISO YYYY-MM-DD (e.g., "
                            "1990-05-14). For 'aadhaar_last4': exactly 4 "
                            "digits. For 'pincode': exactly 6 digits."
                        ),
                    },
                },
                "required": ["type", "value"],
            },
        },
        "required": ["account_id", "full_name", "secondary_factor"],
    },
}

PROCESS_PAYMENT_SCHEMA: dict[str, Any] = {
    "name": TOOL_PROCESS_PAYMENT,
    "description": (
        "Submit a payment. Requires verification to have succeeded AND a "
        "matching pending confirmation (see render_canonical_message with "
        "kind='confirmation_prompt'). Local validation (Luhn, CVV format, "
        "expiry, amount) runs before the API call; validation failures do "
        "not burn the typo-class retry budget. Payment is never retried "
        "on transient failure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string"},
            "amount": {
                "type": "number",
                "description": "Payment amount in INR. Must be positive.",
            },
            "card": {
                "type": "object",
                "properties": {
                    "number": {
                        "type": "string",
                        "description": (
                            "PAN as digits; spaces and dashes are stripped."
                        ),
                    },
                    "cvv": {
                        "type": "string",
                        "description": "3 or 4 digits.",
                    },
                    "expiry_month": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 12,
                    },
                    "expiry_year": {
                        "type": "integer",
                        "description": "4-digit year (e.g., 2027).",
                    },
                },
                "required": ["number", "cvv", "expiry_month", "expiry_year"],
            },
        },
        "required": ["account_id", "amount", "card"],
    },
}

CANCEL_SESSION_SCHEMA: dict[str, Any] = {
    "name": TOOL_CANCEL_SESSION,
    "description": (
        "Terminate the session by user intent. Call this when the user "
        "wants to stop, declines to verify, or attempts to redirect to "
        "an off-topic / out-of-scope task. Sets session terminal=cancelled. "
        "Do not call this for normal flow completion (process_payment "
        "success handles that)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": [
                    "user_declined_verification",
                    "user_requested_cancellation",
                    "scope_shift",
                    "out_of_scope_request",
                    "other",
                ],
            },
            "detail": {
                "type": "string",
                "description": "Optional free-text context for logs.",
            },
        },
        "required": ["reason"],
    },
}

RENDER_CANONICAL_MESSAGE_SCHEMA: dict[str, Any] = {
    "name": TOOL_RENDER_CANONICAL_MESSAGE,
    "description": (
        "Render a canonical spec-required message. Use the returned "
        "'message' string in your reply to the user. For "
        "'confirmation_prompt', the call also sets a pending-confirmation "
        "flag on the session that process_payment requires."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ALL_KINDS,
            },
            "slots": {
                "type": "object",
                "description": (
                    "Per-kind required slots. greeting / account_not_found / "
                    "verification_exhausted / payment_exhausted / "
                    "payment_unknown / cancelled / session_closed: no slots "
                    "(pass {}). verify_success_with_balance: {full_name, "
                    "balance}. confirmation_prompt: {amount, last4, "
                    "expiry_month, expiry_year}. payment_success_recap: "
                    "{account_id, amount, transaction_id, last4, "
                    "remaining_balance}."
                ),
            },
        },
        "required": ["kind", "slots"],
    },
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    LOOKUP_ACCOUNT_SCHEMA,
    SUBMIT_VERIFICATION_SCHEMA,
    PROCESS_PAYMENT_SCHEMA,
    CANCEL_SESSION_SCHEMA,
    RENDER_CANONICAL_MESSAGE_SCHEMA,
]


# ===========================================================================
# Canonical templates
# ===========================================================================
#
# Each entry is (required_slot_names, template_string). The template is
# formatted via .format(**slots); slot names must match. ₹ is U+20B9.

_TEMPLATES: dict[str, tuple[set[str], str]] = {
    KIND_GREETING: (
        set(),
        "Hello! I'm here to help you make a payment. Could you share your "
        "account ID to get started?",
    ),
    KIND_ACCOUNT_NOT_FOUND: (
        set(),
        "I couldn't find that account. For your security, this session has "
        "ended. Please contact our support team for assistance.",
    ),
    KIND_VERIFY_SUCCESS_WITH_BALANCE: (
        {"full_name", "balance"},
        "Thanks, {full_name}! Your identity is verified. Your outstanding "
        "balance is ₹{balance:,.2f}.",
    ),
    KIND_CONFIRMATION_PROMPT: (
        {"amount", "last4", "expiry_month", "expiry_year"},
        "I'm about to charge ₹{amount:,.2f} to card ending {last4}, "
        "expiry {expiry_month:02d}/{expiry_year}. Reply 'yes' to confirm.",
    ),
    KIND_PAYMENT_SUCCESS_RECAP: (
        {"account_id", "amount", "transaction_id", "last4", "remaining_balance"},
        "Payment successful. Account: {account_id}. Amount paid: "
        "₹{amount:,.2f}. Transaction ID: {transaction_id}. Card "
        "ending: {last4}. Remaining balance: ₹{remaining_balance:,.2f}.",
    ),
    KIND_VERIFICATION_EXHAUSTED: (
        set(),
        "I wasn't able to verify your identity. For your security, this "
        "session has ended. Please contact our support team for assistance.",
    ),
    KIND_PAYMENT_EXHAUSTED: (
        set(),
        "I wasn't able to process your payment after several attempts. For "
        "your security, this session has ended. Please contact our support "
        "team for assistance.",
    ),
    KIND_PAYMENT_UNKNOWN: (
        set(),
        "I couldn't confirm the outcome of your payment. For your security, "
        "please contact our support team to check the status before retrying.",
    ),
    KIND_CANCELLED: (
        set(),
        "Session cancelled. If you'd like to make a payment, please start a "
        "new session.",
    ),
    KIND_SESSION_CLOSED: (
        set(),
        "This session has ended. Please start a new session if you'd like "
        "to make a payment.",
    ),
}


# ===========================================================================
# Result builders
# ===========================================================================
#
# Each tool returns a dict that the orchestrator serializes verbatim into
# a tool_result block. Helper builders keep the field ordering consistent
# so eval assertions and the system prompt can rely on a stable shape.


def _lookup_result(
    *,
    found: bool,
    stage: str,
    account_id: str,
    full_name: str | None,
    balance: float | None,
    currency: str | None,
    error_class: str | None,
    terminal: str | None,
    message: str,
) -> dict[str, Any]:
    return {
        "found": found,
        "stage": stage,
        "account_id": account_id,
        "full_name": full_name,
        "balance": balance,
        "currency": currency,
        "error_class": error_class,
        "terminal": terminal,
        "message": message,
    }


def _verification_result(
    *,
    verified: bool,
    stage: str,
    error_class: str | None,
    retries_remaining: int,
    terminal: str | None,
    message: str,
    failed_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "verified": verified,
        "stage": stage,
        "failed_fields": list(failed_fields) if failed_fields else [],
        "error_class": error_class,
        "retries_remaining": retries_remaining,
        "terminal": terminal,
        "message": message,
    }


def _payment_result(
    *,
    success: bool,
    stage: str,
    error_class: str | None,
    retries_remaining: int,
    terminal: str | None,
    message: str,
    transaction_id: str | None = None,
    remaining_balance: float | None = None,
    last4: str | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "stage": stage,
        "transaction_id": transaction_id,
        "error_class": error_class,
        "retries_remaining": retries_remaining,
        "remaining_balance": remaining_balance,
        "last4": last4,
        "terminal": terminal,
        "message": message,
    }


# ===========================================================================
# Tool handlers
# ===========================================================================


def lookup_account_handler(
    session: SessionState,
    input_args: dict[str, Any],
    *,
    http_client: httpx.Client | None = None,
    **_unused: Any,
) -> dict[str, Any]:
    """Tool implementation for ``lookup_account``.

    Performs the full lookup retry budget (1 silent + 3 transparent =
    4 attempts) inside one call. The LLM sees only the final outcome:
    success, account_not_found (terminal), or lookup_unresolvable
    (terminal).
    """
    account_id_raw = input_args.get("account_id", "")
    if not isinstance(account_id_raw, str):
        account_id_raw = (
            str(account_id_raw) if account_id_raw is not None else ""
        )

    # Anti-enumeration precondition.
    if session.lookup is not None:
        return _lookup_result(
            found=False,
            stage="preconditions",
            account_id=account_id_raw,
            full_name=None,
            balance=None,
            currency=None,
            error_class="ACCOUNT_ALREADY_LOOKED_UP",
            terminal=None,
            message=(
                "An account has already been looked up for this session. "
                "Anti-enumeration prevents looking up a different account."
            ),
        )

    # Local validation.
    try:
        cleaned = validate.validate_account_id(account_id_raw)
    except ValueError as e:
        return _lookup_result(
            found=False,
            stage="local_validation",
            account_id=account_id_raw,
            full_name=None,
            balance=None,
            currency=None,
            error_class="INVALID_ACCOUNT_ID_FORMAT",
            terminal=None,
            message=f"Account ID must be ACC followed by 4 digits ({e}).",
        )

    # API call with kernel-internal retry budget. First attempt outside
    # the loop so ``result`` is unambiguously non-None at the use sites.
    result: LookupEffectResult = api.lookup_account(cleaned, client=http_client)
    for _attempt in range(_LOOKUP_USER_VISIBLE_RETRIES):
        if result.outcome != LookupOutcome.TRANSIENT:
            break
        result = api.lookup_account(cleaned, client=http_client)

    if result.outcome == LookupOutcome.SUCCESS:
        data = result.account_data
        if data is None:
            # Defensive: api.lookup_account guarantees account_data on
            # SUCCESS, but a future refactor could regress this. Treat
            # as transient-unresolvable rather than crashing.
            session.terminal = TerminalKind.LOOKUP_UNRESOLVABLE
            return _lookup_result(
                found=False,
                stage="api_call",
                account_id=cleaned,
                full_name=None,
                balance=None,
                currency=None,
                error_class="TRANSIENT_UNRESOLVABLE",
                terminal=TerminalKind.LOOKUP_UNRESOLVABLE.value,
                message=(
                    "Internal: API returned success without account_data."
                ),
            )
        session.lookup = LookupResult(
            account_id=data.account_id,
            full_name=data.full_name,
            dob=data.dob,
            aadhaar_last4=data.aadhaar_last4,
            pincode=data.pincode,
            balance=data.balance,
            currency="INR",
        )
        return _lookup_result(
            found=True,
            stage="api_response",
            account_id=data.account_id,
            full_name=data.full_name,
            balance=float(data.balance),
            currency="INR",
            error_class=None,
            terminal=None,
            message="Account found.",
        )

    if result.outcome == LookupOutcome.ACCOUNT_NOT_FOUND:
        session.terminal = TerminalKind.ACCOUNT_NOT_FOUND
        return _lookup_result(
            found=False,
            stage="api_response",
            account_id=cleaned,
            full_name=None,
            balance=None,
            currency=None,
            error_class="ACCOUNT_NOT_FOUND",
            terminal=TerminalKind.ACCOUNT_NOT_FOUND.value,
            message="That account could not be found.",
        )

    # TRANSIENT after full retry budget exhausted.
    session.terminal = TerminalKind.LOOKUP_UNRESOLVABLE
    return _lookup_result(
        found=False,
        stage="api_call",
        account_id=cleaned,
        full_name=None,
        balance=None,
        currency=None,
        error_class="TRANSIENT_UNRESOLVABLE",
        terminal=TerminalKind.LOOKUP_UNRESOLVABLE.value,
        message=(
            "Unable to look up the account due to a service issue. The "
            "session has ended."
        ),
    )


def submit_verification_handler(
    session: SessionState,
    input_args: dict[str, Any],
    **_unused: Any,
) -> dict[str, Any]:
    """Tool implementation for ``submit_verification``.

    See module docstring for the retry-burn rules and cycle-lock
    semantics.
    """
    account_id = input_args.get("account_id", "")
    full_name = input_args.get("full_name", "")
    factor_obj = input_args.get("secondary_factor") or {}
    factor_type = (
        factor_obj.get("type") if isinstance(factor_obj, dict) else None
    )
    factor_value = (
        factor_obj.get("value") if isinstance(factor_obj, dict) else None
    )

    # Preconditions.
    if session.lookup is None:
        return _verification_result(
            verified=False,
            stage="preconditions",
            error_class="NOT_LOOKED_UP",
            retries_remaining=session.verification.counter,
            terminal=None,
            message="No account has been looked up yet; cannot verify.",
        )

    if account_id != session.lookup.account_id:
        return _verification_result(
            verified=False,
            stage="preconditions",
            error_class="ACCOUNT_MISMATCH",
            retries_remaining=session.verification.counter,
            terminal=None,
            message=(
                "The account_id passed to submit_verification does not "
                "match the account that was looked up."
            ),
        )

    # Cycle name lock (V2-10). Canonicalize before compare so whitespace
    # and case don't trigger a spurious lock violation.
    if not isinstance(full_name, str) or not full_name.strip():
        return _verification_result(
            verified=False,
            stage="local_validation",
            error_class="INVALID_NAME_FORMAT",
            retries_remaining=session.verification.counter,
            terminal=None,
            message="full_name must be a non-empty string.",
        )

    canonical_submitted = verify._canonicalize_name(full_name)
    if session.verification.locked_name is None:
        session.verification.locked_name = canonical_submitted
    elif session.verification.locked_name != canonical_submitted:
        return _verification_result(
            verified=False,
            stage="cycle_lock",
            error_class="CYCLE_VIOLATION_NAME",
            retries_remaining=session.verification.counter,
            terminal=None,
            failed_fields=["name"],
            message=(
                "A different name was given earlier in this verification "
                "cycle. Re-confirm with the user and submit the same name, "
                "or call cancel_session."
            ),
        )

    # Local validation of factor format. Then run strict compare.
    parsed_dob: date | None = None
    if factor_type == "dob":
        if not isinstance(factor_value, str):
            return _verification_result(
                verified=False,
                stage="local_validation",
                error_class="INVALID_DOB_FORMAT",
                retries_remaining=session.verification.counter,
                terminal=None,
                message="dob value must be a string in YYYY-MM-DD format.",
            )
        try:
            parsed_dob = validate.parse_iso_date(factor_value)
        except ValueError as e:
            return _verification_result(
                verified=False,
                stage="local_validation",
                error_class="INVALID_DOB_FORMAT",
                retries_remaining=session.verification.counter,
                terminal=None,
                message=f"DOB format invalid: {e}",
            )
    elif factor_type == "aadhaar_last4":
        if not (
            isinstance(factor_value, str)
            and factor_value.isdigit()
            and len(factor_value) == 4
        ):
            return _verification_result(
                verified=False,
                stage="local_validation",
                error_class="INVALID_AADHAAR_FORMAT",
                retries_remaining=session.verification.counter,
                terminal=None,
                message="aadhaar_last4 must be exactly 4 digits.",
            )
    elif factor_type == "pincode":
        if not (
            isinstance(factor_value, str)
            and factor_value.isdigit()
            and len(factor_value) == 6
        ):
            return _verification_result(
                verified=False,
                stage="local_validation",
                error_class="INVALID_PINCODE_FORMAT",
                retries_remaining=session.verification.counter,
                terminal=None,
                message="pincode must be exactly 6 digits.",
            )
    else:
        return _verification_result(
            verified=False,
            stage="local_validation",
            error_class="INVALID_FACTOR_TYPE",
            retries_remaining=session.verification.counter,
            terminal=None,
            message=(
                f"Unknown factor type: {factor_type!r}. "
                "Expected one of: dob, aadhaar_last4, pincode."
            ),
        )

    # Strict comparison via verify.py.
    failed_fields: list[str] = []
    if not verify.compare_name(full_name, session.lookup.full_name):
        failed_fields.append("name")

    if factor_type == "dob":
        assert parsed_dob is not None
        if not verify.compare_dob(parsed_dob, session.lookup.dob):
            failed_fields.append("dob")
    elif factor_type == "aadhaar_last4":
        # compare_aadhaar_last4 may raise ValueError on malformed input;
        # we already format-checked above, so this is defense-in-depth.
        try:
            if not verify.compare_aadhaar_last4(
                factor_value, session.lookup.aadhaar_last4
            ):
                failed_fields.append("aadhaar_last4")
        except ValueError as e:
            return _verification_result(
                verified=False,
                stage="local_validation",
                error_class="INVALID_AADHAAR_FORMAT",
                retries_remaining=session.verification.counter,
                terminal=None,
                message=f"Aadhaar format check failed: {e}",
            )
    elif factor_type == "pincode":
        try:
            if not verify.compare_pincode(
                factor_value, session.lookup.pincode
            ):
                failed_fields.append("pincode")
        except ValueError as e:
            return _verification_result(
                verified=False,
                stage="local_validation",
                error_class="INVALID_PINCODE_FORMAT",
                retries_remaining=session.verification.counter,
                terminal=None,
                message=f"Pincode format check failed: {e}",
            )

    # Outcome.
    if not failed_fields:
        session.verification.verified = True
        return _verification_result(
            verified=True,
            stage="comparison",
            error_class=None,
            retries_remaining=session.verification.counter,
            terminal=None,
            message="Identity verified.",
        )

    # Failed comparison: decrement first, then check terminal. The
    # intuitive semantic — "3 retries" means 3 total attempts before
    # the kernel terminates the session. retries_remaining=N in the
    # tool return means "N more attempts allowed"; at 0 the kernel
    # has already terminated on this failure.
    session.verification.counter -= 1
    if session.verification.counter == 0:
        session.terminal = TerminalKind.VERIFICATION_EXHAUSTED
        return _verification_result(
            verified=False,
            stage="comparison",
            error_class=None,
            retries_remaining=0,
            terminal=TerminalKind.VERIFICATION_EXHAUSTED.value,
            failed_fields=failed_fields,
            message="Verification failed and the retry budget is exhausted.",
        )

    return _verification_result(
        verified=False,
        stage="comparison",
        error_class=None,
        retries_remaining=session.verification.counter,
        terminal=None,
        failed_fields=failed_fields,
        message=f"Verification failed on: {', '.join(failed_fields)}.",
    )


def process_payment_handler(
    session: SessionState,
    input_args: dict[str, Any],
    *,
    http_client: httpx.Client | None = None,
    **_unused: Any,
) -> dict[str, Any]:
    """Tool implementation for ``process_payment``.

    See module docstring for retry-burn rules and the confirmation-pending
    gate.
    """
    account_id = input_args.get("account_id", "")
    amount_raw = input_args.get("amount")
    card_obj = input_args.get("card") or {}
    if not isinstance(card_obj, dict):
        card_obj = {}

    # Preconditions: verified + account match.
    if not session.verification.verified:
        return _payment_result(
            success=False,
            stage="preconditions",
            error_class="NOT_VERIFIED",
            retries_remaining=session.payment.counter,
            terminal=None,
            message=(
                "Payment cannot proceed: identity has not been verified."
            ),
        )

    if session.lookup is None or account_id != session.lookup.account_id:
        return _payment_result(
            success=False,
            stage="preconditions",
            error_class="ACCOUNT_MISMATCH",
            retries_remaining=session.payment.counter,
            terminal=None,
            message="account_id does not match the verified account.",
        )

    # Confirmation gate (V2-11). Compute last4 from the submitted PAN to
    # compare against the pending flag.
    pan_raw = str(card_obj.get("number", ""))
    pan_digits = "".join(c for c in pan_raw if c.isdigit())
    last4 = pan_digits[-4:] if len(pan_digits) >= 4 else pan_digits

    try:
        amount_dec = Decimal(str(amount_raw))
    except (InvalidOperation, ValueError, TypeError):
        amount_dec = None

    if session.confirmation_pending is None:
        return _payment_result(
            success=False,
            stage="preconditions",
            error_class="NOT_CONFIRMED",
            retries_remaining=session.payment.counter,
            terminal=None,
            message=(
                "No pending confirmation. Emit confirmation_prompt via "
                "render_canonical_message and wait for the user's "
                "affirmative before calling process_payment."
            ),
        )

    pending = session.confirmation_pending
    if (
        amount_dec is None
        or pending.amount != amount_dec
        or pending.last4 != last4
    ):
        # Consume the flag — re-confirmation required.
        session.confirmation_pending = None
        return _payment_result(
            success=False,
            stage="preconditions",
            error_class="CONFIRMATION_MISMATCH",
            retries_remaining=session.payment.counter,
            terminal=None,
            message=(
                "The amount or card last-4 in process_payment does not "
                "match the pending confirmation. Re-emit confirmation_"
                "prompt with the correct values and obtain user confirmation "
                "before retrying."
            ),
        )

    # Confirmation matches. Consume the flag now; payment is being
    # attempted.
    session.confirmation_pending = None

    # Local validation (stage=local_validation; never burns a retry).
    try:
        amount_validated = validate.validate_amount(amount_dec)
    except ValueError as e:
        return _payment_result(
            success=False,
            stage="local_validation",
            error_class="INVALID_AMOUNT",
            retries_remaining=session.payment.counter,
            terminal=None,
            message=f"Amount invalid: {e}",
        )

    try:
        pan_validated = validate.validate_pan(pan_raw)
    except ValueError as e:
        return _payment_result(
            success=False,
            stage="local_validation",
            error_class="INVALID_CARD_LUHN",
            retries_remaining=session.payment.counter,
            terminal=None,
            message=f"Card number did not pass our checksum: {e}",
        )

    try:
        cvv_validated = validate.validate_cvv(str(card_obj.get("cvv", "")))
    except ValueError as e:
        return _payment_result(
            success=False,
            stage="local_validation",
            error_class="INVALID_CVV_FORMAT",
            retries_remaining=session.payment.counter,
            terminal=None,
            message=f"CVV format invalid: {e}",
        )

    expiry_month_raw = card_obj.get("expiry_month")
    expiry_year_raw = card_obj.get("expiry_year")
    if not (
        isinstance(expiry_month_raw, int)
        and not isinstance(expiry_month_raw, bool)
        and 1 <= expiry_month_raw <= 12
    ):
        return _payment_result(
            success=False,
            stage="local_validation",
            error_class="INVALID_EXPIRY_FORMAT",
            retries_remaining=session.payment.counter,
            terminal=None,
            message="expiry_month must be an integer 1-12.",
        )
    if not (
        isinstance(expiry_year_raw, int)
        and not isinstance(expiry_year_raw, bool)
        and 2000 <= expiry_year_raw <= 2099
    ):
        return _payment_result(
            success=False,
            stage="local_validation",
            error_class="INVALID_EXPIRY_FORMAT",
            retries_remaining=session.payment.counter,
            terminal=None,
            message="expiry_year must be a 4-digit year.",
        )
    today = date.today()
    if expiry_year_raw < today.year or (
        expiry_year_raw == today.year and expiry_month_raw < today.month
    ):
        return _payment_result(
            success=False,
            stage="local_validation",
            error_class="EXPIRY_PAST",
            retries_remaining=session.payment.counter,
            terminal=None,
            message=f"Card expired ({expiry_month_raw:02d}/{expiry_year_raw}).",
        )

    # API call. Never retried on transient (DECISIONS #13).
    result: PaymentEffectResult = api.process_payment(
        account_id=account_id,
        amount=amount_validated,
        pan=pan_validated,
        cvv=cvv_validated,
        expiry_month=expiry_month_raw,
        expiry_year=expiry_year_raw,
        full_name=session.lookup.full_name,
        client=http_client,
    )

    if result.outcome == PaymentOutcome.SUCCESS:
        session.terminal = TerminalKind.COMPLETED
        remaining = session.lookup.balance - amount_validated
        return _payment_result(
            success=True,
            stage="api_response",
            error_class=None,
            transaction_id=result.transaction_id,
            retries_remaining=session.payment.counter,
            remaining_balance=float(remaining),
            last4=last4,
            terminal=TerminalKind.COMPLETED.value,
            message="Payment processed successfully.",
        )

    if result.outcome == PaymentOutcome.INSUFFICIENT_BALANCE:
        # No retry burn. The LLM should ask the user for a smaller amount,
        # then re-emit confirmation_prompt.
        return _payment_result(
            success=False,
            stage="api_response",
            error_class="INSUFFICIENT_BALANCE",
            retries_remaining=session.payment.counter,
            terminal=None,
            last4=last4,
            message="The amount exceeds the available balance.",
        )

    typo_class = {
        PaymentOutcome.INVALID_CARD: "INVALID_CARD",
        PaymentOutcome.INVALID_CVV: "INVALID_CVV",
        PaymentOutcome.INVALID_EXPIRY: "INVALID_EXPIRY",
    }
    if result.outcome in typo_class:
        # Decrement first, then check terminal — intuitive semantic.
        session.payment.counter -= 1
        if session.payment.counter == 0:
            session.terminal = TerminalKind.PAYMENT_EXHAUSTED
            return _payment_result(
                success=False,
                stage="api_response",
                error_class=typo_class[result.outcome],
                retries_remaining=0,
                terminal=TerminalKind.PAYMENT_EXHAUSTED.value,
                last4=last4,
                message="Payment retry budget exhausted.",
            )
        return _payment_result(
            success=False,
            stage="api_response",
            error_class=typo_class[result.outcome],
            retries_remaining=session.payment.counter,
            terminal=None,
            last4=last4,
            message=f"Payment failed: {typo_class[result.outcome]}.",
        )

    if result.outcome == PaymentOutcome.INVALID_AMOUNT:
        # DECISIONS #4: validators must catch this; if the API surfaces
        # it, the local view of state is out of sync — conservative
        # route to payment_unknown.
        session.terminal = TerminalKind.PAYMENT_UNKNOWN
        return _payment_result(
            success=False,
            stage="api_response",
            error_class="INVALID_AMOUNT_SERVER",
            retries_remaining=session.payment.counter,
            terminal=TerminalKind.PAYMENT_UNKNOWN.value,
            last4=last4,
            message="The processor rejected the amount. Session ended.",
        )

    # UNKNOWN: transient / timeout / transport / undocumented response.
    session.terminal = TerminalKind.PAYMENT_UNKNOWN
    return _payment_result(
        success=False,
        stage="api_call",
        error_class="TRANSIENT_NO_RETRY",
        retries_remaining=session.payment.counter,
        terminal=TerminalKind.PAYMENT_UNKNOWN.value,
        last4=last4,
        message=(
            "I couldn't confirm the outcome of your payment. The session "
            "has ended for safety."
        ),
    )


def cancel_session_handler(
    session: SessionState,
    input_args: dict[str, Any],
    **_unused: Any,
) -> dict[str, Any]:
    """Tool implementation for ``cancel_session``.

    Sets ``session.terminal = TerminalKind.CANCELLED``. No-op if the
    session is already terminal (preserves the original terminal kind).
    """
    reason = input_args.get("reason", "other")
    detail = input_args.get("detail")
    _LOG.info("cancel_session: reason=%s detail=%s", reason, detail)
    if not session.is_terminal():
        session.terminal = TerminalKind.CANCELLED
    return {
        "terminated": True,
        "terminal": (
            session.terminal.value
            if session.terminal is not None
            else TerminalKind.CANCELLED.value
        ),
        "message": (
            "Session cancelled. The agent will no longer process input "
            "for this session."
        ),
    }


def render_canonical_message_handler(
    session: SessionState,
    input_args: dict[str, Any],
    **_unused: Any,
) -> dict[str, Any]:
    """Tool implementation for ``render_canonical_message``.

    Looks up the canonical template by ``kind``, validates required
    slots, formats the message, and (for confirmation_prompt only) sets
    ``session.confirmation_pending`` to bind ``(amount, last4)`` for the
    next ``process_payment`` call.
    """
    kind = input_args.get("kind", "")
    raw_slots = input_args.get("slots")
    slots = raw_slots if isinstance(raw_slots, dict) else {}

    if kind not in _TEMPLATES:
        return {"message": "", "kind": kind, "error": "UNKNOWN_KIND"}

    required_slots, template = _TEMPLATES[kind]
    missing = required_slots - set(slots.keys())
    if missing:
        return {"message": "", "kind": kind, "error": "MISSING_SLOTS"}

    # Coerce numeric slots to float for the template's ',.2f' format spec.
    coerced: dict[str, Any] = {}
    for k, v in slots.items():
        if k in {"balance", "amount", "remaining_balance"}:
            try:
                coerced[k] = float(v)
            except (TypeError, ValueError):
                return {
                    "message": "",
                    "kind": kind,
                    "error": "INVALID_SLOT_TYPE",
                }
        else:
            coerced[k] = v

    try:
        message = template.format(**coerced)
    except (KeyError, ValueError, TypeError):
        return {"message": "", "kind": kind, "error": "INVALID_SLOT_TYPE"}

    if kind == KIND_CONFIRMATION_PROMPT:
        try:
            session.confirmation_pending = ConfirmationPending(
                amount=Decimal(str(coerced["amount"])),
                last4=str(coerced["last4"]),
            )
        except (InvalidOperation, ValueError):
            return {"message": "", "kind": kind, "error": "INVALID_SLOT_TYPE"}

    return {"message": message, "kind": kind, "error": None}


# ===========================================================================
# Registry — looked up by the orchestrator on each tool_use block
# ===========================================================================

TOOL_HANDLERS = {
    TOOL_LOOKUP_ACCOUNT: lookup_account_handler,
    TOOL_SUBMIT_VERIFICATION: submit_verification_handler,
    TOOL_PROCESS_PAYMENT: process_payment_handler,
    TOOL_CANCEL_SESSION: cancel_session_handler,
    TOOL_RENDER_CANONICAL_MESSAGE: render_canonical_message_handler,
}
