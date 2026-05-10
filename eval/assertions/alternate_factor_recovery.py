"""Assertions for alternate_factor_recovery persona."""
from __future__ import annotations

from datetime import date

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
    if not final.get("verified"):
        failures.append("verified must be True after eventual success")
    if not final.get("transaction_id"):
        failures.append("transaction_id must be set after payment success")

    # The user eventually provided ACC1001's actual DOB (May 14 1990).
    expected_dob = date(1990, 5, 14).isoformat()
    actual_dob = final.get("dob")
    if actual_dob != expected_dob:
        failures.append(
            f"slot dob: expected {expected_dob!r} (correct DOB on third "
            f"attempt); got {actual_dob!r}"
        )

    counters = result.final_counters_snapshot
    # Two failed verify attempts (wrong DOB reading + wrong pincode) before
    # the third attempt succeeded. Cap=3 means counter == 2 leaves one retry
    # in budget; counter advances to 2 across the two failures and stops
    # there since the third attempt verified.
    if counters.get("verification_retries") != 2:
        failures.append(
            f"verification_retries: expected 2 (two recoveries before "
            f"success); got {counters.get('verification_retries')}"
        )

    return failures
