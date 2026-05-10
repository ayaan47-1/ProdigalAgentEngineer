"""Tests for state.py — the deterministic kernel.

Coverage focus per PLAN §4:
  - Every retry-cap edge (1st re-prompt, exhaustion edge, post-cap)
  - Every cancel-at-stage entry
  - Every confirm-don't-re-ask path (filled-on-entry vs empty-on-entry)
  - Out-of-order slot fills
  - Slot-mutability rejection (user-stated mid-flow correction)
  - All four orchestrator-controlled carve-outs

Cap semantics across all counters (DECISIONS #22):
    cap N = N events allowed (each re-prompted), action on the (N+1)th event.
    counter < N → re-prompt; counter == N → terminate.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from payment_agent import config, state
from payment_agent.state import (
    CANCELLABLE_STAGES,
    TERMINAL_STAGES,
    Counters,
    ExtractedSlots,
    ExtractionResult,
    Intent,
    LookupEffectResult,
    LookupOutcome,
    LookupResponse,
    PaymentEffectResult,
    PaymentOutcome,
    SideEffect,
    SlotStore,
    Stage,
    StateMachineError,
)
from payment_agent.verify import SecondaryFactor


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

VALID_PAN = "4532015112830366"  # Luhn-valid Visa test PAN
VALID_PAN_LAST4 = "0366"
LOOKUP_NITHIN = LookupResponse(
    account_id="ACC1001",
    full_name="Nithin Jain",
    dob=date(1990, 5, 14),
    aadhaar_last4="5544",
    pincode="560001",
    balance=Decimal("10000"),
)


def make_slots(**overrides: Any) -> SlotStore:
    return SlotStore(**overrides)


def make_counters(**overrides: Any) -> Counters:
    return Counters(**overrides)


def make_extraction(intent: Intent = Intent.AMBIGUOUS, **slot_overrides: Any) -> ExtractionResult:
    return ExtractionResult(
        intent=intent,
        slots=ExtractedSlots(**slot_overrides),
    )


# ===========================================================================
# Section 1 — Basic data structures
# ===========================================================================


class TestStageEnum:
    def test_sixteen_members(self) -> None:
        assert len(list(Stage)) == 16

    def test_six_terminal_stages(self) -> None:
        assert len(TERMINAL_STAGES) == 6

    def test_eight_cancellable_stages(self) -> None:
        assert len(CANCELLABLE_STAGES) == 8

    def test_recap_not_cancellable(self) -> None:
        # Per PLAN §3: recap and terminals don't accept cancellation.
        assert Stage.RECAP_COMPLETED not in CANCELLABLE_STAGES

    def test_terminals_not_cancellable(self) -> None:
        for term in TERMINAL_STAGES:
            assert term not in CANCELLABLE_STAGES


class TestSlotStoreDefaults:
    def test_all_slots_default_none(self) -> None:
        s = SlotStore()
        for field in (
            "account_id", "full_name", "dob", "aadhaar_last4", "pincode",
            "selected_secondary_factor", "amount", "pan", "cvv",
            "expiry_month", "expiry_year", "name_on_card",
            "lookup_response", "transaction_id", "remaining_balance",
            "terminal_cause",
        ):
            assert getattr(s, field) is None, f"{field} should default None"

    def test_verified_defaults_false(self) -> None:
        assert SlotStore().verified is False

    def test_extra_fields_rejected(self) -> None:
        # Pydantic strict mode catches typos at construction time.
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SlotStore(account_id_typo="ACC1001")  # type: ignore[call-arg]


class TestCountersDefaults:
    def test_all_counters_default_zero(self) -> None:
        c = Counters()
        for field in (
            "verification_retries", "payment_retries", "lookup_retries",
            "confirmation_ambig", "dob_disamb_retries", "forced_disamb_retries",
        ):
            assert getattr(c, field) == 0


# ===========================================================================
# Section 2 — merge_extracted_slots (lock-once-filled rule)
# ===========================================================================


class TestMergeExtractedSlots:
    def test_fills_empty_slot(self) -> None:
        slots = make_slots()
        merged = state.merge_extracted_slots(
            slots=slots,
            extracted=ExtractedSlots(account_id="ACC1001"),
        )
        assert merged.account_id == "ACC1001"

    def test_does_not_overwrite_filled_slot(self) -> None:
        # Slot mutability: user-stated mid-flow correction is rejected.
        slots = make_slots(account_id="ACC1001")
        merged = state.merge_extracted_slots(
            slots=slots,
            extracted=ExtractedSlots(account_id="ACC9999"),
        )
        assert merged.account_id == "ACC1001"

    def test_returns_same_instance_when_no_changes(self) -> None:
        slots = make_slots(account_id="ACC1001")
        merged = state.merge_extracted_slots(
            slots=slots, extracted=ExtractedSlots()
        )
        assert merged is slots

    def test_skips_dob_alternate_field(self) -> None:
        # dob_alternate is a signal field, not persisted.
        slots = make_slots()
        merged = state.merge_extracted_slots(
            slots=slots,
            extracted=ExtractedSlots(dob_alternate=date(1990, 4, 5)),
        )
        # dob_alternate is NOT a field on SlotStore, so we just verify no
        # error and dob remains unset.
        assert merged.dob is None

    def test_fills_multiple_slots_at_once(self) -> None:
        slots = make_slots()
        merged = state.merge_extracted_slots(
            slots=slots,
            extracted=ExtractedSlots(
                account_id="ACC1001",
                full_name="Nithin Jain",
                dob=date(1990, 5, 14),
            ),
        )
        assert merged.account_id == "ACC1001"
        assert merged.full_name == "Nithin Jain"
        assert merged.dob == date(1990, 5, 14)

    def test_partial_fill_preserves_existing(self) -> None:
        slots = make_slots(account_id="ACC1001", full_name="Nithin Jain")
        merged = state.merge_extracted_slots(
            slots=slots,
            extracted=ExtractedSlots(account_id="ACC9999", dob=date(1990, 5, 14)),
        )
        assert merged.account_id == "ACC1001"  # locked
        assert merged.full_name == "Nithin Jain"  # untouched
        assert merged.dob == date(1990, 5, 14)  # filled


# ===========================================================================
# Section 3 — cancel_guard
# ===========================================================================


class TestCancelGuard:
    @pytest.mark.parametrize("stage", sorted(CANCELLABLE_STAGES))
    def test_negate_on_cancellable_routes_to_terminal_cancelled(self, stage: Stage) -> None:
        slots = make_slots(account_id="ACC1001")
        counters = make_counters(verification_retries=2)
        result = state.cancel_guard(stage, Intent.NEGATE, slots, counters)
        assert result is not None
        assert result.next_stage == Stage.TERMINAL_CANCELLED
        assert result.slots.terminal_cause == f"cancelled_at_{stage.value}"
        # Cancellation does NOT consume any retry budget (DECISIONS #10).
        assert result.counters.verification_retries == 2

    def test_affirm_on_cancellable_returns_none(self) -> None:
        result = state.cancel_guard(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            Intent.AFFIRM,
            make_slots(),
            make_counters(),
        )
        assert result is None

    def test_ambiguous_on_cancellable_returns_none(self) -> None:
        result = state.cancel_guard(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            Intent.AMBIGUOUS,
            make_slots(),
            make_counters(),
        )
        assert result is None

    @pytest.mark.parametrize("stage", sorted(TERMINAL_STAGES))
    def test_negate_on_terminal_returns_none(self, stage: Stage) -> None:
        result = state.cancel_guard(stage, Intent.NEGATE, make_slots(), make_counters())
        assert result is None

    def test_negate_on_closed_returns_none(self) -> None:
        result = state.cancel_guard(
            Stage.CLOSED, Intent.NEGATE, make_slots(), make_counters()
        )
        assert result is None

    def test_negate_on_recap_completed_returns_none(self) -> None:
        result = state.cancel_guard(
            Stage.RECAP_COMPLETED, Intent.NEGATE, make_slots(), make_counters()
        )
        assert result is None


# ===========================================================================
# Section 4 — step_on_entry (auto-advance + initial prompts)
# ===========================================================================


class TestStepOnEntryGreeting:
    def test_emits_greet_message(self) -> None:
        result = state.step_on_entry(Stage.GREETING, make_slots(), make_counters())
        assert result.next_stage == Stage.GREETING
        assert result.message_key == "greet"
        assert result.side_effect == SideEffect.NONE


class TestStepOnEntryCollectingAccountId:
    def test_empty_slot_prompts(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_ACCOUNT_ID, make_slots(), make_counters()
        )
        assert result.next_stage == Stage.COLLECTING_ACCOUNT_ID
        assert result.side_effect == SideEffect.NONE
        assert result.message_key == "prompt_account_id"

    def test_filled_slot_fires_lookup(self) -> None:
        # confirm-don't-re-ask: account_id was volunteered upstream.
        result = state.step_on_entry(
            Stage.COLLECTING_ACCOUNT_ID,
            make_slots(account_id="ACC1001"),
            make_counters(),
        )
        assert result.next_stage == Stage.COLLECTING_ACCOUNT_ID
        assert result.side_effect == SideEffect.CALL_LOOKUP
        assert result.message_key == "lookup_in_progress"


class TestStepOnEntryCollectingIdentity:
    def test_empty_prompts(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY,
            make_slots(lookup_response=LOOKUP_NITHIN),
            make_counters(),
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.message_key == "prompt_identity"

    def test_partial_fill_prompts(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY,
            make_slots(full_name="Nithin Jain", lookup_response=LOOKUP_NITHIN),
            make_counters(),
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.message_key == "prompt_identity"

    def test_complete_and_correct_advances_to_amount(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            dob=date(1990, 5, 14),
            selected_secondary_factor=SecondaryFactor.DOB,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters()
        )
        assert result.next_stage == Stage.COLLECTING_AMOUNT
        assert result.slots.verified is True

    def test_wrong_dob_below_cap_clears_dob_and_reprompts(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            dob=date(1990, 1, 1),  # wrong
            selected_secondary_factor=SecondaryFactor.DOB,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters(verification_retries=0)
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.counters.verification_retries == 1
        assert result.slots.dob is None  # carve-out #4
        assert result.message_key == "verify_fail_dob"

    def test_wrong_dob_at_cap_terminates(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            dob=date(1990, 1, 1),
            selected_secondary_factor=SecondaryFactor.DOB,
            lookup_response=LOOKUP_NITHIN,
        )
        # counter == cap (3) at decision time → terminate
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY,
            slots,
            make_counters(verification_retries=config.VERIFICATION_RETRY_CAP),
        )
        assert result.next_stage == Stage.TERMINAL_VERIFICATION_EXHAUSTED
        assert result.counters.verification_retries == config.VERIFICATION_RETRY_CAP + 1
        assert result.slots.terminal_cause == "terminal_verification_exhausted"

    def test_wrong_name_clears_full_name(self) -> None:
        slots = make_slots(
            full_name="Wrong Name",
            dob=date(1990, 5, 14),
            selected_secondary_factor=SecondaryFactor.DOB,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters()
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.slots.full_name is None  # carve-out #4
        assert result.message_key == "verify_fail_name"

    def test_malformed_factor_does_not_consume_retry(self) -> None:
        # If extractor produced a 3-digit aadhaar (malformed), verify raises;
        # counter must NOT increment per DECISIONS #3 + Step-3 reviewer fix.
        slots = make_slots(
            full_name="Nithin Jain",
            aadhaar_last4="123",  # malformed
            selected_secondary_factor=SecondaryFactor.AADHAAR_LAST4,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters(verification_retries=0)
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.counters.verification_retries == 0  # NOT incremented
        assert result.message_key == "prompt_identity_malformed"


class TestStepOnEntryDobDisambiguation:
    def test_emits_prompt_without_resetting(self) -> None:
        # Reset semantics moved per MEDIUM #1: dob_disamb_retries reset
        # happens on the routing transition (step_on_input(COLLECTING_IDENTITY)
        # when dob_alternate is set), not on every step_on_entry call.
        # That decouples the contract with agent.py.
        result = state.step_on_entry(
            Stage.DOB_DISAMBIGUATION,
            make_slots(),
            make_counters(dob_disamb_retries=2),
        )
        assert result.next_stage == Stage.DOB_DISAMBIGUATION
        assert result.counters.dob_disamb_retries == 2  # NOT reset
        assert result.message_key == "prompt_dob_disambiguation"

    def test_routing_transition_resets_counter(self) -> None:
        # The routing transition (collecting_identity → dob_disambiguation
        # via dob_alternate) is what does the reset.
        result = state.step_on_input(
            Stage.COLLECTING_IDENTITY,
            make_slots(full_name="Nithin Jain", lookup_response=LOOKUP_NITHIN),
            make_counters(dob_disamb_retries=99),  # arbitrary stale value
            make_extraction(
                dob=date(1990, 4, 5),
                dob_alternate=date(1990, 5, 4),
            ),
        )
        assert result.next_stage == Stage.DOB_DISAMBIGUATION
        assert result.counters.dob_disamb_retries == 0  # reset by routing


class TestStepOnEntryCollectingAmount:
    def test_empty_prompts(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_AMOUNT, make_slots(), make_counters()
        )
        assert result.next_stage == Stage.COLLECTING_AMOUNT
        assert result.message_key == "prompt_amount"

    def test_valid_amount_advances_to_card(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_AMOUNT,
            make_slots(amount=Decimal("500")),
            make_counters(),
        )
        assert result.next_stage == Stage.COLLECTING_CARD
        assert result.message_key == "entry_collect_card"


class TestStepOnEntryCollectingCard:
    def test_empty_prompts(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_CARD, make_slots(), make_counters()
        )
        assert result.next_stage == Stage.COLLECTING_CARD
        assert result.message_key == "prompt_card"

    def test_partial_fill_prompts(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_CARD,
            make_slots(pan=VALID_PAN, cvv="123"),  # missing expiry
            make_counters(),
        )
        assert result.next_stage == Stage.COLLECTING_CARD
        assert result.message_key == "prompt_card"

    def test_complete_and_valid_advances_to_confirmation(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_CARD,
            make_slots(
                pan=VALID_PAN,
                cvv="123",
                expiry_month=12,
                expiry_year=2030,
            ),
            make_counters(),
        )
        assert result.next_stage == Stage.AWAITING_PAYMENT_CONFIRMATION
        assert result.message_key == "entry_confirm_payment"


class TestStepOnEntryAwaitingPaymentConfirmation:
    def test_emits_confirmation_without_resetting(self) -> None:
        # Reset semantics moved per MEDIUM #1: confirmation_ambig reset
        # happens on the auto-advance from collecting_card.
        result = state.step_on_entry(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            make_slots(),
            make_counters(confirmation_ambig=1),
        )
        assert result.counters.confirmation_ambig == 1  # NOT reset
        assert result.message_key == "prompt_payment_confirmation"

    def test_card_auto_advance_resets_confirmation_ambig(self) -> None:
        # The transition that ROUTES into awaiting_payment_confirmation
        # (auto-advance from collecting_card) is what resets the counter.
        result = state.step_on_entry(
            Stage.COLLECTING_CARD,
            make_slots(
                pan=VALID_PAN, cvv="123", expiry_month=12, expiry_year=2030,
            ),
            make_counters(confirmation_ambig=99),  # arbitrary stale value
        )
        assert result.next_stage == Stage.AWAITING_PAYMENT_CONFIRMATION
        assert result.counters.confirmation_ambig == 0  # reset by routing


class TestStepOnEntryForcedDisambiguation:
    def test_emits_forced_prompt_without_resetting(self) -> None:
        # Reset semantics moved per MEDIUM #1: forced_disamb_retries reset
        # happens on the routing transition from awaiting_payment_confirmation.
        result = state.step_on_entry(
            Stage.FORCED_DISAMBIGUATION,
            make_slots(),
            make_counters(forced_disamb_retries=3),
        )
        assert result.counters.forced_disamb_retries == 3  # NOT reset
        assert result.message_key == "prompt_forced_disambiguation"

    def test_routing_transition_resets_forced_counter(self) -> None:
        result = state.step_on_input(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            make_slots(),
            make_counters(
                confirmation_ambig=config.CONFIRMATION_AMBIGUOUS_CAP,
                forced_disamb_retries=99,  # arbitrary stale value
            ),
            make_extraction(intent=Intent.AMBIGUOUS),
        )
        assert result.next_stage == Stage.FORCED_DISAMBIGUATION
        assert result.counters.forced_disamb_retries == 0  # reset by routing


class TestStepOnEntryRecap:
    def test_recap_completed_auto_advances_to_terminal_completed_same_turn(self) -> None:
        # PLAN §3 row 1 for recap_completed: "deliver outcome + recap, then
        # to terminal_completed". Single-turn advance.
        result = state.step_on_entry(
            Stage.RECAP_COMPLETED, make_slots(), make_counters()
        )
        assert result.next_stage == Stage.TERMINAL_COMPLETED
        assert result.slots.terminal_cause == "terminal_completed"
        assert result.message_key == "recap_success"


class TestStepOnEntryTerminal:
    @pytest.mark.parametrize("stage", sorted(TERMINAL_STAGES))
    def test_terminal_emits_cause_specific_message(self, stage: Stage) -> None:
        result = state.step_on_entry(stage, make_slots(), make_counters())
        assert result.next_stage == stage
        assert result.message_key == stage.value

    def test_closed_emits_generic_reentry(self) -> None:
        result = state.step_on_entry(Stage.CLOSED, make_slots(), make_counters())
        assert result.next_stage == Stage.CLOSED
        assert result.message_key == "closed_reentry"


# ===========================================================================
# Section 5 — step_on_input
# ===========================================================================


class TestStepOnInputGreeting:
    def test_advances_to_collecting_account_id(self) -> None:
        result = state.step_on_input(
            Stage.GREETING, make_slots(), make_counters(), make_extraction()
        )
        assert result.next_stage == Stage.COLLECTING_ACCOUNT_ID


class TestStepOnInputDobDisambiguation:
    def test_resolved_choice_writes_dob_and_advances(self) -> None:
        result = state.step_on_input(
            Stage.DOB_DISAMBIGUATION,
            make_slots(full_name="Nithin Jain", lookup_response=LOOKUP_NITHIN),
            make_counters(),
            make_extraction(dob=date(1990, 5, 14)),
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.slots.dob == date(1990, 5, 14)  # carve-out #2
        assert result.message_key == "dob_resolved"

    def test_unrecognized_below_cap_reprompts(self) -> None:
        result = state.step_on_input(
            Stage.DOB_DISAMBIGUATION,
            make_slots(),
            make_counters(dob_disamb_retries=0),
            make_extraction(),
        )
        assert result.next_stage == Stage.DOB_DISAMBIGUATION
        assert result.counters.dob_disamb_retries == 1
        assert result.message_key == "reprompt_dob_disambiguation"

    def test_unrecognized_at_cap_terminates(self) -> None:
        result = state.step_on_input(
            Stage.DOB_DISAMBIGUATION,
            make_slots(),
            make_counters(dob_disamb_retries=config.DOB_DISAMB_RETRY_CAP),
            make_extraction(),
        )
        assert result.next_stage == Stage.TERMINAL_CANCELLED
        assert result.message_key == "terminal_cancelled_dob_disamb"


class TestStepOnInputCollectingIdentityRoutesToDisambiguation:
    def test_dob_alternate_routes_to_dob_disambiguation(self) -> None:
        result = state.step_on_input(
            Stage.COLLECTING_IDENTITY,
            make_slots(full_name="Nithin Jain", lookup_response=LOOKUP_NITHIN),
            make_counters(),
            make_extraction(
                dob=date(1990, 4, 5),
                dob_alternate=date(1990, 5, 4),
            ),
        )
        assert result.next_stage == Stage.DOB_DISAMBIGUATION


class TestStepOnInputAwaitingPaymentConfirmation:
    def test_affirm_fires_payment(self) -> None:
        result = state.step_on_input(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            make_slots(),
            make_counters(),
            make_extraction(intent=Intent.AFFIRM),
        )
        assert result.side_effect == SideEffect.CALL_PROCESS_PAYMENT
        assert result.message_key == "payment_in_progress"

    def test_ambiguous_first_reprompts(self) -> None:
        result = state.step_on_input(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            make_slots(),
            make_counters(confirmation_ambig=0),
            make_extraction(intent=Intent.AMBIGUOUS),
        )
        assert result.next_stage == Stage.AWAITING_PAYMENT_CONFIRMATION
        assert result.counters.confirmation_ambig == 1

    def test_ambiguous_at_cap_routes_to_forced(self) -> None:
        result = state.step_on_input(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            make_slots(),
            make_counters(confirmation_ambig=config.CONFIRMATION_AMBIGUOUS_CAP),
            make_extraction(intent=Intent.AMBIGUOUS),
        )
        assert result.next_stage == Stage.FORCED_DISAMBIGUATION
        assert result.counters.confirmation_ambig == config.CONFIRMATION_AMBIGUOUS_CAP + 1


class TestStepOnInputForcedDisambiguation:
    def test_affirm_fires_payment(self) -> None:
        result = state.step_on_input(
            Stage.FORCED_DISAMBIGUATION,
            make_slots(),
            make_counters(),
            make_extraction(intent=Intent.AFFIRM),
        )
        assert result.side_effect == SideEffect.CALL_PROCESS_PAYMENT

    def test_ambiguous_below_cap_reprompts(self) -> None:
        result = state.step_on_input(
            Stage.FORCED_DISAMBIGUATION,
            make_slots(),
            make_counters(forced_disamb_retries=0),
            make_extraction(intent=Intent.AMBIGUOUS),
        )
        assert result.next_stage == Stage.FORCED_DISAMBIGUATION
        assert result.counters.forced_disamb_retries == 1

    def test_ambiguous_at_cap_terminates(self) -> None:
        result = state.step_on_input(
            Stage.FORCED_DISAMBIGUATION,
            make_slots(),
            make_counters(forced_disamb_retries=config.FORCED_DISAMB_RETRY_CAP),
            make_extraction(intent=Intent.AMBIGUOUS),
        )
        assert result.next_stage == Stage.TERMINAL_CANCELLED


class TestStepOnInputRecap:
    def test_recap_advances_to_terminal_completed(self) -> None:
        result = state.step_on_input(
            Stage.RECAP_COMPLETED,
            make_slots(),
            make_counters(),
            make_extraction(),
        )
        assert result.next_stage == Stage.TERMINAL_COMPLETED


class TestStepOnInputTerminalsAndClosed:
    def test_closed_stays_closed(self) -> None:
        result = state.step_on_input(
            Stage.CLOSED, make_slots(), make_counters(), make_extraction()
        )
        assert result.next_stage == Stage.CLOSED
        assert result.message_key == "closed_reentry"

    def test_terminal_routes_to_closed(self) -> None:
        result = state.step_on_input(
            Stage.TERMINAL_COMPLETED, make_slots(), make_counters(), make_extraction()
        )
        assert result.next_stage == Stage.CLOSED


# ===========================================================================
# Section 6 — step_on_effect
# ===========================================================================


class TestStepOnEffectLookup:
    def test_success_advances_to_collecting_identity_and_caches_response(self) -> None:
        slots = make_slots(account_id="ACC1001")
        result = state.step_on_effect(
            Stage.COLLECTING_ACCOUNT_ID,
            slots,
            make_counters(),
            LookupEffectResult(outcome=LookupOutcome.SUCCESS, account_data=LOOKUP_NITHIN),
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.slots.lookup_response == LOOKUP_NITHIN
        assert result.message_key == "entry_collect_identity"

    def test_account_not_found_below_cap_clears_account_id(self) -> None:
        slots = make_slots(account_id="ACC9999")
        result = state.step_on_effect(
            Stage.COLLECTING_ACCOUNT_ID,
            slots,
            make_counters(lookup_retries=0),
            LookupEffectResult(outcome=LookupOutcome.ACCOUNT_NOT_FOUND),
        )
        assert result.next_stage == Stage.COLLECTING_ACCOUNT_ID
        assert result.counters.lookup_retries == 1
        # Carve-out #3: account_id cleared.
        assert result.slots.account_id is None
        assert result.message_key == "lookup_not_found_retry"

    def test_account_not_found_at_cap_terminates(self) -> None:
        slots = make_slots(account_id="ACC9999")
        result = state.step_on_effect(
            Stage.COLLECTING_ACCOUNT_ID,
            slots,
            make_counters(lookup_retries=config.LOOKUP_RETRY_CAP),
            LookupEffectResult(outcome=LookupOutcome.ACCOUNT_NOT_FOUND),
        )
        assert result.next_stage == Stage.TERMINAL_ACCOUNT_NOT_FOUND
        assert result.slots.terminal_cause == "terminal_account_not_found"

    def test_transient_below_cap_keeps_account_id(self) -> None:
        slots = make_slots(account_id="ACC1001")
        result = state.step_on_effect(
            Stage.COLLECTING_ACCOUNT_ID,
            slots,
            make_counters(lookup_retries=0),
            LookupEffectResult(outcome=LookupOutcome.TRANSIENT),
        )
        assert result.next_stage == Stage.COLLECTING_ACCOUNT_ID
        assert result.counters.lookup_retries == 1
        # Transient: account_id NOT cleared (the value may be correct).
        assert result.slots.account_id == "ACC1001"
        assert result.message_key == "lookup_transient_retry"

    def test_transient_at_cap_terminates(self) -> None:
        result = state.step_on_effect(
            Stage.COLLECTING_ACCOUNT_ID,
            make_slots(account_id="ACC1001"),
            make_counters(lookup_retries=config.LOOKUP_RETRY_CAP),
            LookupEffectResult(outcome=LookupOutcome.TRANSIENT),
        )
        assert result.next_stage == Stage.TERMINAL_ACCOUNT_NOT_FOUND


class TestStepOnEffectPayment:
    def _confirm_slots(self) -> SlotStore:
        return make_slots(
            account_id="ACC1001",
            amount=Decimal("500"),
            pan=VALID_PAN,
            cvv="123",
            expiry_month=12,
            expiry_year=2030,
            verified=True,
            lookup_response=LOOKUP_NITHIN,
        )

    def test_success_routes_to_recap_with_txn_id_and_remaining(self) -> None:
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            self._confirm_slots(),
            make_counters(),
            PaymentEffectResult(
                outcome=PaymentOutcome.SUCCESS,
                transaction_id="TXN12345",
            ),
        )
        assert result.next_stage == Stage.RECAP_COMPLETED
        assert result.slots.transaction_id == "TXN12345"
        # remaining = 10000 - 500 = 9500 (session-tracked per DECISIONS #7).
        assert result.slots.remaining_balance == Decimal("9500")

    @pytest.mark.parametrize(
        "outcome,cleared_field",
        [
            (PaymentOutcome.INVALID_CARD, "pan"),
            (PaymentOutcome.INVALID_CVV, "cvv"),
        ],
    )
    def test_typo_below_cap_clears_named_field(
        self, outcome: PaymentOutcome, cleared_field: str
    ) -> None:
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            self._confirm_slots(),
            make_counters(payment_retries=0),
            PaymentEffectResult(outcome=outcome),
        )
        assert result.next_stage == Stage.COLLECTING_CARD
        assert result.counters.payment_retries == 1
        # Carve-out #1: named card field cleared.
        assert getattr(result.slots, cleared_field) is None

    def test_invalid_expiry_clears_both_month_and_year(self) -> None:
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            self._confirm_slots(),
            make_counters(payment_retries=0),
            PaymentEffectResult(outcome=PaymentOutcome.INVALID_EXPIRY),
        )
        assert result.slots.expiry_month is None
        assert result.slots.expiry_year is None

    def test_typo_at_cap_terminates(self) -> None:
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            self._confirm_slots(),
            make_counters(payment_retries=config.PAYMENT_TYPO_RETRY_CAP),
            PaymentEffectResult(outcome=PaymentOutcome.INVALID_CARD),
        )
        assert result.next_stage == Stage.TERMINAL_PAYMENT_EXHAUSTED

    def test_insufficient_balance_clears_amount_no_counter(self) -> None:
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            self._confirm_slots(),
            make_counters(payment_retries=2),
            PaymentEffectResult(outcome=PaymentOutcome.INSUFFICIENT_BALANCE),
        )
        assert result.next_stage == Stage.COLLECTING_AMOUNT
        # No counter consumed (DECISIONS #4).
        assert result.counters.payment_retries == 2
        # Amount cleared to allow re-prompt.
        assert result.slots.amount is None

    def test_unknown_routes_to_terminal_payment_unknown(self) -> None:
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            self._confirm_slots(),
            make_counters(),
            PaymentEffectResult(outcome=PaymentOutcome.UNKNOWN),
        )
        assert result.next_stage == Stage.TERMINAL_PAYMENT_UNKNOWN

    def test_invalid_amount_treated_as_bug_routes_to_unknown(self) -> None:
        # DECISIONS #4: invalid_amount must be caught client-side. If it
        # surfaces, the API state is suspect — route conservatively.
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            self._confirm_slots(),
            make_counters(),
            PaymentEffectResult(outcome=PaymentOutcome.INVALID_AMOUNT),
        )
        assert result.next_stage == Stage.TERMINAL_PAYMENT_UNKNOWN

    def test_payment_from_forced_disambiguation_routes_same_way(self) -> None:
        # forced_disambiguation also fires CALL_PROCESS_PAYMENT; effect
        # handling must match.
        result = state.step_on_effect(
            Stage.FORCED_DISAMBIGUATION,
            self._confirm_slots(),
            make_counters(),
            PaymentEffectResult(
                outcome=PaymentOutcome.SUCCESS, transaction_id="TXN99",
            ),
        )
        assert result.next_stage == Stage.RECAP_COMPLETED


# ===========================================================================
# Section 7 — Cancellation through cancel_guard records correct cause
# ===========================================================================


class TestCancelGuardCauseStrings:
    def test_cancelled_at_collecting_identity(self) -> None:
        result = state.cancel_guard(
            Stage.COLLECTING_IDENTITY, Intent.NEGATE, make_slots(), make_counters()
        )
        assert result is not None
        assert result.slots.terminal_cause == "cancelled_at_collecting_identity"

    def test_cancelled_at_awaiting_payment_confirmation(self) -> None:
        # Per DECISIONS #5: NEGATE on confirmation gate is a graceful exit.
        result = state.cancel_guard(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            Intent.NEGATE,
            make_slots(),
            make_counters(),
        )
        assert result is not None
        assert result.slots.terminal_cause == "cancelled_at_awaiting_payment_confirmation"


# ===========================================================================
# Section 8 — Verified flag flow + remaining_balance computation
# ===========================================================================


# ===========================================================================
# Section 9 — CRITICAL fixes: invalid-value clearing on entry/input
# ===========================================================================


class TestCriticalInvalidAccountId:
    def test_invalid_format_account_id_cleared_on_entry(self) -> None:
        # Pre-filled invalid account_id from extractor (e.g., "ACC123" too short).
        # Without the fix, lock-once-filled would trap the user with a bad value.
        result = state.step_on_entry(
            Stage.COLLECTING_ACCOUNT_ID,
            make_slots(account_id="ACC123"),
            make_counters(),
        )
        assert result.slots.account_id is None
        assert result.side_effect == SideEffect.NONE  # no API call fired
        assert result.message_key == "prompt_account_id_invalid_format"

    def test_invalid_format_account_id_cleared_on_input(self) -> None:
        # Same shape via step_on_input (which delegates to entry).
        result = state.step_on_input(
            Stage.COLLECTING_ACCOUNT_ID,
            make_slots(account_id="BCC9999"),  # wrong prefix
            make_counters(),
            make_extraction(),
        )
        assert result.slots.account_id is None
        assert result.side_effect == SideEffect.NONE
        assert result.message_key == "prompt_account_id_invalid_format"

    def test_valid_account_id_still_fires_lookup(self) -> None:
        # Regression: well-formed account_id still triggers lookup.
        result = state.step_on_entry(
            Stage.COLLECTING_ACCOUNT_ID,
            make_slots(account_id="ACC1001"),
            make_counters(),
        )
        assert result.side_effect == SideEffect.CALL_LOOKUP


class TestCriticalInvalidAmount:
    def test_pre_filled_invalid_amount_cleared_on_entry(self) -> None:
        # Out-of-order extraction filled amount with an invalid value
        # (e.g., 100.005 = 3 decimal places). User would otherwise be stuck.
        result = state.step_on_entry(
            Stage.COLLECTING_AMOUNT,
            make_slots(amount=Decimal("100.005")),
            make_counters(),
        )
        assert result.slots.amount is None
        assert result.message_key == "prompt_amount_invalid_format"

    def test_negative_amount_cleared_on_entry(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_AMOUNT,
            make_slots(amount=Decimal("-100")),
            make_counters(),
        )
        assert result.slots.amount is None
        assert result.message_key == "prompt_amount_invalid_format"

    def test_zero_amount_cleared_on_entry(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_AMOUNT,
            make_slots(amount=Decimal("0")),
            make_counters(),
        )
        assert result.slots.amount is None
        assert result.message_key == "prompt_amount_invalid_format"

    def test_valid_amount_still_advances(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_AMOUNT,
            make_slots(amount=Decimal("500")),
            make_counters(),
        )
        assert result.next_stage == Stage.COLLECTING_CARD


class TestCriticalInvalidCardFields:
    def test_luhn_failing_pan_cleared(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_CARD,
            make_slots(
                pan="4532015112830365",  # last digit changed → Luhn fail
                cvv="123",
                expiry_month=12,
                expiry_year=2030,
            ),
            make_counters(),
        )
        assert result.slots.pan is None  # cleared
        assert result.slots.cvv == "123"  # untouched
        assert result.message_key == "prompt_card_invalid_pan"

    def test_invalid_cvv_cleared(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_CARD,
            make_slots(
                pan=VALID_PAN,
                cvv="12",  # too short
                expiry_month=12,
                expiry_year=2030,
            ),
            make_counters(),
        )
        assert result.slots.cvv is None
        assert result.slots.pan == VALID_PAN  # untouched
        assert result.message_key == "prompt_card_invalid_cvv"

    def test_expired_card_cleared(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_CARD,
            make_slots(
                pan=VALID_PAN,
                cvv="123",
                expiry_month=1,
                expiry_year=2020,  # in the past
            ),
            make_counters(),
        )
        assert result.slots.expiry_month is None
        assert result.slots.expiry_year is None
        assert result.slots.pan == VALID_PAN  # untouched
        assert result.message_key == "prompt_card_invalid_expiry"

    def test_invalid_month_cleared(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_CARD,
            make_slots(
                pan=VALID_PAN,
                cvv="123",
                expiry_month=13,  # invalid month
                expiry_year=2030,
            ),
            make_counters(),
        )
        assert result.slots.expiry_month is None
        assert result.slots.expiry_year is None

    def test_valid_card_still_advances(self) -> None:
        result = state.step_on_entry(
            Stage.COLLECTING_CARD,
            make_slots(
                pan=VALID_PAN,
                cvv="123",
                expiry_month=12,
                expiry_year=2030,
            ),
            make_counters(),
        )
        assert result.next_stage == Stage.AWAITING_PAYMENT_CONFIRMATION


# ===========================================================================
# Section 10 — HIGH #1: StateMachineError vs ValueError discrimination
# ===========================================================================


class TestStateMachineErrorVsValueError:
    def test_invariant_violation_propagates_as_state_machine_error(self) -> None:
        # _verify_identity called with no lookup_response — programmer bug.
        # Must propagate as StateMachineError, NOT silently absorbed as
        # "malformed extraction" the way the old code did.
        slots = make_slots(
            full_name="Nithin Jain",
            dob=date(1990, 5, 14),
            selected_secondary_factor=SecondaryFactor.DOB,
            lookup_response=None,  # invariant violation
        )
        with pytest.raises(StateMachineError):
            state.step_on_entry(Stage.COLLECTING_IDENTITY, slots, make_counters())

    def test_missing_full_name_propagates(self) -> None:
        # Identity-complete returned True via fallback (factor inferred from
        # filled slots) but full_name is None — invariant violation.
        # In practice this can't happen because _identity_complete checks
        # full_name first; verify that the StateMachineError discipline holds
        # if logic ever drifts.
        slots = make_slots(
            dob=date(1990, 5, 14),
            selected_secondary_factor=SecondaryFactor.DOB,
            lookup_response=LOOKUP_NITHIN,
        )
        # _identity_complete returns False here (full_name None) so verify
        # is not called. Result is a re-prompt. Test that we don't raise.
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters()
        )
        assert result.message_key == "prompt_identity"

    def test_malformed_aadhaar_still_caught_as_value_error(self) -> None:
        # ValueError path (user-input format) must still be absorbed as
        # "malformed extraction" without consuming a retry.
        slots = make_slots(
            full_name="Nithin Jain",
            aadhaar_last4="123",  # malformed (3 digits)
            selected_secondary_factor=SecondaryFactor.AADHAAR_LAST4,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters(verification_retries=0)
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.counters.verification_retries == 0  # NOT incremented
        assert result.message_key == "prompt_identity_malformed"


# ===========================================================================
# Section 11 — MEDIUM #2: aadhaar / pincode wrong-value verify paths
# ===========================================================================


class TestVerifyWrongFactorValues:
    def test_wrong_aadhaar_below_cap_clears_aadhaar(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            aadhaar_last4="9999",  # wrong but well-formed
            selected_secondary_factor=SecondaryFactor.AADHAAR_LAST4,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters(verification_retries=0)
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.counters.verification_retries == 1
        assert result.slots.aadhaar_last4 is None  # carve-out #4
        assert result.slots.full_name == "Nithin Jain"  # untouched
        assert result.message_key == "verify_fail_aadhaar_last4"

    def test_wrong_aadhaar_at_cap_terminates(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            aadhaar_last4="9999",
            selected_secondary_factor=SecondaryFactor.AADHAAR_LAST4,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY,
            slots,
            make_counters(verification_retries=config.VERIFICATION_RETRY_CAP),
        )
        assert result.next_stage == Stage.TERMINAL_VERIFICATION_EXHAUSTED

    def test_wrong_pincode_below_cap_clears_pincode(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            pincode="999999",  # wrong but well-formed
            selected_secondary_factor=SecondaryFactor.PINCODE,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters(verification_retries=0)
        )
        assert result.next_stage == Stage.COLLECTING_IDENTITY
        assert result.counters.verification_retries == 1
        assert result.slots.pincode is None
        assert result.message_key == "verify_fail_pincode"

    def test_wrong_pincode_at_cap_terminates(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            pincode="999999",
            selected_secondary_factor=SecondaryFactor.PINCODE,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY,
            slots,
            make_counters(verification_retries=config.VERIFICATION_RETRY_CAP),
        )
        assert result.next_stage == Stage.TERMINAL_VERIFICATION_EXHAUSTED


# ===========================================================================
# Section 12 — MEDIUM #4: _compute_remaining_balance edge paths
# ===========================================================================


class TestComputeRemainingBalanceEdgePaths:
    def test_returns_none_when_lookup_response_missing(self) -> None:
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            make_slots(amount=Decimal("500"), lookup_response=None),
            make_counters(),
            PaymentEffectResult(
                outcome=PaymentOutcome.SUCCESS, transaction_id="TXN1",
            ),
        )
        # Without lookup_response, remaining_balance computation returns None.
        assert result.slots.remaining_balance is None
        # step_on_effect routes to RECAP_COMPLETED; the orchestrator's
        # subsequent step_on_entry(RECAP_COMPLETED) is what auto-advances
        # to TERMINAL_COMPLETED in the same turn.
        assert result.next_stage == Stage.RECAP_COMPLETED

    def test_returns_none_when_amount_missing(self) -> None:
        result = state.step_on_effect(
            Stage.AWAITING_PAYMENT_CONFIRMATION,
            make_slots(amount=None, lookup_response=LOOKUP_NITHIN),
            make_counters(),
            PaymentEffectResult(
                outcome=PaymentOutcome.SUCCESS, transaction_id="TXN1",
            ),
        )
        assert result.slots.remaining_balance is None


# ===========================================================================
# Section 13 — Verified flag flow + remaining_balance computation
# ===========================================================================


class TestVerifiedAndRemainingBalance:
    def test_verified_set_only_on_successful_verify(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            dob=date(1990, 5, 14),
            selected_secondary_factor=SecondaryFactor.DOB,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters()
        )
        assert result.slots.verified is True

    def test_verified_not_set_on_failed_verify(self) -> None:
        slots = make_slots(
            full_name="Nithin Jain",
            dob=date(1990, 1, 1),  # wrong
            selected_secondary_factor=SecondaryFactor.DOB,
            lookup_response=LOOKUP_NITHIN,
        )
        result = state.step_on_entry(
            Stage.COLLECTING_IDENTITY, slots, make_counters()
        )
        assert result.slots.verified is False
