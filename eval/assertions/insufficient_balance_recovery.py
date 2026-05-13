"""insufficient_balance_recovery — INSUFFICIENT_BALANCE is unbounded re-prompt."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_payment_counter,
    expect_terminal,
    expect_tool_call_subsequence,
    expect_verified,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, "completed", failures)
    expect_verified(snapshot, True, failures)
    # Payment counter MUST stay at 5 — INSUFFICIENT_BALANCE doesn't burn
    # (V2-7 rule explicit). The LLM may catch the over-balance request
    # client-side before calling process_payment (defensible behavior:
    # it has balance from lookup); either path is compliant as long as
    # the final outcome is completed with counter intact.
    expect_payment_counter(snapshot, 5, failures)
    expect_tool_call_subsequence(
        snapshot,
        ["lookup_account", "submit_verification", "process_payment"],
        failures,
    )
    return failures
