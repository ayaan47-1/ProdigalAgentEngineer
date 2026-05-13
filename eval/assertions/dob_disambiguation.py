"""dob_disambiguation — LLM resolves numerically-ambiguous DOB conversationally.

Verification-only test: ACC1003 has balance 0 on the live stub so this
persona stops after the verification turn. We assert verified=true and
that the session is still mid-flow (not terminal).
"""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_payment_counter,
    expect_terminal,
    expect_verified,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, None, failures)
    expect_verified(snapshot, True, failures)
    # Verification may or may not have used a retry; the model's
    # behavior on ambiguity is the open question. Both 3 and 2 are
    # acceptable counter values (3 = LLM disambiguated correctly on
    # first attempt; 2 = one wrong-reading attempt burned).
    counter = (snapshot.get("verification") or {}).get("counter")
    if counter not in (3, 2):
        failures.append(
            f"expected verification.counter in {{3, 2}}, got {counter}"
        )
    expect_payment_counter(snapshot, 5, failures)
    return failures
