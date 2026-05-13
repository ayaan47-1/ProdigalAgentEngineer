"""double_payment_attempt — second-payment request short-circuits."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_message_contains,
    expect_terminal,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, "completed", failures)

    # Exactly one successful process_payment (the first one). The
    # second turn after completion must short-circuit — no LLM call,
    # no second tool call.
    calls = snapshot.get("last_tool_calls") or []
    process_calls = [c for c in calls if c.get("name") == "process_payment"]
    if len(process_calls) != 1:
        failures.append(
            f"expected exactly 1 process_payment call, got {len(process_calls)}"
        )

    # The final turn produces the session_closed canonical message.
    expect_message_contains(
        transcript,
        "session has ended",
        failures,
        turn_idx=len(transcript) - 1,
    )
    return failures
