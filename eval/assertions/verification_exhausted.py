"""verification_exhausted — 4 wrong attempts → terminal_verification_exhausted."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_payment_counter,
    expect_terminal,
    expect_tool_called,
    expect_tool_not_called,
    expect_verification_counter,
    expect_verified,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, "verification_exhausted", failures)
    expect_verified(snapshot, False, failures)
    # Counter dropped to 0 across the first 3 failures; 4th fired
    # the terminal route, leaving the counter at 0.
    expect_verification_counter(snapshot, 0, failures)
    # Payment never attempted.
    expect_tool_called(snapshot, "submit_verification", failures)
    expect_tool_not_called(snapshot, "process_payment", failures)
    expect_payment_counter(snapshot, 5, failures)
    return failures
