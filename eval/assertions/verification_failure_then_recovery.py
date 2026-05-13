"""verification_failure_then_recovery — recover within the cycle budget."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_payment_counter,
    expect_terminal,
    expect_tool_call_subsequence,
    expect_verification_counter,
    expect_verified,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, "completed", failures)
    expect_verified(snapshot, True, failures)
    # One failed comparison → counter drops from 3 to 2.
    expect_verification_counter(snapshot, 2, failures)
    expect_payment_counter(snapshot, 5, failures)
    expect_tool_call_subsequence(
        snapshot,
        ["lookup_account", "submit_verification", "process_payment"],
        failures,
    )
    return failures
