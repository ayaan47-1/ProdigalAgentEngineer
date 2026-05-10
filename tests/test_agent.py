"""Wiring tests for ``Agent``.

Scope per user calibration: 25-30 tests focused on orchestrator wiring
(does extract get called with correct args; does cancel guard fire; does
the right side effect dispatch; does the message reach templates with
the correct message_key; does pre-extract terminal routing skip the LLM).

NOT in scope:
- Re-testing state machine transitions (``test_state_transitions.py``
  has 124 tests covering every cap edge, every carve-out, every stage).
- Re-testing API behavior (``test_api.py`` has 30 tests covering
  request shape, error mapping, retry policy, redacted logging).
- Re-testing extraction quality (``eval/extractor_eval.py`` is the
  authoritative quality measure).
- Re-testing template content (``test_templates.py`` has 92+ tests).

Strategy:
- Real ``state`` + ``templates`` (the deterministic kernel — exercising it
  is what proves the wiring works).
- Mocked ``extract_turn`` (controlled per-turn extraction) and mocked
  ``api.lookup_account`` / ``api.process_payment`` (controlled effect
  outcomes). This isolates wiring from LLM cost and HTTP behavior.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Iterator

import pytest

from payment_agent import agent as agent_module
from payment_agent.agent import Agent
from payment_agent.errors import LlmCallFailed
from payment_agent.state import (
    Counters,
    ExtractedSlots,
    ExtractionResult,
    Intent,
    LookupEffectResult,
    LookupOutcome,
    LookupResponse,
    PaymentEffectResult,
    PaymentOutcome,
    SlotStore,
    Stage,
)
from payment_agent.verify import SecondaryFactor


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _result(intent: Intent = Intent.AMBIGUOUS, **slots: Any) -> ExtractionResult:
    """Helper: build an ExtractionResult from kwargs naming filled slots."""
    return ExtractionResult(slots=ExtractedSlots(**slots), intent=intent)


def _lookup_success(
    *,
    account_id: str = "ACC1001",
    full_name: str = "Nithin Jain",
    dob: date = date(1990, 5, 14),
    aadhaar_last4: str = "1234",
    pincode: str = "560001",
    balance: Decimal = Decimal("1250.75"),
) -> LookupEffectResult:
    return LookupEffectResult(
        outcome=LookupOutcome.SUCCESS,
        account_data=LookupResponse(
            account_id=account_id,
            full_name=full_name,
            dob=dob,
            aadhaar_last4=aadhaar_last4,
            pincode=pincode,
            balance=balance,
        ),
    )


def _payment_success(txn: str = "TXN-001") -> PaymentEffectResult:
    return PaymentEffectResult(outcome=PaymentOutcome.SUCCESS, transaction_id=txn)


@pytest.fixture
def patched_agent(monkeypatch):
    """Provides a builder for an Agent with stubbed extract and api.

    Usage:
        a, calls = patched_agent(
            extractions=[_result(Intent.AMBIGUOUS, account_id="ACC1001"), ...],
            lookups=[_lookup_success(), ...],
            payments=[_payment_success(), ...],
        )
    """
    def build(
        *,
        extractions: list[ExtractionResult] | None = None,
        lookups: list[LookupEffectResult] | None = None,
        payments: list[PaymentEffectResult] | None = None,
        extract_raises: BaseException | None = None,
    ) -> tuple[Agent, dict[str, list]]:
        calls: dict[str, list] = {
            "extract": [], "lookup": [], "payment": [],
        }

        ex_iter: Iterator[ExtractionResult] = iter(extractions or [])

        def fake_extract(user_input, stage, slots, *, api_key=None):
            calls["extract"].append({
                "user_input": user_input,
                "stage": stage,
                "slots_snapshot": slots.model_dump(),
                "api_key": api_key,
            })
            if extract_raises is not None:
                raise extract_raises
            try:
                return next(ex_iter)
            except StopIteration:
                return ExtractionResult(
                    slots=ExtractedSlots(), intent=Intent.AMBIGUOUS,
                )

        lookup_iter: Iterator[LookupEffectResult] = iter(lookups or [])

        def fake_lookup(account_id, *, client=None):
            calls["lookup"].append({"account_id": account_id})
            return next(lookup_iter)

        payment_iter: Iterator[PaymentEffectResult] = iter(payments or [])

        def fake_payment(*, client=None, **kwargs):
            calls["payment"].append(kwargs)
            return next(payment_iter)

        monkeypatch.setattr(agent_module, "extract_turn", fake_extract)
        monkeypatch.setattr(agent_module.api, "lookup_account", fake_lookup)
        monkeypatch.setattr(agent_module.api, "process_payment", fake_payment)

        return Agent(api_key="test-key"), calls

    return build


# ---------------------------------------------------------------------------
# Happy path end-to-end (1 test)
# ---------------------------------------------------------------------------


def test_happy_path_full_payment_cycle(patched_agent):
    """Greeting → account_id → lookup success → identity verify (DOB
    factor) → amount → card → confirm AFFIRM → payment success → recap →
    closed. End-to-end orchestration through the deterministic kernel
    with mocked extract + api."""
    a, calls = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain", dob=date(1990, 5, 14),
                    selected_secondary_factor=SecondaryFactor.DOB),
            _result(amount=Decimal("500")),
            _result(pan="4532015112830366", cvv="123",
                    expiry_month=12, expiry_year=2027),
            _result(intent=Intent.AFFIRM),  # confirmation gate
        ],
        lookups=[_lookup_success()],
        payments=[_payment_success("TXN-XYZ")],
    )

    out1 = a.next("ACC1001")
    out2 = a.next("Nithin Jain, DOB 14-05-1990")
    out3 = a.next("500")
    out4 = a.next("card 4532015112830366 cvv 123 exp 12/27")
    out5 = a.next("yes")

    # Each turn returns the spec-required dict shape.
    for out in (out1, out2, out3, out4, out5):
        assert isinstance(out, dict) and "message" in out
        assert isinstance(out["message"], str) and out["message"]

    # Lookup fired exactly once with the right account; payment fired once.
    assert calls["lookup"] == [{"account_id": "ACC1001"}]
    assert len(calls["payment"]) == 1
    assert calls["payment"][0]["account_id"] == "ACC1001"
    assert calls["payment"][0]["amount"] == Decimal("500")
    assert calls["payment"][0]["full_name"] == "Nithin Jain"

    # Final state is the success terminal; recap message persisted via slots.
    assert a._stage == Stage.TERMINAL_COMPLETED
    assert a._slots.transaction_id == "TXN-XYZ"
    assert a._slots.verified is True


# ---------------------------------------------------------------------------
# Verification exhaustion (1 test)
# ---------------------------------------------------------------------------


def test_verification_exhaustion_routes_to_terminal(patched_agent):
    """3 wrong DOBs (cap=3) → terminal_verification_exhausted on the 4th
    submission. State machine owns the cap; we just verify wiring."""
    wrong = date(2000, 1, 1)
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain", dob=wrong,
                    selected_secondary_factor=SecondaryFactor.DOB),
            _result(dob=wrong, selected_secondary_factor=SecondaryFactor.DOB),
            _result(dob=wrong, selected_secondary_factor=SecondaryFactor.DOB),
            _result(dob=wrong, selected_secondary_factor=SecondaryFactor.DOB),
        ],
        lookups=[_lookup_success()],
    )
    a.next("ACC1001")
    a.next("Nithin Jain dob wrong")
    a.next("wrong again")
    a.next("third")
    out = a.next("fourth — should terminate")
    assert a._stage == Stage.TERMINAL_VERIFICATION_EXHAUSTED
    # Terminal-specific keyword, NOT "session" (which appears in the
    # generic closed-reentry message and would hide a wrong-key bug).
    msg = out["message"].lower()
    assert "verify" in msg or "support" in msg or "exhausted" in msg, (
        f"verification-exhausted message must mention verify/support/exhausted; "
        f"got: {out['message']!r}"
    )


# ---------------------------------------------------------------------------
# Payment typo recovery (1 test)
# ---------------------------------------------------------------------------


def test_payment_typo_recovery_clears_failed_field_and_succeeds(patched_agent):
    """Bad CVV → typo-class outcome → re-prompt for CVV → good CVV → success."""
    a, calls = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain", dob=date(1990, 5, 14),
                    selected_secondary_factor=SecondaryFactor.DOB),
            _result(amount=Decimal("500")),
            _result(pan="4532015112830366", cvv="999",
                    expiry_month=12, expiry_year=2027),
            _result(intent=Intent.AFFIRM),
            _result(cvv="123"),
            _result(intent=Intent.AFFIRM),
        ],
        lookups=[_lookup_success()],
        payments=[
            PaymentEffectResult(outcome=PaymentOutcome.INVALID_CVV),
            _payment_success("TXN-OK"),
        ],
    )
    a.next("ACC1001")
    a.next("identity")
    a.next("500")
    a.next("card with bad CVV")
    a.next("yes")  # confirm → payment INVALID_CVV → carve-out clears CVV
    a.next("123")  # new CVV
    a.next("yes")  # confirm again → success
    assert a._stage == Stage.TERMINAL_COMPLETED
    assert len(calls["payment"]) == 2


# ---------------------------------------------------------------------------
# Leap-year ACC1004 (1 test)
# ---------------------------------------------------------------------------


def test_leap_year_acc1004_dob_carries_through(patched_agent):
    """ACC1004 holder DOB 1988-02-29 (valid leap-year date) carries through
    extraction → merge → verify path without strict-parse rejection."""
    leap_dob = date(1988, 2, 29)
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1004"),
            _result(full_name="Vivek Joshi", dob=leap_dob,
                    selected_secondary_factor=SecondaryFactor.DOB),
        ],
        lookups=[_lookup_success(
            account_id="ACC1004", full_name="Vivek Joshi", dob=leap_dob,
        )],
    )
    a.next("ACC1004")
    a.next("Vivek Joshi DOB 29-02-1988")
    assert a._slots.dob == leap_dob
    assert a._slots.verified is True
    assert a._stage == Stage.COLLECTING_AMOUNT


# ---------------------------------------------------------------------------
# Cancellation (3 tests covering different cancellable stages)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage_name,setup_extractions,setup_lookups",
    [
        # Cancel at greeting (first turn negate)
        ("greeting",
         [_result(intent=Intent.NEGATE)],
         []),
        # Cancel during collecting_identity (after lookup succeeds)
        ("collecting_identity",
         [_result(account_id="ACC1001"), _result(intent=Intent.NEGATE)],
         [_lookup_success()]),
        # Cancel during awaiting_payment_confirmation
        ("awaiting_payment_confirmation",
         [
             _result(account_id="ACC1001"),
             _result(full_name="Nithin Jain", dob=date(1990, 5, 14),
                     selected_secondary_factor=SecondaryFactor.DOB),
             _result(amount=Decimal("500")),
             _result(pan="4532015112830366", cvv="123",
                     expiry_month=12, expiry_year=2027),
             _result(intent=Intent.NEGATE),
         ],
         [_lookup_success()]),
    ],
)
def test_negate_in_cancellable_stages_routes_to_terminal_cancelled(
    patched_agent, stage_name, setup_extractions, setup_lookups,
):
    """Cancel guard fires for every cancellable stage; cancellation never
    consumes retry budget (DECISIONS #10)."""
    a, _ = patched_agent(
        extractions=setup_extractions, lookups=setup_lookups,
    )
    for _ in setup_extractions:
        a.next("input")
    assert a._stage == Stage.TERMINAL_CANCELLED
    assert a._slots.terminal_cause == f"cancelled_at_{stage_name}"
    # Counters are unchanged from the cancel — no retry consumed.
    assert a._counters.verification_retries == 0
    assert a._counters.payment_retries == 0


# ---------------------------------------------------------------------------
# Out-of-order slot fill (1 test)
# ---------------------------------------------------------------------------


def test_out_of_order_slot_fill_carries_volunteered_info(patched_agent):
    """User volunteers everything in the greeting; slots persist and the
    flow auto-advances through stages without re-asking."""
    a, calls = patched_agent(
        extractions=[
            _result(account_id="ACC1001",
                    full_name="Nithin Jain",
                    dob=date(1990, 5, 14),
                    selected_secondary_factor=SecondaryFactor.DOB),
        ],
        lookups=[_lookup_success()],
    )
    a.next("ACC1001 name Nithin Jain DOB 14-05-1990")
    # Should auto-advance through greeting → lookup → identity verify → amount.
    assert a._slots.account_id == "ACC1001"
    assert a._slots.full_name == "Nithin Jain"
    assert a._slots.verified is True
    assert a._stage == Stage.COLLECTING_AMOUNT
    assert calls["lookup"] == [{"account_id": "ACC1001"}]


# ---------------------------------------------------------------------------
# Re-entry post-terminal (1 test)
# ---------------------------------------------------------------------------


def test_post_terminal_returns_generic_reentry_without_extract(
    patched_agent,
):
    """Once at a terminal stage, subsequent next() calls return the
    generic closed re-entry message and do NOT invoke extract (DECISIONS
    #23 + cost guard)."""
    a, calls = patched_agent(extractions=[_result(intent=Intent.NEGATE)])
    a.next("cancel")
    assert a._stage == Stage.TERMINAL_CANCELLED

    # Next call: should route terminal → closed inline, no LLM.
    extract_count_before = len(calls["extract"])
    out = a.next("hello again")
    assert a._stage == Stage.CLOSED
    assert "session has ended" in out["message"].lower() or "new session" in out["message"].lower() or "ended" in out["message"].lower()
    assert len(calls["extract"]) == extract_count_before  # no new extract


# ---------------------------------------------------------------------------
# LlmCallFailed graceful + no-key (2 tests)
# ---------------------------------------------------------------------------


def test_llm_call_failed_degrades_to_ambiguous_reprompt(patched_agent):
    """SDK-level failures from extract.extract_turn (auth/rate-limit/
    transient) degrade to AMBIGUOUS+empty so the orchestrator re-prompts.
    No exception escapes to the caller."""
    a, _ = patched_agent(extract_raises=LlmCallFailed("simulated SDK 500"))
    out = a.next("anything")
    # Should not raise; should emit a prompt-style message.
    assert isinstance(out["message"], str) and out["message"]
    # Stage should not be terminal (graceful, not crash).
    assert a._stage not in {Stage.TERMINAL_CANCELLED, Stage.CLOSED}


def test_no_api_key_routes_through_extract_fail_soft(monkeypatch):
    """Agent constructed without api_key plumbs None to extract_turn,
    which takes the no-key fail-soft regex path. Verify the api_key arg
    is plumbed through (not silently substituted)."""
    captured: dict[str, Any] = {}

    def fake_extract(user_input, stage, slots, *, api_key=None):
        captured["api_key"] = api_key
        return ExtractionResult(
            slots=ExtractedSlots(account_id="ACC1001"),
            intent=Intent.AMBIGUOUS,
        )

    monkeypatch.setattr(agent_module, "extract_turn", fake_extract)
    monkeypatch.setattr(
        agent_module.api, "lookup_account",
        lambda account_id, *, client=None: _lookup_success(),
    )

    a = Agent()  # no api_key
    a.next("ACC1001")
    assert captured["api_key"] is None


# ---------------------------------------------------------------------------
# Side-effect loop (3 tests)
# ---------------------------------------------------------------------------


def test_lookup_success_advances_to_identity_collection(patched_agent):
    """CALL_LOOKUP success → step_on_effect routes to COLLECTING_IDENTITY
    with lookup_response cached on slots."""
    a, calls = patched_agent(
        extractions=[_result(account_id="ACC1001")],
        lookups=[_lookup_success()],
    )
    a.next("ACC1001")
    assert a._stage == Stage.COLLECTING_IDENTITY
    assert a._slots.lookup_response is not None
    assert a._slots.lookup_response.account_id == "ACC1001"
    assert calls["lookup"] == [{"account_id": "ACC1001"}]


def test_lookup_account_not_found_clears_slot_and_increments_counter(
    patched_agent,
):
    """ACCOUNT_NOT_FOUND outcome clears account_id (carve-out #3) and
    increments lookup_retries; agent stays in COLLECTING_ACCOUNT_ID."""
    a, _ = patched_agent(
        extractions=[_result(account_id="ACC9999")],
        lookups=[LookupEffectResult(outcome=LookupOutcome.ACCOUNT_NOT_FOUND)],
    )
    a.next("ACC9999")
    assert a._stage == Stage.COLLECTING_ACCOUNT_ID
    assert a._slots.account_id is None  # cleared by carve-out
    assert a._counters.lookup_retries == 1


def test_payment_success_advances_through_recap_to_terminal_completed(
    patched_agent,
):
    """Payment success → step_on_effect → recap_completed → step_on_entry
    auto-advances to TERMINAL_COMPLETED with message_key='recap_success'
    (NOT 'terminal_completed' — the loop must not re-enter terminal stages
    and overwrite the recap key per TODO.md)."""
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain", dob=date(1990, 5, 14),
                    selected_secondary_factor=SecondaryFactor.DOB),
            _result(amount=Decimal("500")),
            _result(pan="4532015112830366", cvv="123",
                    expiry_month=12, expiry_year=2027),
            _result(intent=Intent.AFFIRM),
        ],
        lookups=[_lookup_success()],
        payments=[_payment_success("TXN-RECAP")],
    )
    a.next("ACC1001")
    a.next("identity")
    a.next("500")
    a.next("card")
    out = a.next("yes")
    # The user-visible message must be the recap (which mentions the txn id),
    # not the generic terminal_completed message.
    assert "TXN-RECAP" in out["message"]
    assert a._stage == Stage.TERMINAL_COMPLETED


# ---------------------------------------------------------------------------
# Cancel guard wiring (1 test)
# ---------------------------------------------------------------------------


def test_cancel_guard_fires_before_step_on_input(patched_agent):
    """NEGATE intent in a cancellable stage must route to terminal_cancelled
    BEFORE step_on_input runs (otherwise step_on_input could interpret the
    same input as a slot-fill and spend an LLM call worth of orchestration)."""
    # Set up: user says "cancel" in collecting_identity. Lookup already
    # succeeded on first turn.
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(intent=Intent.NEGATE),
        ],
        lookups=[_lookup_success()],
    )
    a.next("ACC1001")
    assert a._stage == Stage.COLLECTING_IDENTITY
    a.next("cancel")
    assert a._stage == Stage.TERMINAL_CANCELLED
    # No identity slots got filled by a stray slot-fill path.
    assert a._slots.full_name is None
    assert a._slots.dob is None


# ---------------------------------------------------------------------------
# Pre-extract terminal routing (2 tests)
# ---------------------------------------------------------------------------


def test_pre_extract_routing_skips_extract_when_stage_is_terminal(monkeypatch):
    """If the agent is left in a terminal_* stage and next() is called, the
    orchestrator routes to CLOSED and emits the generic re-entry without
    calling extract_turn (DECISIONS #23 + cost guard)."""
    extract_called = {"count": 0}

    def fake_extract(*args, **kwargs):
        extract_called["count"] += 1
        return ExtractionResult(slots=ExtractedSlots(), intent=Intent.AMBIGUOUS)

    monkeypatch.setattr(agent_module, "extract_turn", fake_extract)
    a = Agent()
    a._stage = Stage.TERMINAL_CANCELLED
    out = a.next("anything")
    assert extract_called["count"] == 0
    assert a._stage == Stage.CLOSED
    assert out["message"]


def test_pre_extract_routing_keeps_closed_closed(monkeypatch):
    """A second call after re-entry stays in CLOSED and continues to skip
    extract."""
    monkeypatch.setattr(
        agent_module, "extract_turn",
        lambda *a, **k: pytest.fail("extract must not be called in CLOSED"),
    )
    a = Agent()
    a._stage = Stage.CLOSED
    out = a.next("anything")
    assert a._stage == Stage.CLOSED
    assert out["message"]


# ---------------------------------------------------------------------------
# Auto-advance terminal stop (1 test)
# ---------------------------------------------------------------------------


def test_auto_advance_loop_stops_at_terminal_to_preserve_message_key(
    patched_agent,
):
    """RECAP_COMPLETED → step_on_entry advances to TERMINAL_COMPLETED
    with message_key='recap_success'. The orchestrator must NOT then call
    step_on_entry again on TERMINAL_COMPLETED (which would overwrite with
    'terminal_completed')."""
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain", dob=date(1990, 5, 14),
                    selected_secondary_factor=SecondaryFactor.DOB),
            _result(amount=Decimal("500")),
            _result(pan="4532015112830366", cvv="123",
                    expiry_month=12, expiry_year=2027),
            _result(intent=Intent.AFFIRM),
        ],
        lookups=[_lookup_success()],
        payments=[_payment_success("TXN-AUTO")],
    )
    a.next("ACC1001")
    a.next("identity")
    a.next("500")
    a.next("card")
    out = a.next("yes")
    # Recap content (which mentions txn id) — proof message_key was preserved.
    assert "TXN-AUTO" in out["message"]
    assert a._slots.transaction_id == "TXN-AUTO"


# ---------------------------------------------------------------------------
# Wiring assertions (5 tests)
# ---------------------------------------------------------------------------


def test_extract_called_with_current_stage_and_slots_snapshot(patched_agent):
    """Each turn calls extract_turn with the agent's current stage and
    slots BEFORE merging the new extraction."""
    a, calls = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain"),
        ],
        lookups=[_lookup_success()],
    )
    a.next("ACC1001")
    a.next("Nithin Jain")
    # Second call: stage should be COLLECTING_IDENTITY, slots include
    # account_id from previous turn.
    second = calls["extract"][1]
    assert second["stage"] == Stage.COLLECTING_IDENTITY
    assert second["slots_snapshot"]["account_id"] == "ACC1001"


def test_api_key_plumbed_to_extract_each_turn(patched_agent):
    """Agent's api_key flows through to extract_turn on every call."""
    a, calls = patched_agent(
        extractions=[_result(account_id="ACC1001")],
        lookups=[_lookup_success()],
    )
    a.next("ACC1001")
    assert calls["extract"][0]["api_key"] == "test-key"


def test_fresh_agent_resets_counters_and_slots():
    """Fresh Agent() instances start with zero counters and empty slots
    (DECISIONS #11: per-cycle reset is via fresh instantiation)."""
    a = Agent()
    assert a._counters == Counters()
    assert a._slots == SlotStore()
    assert a._stage == Stage.GREETING


def test_payment_call_args_built_from_slots(patched_agent):
    """The orchestrator passes slot values to api.process_payment using
    the locked field names; full_name (not name_on_card) plumbs to the
    cardholder boundary per Step 6 design."""
    a, calls = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain", dob=date(1990, 5, 14),
                    selected_secondary_factor=SecondaryFactor.DOB),
            _result(amount=Decimal("500")),
            _result(pan="4532015112830366", cvv="123",
                    expiry_month=12, expiry_year=2027),
            _result(intent=Intent.AFFIRM),
        ],
        lookups=[_lookup_success()],
        payments=[_payment_success()],
    )
    for inp in ("ACC1001", "id", "500", "card", "yes"):
        a.next(inp)
    pay = calls["payment"][0]
    assert pay["account_id"] == "ACC1001"
    assert pay["amount"] == Decimal("500")
    assert pay["pan"] == "4532015112830366"
    assert pay["cvv"] == "123"
    assert pay["expiry_month"] == 12
    assert pay["expiry_year"] == 2027
    assert pay["full_name"] == "Nithin Jain"


def test_returned_message_is_dict_with_string_message_key(patched_agent):
    """Every turn returns the spec-required ``{"message": str}`` shape;
    no extra keys, no nested data, just the rendered string."""
    a, _ = patched_agent(extractions=[_result(intent=Intent.AMBIGUOUS)])
    out = a.next("hello")
    assert isinstance(out, dict)
    assert set(out.keys()) == {"message"}
    assert isinstance(out["message"], str)
    assert out["message"]  # non-empty


# ---------------------------------------------------------------------------
# DOB disambiguation v1 wiring (1 test)
# ---------------------------------------------------------------------------


def test_agent_internal_error_converts_to_closed_session(monkeypatch, capsys):
    """Defensive paths (AgentInternalError, AssertionError from state.py)
    are caught at the next() boundary and converted to a closed-session
    message. The eval harness is a graded deliverable; uncaught exceptions
    would crash the runner and prevent recording the persona's failure.
    State-machine bugs surface as a stderr warning, never as a Python
    traceback escaping next()."""
    from payment_agent.errors import AgentInternalError

    def fake_extract(user_input, stage, slots, *, api_key=None):
        return ExtractionResult(slots=ExtractedSlots(), intent=Intent.AMBIGUOUS)

    # Simulate a state-machine bug: _drive raises AgentInternalError.
    def fake_drive(self, result, *, prev_stage):
        raise AgentInternalError("simulated invariant violation in test")

    monkeypatch.setattr(agent_module, "extract_turn", fake_extract)
    monkeypatch.setattr(Agent, "_drive", fake_drive)

    a = Agent()
    out = a.next("anything")
    # Interface contract honored: dict[str, str], not a raised exception.
    assert isinstance(out, dict) and "message" in out
    assert isinstance(out["message"], str) and out["message"]
    # Routed to CLOSED so subsequent turns skip extract entirely.
    assert a._stage == Stage.CLOSED
    # Bug surfaced via stderr (visible to dev/CI), not via traceback.
    captured = capsys.readouterr()
    assert "internal error" in captured.err.lower()
    assert "simulated invariant violation" in captured.err


def test_verify_fail_message_key_preserved_through_drive(patched_agent):
    """Regression: drive loop must NOT clobber the verify_fail_<field>
    message_key by re-firing step_on_entry on the now-cleared slot. Live
    persona transcripts caught this in Step 11; lock the fix in a unit test
    so removing the prev_stage gate from _drive surfaces immediately."""
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain", dob=date(2000, 1, 1),
                    selected_secondary_factor=SecondaryFactor.DOB),
        ],
        lookups=[_lookup_success()],  # real DOB on file is 1990-05-14
    )
    a.next("ACC1001")
    out = a.next("Nithin Jain DOB 01-01-2000")  # wrong DOB; verify will fail
    # The agent's response must be the field-specific verify_fail copy,
    # NOT the generic "I've found your account" entry/prompt copy.
    assert "doesn't match" in out["message"].lower(), (
        f"expected verify_fail_dob copy ('doesn't match'); got: "
        f"{out['message']!r}"
    )
    # Counter incremented by 1 (one wrong attempt).
    assert a._counters.verification_retries == 1


def test_dob_disambiguation_resolved_advances_to_collecting_amount_when_verify_passes(
    patched_agent,
):
    """Regression: after dob_disambiguation resolves with a DOB that
    matches the on-file value, the agent must run verify_identity from
    step_on_entry(COLLECTING_IDENTITY) and advance to COLLECTING_AMOUNT
    with verified=True.

    Previously, the message-key preservation in _drive overwrote entry's
    advancing transition with the routing's "dob_resolved" ("Got it.")
    message. Manual testing surfaced the symmetric bug below; this test
    locks the success path so the fix doesn't regress the happy case.
    """
    primary_dob = date(1990, 4, 5)        # April 5 (DD-MM reading)
    alternate_dob = date(1990, 5, 4)      # May 4 (MM-DD reading)
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            # First identity attempt — ambiguous DOB, both readings valid.
            _result(full_name="Nithin Jain",
                    dob=primary_dob, dob_alternate=alternate_dob,
                    selected_secondary_factor=SecondaryFactor.DOB),
            # User AFFIRMs the primary in disambiguation.
            _result(intent=Intent.AFFIRM),
        ],
        # Lookup returns DOB matching the PRIMARY reading so verify passes
        # after disambiguation.
        lookups=[_lookup_success(dob=primary_dob)],
    )
    a.next("ACC1001")
    a.next("Nithin Jain DOB 05-04-1990")        # ambiguous → disambiguation
    out = a.next("yes that's right")             # AFFIRM primary

    assert a._stage == Stage.COLLECTING_AMOUNT, (
        f"after disambiguation + verify-pass, stage must advance to "
        f"COLLECTING_AMOUNT; got {a._stage}"
    )
    assert a._slots.verified is True, (
        "verified flag must be True after successful identity verification "
        "via the dob_disambiguation path"
    )
    # User-visible message must be the entry_collect_amount template (with
    # DECISIONS #28 balance share), NOT "Got it." from dob_resolved.
    assert "balance" in out["message"].lower(), (
        f"post-verify message must surface the balance share per "
        f"DECISIONS #28; got: {out['message']!r}"
    )


def test_v2_disambiguation_md_match_resolves_to_stored_year_when_user_omits_year(
    patched_agent,
):
    """v2 (m,d) match: when the user types 'May 4' (no year) and the LLM
    defaults the year to today, the (month, day) match against the stored
    alternate's (month, day) wins — and the resolved slots.dob carries the
    STORED year (from the original ambiguous input), not the LLM's default
    year. This is the v2 UX win: the user can say 'May 4' instead of
    retyping the full date with year."""
    primary_dob = date(1990, 4, 5)            # April 5, 1990 (DD-MM)
    alternate_dob = date(1990, 5, 4)          # May 4, 1990 (MM-DD)
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain",
                    dob=primary_dob, dob_alternate=alternate_dob,
                    selected_secondary_factor=SecondaryFactor.DOB),
            # User typed "May 4" — LLM extracts with current year default
            # (date(2026, 5, 4)). The (m,d) match logic must resolve this
            # to the STORED alternate (1990-05-04), not the LLM's 2026 date.
            _result(dob=date(2026, 5, 4)),
        ],
        lookups=[_lookup_success(dob=alternate_dob)],
    )
    a.next("ACC1001")
    a.next("Nithin Jain DOB 04-05-1990")
    out = a.next("May 4")

    assert a._stage == Stage.COLLECTING_AMOUNT
    assert a._slots.dob == alternate_dob, (
        f"v2 (m,d) match must resolve to the STORED 1990 year, not the "
        f"LLM's current-year default; got {a._slots.dob}"
    )
    assert a._slots.verified is True
    # Disambiguation slots cleared after resolution.
    assert a._slots.dob_disamb_primary is None
    assert a._slots.dob_disamb_alternate is None


def test_dob_disambiguation_resolved_via_carve_out_advances_when_verify_passes(
    patched_agent,
):
    """Regression for the carve-out #2 path: user types an explicit
    alternate date in dob_disambiguation (rather than AFFIRMing the
    primary). step_on_input writes the new DOB via slot model_copy
    (carve-out #2), drive fires step_on_entry, verify runs against the
    new DOB, passes, advances to COLLECTING_AMOUNT.

    This is the path manual testing exercised. The AFFIRM-success test
    above doesn't cover it — AFFIRM doesn't mutate slots, while carve-out
    #2 does, and the slot mutation interacts with the message-key
    preservation rule in _drive."""
    primary_dob = date(1990, 4, 5)
    alternate_dob = date(1990, 5, 4)
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain",
                    dob=primary_dob, dob_alternate=alternate_dob,
                    selected_secondary_factor=SecondaryFactor.DOB),
            # User typed the alternate explicitly; extraction returns dob
            # set (no alternate this turn), triggering carve-out #2.
            _result(dob=alternate_dob),
        ],
        # On-file DOB matches the user's chosen alternate.
        lookups=[_lookup_success(dob=alternate_dob)],
    )
    a.next("ACC1001")
    a.next("Nithin Jain DOB 05-04-1990")
    out = a.next("I meant May 4th")

    assert a._stage == Stage.COLLECTING_AMOUNT, (
        f"carve-out #2 success: stage must advance to COLLECTING_AMOUNT; "
        f"got {a._stage}"
    )
    assert a._slots.dob == alternate_dob, (
        f"carve-out #2 must overwrite the locked dob slot with the "
        f"chosen alternate; got {a._slots.dob}"
    )
    assert a._slots.verified is True
    assert "balance" in out["message"].lower(), (
        f"post-verify message must surface the balance share, NOT "
        f"'Got it.'; got: {out['message']!r}"
    )


def test_dob_disambiguation_resolved_surfaces_verify_fail_when_dob_doesnt_match(
    patched_agent,
):
    """Regression for the manual-testing bug: after dob_disambiguation
    resolves to a DOB that does NOT match the on-file value, the agent
    must surface verify_fail_dob (not silently swallow the failure with
    a "Got it." message). Previously, _drive's message-key preservation
    treated the dob_resolved -> verify_fail transition as a no-op entry
    and overwrote verify_fail_dob with dob_resolved — the user saw "Got
    it." while a retry was silently consumed and the dob slot cleared."""
    primary_dob = date(1990, 4, 5)
    alternate_dob = date(1990, 5, 4)
    on_file_dob = date(1990, 5, 14)              # neither reading matches
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain",
                    dob=primary_dob, dob_alternate=alternate_dob,
                    selected_secondary_factor=SecondaryFactor.DOB),
            _result(intent=Intent.AFFIRM),
        ],
        lookups=[_lookup_success(dob=on_file_dob)],
    )
    a.next("ACC1001")
    a.next("Nithin Jain DOB 05-04-1990")
    out = a.next("yes that's right")

    # Stage stays at COLLECTING_IDENTITY (verify failed); slot cleared by
    # carve-out #4; counter incremented; user sees the field-specific
    # failure copy instead of the misleading "Got it.".
    assert a._stage == Stage.COLLECTING_IDENTITY
    assert a._slots.dob is None, (
        "carve-out #4 must clear the failed dob slot for the user to "
        "re-enter; got dob still set"
    )
    assert a._counters.verification_retries == 1, (
        f"verify failure must consume one retry; got "
        f"{a._counters.verification_retries}"
    )
    assert "doesn't match" in out["message"].lower(), (
        f"user must see the verify_fail_dob copy, not 'Got it.'; "
        f"got: {out['message']!r}"
    )
    assert "got it" not in out["message"].lower()


def test_dob_alternate_routes_to_dob_disambiguation(patched_agent):
    """When extraction returns dob + dob_alternate, step_on_input(IDENTITY)
    routes to DOB_DISAMBIGUATION. Carve-out #2 (in step_on_input) writes a
    user-typed correction in the next turn. v1 design per DECISIONS #21
    + DECISIONS #15."""
    a, _ = patched_agent(
        extractions=[
            _result(account_id="ACC1001"),
            _result(full_name="Nithin Jain",
                    dob=date(1990, 5, 4), dob_alternate=date(1990, 4, 5),
                    selected_secondary_factor=SecondaryFactor.DOB),
        ],
        lookups=[_lookup_success()],
    )
    a.next("ACC1001")
    a.next("Nithin Jain DOB 04-05-1990")
    assert a._stage == Stage.DOB_DISAMBIGUATION
