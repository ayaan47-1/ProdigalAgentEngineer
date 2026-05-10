"""Assertions for the happy_path persona.

Per DECISIONS #14 + #8: assert on STATE + side-effects, not message text
equality. Templates are content-stable but tests stay decoupled from prose.
"""
from __future__ import annotations

from payment_agent.state import Stage
from eval.harness import PersonaResult


def assert_persona(result: PersonaResult) -> list[str]:
    """Return list of failure messages; empty list = pass."""
    failures: list[str] = []

    if result.final_stage != Stage.TERMINAL_COMPLETED.value:
        failures.append(
            f"final_stage: expected {Stage.TERMINAL_COMPLETED.value!r}, "
            f"got {result.final_stage!r}"
        )

    final = result.final_slots_snapshot
    if not final.get("verified"):
        failures.append("verified flag should be True at end of happy path")

    if not final.get("transaction_id"):
        failures.append("transaction_id should be set after payment success")

    # Counters: zero retries on the happy path.
    counters = result.final_counters_snapshot
    for name in (
        "verification_retries", "payment_retries",
        "lookup_retries", "confirmation_ambig",
    ):
        if counters.get(name, 0) != 0:
            failures.append(
                f"counter {name} should be 0 on happy path; "
                f"got {counters.get(name)}"
            )

    return failures
