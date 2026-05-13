"""leap_year_acc1004 — leap-year DOB strict-parse canary."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_lookup_account_id,
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
    expect_payment_counter(snapshot, 5, failures)
    expect_lookup_account_id(snapshot, "ACC1004", failures)
    expect_tool_call_subsequence(
        snapshot,
        ["lookup_account", "submit_verification", "process_payment"],
        failures,
    )
    return failures
