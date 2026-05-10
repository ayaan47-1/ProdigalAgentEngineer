"""State machine — the deterministic kernel.

Owns:
  - Stage enum (16 members per PLAN §3)
  - Intent enum (3-way classifier output: AFFIRM / NEGATE / AMBIGUOUS)
  - SideEffect enum (NONE / CALL_LOOKUP / CALL_PROCESS_PAYMENT)
  - SlotStore (Pydantic; persistent per-session; locked-once-filled with 4 carve-outs per DECISIONS #21)
  - Counters (Pydantic; the 6 retry counters)
  - ExtractedSlots / ExtractionResult (per-turn extractor output)
  - LookupEffectResult / PaymentEffectResult (side-effect callbacks)
  - StepResult (transition output)
  - cancel_guard (orchestrator-level NEGATE handler)
  - step_on_entry / step_on_input / step_on_effect (the three transition entry points)

Transition functions are *pure* (DECISIONS #8). Side effects (HTTP) live in
api.py and are executed by the orchestrator (agent.py) outside these
functions; their results feed step_on_effect.

Cap semantics across all counters (DECISIONS #22):
    cap N = N events allowed (each re-prompted), action on the (N+1)th event.
    counter < N → re-prompt (counter increments by 1)
    counter == N → terminate (counter increments by 1)

Slot mutability (DECISIONS #21):
    Default: lock once filled. User-stated mid-flow correction is rejected.
    Four orchestrator-controlled carve-outs:
      1. Typo-class payment failure clears the named failed card field
      2. DOB disambiguation resolution writes DOB
      3. Lookup `account_not_found` clears `account_id`
      4. Verification failure clears the failed name and/or factor slot
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from payment_agent import config, validate, verify
from payment_agent.verify import SecondaryFactor


class StateMachineError(Exception):
    """Raised by state.py when an invariant of the state machine is violated.

    Distinct from ``ValueError`` so the orchestrator can distinguish
    programmer errors (bug — should crash loudly) from user-input format
    errors (recoverable — re-prompt without consuming a retry).
    """

# ===========================================================================
# Enums
# ===========================================================================


class Stage(StrEnum):
    """The 16-member stage enum (PLAN §3 signed-off list)."""

    GREETING = "greeting"
    COLLECTING_ACCOUNT_ID = "collecting_account_id"
    COLLECTING_IDENTITY = "collecting_identity"
    DOB_DISAMBIGUATION = "dob_disambiguation"
    COLLECTING_AMOUNT = "collecting_amount"
    COLLECTING_CARD = "collecting_card"
    AWAITING_PAYMENT_CONFIRMATION = "awaiting_payment_confirmation"
    FORCED_DISAMBIGUATION = "forced_disambiguation"
    RECAP_COMPLETED = "recap_completed"
    TERMINAL_COMPLETED = "terminal_completed"
    TERMINAL_VERIFICATION_EXHAUSTED = "terminal_verification_exhausted"
    TERMINAL_PAYMENT_EXHAUSTED = "terminal_payment_exhausted"
    TERMINAL_CANCELLED = "terminal_cancelled"
    TERMINAL_ACCOUNT_NOT_FOUND = "terminal_account_not_found"
    TERMINAL_PAYMENT_UNKNOWN = "terminal_payment_unknown"
    CLOSED = "closed"


TERMINAL_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.TERMINAL_COMPLETED,
        Stage.TERMINAL_VERIFICATION_EXHAUSTED,
        Stage.TERMINAL_PAYMENT_EXHAUSTED,
        Stage.TERMINAL_CANCELLED,
        Stage.TERMINAL_ACCOUNT_NOT_FOUND,
        Stage.TERMINAL_PAYMENT_UNKNOWN,
    }
)

# Cancellable: every active stage except recap_completed and terminals/closed.
CANCELLABLE_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.GREETING,
        Stage.COLLECTING_ACCOUNT_ID,
        Stage.COLLECTING_IDENTITY,
        Stage.DOB_DISAMBIGUATION,
        Stage.COLLECTING_AMOUNT,
        Stage.COLLECTING_CARD,
        Stage.AWAITING_PAYMENT_CONFIRMATION,
        Stage.FORCED_DISAMBIGUATION,
    }
)


class Intent(StrEnum):
    """Three-way classifier output (per architecture commit)."""

    AFFIRM = "affirm"
    NEGATE = "negate"
    AMBIGUOUS = "ambiguous"


class SideEffect(StrEnum):
    """Side-effect requests returned by transitions; executed by the orchestrator."""

    NONE = "none"
    CALL_LOOKUP = "call_lookup"
    CALL_PROCESS_PAYMENT = "call_process_payment"


class LookupOutcome(StrEnum):
    """Discriminator for LookupEffectResult."""

    SUCCESS = "success"
    ACCOUNT_NOT_FOUND = "account_not_found"
    TRANSIENT = "transient"  # post-silent-retry transient failure (DECISIONS #13)


class PaymentOutcome(StrEnum):
    """Discriminator for PaymentEffectResult.

    INVALID_AMOUNT should NEVER reach here per DECISIONS #4 — validators must
    catch zero/negative/>2dp client-side. If it surfaces, it's a bug.
    """

    SUCCESS = "success"
    INVALID_CARD = "invalid_card"
    INVALID_CVV = "invalid_cvv"
    INVALID_EXPIRY = "invalid_expiry"
    INVALID_AMOUNT = "invalid_amount"  # bug surface
    INSUFFICIENT_BALANCE = "insufficient_balance"
    UNKNOWN = "unknown"  # 5xx / timeout / undocumented 4xx (DECISIONS #13)


# Typo-class outcomes (per DECISIONS #4) that consume payment_retries.
_TYPO_CLASS_OUTCOMES: frozenset[PaymentOutcome] = frozenset(
    {
        PaymentOutcome.INVALID_CARD,
        PaymentOutcome.INVALID_CVV,
        PaymentOutcome.INVALID_EXPIRY,
    }
)


# ===========================================================================
# Pydantic models
# ===========================================================================


class _StrictModel(BaseModel):
    """Base for our models. Forbid extra fields so silent typos at construction
    sites surface as ValidationError rather than no-ops."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class LookupResponse(_StrictModel):
    """Typed view of the ``lookup_account`` API response (verified against
    PDF spec). api.py validates wire JSON into this model before caching, so
    schema drift surfaces as ValidationError at the boundary rather than as
    a silent downstream failure.

    All fields per the spec's 200-response example; ``account_id`` is
    included even though we already know it from the request — strict
    Pydantic with ``extra="forbid"`` would otherwise raise on every real
    response.
    """

    account_id: str
    full_name: str
    dob: date
    aadhaar_last4: str
    pincode: str
    balance: Decimal


class ExtractedSlots(_StrictModel):
    """Per-turn extractor output. All slots are optional — the extractor fills
    only what was actually present in the user's message this turn.
    """

    account_id: str | None = None
    full_name: str | None = None
    dob: date | None = None
    aadhaar_last4: str | None = None
    pincode: str | None = None
    selected_secondary_factor: SecondaryFactor | None = None
    amount: Decimal | None = None
    pan: str | None = None
    cvv: str | None = None
    expiry_month: int | None = None
    expiry_year: int | None = None
    name_on_card: str | None = None
    # DOB disambiguation alternate reading (set when both DD-MM and MM-DD parse
    # to plausible dates per DECISIONS #15).
    dob_alternate: date | None = None


class ExtractionResult(_StrictModel):
    slots: ExtractedSlots = Field(default_factory=ExtractedSlots)
    intent: Intent = Intent.AMBIGUOUS


class SlotStore(_StrictModel):
    """Persistent per-session slot state. Locked-once-filled with four
    orchestrator-controlled carve-outs (DECISIONS #21)."""

    # Identity slots
    account_id: str | None = None
    full_name: str | None = None
    dob: date | None = None
    aadhaar_last4: str | None = None
    pincode: str | None = None
    selected_secondary_factor: SecondaryFactor | None = None

    # DOB disambiguation v2: when initial extraction is numerically
    # ambiguous (LLM returns dob + dob_alternate), the orchestrator stores
    # BOTH readings here and does NOT merge primary into slots.dob.
    # step_on_input(DOB_DISAMBIGUATION) resolves user's choice into
    # slots.dob and clears these. Both fields cleared on resolution.
    dob_disamb_primary: date | None = None
    dob_disamb_alternate: date | None = None

    # Payment slots
    amount: Decimal | None = None
    pan: str | None = None
    cvv: str | None = None  # NEVER serialized to logs
    expiry_month: int | None = None
    expiry_year: int | None = None
    name_on_card: str | None = None

    # Derived state
    verified: bool = False
    lookup_response: LookupResponse | None = None  # validated at api.py boundary
    transaction_id: str | None = None
    remaining_balance: Decimal | None = None  # session-tracked per DECISIONS #7
    terminal_cause: str | None = None


class Counters(_StrictModel):
    """The six retry counters. All cap semantics per DECISIONS #22."""

    verification_retries: int = 0
    payment_retries: int = 0
    lookup_retries: int = 0
    confirmation_ambig: int = 0
    dob_disamb_retries: int = 0
    forced_disamb_retries: int = 0


class LookupEffectResult(_StrictModel):
    """What the orchestrator passes back to step_on_effect after CALL_LOOKUP."""

    outcome: LookupOutcome
    account_data: LookupResponse | None = None  # required when outcome == SUCCESS


class PaymentEffectResult(_StrictModel):
    """What the orchestrator passes back to step_on_effect after CALL_PROCESS_PAYMENT."""

    outcome: PaymentOutcome
    transaction_id: str | None = None  # required when outcome == SUCCESS


EffectResult = LookupEffectResult | PaymentEffectResult


class StepResult(_StrictModel):
    """Output of a transition function."""

    next_stage: Stage
    slots: SlotStore
    counters: Counters
    side_effect: SideEffect = SideEffect.NONE
    message_key: str


# ===========================================================================
# Helpers
# ===========================================================================


def _terminal_cause(stage: Stage) -> str:
    """Standardised terminal_cause string used in slot_store.terminal_cause."""
    return stage.value


def _route_terminal(
    *,
    stage: Stage,
    slots: SlotStore,
    counters: Counters,
    message_key: str,
) -> StepResult:
    """Construct a StepResult landing in a terminal stage with terminal_cause set."""
    new_slots = slots.model_copy(update={"terminal_cause": _terminal_cause(stage)})
    return StepResult(
        next_stage=stage,
        slots=new_slots,
        counters=counters,
        side_effect=SideEffect.NONE,
        message_key=message_key,
    )


def _identity_complete(slots: SlotStore) -> bool:
    """True iff name + the selected secondary factor are both filled."""
    if not slots.full_name:
        return False
    factor = slots.selected_secondary_factor
    if factor is None:
        # Fall back: any one secondary factor present counts as "selected."
        return bool(slots.dob or slots.aadhaar_last4 or slots.pincode)
    if factor == SecondaryFactor.DOB:
        return slots.dob is not None
    if factor == SecondaryFactor.AADHAAR_LAST4:
        return slots.aadhaar_last4 is not None
    if factor == SecondaryFactor.PINCODE:
        return slots.pincode is not None
    return False


def _selected_or_inferred_factor(slots: SlotStore) -> SecondaryFactor | None:
    """Use selected_secondary_factor if set; else infer from filled slots."""
    if slots.selected_secondary_factor is not None:
        return slots.selected_secondary_factor
    if slots.dob is not None:
        return SecondaryFactor.DOB
    if slots.aadhaar_last4 is not None:
        return SecondaryFactor.AADHAAR_LAST4
    if slots.pincode is not None:
        return SecondaryFactor.PINCODE
    return None


def _amount_valid(slots: SlotStore) -> bool:
    if slots.amount is None:
        return False
    try:
        validate.validate_amount(slots.amount)
    except ValueError:
        return False
    return True


def _expiry_components_valid(month: int | None, year: int | None) -> bool:
    """Validate (month, year) integer components for an unexpired card."""
    if month is None or year is None:
        return False
    if not 1 <= month <= 12:
        return False
    today = date.today()
    if year < today.year or (year == today.year and month < today.month):
        return False
    return True


def _card_complete_and_valid(slots: SlotStore) -> bool:
    if not (slots.pan and slots.cvv and slots.expiry_month and slots.expiry_year):
        return False
    try:
        validate.validate_pan(slots.pan)
        validate.validate_cvv(slots.cvv)
    except ValueError:
        return False
    return _expiry_components_valid(slots.expiry_month, slots.expiry_year)


def _account_id_valid(account_id: str | None) -> bool:
    if account_id is None:
        return False
    try:
        validate.validate_account_id(account_id)
    except ValueError:
        return False
    return True


def _clear_invalid_account_id(slots: SlotStore) -> tuple[SlotStore, bool]:
    """Carve-out: extractor produced an invalid account_id.

    Returns (cleared_slots, was_cleared). If account_id was filled but invalid,
    clears the slot so the lock-once-filled rule doesn't trap the user with a
    bad value. Otherwise returns slots unchanged.
    """
    if slots.account_id is not None and not _account_id_valid(slots.account_id):
        return slots.model_copy(update={"account_id": None}), True
    return slots, False


def _clear_invalid_amount(slots: SlotStore) -> tuple[SlotStore, bool]:
    """Carve-out: extractor produced an invalid amount."""
    if slots.amount is not None and not _amount_valid(slots):
        return slots.model_copy(update={"amount": None}), True
    return slots, False


def _clear_invalid_card_fields(slots: SlotStore) -> tuple[SlotStore, list[str]]:
    """Carve-out: extractor produced one or more invalid card fields.

    Returns (cleared_slots, list_of_cleared_field_names). Each card field is
    individually checked against its own validator; only the failing fields
    are cleared so the user can correct them by name.
    """
    update: dict[str, Any] = {}
    cleared: list[str] = []
    if slots.pan is not None:
        try:
            validate.validate_pan(slots.pan)
        except ValueError:
            update["pan"] = None
            cleared.append("pan")
    if slots.cvv is not None:
        try:
            validate.validate_cvv(slots.cvv)
        except ValueError:
            update["cvv"] = None
            cleared.append("cvv")
    if (slots.expiry_month is not None or slots.expiry_year is not None) and not _expiry_components_valid(
        slots.expiry_month, slots.expiry_year
    ):
        update["expiry_month"] = None
        update["expiry_year"] = None
        cleared.append("expiry")
    if not update:
        return slots, []
    return slots.model_copy(update=update), cleared


def _verify_identity(slots: SlotStore) -> tuple[bool, str | None]:
    """Run the in-process verification.

    Returns (passed, failed_field_label). On pass, returns (True, None).
    On fail, returns (False, "name") or (False, factor.value).

    Raises ``StateMachineError`` for invariant violations (lookup not cached,
    missing slots that orchestrator should have already filled). These are
    bugs and must surface — never absorbed as "malformed extraction."

    Raises ``ValueError`` only when the submitted secondary-factor format is
    malformed (e.g., 3-digit aadhaar). The caller catches ``ValueError`` and
    re-prompts without consuming a retry per DECISIONS #3 ("malformed
    extraction is not a submission").
    """
    if slots.lookup_response is None:
        raise StateMachineError("verify called before lookup_response is cached")
    if slots.full_name is None:
        raise StateMachineError("verify called with full_name slot empty")

    if not verify.compare_name(slots.full_name, slots.lookup_response.full_name):
        return False, "name"

    factor = _selected_or_inferred_factor(slots)
    if factor is None:
        raise StateMachineError("verify called without a selected secondary factor")

    if factor == SecondaryFactor.DOB:
        if slots.dob is None:
            raise StateMachineError("verify called without DOB slot")
        if not verify.compare_dob(slots.dob, slots.lookup_response.dob):
            return False, "dob"
    elif factor == SecondaryFactor.AADHAAR_LAST4:
        if slots.aadhaar_last4 is None:
            raise StateMachineError("verify called without aadhaar_last4 slot")
        # ValueError from compare_aadhaar_last4 = malformed extraction (propagates)
        if not verify.compare_aadhaar_last4(
            slots.aadhaar_last4, slots.lookup_response.aadhaar_last4
        ):
            return False, "aadhaar_last4"
    elif factor == SecondaryFactor.PINCODE:
        if slots.pincode is None:
            raise StateMachineError("verify called without pincode slot")
        if not verify.compare_pincode(
            slots.pincode, slots.lookup_response.pincode
        ):
            return False, "pincode"

    return True, None


# ===========================================================================
# Cancel guard (orchestrator-level)
# ===========================================================================


def cancel_guard(
    stage: Stage, intent: Intent, slots: SlotStore, counters: Counters
) -> StepResult | None:
    """Return a routing StepResult if the user's intent is NEGATE in a
    cancellable stage; otherwise None.

    Per DECISIONS #10: cancellation does NOT consume any retry budget. The
    counters are passed through unchanged.
    """
    if intent != Intent.NEGATE:
        return None
    if stage not in CANCELLABLE_STAGES:
        return None
    new_slots = slots.model_copy(
        update={"terminal_cause": f"cancelled_at_{stage.value}"}
    )
    return StepResult(
        next_stage=Stage.TERMINAL_CANCELLED,
        slots=new_slots,
        counters=counters,
        side_effect=SideEffect.NONE,
        message_key="terminal_cancelled",
    )


# ===========================================================================
# Slot merging (with mutability rules)
# ===========================================================================


def merge_extracted_slots(
    *, slots: SlotStore, extracted: ExtractedSlots
) -> SlotStore:
    """Apply the lock-once-filled rule: only fill slots that are currently None.

    Existing slot values are NEVER overwritten by user-stated data. The four
    orchestrator-controlled carve-outs (clear-on-failure) happen in the
    transition functions below by writing None to a slot before re-prompting,
    after which the next merge can fill it again.
    """
    update: dict[str, Any] = {}
    for field_name in extracted.model_fields:
        if field_name == "dob_alternate":
            continue  # signal field, not persisted to slot store
        new_value = getattr(extracted, field_name)
        if new_value is None:
            continue
        existing = getattr(slots, field_name, None)
        if existing is None:
            update[field_name] = new_value
    if not update:
        return slots
    return slots.model_copy(update=update)


# ===========================================================================
# step_on_entry — auto-advance and stage-entry behavior
# ===========================================================================


def step_on_entry(
    stage: Stage, slots: SlotStore, counters: Counters
) -> StepResult:
    """Fire stage-entry behavior. Either auto-advance, request a side effect,
    or stay-and-prompt with the entry message_key.

    For terminal stages, returns the cause-specific terminal message_key.
    For closed, returns the generic re-entry message_key.
    """
    # --- Closed: absorbing state ---
    if stage == Stage.CLOSED:
        return StepResult(
            next_stage=Stage.CLOSED,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="closed_reentry",
        )

    # --- Terminal stages: emit cause-specific copy, no further transition. ---
    if stage in TERMINAL_STAGES:
        return StepResult(
            next_stage=stage,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key=stage.value,  # message key matches stage name (e.g. "terminal_cancelled")
        )

    # --- Recap completed: emit success recap inline AND auto-advance to
    # terminal_completed in the same turn (PLAN §3 row 1: "deliver outcome +
    # recap, then to terminal_completed"). The orchestrator picks up next_stage
    # = TERMINAL_COMPLETED and emits the recap message in this same turn.
    if stage == Stage.RECAP_COMPLETED:
        new_slots = slots.model_copy(
            update={"terminal_cause": Stage.TERMINAL_COMPLETED.value}
        )
        return StepResult(
            next_stage=Stage.TERMINAL_COMPLETED,
            slots=new_slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="recap_success",
        )

    # --- Greeting: just prompt; the next user input transitions out. ---
    if stage == Stage.GREETING:
        return StepResult(
            next_stage=Stage.GREETING,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="greet",
        )

    # --- collecting_account_id: auto-fire CALL_LOOKUP if account_id filled
    # AND well-formed. Carve-out: clear if filled-but-invalid (CRITICAL fix). ---
    if stage == Stage.COLLECTING_ACCOUNT_ID:
        slots, cleared = _clear_invalid_account_id(slots)
        if cleared:
            return StepResult(
                next_stage=Stage.COLLECTING_ACCOUNT_ID,
                slots=slots,
                counters=counters,
                side_effect=SideEffect.NONE,
                message_key="prompt_account_id_invalid_format",
            )
        if slots.account_id is not None:
            return StepResult(
                next_stage=Stage.COLLECTING_ACCOUNT_ID,
                slots=slots,
                counters=counters,
                side_effect=SideEffect.CALL_LOOKUP,
                message_key="lookup_in_progress",
            )
        return StepResult(
            next_stage=Stage.COLLECTING_ACCOUNT_ID,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="prompt_account_id",
        )

    # --- collecting_identity: auto-verify if name+factor complete and unambiguous. ---
    if stage == Stage.COLLECTING_IDENTITY:
        if not _identity_complete(slots):
            return StepResult(
                next_stage=Stage.COLLECTING_IDENTITY,
                slots=slots,
                counters=counters,
                side_effect=SideEffect.NONE,
                message_key="prompt_identity",
            )
        # Run verify in-process.
        # ValueError ONLY = user-input format failure (malformed extraction);
        # caller re-prompts without consuming a retry per DECISIONS #3.
        # StateMachineError = invariant violation (programmer bug); not caught
        # here — it propagates out and crashes loudly so the bug surfaces.
        try:
            passed, failed_field = _verify_identity(slots)
        except ValueError:
            cleared = _clear_secondary_factor_slots(slots)
            return StepResult(
                next_stage=Stage.COLLECTING_IDENTITY,
                slots=cleared,
                counters=counters,
                side_effect=SideEffect.NONE,
                message_key="prompt_identity_malformed",
            )

        if passed:
            new_slots = slots.model_copy(update={"verified": True})
            return StepResult(
                next_stage=Stage.COLLECTING_AMOUNT,
                slots=new_slots,
                counters=counters,
                side_effect=SideEffect.NONE,
                message_key="entry_collect_amount",
            )

        # Verify failed; increment counter, possibly terminate.
        new_count = counters.verification_retries + 1
        new_counters = counters.model_copy(update={"verification_retries": new_count})
        if counters.verification_retries == config.VERIFICATION_RETRY_CAP:
            return _route_terminal(
                stage=Stage.TERMINAL_VERIFICATION_EXHAUSTED,
                slots=slots,
                counters=new_counters,
                message_key="terminal_verification_exhausted",
            )
        # Clear failed slot(s) and re-prompt.
        cleared = _clear_failed_verification_slot(slots, failed_field)
        return StepResult(
            next_stage=Stage.COLLECTING_IDENTITY,
            slots=cleared,
            counters=new_counters,
            side_effect=SideEffect.NONE,
            message_key=f"verify_fail_{failed_field}",
        )

    # --- dob_disambiguation: emit the two-option prompt.
    # NOTE: dob_disamb_retries reset moved to the transition that ROUTES into
    # this stage (step_on_input(COLLECTING_IDENTITY) when dob_alternate is set).
    # Resetting here would zero an in-progress counter on same-stage re-entries.
    if stage == Stage.DOB_DISAMBIGUATION:
        return StepResult(
            next_stage=Stage.DOB_DISAMBIGUATION,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="prompt_dob_disambiguation",
        )

    # --- collecting_amount: auto-advance if amount valid.
    # Carve-out: clear if filled-but-invalid (CRITICAL fix). ---
    if stage == Stage.COLLECTING_AMOUNT:
        slots, cleared = _clear_invalid_amount(slots)
        if cleared:
            return StepResult(
                next_stage=Stage.COLLECTING_AMOUNT,
                slots=slots,
                counters=counters,
                side_effect=SideEffect.NONE,
                message_key="prompt_amount_invalid_format",
            )
        if _amount_valid(slots):
            return StepResult(
                next_stage=Stage.COLLECTING_CARD,
                slots=slots,
                counters=counters,
                side_effect=SideEffect.NONE,
                message_key="entry_collect_card",
            )
        return StepResult(
            next_stage=Stage.COLLECTING_AMOUNT,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="prompt_amount",
        )

    # --- collecting_card: auto-advance if all card fields valid.
    # Carve-out: clear specific invalid fields (CRITICAL fix).
    # NOTE: confirmation_ambig reset on advance happens in the StepResult below;
    # not in step_on_entry(AWAITING_PAYMENT_CONFIRMATION). ---
    if stage == Stage.COLLECTING_CARD:
        slots, cleared_fields = _clear_invalid_card_fields(slots)
        if cleared_fields:
            return StepResult(
                next_stage=Stage.COLLECTING_CARD,
                slots=slots,
                counters=counters,
                side_effect=SideEffect.NONE,
                message_key=f"prompt_card_invalid_{cleared_fields[0]}",
            )
        if _card_complete_and_valid(slots):
            advancing_counters = counters.model_copy(
                update={"confirmation_ambig": 0}
            )
            return StepResult(
                next_stage=Stage.AWAITING_PAYMENT_CONFIRMATION,
                slots=slots,
                counters=advancing_counters,
                side_effect=SideEffect.NONE,
                message_key="entry_confirm_payment",
            )
        return StepResult(
            next_stage=Stage.COLLECTING_CARD,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="prompt_card",
        )

    # --- awaiting_payment_confirmation: emit confirmation.
    # NOTE: confirmation_ambig reset moved to the auto-advance from
    # collecting_card (above). Resetting here would zero an in-progress
    # counter on same-stage re-entries.
    if stage == Stage.AWAITING_PAYMENT_CONFIRMATION:
        return StepResult(
            next_stage=Stage.AWAITING_PAYMENT_CONFIRMATION,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="prompt_payment_confirmation",
        )

    # --- forced_disambiguation: emit forced prompt.
    # NOTE: forced_disamb_retries reset moved to the transition that ROUTES
    # into this stage (step_on_input(AWAITING_PAYMENT_CONFIRMATION) when
    # confirmation_ambig hits cap). ---
    if stage == Stage.FORCED_DISAMBIGUATION:
        return StepResult(
            next_stage=Stage.FORCED_DISAMBIGUATION,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="prompt_forced_disambiguation",
        )

    raise AssertionError(f"unhandled stage in step_on_entry: {stage}")


# ===========================================================================
# step_on_input — handle user input for the current stage
# ===========================================================================


def step_on_input(
    stage: Stage,
    slots: SlotStore,
    counters: Counters,
    extraction: ExtractionResult,
) -> StepResult:
    """Handle a user message in the given stage.

    Slot merging happens BEFORE this function is called by the orchestrator;
    `slots` already reflects the merged state. The cancel guard also fires
    before this function (orchestrator-level).
    """
    # --- Closed and terminals: orchestrator (agent.py) routes terminals
    # to closed BEFORE calling extract+step_on_input on the next turn — that
    # is the primary enforcement point for DECISIONS #23, and it also avoids
    # spending an LLM call on a terminated session. This branch is
    # defense-in-depth: if the orchestrator ever fails to route, we still
    # return the right closed-reentry message rather than crashing.
    if stage == Stage.CLOSED or stage in TERMINAL_STAGES:
        return step_on_entry(Stage.CLOSED, slots, counters)

    # --- Greeting: shed to collecting_account_id. The orchestrator will
    # then run step_on_entry on collecting_account_id, which auto-advances
    # if account_id was already filled. ---
    if stage == Stage.GREETING:
        return StepResult(
            next_stage=Stage.COLLECTING_ACCOUNT_ID,
            slots=slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="greet_advance",
        )

    # --- collecting_account_id: handled entirely by entry auto-advance ---
    # (after merge). If account_id is filled, fire lookup; else prompt.
    if stage == Stage.COLLECTING_ACCOUNT_ID:
        return step_on_entry(stage, slots, counters)

    # --- collecting_identity: route through entry. ---
    # v2 disambiguation: orchestrator (agent.py) detects ambiguity in
    # extraction and pre-populates slots.dob_disamb_primary/alternate
    # WITHOUT merging primary into slots.dob. We route to disambiguation
    # based on the slot fields (single source of truth at this point).
    # The legacy extraction-based check is preserved as a defensive
    # fallback for callers that bypass the orchestrator (state.py tests).
    if stage == Stage.COLLECTING_IDENTITY:
        v2_routing = (
            slots.dob_disamb_primary is not None
            and slots.dob_disamb_alternate is not None
        )
        v1_fallback = extraction.slots.dob_alternate is not None
        if v2_routing or v1_fallback:
            new_counters = counters.model_copy(update={"dob_disamb_retries": 0})
            new_slots = slots
            if v1_fallback and not v2_routing:
                # Caller skipped orchestrator pre-merge — populate disamb
                # slots from extraction and un-merge any primary that
                # merge_extracted_slots wrote into slots.dob.
                new_slots = slots.model_copy(update={
                    "dob_disamb_primary": extraction.slots.dob,
                    "dob_disamb_alternate": extraction.slots.dob_alternate,
                    "dob": None,
                })
            return StepResult(
                next_stage=Stage.DOB_DISAMBIGUATION,
                slots=new_slots,
                counters=new_counters,
                side_effect=SideEffect.NONE,
                message_key="prompt_dob_disambiguation",
            )
        return step_on_entry(stage, slots, counters)

    # --- dob_disambiguation: v2 two-option resolution.
    # Resolution order:
    #   (a) Extracted DOB whose (month, day) matches one of the two stored
    #       readings → choose that reading (handles "April 5" without year:
    #       extracted (4, 5) matches primary's (4, 5) → primary wins, year
    #       comes from the original ambiguous input).
    #   (b) Extracted DOB matching neither reading → carve-out: user is
    #       providing a different date entirely, write through.
    #   (c) AFFIRM intent (no extracted dob) → backward-compat: choose the
    #       primary reading. Documented in the prompt copy as "reply with
    #       the correct date" but accepts "yes" gracefully.
    #   (d) AMBIGUOUS / unrecognized → cap-edge re-prompt.
    #   (NEGATE handled by cancel_guard upstream.)
    if stage == Stage.DOB_DISAMBIGUATION:
        primary = slots.dob_disamb_primary
        alternate = slots.dob_disamb_alternate
        extracted_dob = extraction.slots.dob

        chosen: date | None = None
        if extracted_dob is not None:
            if primary is not None and (extracted_dob.month, extracted_dob.day) == (primary.month, primary.day):
                chosen = primary
            elif alternate is not None and (extracted_dob.month, extracted_dob.day) == (alternate.month, alternate.day):
                chosen = alternate
            else:
                # Carve-out: different date entirely
                chosen = extracted_dob
        elif extraction.intent == Intent.AFFIRM and primary is not None:
            # Backward-compat: AFFIRM resolves to primary.
            chosen = primary

        if chosen is not None:
            new_slots = slots.model_copy(update={
                "dob": chosen,
                "dob_disamb_primary": None,
                "dob_disamb_alternate": None,
            })
            return StepResult(
                next_stage=Stage.COLLECTING_IDENTITY,
                slots=new_slots,
                counters=counters,
                side_effect=SideEffect.NONE,
                message_key="dob_resolved",
            )

        # Unrecognized response → cap-edge logic.
        new_count = counters.dob_disamb_retries + 1
        new_counters = counters.model_copy(update={"dob_disamb_retries": new_count})
        if counters.dob_disamb_retries == config.DOB_DISAMB_RETRY_CAP:
            return _route_terminal(
                stage=Stage.TERMINAL_CANCELLED,
                slots=slots,
                counters=new_counters,
                message_key="terminal_cancelled_dob_disamb",
            )
        return StepResult(
            next_stage=Stage.DOB_DISAMBIGUATION,
            slots=slots,
            counters=new_counters,
            side_effect=SideEffect.NONE,
            message_key="reprompt_dob_disambiguation",
        )

    # --- collecting_amount: route through entry ---
    if stage == Stage.COLLECTING_AMOUNT:
        return step_on_entry(stage, slots, counters)

    # --- collecting_card: route through entry ---
    if stage == Stage.COLLECTING_CARD:
        return step_on_entry(stage, slots, counters)

    # --- awaiting_payment_confirmation: dispatch on intent ---
    if stage == Stage.AWAITING_PAYMENT_CONFIRMATION:
        if extraction.intent == Intent.AFFIRM:
            return StepResult(
                next_stage=Stage.AWAITING_PAYMENT_CONFIRMATION,
                slots=slots,
                counters=counters,
                side_effect=SideEffect.CALL_PROCESS_PAYMENT,
                message_key="payment_in_progress",
            )
        # AMBIGUOUS (NEGATE was caught by cancel_guard upstream)
        new_count = counters.confirmation_ambig + 1
        new_counters = counters.model_copy(update={"confirmation_ambig": new_count})
        if counters.confirmation_ambig == config.CONFIRMATION_AMBIGUOUS_CAP:
            # Reset forced_disamb_retries on the routing transition (was moved
            # out of step_on_entry per MEDIUM #1).
            forced_entry_counters = new_counters.model_copy(
                update={"forced_disamb_retries": 0}
            )
            return StepResult(
                next_stage=Stage.FORCED_DISAMBIGUATION,
                slots=slots,
                counters=forced_entry_counters,
                side_effect=SideEffect.NONE,
                message_key="prompt_forced_disambiguation",
            )
        return StepResult(
            next_stage=Stage.AWAITING_PAYMENT_CONFIRMATION,
            slots=slots,
            counters=new_counters,
            side_effect=SideEffect.NONE,
            message_key="reprompt_payment_confirmation",
        )

    # --- forced_disambiguation: same dispatch as awaiting_payment_confirmation
    # but with its own cap and counter ---
    if stage == Stage.FORCED_DISAMBIGUATION:
        if extraction.intent == Intent.AFFIRM:
            return StepResult(
                next_stage=Stage.FORCED_DISAMBIGUATION,
                slots=slots,
                counters=counters,
                side_effect=SideEffect.CALL_PROCESS_PAYMENT,
                message_key="payment_in_progress",
            )
        new_count = counters.forced_disamb_retries + 1
        new_counters = counters.model_copy(
            update={"forced_disamb_retries": new_count}
        )
        if counters.forced_disamb_retries == config.FORCED_DISAMB_RETRY_CAP:
            return _route_terminal(
                stage=Stage.TERMINAL_CANCELLED,
                slots=slots,
                counters=new_counters,
                message_key="terminal_cancelled_forced_disamb",
            )
        return StepResult(
            next_stage=Stage.FORCED_DISAMBIGUATION,
            slots=slots,
            counters=new_counters,
            side_effect=SideEffect.NONE,
            message_key="reprompt_forced_disambiguation",
        )

    # --- recap_completed: any next() call after recap → terminal_completed.
    # Orchestrator should have already routed terminal → closed before us.
    if stage == Stage.RECAP_COMPLETED:
        return _route_terminal(
            stage=Stage.TERMINAL_COMPLETED,
            slots=slots,
            counters=counters,
            message_key="terminal_completed",
        )

    raise AssertionError(f"unhandled stage in step_on_input: {stage}")


# ===========================================================================
# step_on_effect — handle side-effect callback
# ===========================================================================


def step_on_effect(
    stage: Stage,
    slots: SlotStore,
    counters: Counters,
    effect_result: EffectResult,
) -> StepResult:
    """Handle the result of a side effect (CALL_LOOKUP or CALL_PROCESS_PAYMENT).

    Dispatches on (stage, effect_result type).
    """
    # --- LOOKUP result for collecting_account_id ---
    if (
        stage == Stage.COLLECTING_ACCOUNT_ID
        and isinstance(effect_result, LookupEffectResult)
    ):
        return _handle_lookup_result(slots, counters, effect_result)

    # --- PAYMENT result for awaiting_payment_confirmation or forced_disambiguation ---
    if (
        stage in (Stage.AWAITING_PAYMENT_CONFIRMATION, Stage.FORCED_DISAMBIGUATION)
        and isinstance(effect_result, PaymentEffectResult)
    ):
        return _handle_payment_result(slots, counters, effect_result)

    raise AssertionError(
        f"unhandled (stage, effect_result) in step_on_effect: "
        f"({stage}, {type(effect_result).__name__})"
    )


def _handle_lookup_result(
    slots: SlotStore, counters: Counters, result: LookupEffectResult
) -> StepResult:
    if result.outcome == LookupOutcome.SUCCESS:
        new_slots = slots.model_copy(update={"lookup_response": result.account_data})
        return StepResult(
            next_stage=Stage.COLLECTING_IDENTITY,
            slots=new_slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="entry_collect_identity",
        )

    # Increment lookup_retries; on cap, terminate.
    new_count = counters.lookup_retries + 1
    new_counters = counters.model_copy(update={"lookup_retries": new_count})

    if counters.lookup_retries == config.LOOKUP_RETRY_CAP:
        return _route_terminal(
            stage=Stage.TERMINAL_ACCOUNT_NOT_FOUND,
            slots=slots,
            counters=new_counters,
            message_key="terminal_account_not_found",
        )

    if result.outcome == LookupOutcome.ACCOUNT_NOT_FOUND:
        # Carve-out #3: clear account_id so user can submit a different one.
        cleared = slots.model_copy(update={"account_id": None})
        return StepResult(
            next_stage=Stage.COLLECTING_ACCOUNT_ID,
            slots=cleared,
            counters=new_counters,
            side_effect=SideEffect.NONE,
            message_key="lookup_not_found_retry",
        )

    # TRANSIENT: keep account_id (the value may be correct; infra failed).
    return StepResult(
        next_stage=Stage.COLLECTING_ACCOUNT_ID,
        slots=slots,
        counters=new_counters,
        side_effect=SideEffect.NONE,
        message_key="lookup_transient_retry",
    )


def _handle_payment_result(
    slots: SlotStore, counters: Counters, result: PaymentEffectResult
) -> StepResult:
    if result.outcome == PaymentOutcome.SUCCESS:
        # Compute session-tracked remaining balance per DECISIONS #7.
        remaining = _compute_remaining_balance(slots)
        new_slots = slots.model_copy(
            update={
                "transaction_id": result.transaction_id,
                "remaining_balance": remaining,
            }
        )
        return StepResult(
            next_stage=Stage.RECAP_COMPLETED,
            slots=new_slots,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="recap_success",
        )

    if result.outcome == PaymentOutcome.INSUFFICIENT_BALANCE:
        # No counter; clear amount (carve-out: orchestrator clears stale amount).
        cleared = slots.model_copy(update={"amount": None})
        return StepResult(
            next_stage=Stage.COLLECTING_AMOUNT,
            slots=cleared,
            counters=counters,
            side_effect=SideEffect.NONE,
            message_key="insufficient_balance_reprompt",
        )

    if result.outcome == PaymentOutcome.INVALID_AMOUNT:
        # DECISIONS #4: validators must catch this client-side. If we see it,
        # treat as a defensive bug — route to terminal_payment_unknown rather
        # than try to recover, since the API state is suspect.
        return _route_terminal(
            stage=Stage.TERMINAL_PAYMENT_UNKNOWN,
            slots=slots,
            counters=counters,
            message_key="terminal_payment_unknown",
        )

    if result.outcome == PaymentOutcome.UNKNOWN:
        return _route_terminal(
            stage=Stage.TERMINAL_PAYMENT_UNKNOWN,
            slots=slots,
            counters=counters,
            message_key="terminal_payment_unknown",
        )

    if result.outcome in _TYPO_CLASS_OUTCOMES:
        new_count = counters.payment_retries + 1
        new_counters = counters.model_copy(update={"payment_retries": new_count})
        if counters.payment_retries == config.PAYMENT_TYPO_RETRY_CAP:
            return _route_terminal(
                stage=Stage.TERMINAL_PAYMENT_EXHAUSTED,
                slots=slots,
                counters=new_counters,
                message_key="terminal_payment_exhausted",
            )
        # Carve-out #1: clear the named failed card field.
        cleared = _clear_failed_card_field(slots, result.outcome)
        return StepResult(
            next_stage=Stage.COLLECTING_CARD,
            slots=cleared,
            counters=new_counters,
            side_effect=SideEffect.NONE,
            message_key=f"reprompt_{result.outcome.value}",
        )

    raise AssertionError(f"unhandled PaymentOutcome: {result.outcome}")


# ===========================================================================
# Orchestrator-controlled slot-clearing helpers (4 carve-outs per DECISIONS #21)
# ===========================================================================


def _clear_failed_card_field(slots: SlotStore, outcome: PaymentOutcome) -> SlotStore:
    """Carve-out #1: clear the specific card field named by the API error."""
    if outcome == PaymentOutcome.INVALID_CARD:
        return slots.model_copy(update={"pan": None})
    if outcome == PaymentOutcome.INVALID_CVV:
        return slots.model_copy(update={"cvv": None})
    if outcome == PaymentOutcome.INVALID_EXPIRY:
        return slots.model_copy(update={"expiry_month": None, "expiry_year": None})
    return slots


def _clear_failed_verification_slot(
    slots: SlotStore, failed_field: str | None
) -> SlotStore:
    """Carve-out #4: clear the failed name and/or factor slot."""
    if failed_field == "name":
        return slots.model_copy(update={"full_name": None})
    if failed_field == "dob":
        return slots.model_copy(update={"dob": None})
    if failed_field == "aadhaar_last4":
        return slots.model_copy(update={"aadhaar_last4": None})
    if failed_field == "pincode":
        return slots.model_copy(update={"pincode": None})
    return slots


def _clear_secondary_factor_slots(slots: SlotStore) -> SlotStore:
    """Used when extractor produced a malformed factor — clear all factor slots
    so the user can re-submit cleanly. selected_secondary_factor stays in case
    the user already chose one."""
    return slots.model_copy(update={"dob": None, "aadhaar_last4": None, "pincode": None})


# ===========================================================================
# Derived helpers
# ===========================================================================


def _compute_remaining_balance(slots: SlotStore) -> Decimal | None:
    """Session-tracked remaining = (balance from lookup) - amount.

    Returns None if either input is missing.
    """
    if slots.lookup_response is None or slots.amount is None:
        return None
    return slots.lookup_response.balance - slots.amount
