"""greeting_then_account — bootstrap greeting + canonical message emitted."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_message_contains,
    expect_tool_called,
    expect_tool_call_subsequence,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    # First turn (bootstrap) must include the canonical greeting fragment.
    expect_message_contains(
        transcript,
        "Could you share your account ID",
        failures,
        turn_idx=0,
    )
    # The LLM must have used render_canonical_message for the greeting,
    # and lookup_account for ACC1001.
    expect_tool_called(snapshot, "render_canonical_message", failures)
    expect_tool_call_subsequence(
        snapshot,
        ["render_canonical_message", "lookup_account"],
        failures,
    )
    return failures
