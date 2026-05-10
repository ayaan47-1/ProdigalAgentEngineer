"""Assertions for verification_exhausted persona."""
from __future__ import annotations

from payment_agent.state import Stage
from eval.harness import PersonaResult


def assert_persona(result: PersonaResult) -> list[str]:
    failures: list[str] = []

    if result.final_stage != Stage.TERMINAL_VERIFICATION_EXHAUSTED.value:
        failures.append(
            f"final_stage: expected "
            f"{Stage.TERMINAL_VERIFICATION_EXHAUSTED.value!r}, "
            f"got {result.final_stage!r}"
        )

    counters = result.final_counters_snapshot
    # Cap=3 semantics per DECISIONS #22: 3 wrong submissions get re-prompted
    # (counter goes 0->1->2->3), the 4th sees counter==3 and terminates.
    # Post-termination counter is 4 (state.py increments BEFORE the cap
    # check — see _next_count = counter + 1; if counter == CAP: terminate).
    # The 4 in the post-state matches the 4 wrong submissions in the dialog.
    if counters.get("verification_retries") != 4:
        failures.append(
            f"verification_retries: expected 4 (3 re-prompts + 1 terminating), "
            f"got {counters.get('verification_retries')}"
        )

    final = result.final_slots_snapshot
    if final.get("verified"):
        failures.append("verified flag must NOT be True on exhaustion")
    if final.get("transaction_id"):
        failures.append("transaction_id must NOT be set on exhaustion")

    return failures
