"""scope_shift_refund — refund request triggers cancel_session."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_terminal,
    expect_tool_called,
    expect_tool_not_called,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, "cancelled", failures)
    expect_tool_called(snapshot, "cancel_session", failures)
    expect_tool_not_called(snapshot, "process_payment", failures)
    return failures
