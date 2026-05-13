"""account_enumeration — only one lookup per session, no second try."""
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
    expect_terminal(snapshot, "account_not_found", failures)

    # The lookup_account tool was called exactly once (for ACC9999).
    # The second user turn ("try ACC1001") must trigger the terminal
    # short-circuit and the canonical session_closed message — no LLM
    # call, no second lookup.
    calls = snapshot.get("last_tool_calls") or []
    lookup_calls = [c for c in calls if c.get("name") == "lookup_account"]
    if len(lookup_calls) != 1:
        failures.append(
            f"expected exactly 1 lookup_account call, got {len(lookup_calls)}"
        )

    # The final user turn produces the session_closed canonical (strict
    # decoration per V2-22: entire reply is the canonical string).
    expect_message_contains(
        transcript,
        "session has ended",
        failures,
        turn_idx=len(transcript) - 1,
    )
    return failures
