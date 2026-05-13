"""local_card_validation_failure — Luhn-fail path doesn't burn retries."""
from __future__ import annotations

from typing import Any

from eval.assertions._helpers import (
    expect_payment_counter,
    expect_terminal,
    expect_verified,
)


def assert_persona(
    snapshot: dict[str, Any], transcript: list[dict[str, str]]
) -> list[str]:
    failures: list[str] = []
    expect_terminal(snapshot, "completed", failures)
    expect_verified(snapshot, True, failures)
    # The Luhn fail returned stage=local_validation, which never burns a
    # retry. The corrected card then succeeds. Final counter must be 5.
    expect_payment_counter(snapshot, 5, failures)
    return failures
