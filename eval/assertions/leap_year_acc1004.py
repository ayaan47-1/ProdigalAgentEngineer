"""Assertions for leap_year_acc1004 persona — the BRIEF #3 canary."""
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
        failures.append("ACC1004 must verify with leap-year DOB 1988-02-29")
    if not final.get("transaction_id"):
        failures.append("transaction_id should be set after success")

    # The leap-year DOB must round-trip correctly through extraction +
    # validate.parse_iso_date + state. (Snapshot field is JSON ISO date string.)
    dob = final.get("dob")
    expected = date(1988, 2, 29).isoformat()
    if dob != expected:
        failures.append(
            f"slot dob: expected {expected!r} (leap-year canary), got {dob!r}"
        )

    return failures
