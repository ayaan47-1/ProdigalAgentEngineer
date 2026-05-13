"""Shared assertion helpers used by persona-specific assertion modules.

Each ``expect_*`` mutates the ``failures`` list (passed by reference) so
callers can chain checks linearly without re-threading the list:

    def assert_persona(snapshot, transcript):
        failures = []
        expect_terminal(snapshot, "completed", failures)
        expect_verified(snapshot, True, failures)
        return failures

The underscore prefix on this module name protects it from being
treated as an assertion module by ``load_assertion_fn`` (which is
keyed on persona_id and personas can't have ``_``-prefixed ids).
"""
from __future__ import annotations

from typing import Any


def expect_terminal(
    snapshot: dict[str, Any], expected: str | None, failures: list[str]
) -> None:
    """Assert ``snapshot["terminal"]`` matches the expected kind."""
    actual = snapshot.get("terminal")
    if actual != expected:
        failures.append(f"expected terminal={expected!r}, got {actual!r}")


def expect_verified(
    snapshot: dict[str, Any], expected: bool, failures: list[str]
) -> None:
    actual = (snapshot.get("verification") or {}).get("verified")
    if actual != expected:
        failures.append(f"expected verified={expected}, got {actual}")


def expect_verification_counter(
    snapshot: dict[str, Any], expected: int, failures: list[str]
) -> None:
    actual = (snapshot.get("verification") or {}).get("counter")
    if actual != expected:
        failures.append(
            f"expected verification.counter={expected}, got {actual}"
        )


def expect_payment_counter(
    snapshot: dict[str, Any], expected: int, failures: list[str]
) -> None:
    actual = (snapshot.get("payment") or {}).get("counter")
    if actual != expected:
        failures.append(f"expected payment.counter={expected}, got {actual}")


def expect_lookup_account_id(
    snapshot: dict[str, Any], expected: str | None, failures: list[str]
) -> None:
    """Assert the cached ``lookup.account_id`` matches; ``expected=None``
    asserts no lookup was cached.
    """
    lookup = snapshot.get("lookup")
    if expected is None:
        if lookup is not None:
            failures.append(
                f"expected no cached lookup, got {lookup!r}"
            )
        return
    if not lookup or lookup.get("account_id") != expected:
        failures.append(
            f"expected lookup.account_id={expected!r}, got {lookup!r}"
        )


def expect_tool_called(
    snapshot: dict[str, Any], tool_name: str, failures: list[str]
) -> None:
    calls = [c.get("name") for c in snapshot.get("last_tool_calls", [])]
    if tool_name not in calls:
        failures.append(
            f"expected tool {tool_name!r} to be called; calls were {calls}"
        )


def expect_tool_not_called(
    snapshot: dict[str, Any], tool_name: str, failures: list[str]
) -> None:
    calls = [c.get("name") for c in snapshot.get("last_tool_calls", [])]
    if tool_name in calls:
        failures.append(
            f"expected tool {tool_name!r} NOT to be called; calls were {calls}"
        )


def expect_tool_call_subsequence(
    snapshot: dict[str, Any],
    expected_subseq: list[str],
    failures: list[str],
) -> None:
    """Assert ``expected_subseq`` appears as a subsequence (not
    necessarily contiguous) within the tool-call history. Useful for
    verifying ordering invariants like "lookup before verification
    before payment" without locking against extra render_canonical_message
    calls interleaved.
    """
    calls = [c.get("name") for c in snapshot.get("last_tool_calls", [])]
    i = 0
    for c in calls:
        if i < len(expected_subseq) and c == expected_subseq[i]:
            i += 1
    if i < len(expected_subseq):
        failures.append(
            f"expected tool subsequence {expected_subseq}, got {calls}"
        )


def expect_transcript_min_turns(
    transcript: list[dict[str, str]], n: int, failures: list[str]
) -> None:
    if len(transcript) < n:
        failures.append(
            f"expected at least {n} turns, got {len(transcript)}"
        )


def expect_message_contains(
    transcript: list[dict[str, str]],
    needle: str,
    failures: list[str],
    *,
    turn_idx: int | None = None,
) -> None:
    """Assert ``needle`` appears in at least one agent message.

    If ``turn_idx`` is set, check that turn only. Useful for asserting
    canonical strings (which are exact / substring per the per-kind
    decoration policy from V2-22) appear at expected points.
    """
    if turn_idx is not None:
        if turn_idx >= len(transcript):
            failures.append(
                f"expected turn {turn_idx} to exist, only {len(transcript)} turns"
            )
            return
        msg = transcript[turn_idx].get("agent_message", "") or ""
        if needle not in msg:
            failures.append(
                f"expected {needle!r} in agent reply at turn {turn_idx}; "
                f"got {msg[:120]!r}"
            )
        return
    for turn in transcript:
        if needle in (turn.get("agent_message", "") or ""):
            return
    failures.append(
        f"expected {needle!r} in some agent reply; none found"
    )


def expect_tool_call_result_field(
    snapshot: dict[str, Any],
    tool_name: str,
    field: str,
    expected: Any,
    failures: list[str],
) -> None:
    """Find the most recent tool call with the given name and assert
    its ``result[field]`` equals ``expected``. Skips calls without the
    field in their summarized result (snapshot filters to a subset).
    """
    for call in reversed(snapshot.get("last_tool_calls", [])):
        if call.get("name") != tool_name:
            continue
        result = call.get("result") or {}
        if field not in result:
            failures.append(
                f"tool {tool_name!r} result has no {field!r} key; "
                f"got {result!r}"
            )
            return
        actual = result[field]
        if actual != expected:
            failures.append(
                f"expected {tool_name}.result.{field}={expected!r}, "
                f"got {actual!r}"
            )
        return
    failures.append(
        f"expected to find tool call {tool_name!r}; none found"
    )
