"""Per-turn orchestrator. The public ``Agent`` class.

Wires together the deterministic kernel (``state``, ``validate``, ``verify``,
``templates``), the API client (``api``), and the LLM extractor (``extract``)
into the spec-required interface:

    class Agent:
        def next(self, user_input: str) -> dict[str, str]:  # {"message": str}

This module is wiring, not behavior. State transitions live in ``state.py``;
HTTP behavior lives in ``api.py``; user-facing prose lives in ``templates.py``;
LLM-call shape lives in ``llm.py``. Tests for those concerns belong in their
own test files, not in ``test_agent.py``.

## Per-turn flow

1. **Pre-extract terminal routing** (DECISIONS #23, cost guard):
   if current stage is terminal/closed, route to ``CLOSED`` and emit the
   generic re-entry message inline. No LLM call, no transition.
2. **Extract** (single LLM call per turn). ``LlmCallFailed`` is degraded
   gracefully into ``Intent.AMBIGUOUS`` + empty slots so the orchestrator
   re-prompts with a clarification — production should distinguish auth /
   rate-limit / transient (TODO.md: deferred Step-7 MEDIUM #3).
3. **Cancel guard** (orchestrator-level per ``state.cancel_guard``):
   ``NEGATE`` in a cancellable stage routes straight to
   ``terminal_cancelled`` without consuming retry budget.
4. **Slot merge** with lock-once-filled (``state.merge_extracted_slots``).
5. **Transition on input** (``state.step_on_input``).
6. **Drive to steady state**: a bounded loop that
   - executes side effects (lookup / process_payment) and feeds the result
     back into ``state.step_on_effect``;
   - calls ``state.step_on_entry`` to fire stage-entry auto-advance;
   - stops when we reach a terminal stage (preserve the routing's
     ``message_key`` so e.g. ``"recap_success"`` is not overwritten by the
     generic ``"terminal_completed"`` per TODO.md), or when entry returns
     a steady-state same-stage with ``side_effect=NONE``;
   - is bounded at 6 iterations (the deepest legitimate reachable chain)
     and over-bound raises ``AgentInternalError``, caught at the
     ``next()`` boundary and converted to a closed-session message.
7. **Compose** the user-visible message via ``templates.render`` using the
   *final* ``message_key`` in the chain.
8. **Persist** ``(stage, slots, counters)`` on the instance.

## What lives here vs. elsewhere

This module never:
- Imports ``anthropic`` (only ``llm.py`` does — architecture commit).
- Validates slot values (``validate``/``verify`` own that).
- Decides state transitions (``state`` owns that — including all retry
  cap semantics, all terminal routing, and all carve-outs).
- Composes user-facing prose (``templates`` owns that — every spec-required
  string is a template).

This module DOES:
- Decide *when* to call extract / api / templates.
- Catch ``LlmCallFailed`` and degrade gracefully (DECISIONS #18 covers the
  no-key fail-soft path; this is the SDK-error path).
- Persist per-instance state across turns (``Agent`` is stateful; the eval
  harness instantiates fresh per persona per DECISIONS #11).
"""
from __future__ import annotations

import sys
from typing import Any

from payment_agent import api, templates
from payment_agent.errors import AgentInternalError, LlmCallFailed
from payment_agent.extract import extract_turn
from payment_agent.state import (
    TERMINAL_STAGES,
    Counters,
    EffectResult,
    ExtractedSlots,
    ExtractionResult,
    Intent,
    PaymentEffectResult,
    SideEffect,
    SlotStore,
    Stage,
    StepResult,
    cancel_guard,
    merge_extracted_slots,
    step_on_effect,
    step_on_entry,
    step_on_input,
)

# Bound on the per-turn drive loop. A correct state machine settles within
# a small number of (side-effect, auto-advance) iterations; spinning past
# this is a state-machine bug and we want it to fail fast. The deepest
# legitimate reachable chain (out-of-order all-slots-prefilled greeting):
#   step_on_input(GREETING)
#   -> step_on_entry(COLLECTING_ACCOUNT_ID, side_effect=CALL_LOOKUP)
#   -> step_on_effect(LOOKUP success) -> COLLECTING_IDENTITY
#   -> step_on_entry(COLLECTING_IDENTITY, identity complete, verifies)
#       -> COLLECTING_AMOUNT
#   -> step_on_entry(COLLECTING_AMOUNT, amount valid) -> COLLECTING_CARD
#   -> step_on_entry(COLLECTING_CARD, card valid) -> AWAITING_PAYMENT_CONFIRMATION
#   -> step_on_entry(AWAITING_PAYMENT_CONFIRMATION) -> steady state
# That's 6 iterations. The bound is set 1 tick above to fail fast on a
# genuinely buggy oscillation; over-bound raises AgentInternalError,
# caught at the next() boundary and converted to a closed-session message.
_DRIVE_LOOP_BOUND: int = 6


class Agent:
    """Conversational payment-collection agent.

    One ``Agent`` instance handles exactly one payment cycle (DECISIONS #6,
    #11). Eval harnesses and CLI sessions instantiate fresh per persona.

    Constructor takes an optional ``api_key`` for the Anthropic SDK; when
    omitted, ``extract_turn`` reads ``ANTHROPIC_API_KEY`` from the env and
    falls back to the deterministic-only path on absence (DECISIONS #18).
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        self._stage: Stage = Stage.GREETING
        self._slots: SlotStore = SlotStore()
        self._counters: Counters = Counters()
        self._api_key: str | None = api_key

    def next(self, user_input: str) -> dict[str, str]:
        """Process one user turn and return ``{"message": str}``.

        Bootstrap convention: callers MAY invoke ``agent.next("")`` exactly
        once before the first user turn to render the greeting. A second
        empty call while still at GREETING re-emits the same greet template
        without advancing — by design but easy to misuse, so harness/eval
        authors should bootstrap at most once and then enter the user-input
        loop.

        State-machine invariant violations (raised as ``AgentInternalError``
        from ``_drive`` / ``_execute_side_effect`` or ``AssertionError``
        from inside ``state.step_on_effect`` for unreachable combos) are
        caught at this boundary and converted to a closed-session message.
        The eval harness is a graded deliverable and must not crash on
        defensive paths; the underlying bug is logged to stderr so it
        surfaces in dev / CI runs without breaking the interface contract.
        """
        try:
            return self._next_impl(user_input)
        except (AgentInternalError, AssertionError) as e:
            # Defensive convergence: any state-machine invariant violation
            # ends the session cleanly. The user sees the standard "session
            # has ended" copy; a developer running interactively sees the
            # stderr warning. No traceback escapes next() — the spec
            # interface is dict[str, str], always.
            print(
                f"[agent] internal error, closing session: {e}",
                file=sys.stderr,
            )
            self._stage = Stage.CLOSED
            return {"message": templates.render(self._slots, "closed_reentry")}

    def _next_impl(self, user_input: str) -> dict[str, str]:
        """Inner per-turn implementation; ``next`` adds the defensive boundary."""
        # 0. Bootstrap path: empty input at GREETING emits the greet template
        # ("Hello! I'm here to help you make a payment...") without
        # transitioning out. Lets cli.py auto-greet via ``agent.next("")``
        # on startup rather than reaching into templates directly. The
        # user's first non-empty input then drives the normal greeting →
        # collecting_account_id transition. This is the only orchestrator
        # special-case; all other empty inputs flow through extract (which
        # short-circuits to AMBIGUOUS) and produce a stage-appropriate
        # re-prompt via the deterministic kernel.
        if not user_input.strip() and self._stage == Stage.GREETING:
            return self._finalize(StepResult(
                next_stage=Stage.GREETING,
                slots=self._slots,
                counters=self._counters,
                side_effect=SideEffect.NONE,
                message_key="greet",
            ))

        # 1. Pre-extract terminal routing. DECISIONS #23 + cost guard.
        # CLOSED is NOT a member of TERMINAL_STAGES (terminals are kept
        # distinguishable for end-of-turn assertions per DECISIONS #23),
        # but both branches get the same closed-reentry treatment here.
        if self._stage == Stage.CLOSED or self._stage in TERMINAL_STAGES:
            result = step_on_entry(Stage.CLOSED, self._slots, self._counters)
            return self._finalize(result)

        # 2. Extract (graceful on LlmCallFailed).
        try:
            extraction = extract_turn(
                user_input, self._stage, self._slots, api_key=self._api_key,
            )
        except LlmCallFailed:
            # Degrade gracefully — the orchestrator re-prompts via the
            # AMBIGUOUS-empty path. Production should distinguish auth /
            # rate-limit / transient (deferred per TODO.md Step 7 MEDIUM #3).
            extraction = ExtractionResult(
                slots=ExtractedSlots(), intent=Intent.AMBIGUOUS,
            )

        # 3. Cancel guard (orchestrator-level per state.cancel_guard).
        cancel_result = cancel_guard(
            self._stage, extraction.intent, self._slots, self._counters,
        )
        if cancel_result is not None:
            return self._finalize(cancel_result)

        # 4. Slot merge with v2 DOB-disambiguation pre-handling.
        #
        # When extraction returns BOTH a primary dob AND a dob_alternate
        # (numerically ambiguous input per DECISIONS #15), the orchestrator
        # stores BOTH readings in slots.dob_disamb_primary/alternate
        # WITHOUT merging primary into slots.dob. This lets
        # step_on_input(DOB_DISAMBIGUATION) present a true two-option
        # choice ("was it April 5 or May 4?") rather than the v1 "I read
        # your DOB as X; reply yes" pattern that pre-committed the parse.
        #
        # Conditions: extraction has both readings AND we don't already
        # have a confirmed dob (don't trigger if user is past
        # disambiguation) AND we don't already have stored disamb options
        # (don't double-write across re-prompt turns).
        extraction_for_merge = extraction.slots
        disamb_writes: dict[str, Any] = {}
        if (
            extraction.slots.dob is not None
            and extraction.slots.dob_alternate is not None
            and self._slots.dob is None
            and self._slots.dob_disamb_primary is None
        ):
            extraction_for_merge = extraction.slots.model_copy(
                update={"dob": None}
            )
            disamb_writes = {
                "dob_disamb_primary": extraction.slots.dob,
                "dob_disamb_alternate": extraction.slots.dob_alternate,
            }

        merged_slots = merge_extracted_slots(
            slots=self._slots, extracted=extraction_for_merge,
        )
        if disamb_writes:
            merged_slots = merged_slots.model_copy(update=disamb_writes)

        # 5. Transition on input.
        result = step_on_input(
            self._stage, merged_slots, self._counters, extraction,
        )

        # 6. Drive to steady state (side-effect loop + auto-advance loop,
        # interleaved, bounded). Pass the pre-transition stage so the drive
        # loop only fires step_on_entry on stages we ACTUALLY transitioned
        # to — not redundantly on the same stage when step_on_input already
        # delegated to step_on_entry internally (otherwise the second entry
        # call overwrites the routing's message_key, e.g., verify_fail_dob
        # gets clobbered by prompt_identity).
        result = self._drive(result, prev_stage=self._stage)

        # 7+8. Compose + persist + return.
        return self._finalize(result)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _drive(self, result: StepResult, *, prev_stage: Stage) -> StepResult:
        """Apply side-effects and stage-entry auto-advance until steady state.

        The loop has three branches per iteration:
          1. **Side effect requested**: execute via api.py, feed result back
             through ``step_on_effect``. The post-effect stage may differ
             from the pre-effect stage (e.g., LOOKUP success advances from
             ``COLLECTING_ACCOUNT_ID`` to ``COLLECTING_IDENTITY``).
          2. **Transitioned to a new stage** (``next_stage != prev_stage``):
             fire ``step_on_entry`` on the new stage to surface its auto-
             advance behavior (CALL_LOOKUP request, recap_completed →
             terminal_completed advance, etc.).
          3. **No transition + no side effect**: steady state. Use the
             routing's ``message_key`` as-is.

        The prev_stage gate is critical. ``state.step_on_input`` for some
        stages (notably COLLECTING_IDENTITY, COLLECTING_ACCOUNT_ID)
        internally delegates to ``step_on_entry``, so the result we receive
        is ALREADY the entry-fired result. Re-firing entry on the same
        stage would clobber the routing's message_key — for instance,
        after a verify failure the routing emits ``verify_fail_dob`` and
        clears the dob slot; a redundant ``step_on_entry`` call on the
        cleared slots would re-evaluate ``_identity_complete=False`` and
        emit the generic ``prompt_identity`` instead.

        Terminal stages stop the loop unconditionally (preserve routing's
        message_key per TODO.md — ``recap_success`` must not be overwritten
        by ``terminal_completed``). Over-bound raises ``AgentInternalError``
        caught at the ``next()`` boundary.
        """
        for _ in range(_DRIVE_LOOP_BOUND):
            if result.side_effect != SideEffect.NONE:
                effect_result = self._execute_side_effect(result)
                # The stage that requested the effect is the prev for the
                # post-effect transition.
                prev_stage = result.next_stage
                result = step_on_effect(
                    result.next_stage,
                    result.slots,
                    result.counters,
                    effect_result,
                )
                continue

            # Terminal stages: stop, preserve routing's message_key.
            if result.next_stage in TERMINAL_STAGES:
                return result

            # No transition + no side effect: steady state.
            if result.next_stage == prev_stage:
                return result

            # Genuine stage transition: fire step_on_entry on the new
            # stage to surface its auto-advance / side-effect behavior.
            prev_stage = result.next_stage
            entry = step_on_entry(
                result.next_stage, result.slots, result.counters,
            )

            # Preserve the transition's message_key ONLY when entry was a
            # true no-op — same stage, no side effect, no slot mutation,
            # no counter increment. In that case, entry just emitted a
            # generic re-prompt (e.g., prompt_amount) and the transition's
            # contextual message (e.g., entry_collect_amount with the
            # DECISIONS #28 balance share) is the right user-facing copy.
            #
            # If entry MODIFIED slots or counters (e.g., verify failed and
            # cleared the failed slot + incremented verification_retries;
            # invalid amount cleared the slot), entry's message_key
            # describes that mutation and MUST NOT be hidden. The original
            # bug here was a dob_disambiguation -> collecting_identity
            # transition where verify then failed: routing's "dob_resolved"
            # ("Got it.") clobbered entry's "verify_fail_dob", silently
            # consuming a retry without telling the user the DOB was wrong.
            no_op_entry = (
                entry.next_stage == result.next_stage
                and entry.side_effect == SideEffect.NONE
                and entry.slots == result.slots
                and entry.counters == result.counters
            )
            if no_op_entry:
                result = StepResult(
                    next_stage=entry.next_stage,
                    slots=entry.slots,
                    counters=entry.counters,
                    side_effect=entry.side_effect,
                    message_key=result.message_key,  # routing's key wins
                )
            else:
                # Entry advanced, requested a side effect, or mutated
                # state — use entry's full result. Its message_key
                # reflects the new state.
                result = entry

        raise AgentInternalError(
            "Agent._drive exceeded loop bound — state-machine bug; "
            f"last stage={result.next_stage}, message_key={result.message_key}"
        )

    def _execute_side_effect(self, result: StepResult) -> EffectResult:
        """Dispatch to api.py based on the requested side effect.

        Returns the typed ``EffectResult`` that ``step_on_effect`` will
        consume. Both api functions never raise — they map all errors to
        outcome enums.
        """
        if result.side_effect == SideEffect.CALL_LOOKUP:
            account_id = result.slots.account_id
            if account_id is None:
                # Defensive: the state machine shouldn't request CALL_LOOKUP
                # without account_id filled. Surface as AgentInternalError
                # so next() converts to a clean closed-session message.
                raise AgentInternalError(
                    "CALL_LOOKUP requested with account_id=None — "
                    "state machine invariant violated"
                )
            return api.lookup_account(account_id)

        if result.side_effect == SideEffect.CALL_PROCESS_PAYMENT:
            return self._call_process_payment(result.slots)

        raise AgentInternalError(
            f"Unhandled SideEffect: {result.side_effect}"
        )

    def _call_process_payment(self, slots: SlotStore) -> PaymentEffectResult:
        """Build the api.process_payment call from slot state.

        All required fields MUST be filled by the time we reach this side
        effect — the state machine guarantees it. We narrow each field to a
        non-Optional local after a None-check guard rather than building a
        dict and indexing into it; this keeps the call site type-safe and
        avoids the union-type spread that would otherwise force every
        keyword argument to ``api.process_payment`` into ``str | Decimal |
        int | None``. Missing fields surface as ``AgentInternalError``;
        ``next()`` catches it and ends the session cleanly.
        """
        if slots.account_id is None:
            raise AgentInternalError("CALL_PROCESS_PAYMENT missing account_id")
        if slots.amount is None:
            raise AgentInternalError("CALL_PROCESS_PAYMENT missing amount")
        if slots.pan is None:
            raise AgentInternalError("CALL_PROCESS_PAYMENT missing pan")
        if slots.cvv is None:
            raise AgentInternalError("CALL_PROCESS_PAYMENT missing cvv")
        if slots.expiry_month is None:
            raise AgentInternalError("CALL_PROCESS_PAYMENT missing expiry_month")
        if slots.expiry_year is None:
            raise AgentInternalError("CALL_PROCESS_PAYMENT missing expiry_year")
        if slots.full_name is None:
            raise AgentInternalError("CALL_PROCESS_PAYMENT missing full_name")

        return api.process_payment(
            account_id=slots.account_id,
            amount=slots.amount,
            pan=slots.pan,
            cvv=slots.cvv,
            expiry_month=slots.expiry_month,
            expiry_year=slots.expiry_year,
            full_name=slots.full_name,
        )

    def _finalize(self, result: StepResult) -> dict[str, str]:
        """Persist state and render the user-facing message.

        Renders BEFORE persisting state. If ``templates.render`` raises
        (an unknown ``message_key`` would surface a wiring bug), the
        instance's stage/slots/counters remain at their pre-turn values
        and the next ``next()`` call retries from a coherent state rather
        than landing in a half-advanced one. Templates are pure per
        DECISIONS #8, so this should not happen in production — defense
        in depth.
        """
        message = templates.render(result.slots, result.message_key)
        self._stage = result.next_stage
        self._slots = result.slots
        self._counters = result.counters
        return {"message": message}
