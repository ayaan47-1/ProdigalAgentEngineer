"""multi_factor_attempt_sequence — factor switching within the cycle budget."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_payment_counter,
    expect_terminal,
    expect_verification_counter,
    expect_verified,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, "completed", failures)
    expect_verified(snapshot, True, failures)
    # 2 failed comparisons consumed 2 of the 3 retries → counter = 1.
    expect_verification_counter(snapshot, 1, failures)
    expect_payment_counter(snapshot, 5, failures)
    return failures
