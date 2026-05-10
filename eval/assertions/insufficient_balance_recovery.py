"""Assertions for insufficient_balance_recovery persona."""
from __future__ import annotations

from payment_agent.state import Stage
from eval.harness import PersonaResult


def assert_persona(result: PersonaResult) -> list[str]:
    failures: list[str] = []

    if result.final_stage != Stage.TERMINAL_COMPLETED.value:
        failures.append(
            f"final_stage: expected {Stage.TERMINAL_COMPLETED.value!r}, "
            f"got {result.final_stage!r}"
        )

    final = result.final_slots_snapshot
    if not final.get("transaction_id"):
        failures.append("transaction_id should be set after eventual success")

    counters = result.final_counters_snapshot
    # DECISIONS #4: insufficient_balance is unbounded and does NOT consume
    # payment_retries (which is reserved for typo-class outcomes).
    if counters.get("payment_retries", 0) != 0:
        failures.append(
            f"payment_retries must NOT increment on insufficient_balance "
            f"per DECISIONS #4; got {counters.get('payment_retries')}"
        )

    return failures
