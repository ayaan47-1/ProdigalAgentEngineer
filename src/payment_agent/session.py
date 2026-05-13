"""Session state — the imperative kernel mirror.

`SessionState` is one of two parallel stores on the Agent instance (the
other is the LLM message log in `agent._history`). It exists because the
kernel needs O(1) reads of retry counters, verified flag, lookup results,
and confirmation-pending values at tool-call time; reconstructing these
from history each turn would be wasteful and error-prone.

**Single-writer discipline.** Only tool implementations mutate
`SessionState`. The orchestrator (`agent.py`) reads `terminal` for the
short-circuit check and passes the SessionState into tools; it never
writes. The LLM never sees `SessionState` as a structured object — only
through tool returns, which are projections of SessionState plus the
operation outcome.

Substates:
  - LookupResult: set by `lookup_account` on success
  - VerificationState: counter (3), locked_name (anti-pivot), verified flag
  - PaymentState: counter (5 typo-class budget)
  - ConfirmationPending: bound (amount, last4) for process_payment to honor
  - terminal: TerminalKind | None — once set, agent.next() short-circuits

See `DECISIONS_V2.md` V2-5, V2-10, V2-11, V2-12, V2-13 for the rationale.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any


class TerminalKind(StrEnum):
    """The seven session-terminal states the kernel can route to.

    Distinct kinds let the LLM (via tool returns) and the eval harness
    (via `Agent.snapshot()`) distinguish *why* the session ended.
    """

    COMPLETED = "completed"
    VERIFICATION_EXHAUSTED = "verification_exhausted"
    PAYMENT_EXHAUSTED = "payment_exhausted"
    PAYMENT_UNKNOWN = "payment_unknown"
    ACCOUNT_NOT_FOUND = "account_not_found"
    LOOKUP_UNRESOLVABLE = "lookup_unresolvable"
    CANCELLED = "cancelled"


@dataclass
class LookupResult:
    """Cached on-file account data from a successful `lookup_account` call.

    Set once per session (first successful lookup). Subsequent
    `lookup_account` calls return `ACCOUNT_ALREADY_LOOKED_UP` without
    touching the API — see V2-3 and the `lookup_account` tool schema.

    Holds the full LookupResponse content so `submit_verification` can
    run the strict-equality compare against the cached on-file values
    (`dob`, `aadhaar_last4`, `pincode`) without re-fetching. These three
    fields are sensitive: tool returns and the agent's user-facing prose
    must NEVER echo them. The forbidden-substring sweep in eval enforces
    this contract from the output side; this dataclass is the canonical
    *internal* store.
    """

    account_id: str
    full_name: str
    dob: date
    aadhaar_last4: str
    pincode: str
    balance: Decimal
    currency: str = "INR"


@dataclass
class VerificationState:
    """Per-cycle verification state.

    A verification cycle begins at the successful `lookup_account` and
    ends at `verified=True` or `terminal=verification_exhausted`. The
    `locked_name` enforces anti-pivot (V2-10): once set on the first
    submit attempt in a cycle, a different `full_name` returns
    `CYCLE_VIOLATION_NAME` without burning a retry.
    """

    counter: int = 3
    locked_name: str | None = None
    verified: bool = False


@dataclass
class PaymentState:
    """Payment typo-class retry budget (V2-7).

    Decrements only on documented typo-class API responses
    (INVALID_CARD / INVALID_CVV / INVALID_EXPIRY / INVALID_AMOUNT_SERVER).
    INSUFFICIENT_BALANCE re-prompts unbounded (no decrement).
    Local-validation failures and preconditions failures never decrement.
    Transient/unknown outcomes are immediate-terminal (no decrement,
    routes to `payment_unknown`).
    """

    counter: int = 5


@dataclass
class ConfirmationPending:
    """The value-binding handle for the structural confirmation gate.

    Set by `render_canonical_message(kind="confirmation_prompt")`; read
    and consumed by `process_payment`. `process_payment` requires this
    flag set with **matching** `(amount, last4)`; mismatch returns
    `CONFIRMATION_MISMATCH`, missing returns `NOT_CONFIRMED`. Neither
    burns a retry. Per V2-11.

    Equality by value (dataclass default) is the matching semantic used
    by the tool implementation.
    """

    amount: Decimal
    last4: str


@dataclass
class SessionState:
    """The imperative kernel mirror of conversation state.

    Mutated only by tool implementations. Read by `agent.next()` for
    terminal short-circuit; read by `Agent.snapshot()` for eval
    introspection.
    """

    lookup: LookupResult | None = None
    verification: VerificationState = field(default_factory=VerificationState)
    payment: PaymentState = field(default_factory=PaymentState)
    confirmation_pending: ConfirmationPending | None = None
    terminal: TerminalKind | None = None

    def is_terminal(self) -> bool:
        """True once any tool has driven the session to a terminal kind."""
        return self.terminal is not None

    def snapshot(self) -> dict[str, Any]:
        """Frozen dict view for eval introspection.

        Returns a deep-copied dict so that mutating the returned dict
        does not affect this SessionState (and vice versa). Used by the
        eval harness to assert on terminal kind, counters, verified
        flag, locked name, etc., without depending on agent prose.

        TerminalKind is stringified to its `.value` so JSON-equality
        comparison in eval doesn't require importing TerminalKind.
        Decimal values pass through as-is (eval converts where needed).
        """
        return {
            "lookup": asdict(self.lookup) if self.lookup is not None else None,
            "verification": asdict(self.verification),
            "payment": asdict(self.payment),
            "confirmation_pending": (
                asdict(self.confirmation_pending)
                if self.confirmation_pending is not None
                else None
            ),
            "terminal": self.terminal.value if self.terminal is not None else None,
        }
