"""Project-wide constants.

Single source of truth for the determinism contract (model + temp + thinking),
API endpoints, retry caps, and timeouts. All values pinned at module level —
no environment-overridable budgets, because that would defeat the determinism
guarantees DECISIONS #8 commits to. The one exception is ``ANTHROPIC_API_KEY``,
which lives in the environment per DECISIONS #18.

Cap semantics across the project: ``cap N`` means N events allowed (each
re-prompted), action on the (N+1)th event. Transition rows compare the
*pre-increment* counter value: ``counter < N`` re-prompts, ``counter == N``
triggers the cap action.
"""
from __future__ import annotations

from typing import Literal, TypedDict


class _ThinkingDisabled(TypedDict):
    """Locked shape for the disabled-thinking config (DECISIONS #20).

    Tightening this from ``dict[str, str]`` to a TypedDict with
    ``Literal["disabled"]`` makes accidental ``"enabled"`` or ``"adaptive"``
    a static type error — the determinism contract is enforced at the type
    layer, not just at runtime via ``test_llm_kwargs.py``.
    """

    type: Literal["disabled"]

# ---------------------------------------------------------------------------
# Anthropic LLM
# ---------------------------------------------------------------------------

# Pinned model. The dateless 4.6+ format IS the snapshot — there is no separate
# YYYYMMDD suffix to record. See DECISIONS #19.
LLM_MODEL: str = "claude-sonnet-4-6"

# Determinism kwargs. ``thinking={"type": "disabled"}`` is passed explicitly
# rather than relying on default-off so the contract is asserted in code
# (test_llm_kwargs.py) and survives future SDK config drift. See DECISIONS #20.
LLM_TEMPERATURE: float = 0.0
LLM_THINKING: _ThinkingDisabled = {"type": "disabled"}

# Max output tokens for the structured-extraction call. Extraction returns a
# small JSON object; cap is generous to absorb tool-use overhead.
LLM_MAX_TOKENS: int = 1024

# Aggregated kwargs splatted into every Anthropic Messages-API call by llm.py.
# Single source of truth — adding a determinism-relevant kwarg here ensures it
# applies to every call site uniformly. Asserted as a whole in test_llm_kwargs.py.
LLM_CALL_KWARGS: dict[str, object] = {
    "model": LLM_MODEL,
    "temperature": LLM_TEMPERATURE,
    "thinking": LLM_THINKING,
    "max_tokens": LLM_MAX_TOKENS,
}

# ---------------------------------------------------------------------------
# Payment-verification API
# ---------------------------------------------------------------------------

API_BASE_URL: str = (
    "https://se-payment-verification-api.service.external.usea2."
    "aws.prodigaltech.com"
)

# Per-call timeouts (DECISIONS #13).
LOOKUP_PER_ATTEMPT_TIMEOUT_S: float = 4.0
LOOKUP_TOTAL_BUDGET_S: float = 10.0
PAYMENT_CALL_TIMEOUT_S: float = 30.0

# Silent transient retry policy (DECISIONS #13).
LOOKUP_SILENT_RETRY_COUNT: int = 1
LOOKUP_SILENT_RETRY_BACKOFF_S: float = 0.5
PAYMENT_SILENT_RETRY_COUNT: int = 0  # process_payment is never retried.

# ---------------------------------------------------------------------------
# Retry caps (user-visible)
# ---------------------------------------------------------------------------

# All caps below follow the semantics from DECISIONS #22:
#   counter == CAP  → action (terminate or escalate)
#   counter <  CAP  → re-prompt (counter increments by 1)
# i.e., CAP=N means N events allowed (each re-prompted), action on the (N+1)th.

# DECISIONS #3
VERIFICATION_RETRY_CAP: int = 3  # counter == 3 → terminal_verification_exhausted

# DECISIONS #4 (typo-class only). insufficient_balance is unbounded; invalid_amount is a bug.
PAYMENT_TYPO_RETRY_CAP: int = 5  # counter == 5 → terminal_payment_exhausted

# DECISIONS #12
LOOKUP_RETRY_CAP: int = 3  # counter == 3 → terminal_account_not_found

# DECISIONS #5
CONFIRMATION_AMBIGUOUS_CAP: int = 2  # counter == 2 → forced_disambiguation

# Plan-mode additions (DECISIONS #22) — guardrail caps to prevent ambiguous-only no-progress loops.
DOB_DISAMB_RETRY_CAP: int = 3  # counter == 3 → terminal_cancelled
FORCED_DISAMB_RETRY_CAP: int = 5  # counter == 5 → terminal_cancelled

# ---------------------------------------------------------------------------
# v2 orchestrator (DECISIONS_V2)
# ---------------------------------------------------------------------------

# V2-16: bound the tool-use loop per turn to prevent the LLM from looping
# tools indefinitely. Happy-path turns use 1–3 tools; 6 leaves headroom
# for the heaviest legitimate turn (e.g., process_payment +
# render_canonical_message recap, possibly with a preceding correction).
ITERATION_CAP: int = 6

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY_ENV: str = "ANTHROPIC_API_KEY"
LIVE_SMOKE_ENV: str = "PAYMENT_AGENT_LIVE_SMOKE"
