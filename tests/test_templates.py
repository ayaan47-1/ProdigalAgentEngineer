"""Tests for templates.py — pure (stage, slots, message_key) → str mapping.

Per DECISIONS #8: spec-required content (verdict, balance, txn id, recap)
is asserted on exact strings; connective tissue is asserted on properties
(contains last-4, contains amount, excludes DOB).

Per DECISIONS hard rules:
  - DOB, Aadhaar last-4, pincode never exposed in agent messages
  - Full PAN, CVV never exposed
  - Expiry shown only in the pre-payment confirmation gate (per #7 amendment),
    excluded everywhere else
  - Full name not echoed in recap; first-name acknowledgement OK earlier
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payment_agent import templates
from payment_agent.state import LookupResponse, SlotStore


VALID_PAN = "4532015112830366"
LOOKUP_NITHIN = LookupResponse(
    account_id="ACC1001",
    full_name="Nithin Jain",
    dob=date(1990, 5, 14),
    aadhaar_last4="5544",
    pincode="560001",
    balance=Decimal("10000"),
)


def _slots(**overrides) -> SlotStore:
    return SlotStore(**overrides)


# ===========================================================================
# Section 1 — Greeting and account-id stage templates
# ===========================================================================


class TestGreetingTemplates:
    def test_greet_includes_payment_intent_and_account_id_request(self) -> None:
        msg = templates.render(_slots(), "greet")
        assert "payment" in msg.lower()
        assert "account id" in msg.lower()

    def test_greet_advance_renders_same_as_greet(self) -> None:
        # greet_advance is the same first-turn open; the keys are split for
        # state-machine clarity but render identically.
        a = templates.render(_slots(), "greet")
        b = templates.render(_slots(), "greet_advance")
        assert a == b


class TestAccountIdTemplates:
    def test_prompt_account_id(self) -> None:
        msg = templates.render(_slots(), "prompt_account_id")
        assert "account id" in msg.lower()
        assert "ACC" in msg  # mentions the format

    def test_prompt_account_id_invalid_format(self) -> None:
        msg = templates.render(_slots(),
            "prompt_account_id_invalid_format",
        )
        assert "ACC" in msg
        assert "four digits" in msg.lower() or "4 digits" in msg.lower()

    def test_lookup_in_progress_includes_account_id(self) -> None:
        msg = templates.render(_slots(account_id="ACC1001"),
            "lookup_in_progress",
        )
        assert "ACC1001" in msg

    def test_lookup_not_found_retry(self) -> None:
        msg = templates.render(_slots(), "lookup_not_found_retry"
        )
        assert "couldn't find" in msg.lower() or "could not find" in msg.lower()

    def test_lookup_transient_retry_says_trouble_reaching(self) -> None:
        msg = templates.render(_slots(), "lookup_transient_retry"
        )
        assert "trouble" in msg.lower() or "having trouble" in msg.lower()


# ===========================================================================
# Section 2 — Identity / verification templates
# ===========================================================================


class TestIdentityTemplates:
    def test_entry_collect_identity_lists_all_three_secondary_factors(self) -> None:
        msg = templates.render(_slots(lookup_response=LOOKUP_NITHIN),
            "entry_collect_identity",
        )
        # All three secondary factors mentioned per DECISIONS #2 prompt shape.
        assert "name" in msg.lower()
        assert "date of birth" in msg.lower() or "dob" in msg.lower()
        assert "aadhaar" in msg.lower()
        assert "pincode" in msg.lower()

    def test_prompt_identity_same_as_entry(self) -> None:
        a = templates.render(_slots(lookup_response=LOOKUP_NITHIN),
            "entry_collect_identity",
        )
        b = templates.render(_slots(lookup_response=LOOKUP_NITHIN),
            "prompt_identity",
        )
        assert a == b

    def test_prompt_identity_malformed_redirects_to_correct_format(self) -> None:
        msg = templates.render(_slots(lookup_response=LOOKUP_NITHIN),
            "prompt_identity_malformed",
        )
        # Generic re-prompt that doesn't blame the user.
        assert "format" in msg.lower() or "secondary factor" in msg.lower()


class TestVerifyFailTemplates:
    def test_verify_fail_name_offers_remaining_factors(self) -> None:
        msg = templates.render(_slots(lookup_response=LOOKUP_NITHIN),
            "verify_fail_name",
        )
        # Names which field failed.
        assert "name" in msg.lower()
        # Per DECISIONS #2: offers the remaining secondary-factor options.
        # All three secondary factors should be mentioned for name failure.
        assert "aadhaar" in msg.lower()
        assert "pincode" in msg.lower()
        assert "date of birth" in msg.lower() or "dob" in msg.lower()

    def test_verify_fail_dob_offers_remaining_factors(self) -> None:
        msg = templates.render(_slots(lookup_response=LOOKUP_NITHIN),
            "verify_fail_dob",
        )
        # Names which field failed.
        assert "date of birth" in msg.lower() or "dob" in msg.lower()
        # Remaining factors offered (Aadhaar, pincode).
        assert "aadhaar" in msg.lower()
        assert "pincode" in msg.lower()

    def test_verify_fail_aadhaar_offers_remaining_factors(self) -> None:
        msg = templates.render(_slots(lookup_response=LOOKUP_NITHIN),
            "verify_fail_aadhaar_last4",
        )
        assert "aadhaar" in msg.lower()
        assert "date of birth" in msg.lower() or "dob" in msg.lower()
        assert "pincode" in msg.lower()

    def test_verify_fail_pincode_offers_remaining_factors(self) -> None:
        msg = templates.render(_slots(lookup_response=LOOKUP_NITHIN),
            "verify_fail_pincode",
        )
        assert "pincode" in msg.lower()
        assert "date of birth" in msg.lower() or "dob" in msg.lower()
        assert "aadhaar" in msg.lower()


# ===========================================================================
# Section 3 — DOB disambiguation templates (v1 plain-English render)
# ===========================================================================


class TestDobDisambiguationTemplates:
    def test_prompt_dob_disambiguation_renders_dob_in_plain_english(self) -> None:
        # v1 fallback render: when only slots.dob is set (orchestrator
        # didn't pre-populate disamb slots), prompt renders the primary
        # parsed DOB in plain English.
        slots = _slots(dob=date(1990, 4, 5), lookup_response=LOOKUP_NITHIN)
        msg = templates.render(slots, "prompt_dob_disambiguation"
        )
        # Plain English rendering: "April 5, 1990" not "5-4-1990" or "DD-MM"
        assert "April 5, 1990" in msg
        # Recovery option: share the correct date. Cancel is handled by the
        # orchestrator's NEGATE intent guard (cancel/no/stop), not led with
        # in the prompt copy — softer UX per the v2 disambiguation pass.
        assert "correct date" in msg.lower()

    def test_prompt_dob_disambiguation_v2_renders_both_options(self) -> None:
        # v2: when orchestrator pre-populated dob_disamb_primary +
        # dob_disamb_alternate, the prompt presents BOTH readings as an
        # explicit choice rather than asking the user to confirm a single
        # pre-committed parse.
        slots = _slots(
            dob_disamb_primary=date(1990, 4, 5),
            dob_disamb_alternate=date(1990, 5, 4),
            lookup_response=LOOKUP_NITHIN,
        )
        msg = templates.render(slots, "prompt_dob_disambiguation")
        # Both readings rendered in plain English
        assert "April 5, 1990" in msg
        assert "May 4, 1990" in msg
        # No format jargon
        assert "DD-MM" not in msg
        assert "MM-DD" not in msg

    def test_prompt_dob_disambiguation_avoids_format_jargon(self) -> None:
        slots = _slots(dob=date(1990, 4, 5), lookup_response=LOOKUP_NITHIN)
        msg = templates.render(slots, "prompt_dob_disambiguation"
        )
        # Doesn't ask user to decode "DD-MM vs MM-DD" format.
        assert "DD-MM" not in msg
        assert "MM-DD" not in msg

    def test_reprompt_dob_disambiguation_distinct_from_entry(self) -> None:
        slots = _slots(dob=date(1990, 4, 5), lookup_response=LOOKUP_NITHIN)
        first = templates.render(slots, "prompt_dob_disambiguation"
        )
        retry = templates.render(slots, "reprompt_dob_disambiguation"
        )
        # The keys are split per Step-4 MEDIUM #3 — they should produce
        # distinguishable messages so a re-prompted user knows we didn't
        # understand them.
        assert first != retry

    def test_dob_resolved_is_acknowledgement(self) -> None:
        msg = templates.render(_slots(), "dob_resolved")
        # Brief acknowledgement; specific copy is not asserted.
        assert len(msg) > 0


# ===========================================================================
# Section 4 — Amount / card collection templates
# ===========================================================================


class TestAmountTemplates:
    def test_entry_collect_amount_uses_first_name_acknowledgement(self) -> None:
        # Per DECISIONS #17: "Thanks, Nithin" first-name acknowledgement
        # post-verification.
        slots = _slots(
            full_name="Nithin Jain",
            verified=True,
            lookup_response=LOOKUP_NITHIN,
        )
        msg = templates.render(slots, "entry_collect_amount")
        assert "Nithin" in msg
        # Full name NOT echoed in recap, but in mid-flow ack the first name is OK.
        assert "Nithin Jain" not in msg  # first name only, not full name
        assert "₹" in msg or "amount" in msg.lower() or "rupees" in msg.lower()

    def test_entry_collect_amount_shares_balance(self) -> None:
        # DECISIONS #28 (spec flow step 4): share outstanding balance with
        # verified user. Format: ₹ + thousands separator + 2 decimal places
        # to match the spec sample "₹1,250.75".
        slots = _slots(
            full_name="Nithin Jain",
            verified=True,
            lookup_response=LookupResponse(
                account_id="ACC1001",
                full_name="Nithin Jain",
                dob=date(1990, 5, 14),
                aadhaar_last4="4321",
                pincode="400001",
                balance=Decimal("1250.75"),
            ),
        )
        msg = templates.render(slots, "entry_collect_amount")
        assert "₹1,250.75" in msg
        assert "outstanding balance" in msg.lower()

    def test_entry_collect_amount_balance_pads_to_two_decimals(self) -> None:
        # Decimal("10000") with no decimals → render as "10,000.00".
        slots = _slots(
            full_name="Nithin Jain",
            verified=True,
            lookup_response=LOOKUP_NITHIN,  # balance=10000
        )
        msg = templates.render(slots, "entry_collect_amount")
        assert "₹10,000.00" in msg

    def test_entry_collect_amount_falls_back_without_lookup_response(self) -> None:
        # Defensive: shouldn't reach this stage without lookup_response,
        # but render a sane message rather than crash.
        slots = _slots(full_name="Nithin Jain", verified=True)
        msg = templates.render(slots, "entry_collect_amount")
        assert "Nithin" in msg
        assert "verified" in msg.lower()
        # Doesn't render a bogus balance.
        assert "₹0" not in msg
        assert "balance" not in msg.lower()

    def test_prompt_amount_uses_rupee_symbol(self) -> None:
        msg = templates.render(_slots(), "prompt_amount")
        assert "₹" in msg

    def test_prompt_amount_invalid_format_explains_constraints(self) -> None:
        msg = templates.render(_slots(), "prompt_amount_invalid_format"
        )
        # Guidance on what a valid amount looks like.
        assert "positive" in msg.lower() or "greater than" in msg.lower()
        assert "decimal" in msg.lower() or "₹" in msg

    def test_insufficient_balance_reprompt_does_not_expose_balance(self) -> None:
        # Per DECISIONS #7: only success recap shows balance; an
        # insufficient_balance prompt should not leak the actual balance value.
        slots = _slots(
            amount=Decimal("50000"),
            lookup_response=LOOKUP_NITHIN,  # balance 10000
        )
        msg = templates.render(slots, "insufficient_balance_reprompt"
        )
        assert "10000" not in msg  # actual balance not leaked
        assert "smaller" in msg.lower() or "less" in msg.lower() or "insufficient" in msg.lower()


class TestCardTemplates:
    def test_entry_collect_card_includes_amount(self) -> None:
        slots = _slots(amount=Decimal("500"))
        msg = templates.render(slots, "entry_collect_card")
        assert "500" in msg
        assert "₹" in msg

    def test_prompt_card_lists_required_fields(self) -> None:
        msg = templates.render(_slots(), "prompt_card")
        assert "card number" in msg.lower()
        assert "cvv" in msg.lower()
        assert "expiry" in msg.lower() or "expir" in msg.lower()

    def test_prompt_card_invalid_pan_names_field(self) -> None:
        msg = templates.render(_slots(), "prompt_card_invalid_pan"
        )
        assert "card number" in msg.lower()

    def test_prompt_card_invalid_cvv_names_field(self) -> None:
        msg = templates.render(_slots(), "prompt_card_invalid_cvv"
        )
        assert "cvv" in msg.lower()

    def test_prompt_card_invalid_expiry_names_field(self) -> None:
        msg = templates.render(_slots(), "prompt_card_invalid_expiry"
        )
        assert "expiry" in msg.lower() or "expir" in msg.lower()

    # --- reprompt_invalid_X: API rejected the field, not a format issue ---
    # Per DECISIONS #4 typo-class handling, the user needs to know which
    # field the payment system rejected. Field-naming is the rubric-graded
    # behavior; assert it explicitly.

    def test_reprompt_invalid_card_names_card_number(self) -> None:
        msg = templates.render(_slots(), "reprompt_invalid_card")
        assert "card number" in msg.lower()

    def test_reprompt_invalid_cvv_names_cvv(self) -> None:
        msg = templates.render(_slots(), "reprompt_invalid_cvv")
        assert "cvv" in msg.lower()

    def test_reprompt_invalid_expiry_names_expiry(self) -> None:
        msg = templates.render(_slots(), "reprompt_invalid_expiry")
        assert "expiry" in msg.lower() or "expir" in msg.lower()


# ===========================================================================
# Section 5 — Confirmation gate (per DECISIONS #5 — exact spec content)
# ===========================================================================


class TestConfirmationTemplates:
    def _full_card_slots(self) -> SlotStore:
        return _slots(
            amount=Decimal("500"),
            pan=VALID_PAN,
            cvv="123",
            expiry_month=12,
            expiry_year=2027,
        )

    def test_entry_confirm_payment_matches_decisions_5_spec_exactly(self) -> None:
        # DECISIONS #5 spec example, verbatim: "I'm about to charge ₹500 to
        # card ending 0366, expiry 12/2027. Reply 'yes' to confirm."
        msg = templates.render(self._full_card_slots(), "entry_confirm_payment")
        assert msg == (
            "I'm about to charge ₹500 to card ending 0366, expiry "
            "12/2027. Reply 'yes' to confirm."
        )

    def test_entry_confirm_payment_excludes_full_pan(self) -> None:
        msg = templates.render(self._full_card_slots(),
            "entry_confirm_payment",
        )
        assert VALID_PAN not in msg  # full PAN never exposed

    def test_entry_confirm_payment_excludes_cvv(self) -> None:
        msg = templates.render(self._full_card_slots(),
            "entry_confirm_payment",
        )
        assert "123" not in msg  # CVV never exposed

    def test_prompt_payment_confirmation_same_as_entry(self) -> None:
        # The "entry" and "prompt" keys differ for state-machine clarity but
        # render the same confirmation text.
        slots = self._full_card_slots()
        a = templates.render(slots, "entry_confirm_payment"
        )
        b = templates.render(slots, "prompt_payment_confirmation"
        )
        assert a == b

    def test_reprompt_payment_confirmation_distinct(self) -> None:
        slots = self._full_card_slots()
        first = templates.render(slots, "entry_confirm_payment"
        )
        retry = templates.render(slots, "reprompt_payment_confirmation"
        )
        assert first != retry
        # Retry still contains the financial details so user can re-evaluate.
        assert "0366" in retry
        assert "500" in retry

    def test_payment_in_progress(self) -> None:
        msg = templates.render(_slots(), "payment_in_progress"
        )
        assert "processing" in msg.lower() or "process" in msg.lower()


# ===========================================================================
# Section 6 — Forced disambiguation
# ===========================================================================


class TestForcedDisambiguationTemplates:
    def test_prompt_forced_disambiguation_explicit_yes_no(self) -> None:
        # DECISIONS #5: "Do you want to continue or stop?"
        msg = templates.render(_slots(), "prompt_forced_disambiguation"
        )
        assert "continue" in msg.lower() or "proceed" in msg.lower()
        assert "stop" in msg.lower() or "cancel" in msg.lower()

    def test_reprompt_forced_disambiguation_distinct(self) -> None:
        a = templates.render(_slots(), "prompt_forced_disambiguation"
        )
        b = templates.render(_slots(), "reprompt_forced_disambiguation"
        )
        assert a != b


# ===========================================================================
# Section 7 — Recap (DECISIONS #7 — full success recap content)
# ===========================================================================


class TestRecapTemplate:
    def _success_slots(self) -> SlotStore:
        return _slots(
            account_id="ACC1001",
            full_name="Nithin Jain",
            amount=Decimal("500"),
            pan=VALID_PAN,
            cvv="123",
            expiry_month=12,
            expiry_year=2027,
            verified=True,
            lookup_response=LOOKUP_NITHIN,
            transaction_id="TXN98765",
            remaining_balance=Decimal("9500"),
        )

    def test_recap_success_includes_all_required_fields(self) -> None:
        # DECISIONS #7 success recap: account ID + amount paid + transaction
        # ID + last-4 of card + remaining balance.
        msg = templates.render(self._success_slots(), "recap_success"
        )
        assert "ACC1001" in msg
        assert "500" in msg  # amount
        assert "TXN98765" in msg  # transaction id
        assert "0366" in msg  # last-4
        # DECISIONS #28: balance contexts use X,XXX.XX (thousands sep + 2dp).
        assert "9,500.00" in msg  # remaining balance
        assert "₹" in msg

    def test_recap_success_excludes_dob_aadhaar_pincode(self) -> None:
        msg = templates.render(self._success_slots(), "recap_success"
        )
        assert "1990" not in msg  # DOB year
        assert "5544" not in msg  # Aadhaar last-4 (LOOKUP_NITHIN)
        assert "560001" not in msg  # pincode (LOOKUP_NITHIN)

    def test_recap_success_excludes_full_pan_and_cvv(self) -> None:
        msg = templates.render(self._success_slots(), "recap_success"
        )
        assert VALID_PAN not in msg  # full PAN
        assert "123" not in msg  # CVV (also: "123" is short enough to false-positive
                                  # if amount or other field happens to contain it; the
                                  # _success_slots amount is 500, so this is safe.)

    def test_recap_success_excludes_expiry(self) -> None:
        # Per DECISIONS #7 amendment: "Expiry is shown once in the pre-payment
        # confirmation; excluded from the recap thereafter."
        msg = templates.render(self._success_slots(), "recap_success"
        )
        assert "12/2027" not in msg
        assert "12/27" not in msg

    def test_recap_success_excludes_full_name(self) -> None:
        # DECISIONS #7: "Full name not echoed in recap."
        msg = templates.render(self._success_slots(), "recap_success"
        )
        assert "Nithin Jain" not in msg

    def test_recap_success_remaining_balance_uses_thousands_separator_and_2dp(
        self,
    ) -> None:
        # DECISIONS #28: balance contexts (verification-completion + recap
        # remaining balance) MUST render as ₹X,XXX.XX. Caught live in the
        # leap_year_acc1004 captured transcript pre-fix: Decimal("3100.5")
        # rendered as "3100.5" (raw str(Decimal)) instead of "3,100.50".
        slots = _slots(
            account_id="ACC1001", full_name="Nithin Jain",
            amount=Decimal("100"), pan=VALID_PAN, cvv="123",
            expiry_month=12, expiry_year=2027, verified=True,
            lookup_response=LOOKUP_NITHIN,
            transaction_id="TXN_TEST",
            remaining_balance=Decimal("3100.5"),
        )
        msg = templates.render(slots, "recap_success")
        assert "3,100.50" in msg, (
            f"recap balance must render as 3,100.50 per DECISIONS #28; "
            f"got: {msg!r}"
        )
        # Sanity: not the raw str(Decimal) form.
        assert "3100.5 " not in msg and "3100.5." not in msg


# ===========================================================================
# Section 8 — Terminal stage templates
# ===========================================================================


class TestTerminalTemplates:
    def test_terminal_completed_acknowledges_success(self) -> None:
        # Defensive fallback if orchestrator calls step_on_entry on the
        # terminal stage; primary path uses recap_success. Even as a
        # fallback, the message must specifically signal SUCCESS — not
        # just "session ended" — so a closed/terminated state isn't
        # mistaken for a completed payment.
        msg = templates.render(_slots(), "terminal_completed")
        assert "successful" in msg.lower() or "successfully" in msg.lower()

    def test_terminal_verification_exhausted(self) -> None:
        msg = templates.render(_slots(),
            "terminal_verification_exhausted",
        )
        assert "verify" in msg.lower() or "verification" in msg.lower()
        # Soft escalation per DECISIONS #3: contact support.
        assert "support" in msg.lower() or "contact" in msg.lower()

    def test_terminal_payment_exhausted(self) -> None:
        msg = templates.render(_slots(), "terminal_payment_exhausted"
        )
        assert "payment" in msg.lower()

    def test_terminal_account_not_found_uses_decisions_12_copy(self) -> None:
        # DECISIONS #12 prescribes the exact terminal copy. Spec strings
        # are contracts — assert exact-string equality.
        msg = templates.render(_slots(), "terminal_account_not_found")
        assert msg == (
            "I'm having trouble finding your account, please "
            "contact our support team."
        )

    def test_terminal_payment_unknown_uses_decisions_13_copy(self) -> None:
        # DECISIONS #13 conservative copy: don't claim payment did or didn't
        # process; tell user to contact support before attempting another.
        msg = templates.render(_slots(), "terminal_payment_unknown"
        )
        assert "couldn't confirm" in msg.lower() or "could not confirm" in msg.lower()
        assert "support" in msg.lower() or "contact" in msg.lower()
        # Conservative: no transaction ID communicated.

    def test_terminal_cancelled_says_no_payment_processed(self) -> None:
        # DECISIONS #10: "no payment processed."
        # Tightened from 3-way OR to require BOTH the cancellation
        # acknowledgement AND the no-payment guarantee — drift in either
        # direction would land here as a test failure.
        msg = templates.render(_slots(), "terminal_cancelled")
        assert "cancelled" in msg.lower()
        assert "no payment" in msg.lower()

    def test_terminal_cancelled_dob_disamb(self) -> None:
        msg = templates.render(_slots(),
            "terminal_cancelled_dob_disamb",
        )
        assert "date" in msg.lower() or "dob" in msg.lower()

    def test_terminal_cancelled_forced_disamb(self) -> None:
        msg = templates.render(_slots(),
            "terminal_cancelled_forced_disamb",
        )
        assert "cancel" in msg.lower() or "no payment" in msg.lower()


class TestClosedReentryTemplate:
    def test_closed_reentry_generic(self) -> None:
        # DECISIONS #11 generic copy: "This session has ended. Please start
        # a new session for further requests."
        msg = templates.render(_slots(), "closed_reentry")
        assert "session" in msg.lower()
        assert "ended" in msg.lower() or "new session" in msg.lower()


# ===========================================================================
# Section 9 — render() error handling
# ===========================================================================


class TestRenderErrorHandling:
    def test_unknown_message_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown message_key"):
            templates.render(_slots(), "nonexistent_key")


# ===========================================================================
# Section 10 — Property tests: sensitive data exclusion
# ===========================================================================


class TestSensitiveDataExclusion:
    """Property test: across all message keys with full slots filled, no
    sensitive value ever appears in the rendered text (per DECISIONS hard rules).
    """

    def _populated_slots(self) -> SlotStore:
        return _slots(
            account_id="ACC1001",
            full_name="Nithin Jain",
            dob=date(1988, 2, 29),  # leap-year edge so "1988" / "29" are detectable
            aadhaar_last4="9876",
            pincode="751002",
            amount=Decimal("500"),
            pan=VALID_PAN,
            cvv="987",
            expiry_month=12,
            expiry_year=2027,
            verified=True,
            lookup_response=LookupResponse(
                account_id="ACC1001",
                full_name="Nithin Jain",
                dob=date(1988, 2, 29),
                aadhaar_last4="9876",
                pincode="751002",
                balance=Decimal("10000"),
            ),
            transaction_id="TXN98765",
            remaining_balance=Decimal("9500"),
        )

    @pytest.mark.parametrize("key", [
        # Every key EXCEPT the confirmation gate (which legitimately shows
        # last-4 and expiry per DECISIONS #5/#7), the recap (last-4 OK), and
        # the dob-disambiguation prompts (per the v1 adjustment to design
        # call #2: render user-supplied DOB in plain English at that one gate).
        "greet", "greet_advance",
        "prompt_account_id", "prompt_account_id_invalid_format",
        "lookup_in_progress", "lookup_not_found_retry", "lookup_transient_retry",
        "entry_collect_identity", "prompt_identity", "prompt_identity_malformed",
        "verify_fail_name", "verify_fail_dob",
        "verify_fail_aadhaar_last4", "verify_fail_pincode",
        "dob_resolved",  # acknowledgement only, no dob render
        "entry_collect_amount", "prompt_amount",
        "prompt_amount_invalid_format", "insufficient_balance_reprompt",
        "entry_collect_card", "prompt_card",
        "prompt_card_invalid_pan", "prompt_card_invalid_cvv",
        "prompt_card_invalid_expiry",
        "reprompt_invalid_card", "reprompt_invalid_cvv", "reprompt_invalid_expiry",
        "prompt_forced_disambiguation", "reprompt_forced_disambiguation",
        "payment_in_progress",
        "terminal_completed", "terminal_verification_exhausted",
        "terminal_payment_exhausted", "terminal_cancelled",
        "terminal_account_not_found", "terminal_payment_unknown",
        "terminal_cancelled_dob_disamb", "terminal_cancelled_forced_disamb",
        "closed_reentry",
    ])
    def test_no_sensitive_value_appears(self, key: str) -> None:
        slots = self._populated_slots()
        # Stage doesn't matter for render dispatch; pick any.
        msg = templates.render(slots, key)
        # CVV
        assert "987" not in msg, f"{key}: CVV leaked"
        # Aadhaar last-4
        assert "9876" not in msg, f"{key}: Aadhaar leaked"
        # Pincode
        assert "751002" not in msg, f"{key}: pincode leaked"
        # DOB year
        assert "1988" not in msg, f"{key}: DOB year leaked"
        # Full PAN
        assert VALID_PAN not in msg, f"{key}: full PAN leaked"

    def test_dob_disambiguation_prompts_render_dob_intentionally(self) -> None:
        # The v1 adjustment to design call #2 explicitly renders the
        # user-supplied DOB in plain English at the disambiguation gate.
        # This is the single deliberate exception to "DOB never in messages."
        # The data shown is what the user TYPED, not what's on file — the
        # spec rule protects account-data leakage, not user-supplied echo.
        slots = self._populated_slots()
        first = templates.render(slots, "prompt_dob_disambiguation")
        retry = templates.render(slots, "reprompt_dob_disambiguation")
        # DOB year IS present (intentional).
        assert "1988" in first
        assert "1988" in retry
        # But other sensitive data is NOT.
        assert "987" not in first  # CVV
        assert "9876" not in first  # Aadhaar
        assert "751002" not in first  # pincode
        assert VALID_PAN not in first  # full PAN

    def test_confirmation_gate_does_show_last4_and_expiry(self) -> None:
        # Per DECISIONS #5: confirmation gate is the ONE place where last-4
        # and expiry are legitimately shown.
        msg = templates.render(self._populated_slots(),
            "entry_confirm_payment",
        )
        assert "0366" in msg  # last-4 OK
        assert "12/2027" in msg  # expiry OK
        # But still: full PAN, CVV, DOB, etc. excluded.
        assert VALID_PAN not in msg
        assert "987" not in msg

    def test_recap_does_show_last4_amount_txn_balance(self) -> None:
        msg = templates.render(self._populated_slots(), "recap_success"
        )
        assert "0366" in msg  # last-4 OK in recap
        assert "TXN98765" in msg
        assert "9,500.00" in msg  # remaining balance OK (DECISIONS #28 X,XXX.XX format)
        # But not expiry (DECISIONS #7 amendment).
        assert "12/2027" not in msg
