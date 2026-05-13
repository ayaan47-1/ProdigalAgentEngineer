"""account_not_found — terminal immediately, no retry."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_lookup_account_id,
    expect_terminal,
    expect_tool_called,
    expect_tool_not_called,
    expect_verified,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, "account_not_found", failures)
    expect_verified(snapshot, False, failures)
    expect_lookup_account_id(snapshot, None, failures)  # nothing cached
    expect_tool_called(snapshot, "lookup_account", failures)
    # No verification or payment attempts — session ended after the
    # unsuccessful lookup.
    expect_tool_not_called(snapshot, "submit_verification", failures)
    expect_tool_not_called(snapshot, "process_payment", failures)
    return failures
