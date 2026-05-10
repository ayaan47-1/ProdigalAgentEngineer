"""Assertions for dob_disambiguation_success persona."""
from __future__ import annotations

from datetime import date

from payment_agent.state import Stage
from eval.harness import PersonaResult


def assert_persona(result: PersonaResult) -> list[str]:
    failures: list[str] = []

    # Persona ends at the post-verify amount-collection prompt — ACC1003
    # has 0 balance so no payment is attempted. The verification path is
    # what we're demonstrating.
    if result.final_stage != Stage.COLLECTING_AMOUNT.value:
        failures.append(
            f"final_stage: expected {Stage.COLLECTING_AMOUNT.value!r} "
            f"(post-verify); got {result.final_stage!r}"
        )

    final = result.final_slots_snapshot
    if not final.get("verified"):
        failures.append("verified must be True after disambiguation success")

    # The (m,d) match must resolve to the STORED 1992 year, not the LLM's
    # current-year default for a year-omitted "August 10" input.
    expected_dob = date(1992, 8, 10).isoformat()
    actual_dob = final.get("dob")
    if actual_dob != expected_dob:
        failures.append(
            f"slot dob: expected {expected_dob!r} (year carried from "
            f"stored disamb option); got {actual_dob!r}"
        )

    # Disambiguation slots cleared after resolution.
    if final.get("dob_disamb_primary") is not None:
        failures.append("dob_disamb_primary must be None after resolution")
    if final.get("dob_disamb_alternate") is not None:
        failures.append("dob_disamb_alternate must be None after resolution")

    counters = result.final_counters_snapshot
    # Clean disambiguation success: no retries consumed.
    if counters.get("dob_disamb_retries", 0) != 0:
        failures.append(
            f"dob_disamb_retries: expected 0 (clean resolve), "
            f"got {counters.get('dob_disamb_retries')}"
        )
    if counters.get("verification_retries", 0) != 0:
        failures.append(
            f"verification_retries: expected 0 on clean disambiguation "
            f"success path; got {counters.get('verification_retries')}"
        )

    return failures
