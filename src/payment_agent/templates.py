"""Message templates — pure (stage, slots, message_key) → str mapping.

Per DECISIONS #8: spec-required content (verification verdict, balance,
transaction id, recap) is template-driven and assertable as exact strings;
connective tissue varies but is bound by the same templates. The LLM never
generates user-facing prose — that commitment is enforced by this module
being the only place that produces what `agent.next()` returns in `message`.

Per DECISIONS hard rules:
  - DOB, Aadhaar last-4, pincode never exposed in agent messages
  - Full PAN, CVV never exposed (last-4 only, in confirmation + recap)
  - Expiry shown only in the pre-payment confirmation gate
  - Full name never echoed in recap; first-name acknowledgement OK earlier

Per DECISIONS #17: Indian English, polite-professional register, ₹ symbol.

Locale rendering (one-line v1 design choice): DOBs in plain English use
"Month Day, Year" format ("April 5, 1990") — matches the example in
DECISIONS #15 and avoids forcing the user to decode DD-MM/MM-DD jargon.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

from payment_agent import redact
from payment_agent.state import SlotStore


def render(slots: SlotStore, message_key: str) -> str:
    """Render the message for a given (slots, message_key) pair.

    Dispatch is on message_key only — all 45 keys are unique across stages
    by convention, so stage context is unnecessary. The orchestrator (agent.py)
    knows what message_key the transition produced and passes it directly.
    """
    template = _TEMPLATES.get(message_key)
    if template is None:
        raise ValueError(f"unknown message_key: {message_key!r}")
    return template(slots)


# ===========================================================================
# Slot-rendering helpers
# ===========================================================================


def _format_dob_plain(d: date) -> str:
    """Render a date as e.g. 'April 5, 1990'."""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _first_name(full_name: str | None) -> str:
    if not full_name:
        return ""
    return full_name.split(" ")[0]


def _expiry_display(month: int | None, year: int | None) -> str:
    if month is None or year is None:
        return ""
    return f"{month:02d}/{year}"


def _amount_display(amount) -> str:
    if amount is None:
        return ""
    # str(Decimal) preserves the original precision: Decimal("500") → "500",
    # Decimal("500.50") → "500.50", Decimal("1500.5") → "1500.5". Avoids the
    # trailing-zero-strip bug that turned "500" into "5".
    return str(amount)


def _balance_display(amount) -> str:
    """Render a balance amount with thousands separator and 2 decimal places.

    Used for the verification-completion balance share (DECISIONS #28) where
    the spec sample is "₹1,250.75". Different from `_amount_display` so user-
    entered amounts in the confirmation gate stay un-padded ("₹500" matches
    the DECISIONS #5 spec example, not "₹500.00").
    """
    if amount is None:
        return ""
    return f"{amount:,.2f}"


def _last4_or_empty(pan: str | None) -> str:
    if pan is None:
        return ""
    try:
        return redact.last4(pan)
    except ValueError:
        return ""


# ===========================================================================
# Templates
# ===========================================================================


def _t_greet(_s: SlotStore) -> str:
    return (
        "Hello! I'm here to help you make a payment. "
        "Could you share your account ID to get started?"
    )


def _t_prompt_account_id(_s: SlotStore) -> str:
    return (
        "Could you share your account ID? It should look like ACC followed "
        "by four digits (e.g., ACC1001)."
    )


def _t_prompt_account_id_invalid_format(_s: SlotStore) -> str:
    return (
        "That doesn't look like a valid account ID. It should be ACC followed "
        "by exactly four digits (e.g., ACC1001) — could you share it again?"
    )


def _t_lookup_in_progress(s: SlotStore) -> str:
    aid = s.account_id or ""
    return f"Looking up account {aid}..."


def _t_lookup_not_found_retry(_s: SlotStore) -> str:
    return (
        "I couldn't find that account in our records. Could you double-check "
        "and share it again?"
    )


def _t_lookup_transient_retry(_s: SlotStore) -> str:
    return (
        "I'm having trouble reaching our system right now. Could you share "
        "your account ID again?"
    )


def _t_entry_collect_identity(_s: SlotStore) -> str:
    return (
        "I've found your account. To verify your identity, could you share "
        "your full name along with one of: date of birth, last 4 digits of "
        "Aadhaar, or pincode?"
    )


def _t_prompt_identity_malformed(_s: SlotStore) -> str:
    return (
        "I couldn't quite read your last response in the expected format. "
        "Could you share your full name and one secondary factor (date of "
        "birth, Aadhaar last 4, or pincode)?"
    )


def _t_verify_fail_name(_s: SlotStore) -> str:
    return (
        "That name doesn't match what we have on file. You can try the name "
        "again, or share a secondary factor: date of birth, Aadhaar last 4, "
        "or pincode."
    )


def _t_verify_fail_dob(_s: SlotStore) -> str:
    return (
        "That date of birth doesn't match. You can try again, or share a "
        "different secondary factor — Aadhaar last 4 or pincode."
    )


def _t_verify_fail_aadhaar_last4(_s: SlotStore) -> str:
    return (
        "That Aadhaar last 4 doesn't match. You can try again, or share a "
        "different secondary factor — date of birth or pincode."
    )


def _t_verify_fail_pincode(_s: SlotStore) -> str:
    return (
        "That pincode doesn't match. You can try again, or share a "
        "different secondary factor — date of birth or Aadhaar last 4."
    )


def _t_prompt_dob_disambiguation(s: SlotStore) -> str:
    # v2: present both readings explicitly so the user makes a deliberate
    # choice without pre-committing either parse. v1 fallback (single
    # primary reading rendered) preserved for callers that bypass the
    # orchestrator's pre-merge ambiguity handling.
    if s.dob_disamb_primary is not None and s.dob_disamb_alternate is not None:
        primary = _format_dob_plain(s.dob_disamb_primary)
        alternate = _format_dob_plain(s.dob_disamb_alternate)
        return (
            f"That date is ambiguous — could it be {primary} or "
            f"{alternate}? Please reply with the correct date "
            f"(e.g., '{primary}')."
        )
    # v1 fallback: only slots.dob is set (primary merged before disambiguation).
    parsed = _format_dob_plain(s.dob) if s.dob else "the date you shared"
    return (
        f"I read your date of birth as {parsed}. If that's correct, please "
        f"reply 'yes' to proceed. If it's wrong, you can share the "
        f"correct date instead."
    )


def _t_reprompt_dob_disambiguation(s: SlotStore) -> str:
    if s.dob_disamb_primary is not None and s.dob_disamb_alternate is not None:
        primary = _format_dob_plain(s.dob_disamb_primary)
        alternate = _format_dob_plain(s.dob_disamb_alternate)
        return (
            f"I didn't catch which date you meant — was it {primary} or "
            f"{alternate}? Please reply with the correct date."
        )
    parsed = _format_dob_plain(s.dob) if s.dob else "the date you shared"
    return (
        f"I didn't catch a clear answer. I have your date of birth as "
        f"{parsed} — please reply 'yes' to confirm or share the correct date."
    )


def _t_dob_resolved(_s: SlotStore) -> str:
    return "Got it."


def _t_entry_collect_amount(s: SlotStore) -> str:
    # DECISIONS #28: spec flow step 4 mandates sharing the outstanding
    # balance with the verified user. Sample dialog: "Identity verified.
    # Your outstanding balance is ₹1,250.75..."
    name = _first_name(s.full_name)
    greeting = f"Thanks, {name}!" if name else "Thanks!"
    if s.lookup_response is not None:
        balance = _balance_display(s.lookup_response.balance)
        return (
            f"{greeting} Your identity is verified. Your outstanding "
            f"balance is ₹{balance}. How much would you like to pay?"
        )
    # Defensive fallback if lookup_response is missing (shouldn't reach
    # this stage without it, but degrades gracefully rather than crashing).
    return f"{greeting} Your identity is verified. How much would you like to pay (in ₹)?"


def _t_prompt_amount(_s: SlotStore) -> str:
    return "How much would you like to pay (in ₹)?"


def _t_prompt_amount_invalid_format(_s: SlotStore) -> str:
    return (
        "That doesn't look like a valid amount. Please share a positive "
        "amount in ₹ with up to 2 decimal places (e.g., 500 or 1500.50)."
    )


def _t_insufficient_balance_reprompt(_s: SlotStore) -> str:
    # Per DECISIONS #7: do not leak the actual server balance.
    return (
        "Your account doesn't have enough balance for that amount. Could "
        "you share a smaller amount?"
    )


def _t_entry_collect_card(s: SlotStore) -> str:
    amt = _amount_display(s.amount)
    return (
        f"Got it — ₹{amt}. Now I need your card details: card number, CVV, "
        f"and expiry (MM/YY or MM/YYYY)."
    )


def _t_prompt_card(_s: SlotStore) -> str:
    return (
        "Please share your card number, CVV, and expiry date (MM/YY or "
        "MM/YYYY)."
    )


def _t_prompt_card_invalid_pan(_s: SlotStore) -> str:
    return (
        "That card number didn't pass our checks. Could you share the card "
        "number again?"
    )


def _t_prompt_card_invalid_cvv(_s: SlotStore) -> str:
    return (
        "That CVV doesn't look right — it should be 3 or 4 digits. Could "
        "you share it again?"
    )


def _t_prompt_card_invalid_expiry(_s: SlotStore) -> str:
    return (
        "That expiry date doesn't look right (or the card has expired). "
        "Could you share it again as MM/YY or MM/YYYY?"
    )


def _t_reprompt_invalid_card(_s: SlotStore) -> str:
    return (
        "The payment system rejected that card number. Could you share a "
        "different card number?"
    )


def _t_reprompt_invalid_cvv(_s: SlotStore) -> str:
    return "That CVV was rejected by the payment system. Could you share it again?"


def _t_reprompt_invalid_expiry(_s: SlotStore) -> str:
    return (
        "That expiry date was rejected by the payment system. Could you "
        "share it again as MM/YY or MM/YYYY?"
    )


def _t_entry_confirm_payment(s: SlotStore) -> str:
    # DECISIONS #5 exact spec content: amount + last-4 + expiry.
    # Spec example: "I'm about to charge ₹500 to card ending 0366, expiry
    # 12/2027. Reply 'yes' to confirm." Cancellation is globally available
    # per DECISIONS #10 — mentioning it specifically here would create the
    # wrong implication that this is the only point users can cancel.
    amt = _amount_display(s.amount)
    last4 = _last4_or_empty(s.pan)
    expiry = _expiry_display(s.expiry_month, s.expiry_year)
    return (
        f"I'm about to charge ₹{amt} to card ending {last4}, expiry "
        f"{expiry}. Reply 'yes' to confirm."
    )


def _t_reprompt_payment_confirmation(s: SlotStore) -> str:
    amt = _amount_display(s.amount)
    last4 = _last4_or_empty(s.pan)
    expiry = _expiry_display(s.expiry_month, s.expiry_year)
    return (
        f"I didn't catch that. Should I proceed with charging ₹{amt} to "
        f"card ending {last4}, expiry {expiry}? Please reply 'yes' or 'no'."
    )


def _t_payment_in_progress(_s: SlotStore) -> str:
    return "Processing your payment..."


def _t_prompt_forced_disambiguation(_s: SlotStore) -> str:
    # DECISIONS #5: "Do you want to continue or stop?"
    return (
        "I'm not sure if you want to proceed. Do you want to continue with "
        "this payment, or stop?"
    )


def _t_reprompt_forced_disambiguation(_s: SlotStore) -> str:
    return (
        "I still didn't catch that. Please reply with 'continue' to proceed "
        "with the payment, or 'stop' to cancel."
    )


def _t_recap_success(s: SlotStore) -> str:
    # DECISIONS #7 success recap: account ID + amount + transaction id +
    # last-4 + remaining balance. Excludes: DOB, Aadhaar, pincode, full PAN,
    # CVV, expiry, full name.
    amt = _amount_display(s.amount)
    last4 = _last4_or_empty(s.pan)
    # DECISIONS #28: balance contexts use the X,XXX.XX format (thousands
    # separator + fixed 2dp). Raw _amount_display would render Decimal('3100.5')
    # as "3100.5" — visible in the leap_year_acc1004 transcript pre-fix.
    remaining = _balance_display(s.remaining_balance)
    txn = s.transaction_id or ""
    aid = s.account_id or ""
    return (
        f"Payment successful. Account: {aid}. Amount paid: ₹{amt}. "
        f"Transaction ID: {txn}. Card ending: {last4}. "
        f"Remaining balance: ₹{remaining}."
    )


def _t_terminal_completed(_s: SlotStore) -> str:
    # Defensive fallback. Primary success copy is recap_success.
    return "This session has ended. Your payment was completed successfully."


def _t_terminal_verification_exhausted(_s: SlotStore) -> str:
    return (
        "I wasn't able to verify your identity. For your security, this "
        "session has ended. Please contact our support team for assistance."
    )


def _t_terminal_payment_exhausted(_s: SlotStore) -> str:
    return (
        "I wasn't able to process your payment after several attempts. "
        "Please contact our support team for assistance."
    )


def _t_terminal_cancelled(_s: SlotStore) -> str:
    # DECISIONS #10: "no payment processed."
    return "Session cancelled. No payment processed."


def _t_terminal_cancelled_dob_disamb(_s: SlotStore) -> str:
    return (
        "I couldn't make sense of which date you meant. No payment "
        "processed. Please start a new session."
    )


def _t_terminal_cancelled_forced_disamb(_s: SlotStore) -> str:
    return (
        "I couldn't get a clear answer, so I've cancelled this session. "
        "No payment was processed."
    )


def _t_terminal_account_not_found(_s: SlotStore) -> str:
    # DECISIONS #12 exact spec copy: comma after "account", sentence ends at "team."
    return "I'm having trouble finding your account, please contact our support team."


def _t_terminal_payment_unknown(_s: SlotStore) -> str:
    # DECISIONS #13 conservative copy.
    return (
        "I couldn't confirm whether your payment processed. Please "
        "contact our support team before attempting another payment."
    )


def _t_closed_reentry(_s: SlotStore) -> str:
    # DECISIONS #11.
    return "This session has ended. Please start a new session for further requests."


# ===========================================================================
# Dispatch table
# ===========================================================================


_TEMPLATES: dict[str, Callable[[SlotStore], str]] = {
    # Greeting
    "greet": _t_greet,
    "greet_advance": _t_greet,
    # Account ID
    "prompt_account_id": _t_prompt_account_id,
    "prompt_account_id_invalid_format": _t_prompt_account_id_invalid_format,
    "lookup_in_progress": _t_lookup_in_progress,
    "lookup_not_found_retry": _t_lookup_not_found_retry,
    "lookup_transient_retry": _t_lookup_transient_retry,
    # Identity / verification
    "entry_collect_identity": _t_entry_collect_identity,
    "prompt_identity": _t_entry_collect_identity,
    "prompt_identity_malformed": _t_prompt_identity_malformed,
    "verify_fail_name": _t_verify_fail_name,
    "verify_fail_dob": _t_verify_fail_dob,
    "verify_fail_aadhaar_last4": _t_verify_fail_aadhaar_last4,
    "verify_fail_pincode": _t_verify_fail_pincode,
    # DOB disambiguation
    "prompt_dob_disambiguation": _t_prompt_dob_disambiguation,
    "reprompt_dob_disambiguation": _t_reprompt_dob_disambiguation,
    "dob_resolved": _t_dob_resolved,
    # Amount
    "entry_collect_amount": _t_entry_collect_amount,
    "prompt_amount": _t_prompt_amount,
    "prompt_amount_invalid_format": _t_prompt_amount_invalid_format,
    "insufficient_balance_reprompt": _t_insufficient_balance_reprompt,
    # Card
    "entry_collect_card": _t_entry_collect_card,
    "prompt_card": _t_prompt_card,
    "prompt_card_invalid_pan": _t_prompt_card_invalid_pan,
    "prompt_card_invalid_cvv": _t_prompt_card_invalid_cvv,
    "prompt_card_invalid_expiry": _t_prompt_card_invalid_expiry,
    "reprompt_invalid_card": _t_reprompt_invalid_card,
    "reprompt_invalid_cvv": _t_reprompt_invalid_cvv,
    "reprompt_invalid_expiry": _t_reprompt_invalid_expiry,
    # Confirmation
    "entry_confirm_payment": _t_entry_confirm_payment,
    "prompt_payment_confirmation": _t_entry_confirm_payment,
    "reprompt_payment_confirmation": _t_reprompt_payment_confirmation,
    "payment_in_progress": _t_payment_in_progress,
    # Forced disambiguation
    "prompt_forced_disambiguation": _t_prompt_forced_disambiguation,
    "reprompt_forced_disambiguation": _t_reprompt_forced_disambiguation,
    # Recap
    "recap_success": _t_recap_success,
    # Terminal
    "terminal_completed": _t_terminal_completed,
    "terminal_verification_exhausted": _t_terminal_verification_exhausted,
    "terminal_payment_exhausted": _t_terminal_payment_exhausted,
    "terminal_cancelled": _t_terminal_cancelled,
    "terminal_cancelled_dob_disamb": _t_terminal_cancelled_dob_disamb,
    "terminal_cancelled_forced_disamb": _t_terminal_cancelled_forced_disamb,
    "terminal_account_not_found": _t_terminal_account_not_found,
    "terminal_payment_unknown": _t_terminal_payment_unknown,
    # Closed
    "closed_reentry": _t_closed_reentry,
}
