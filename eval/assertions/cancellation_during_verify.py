"""Assertions for cancellation_during_verify persona."""
from __future__ import annotations

from payment_agent.state import Stage
from eval.harness import PersonaResult


def assert_persona(result: PersonaResult) -> list[str]:
    failures: list[str] = []

    if result.final_stage != Stage.TERMINAL_CANCELLED.value:
        failures.append(
            f"final_stage: expected {Stage.TERMINAL_CANCELLED.value!r}, "
            f"got {result.final_stage!r}"
        )

    final = result.final_slots_snapshot
    cause = final.get("terminal_cause")
    if cause != "cancelled_at_collecting_identity":
        failures.append(
            f"terminal_cause: expected 'cancelled_at_collecting_identity', "
            f"got {cause!r}"
        )

    counters = result.final_counters_snapshot
    # Per DECISIONS #10: cancellation does NOT consume retry budget.
    if counters.get("verification_retries", 0) != 0:
        failures.append(
            f"cancellation must not consume verification_retries; "
            f"got {counters.get('verification_retries')}"
        )

    if final.get("transaction_id"):
        failures.append("transaction_id must NOT be set on cancellation")

    return failures
