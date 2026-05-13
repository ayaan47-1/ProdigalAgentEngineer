"""Tests for session.py — the v2 SessionState dataclass and substates.

SessionState is the imperative kernel mirror of conversation state. Mutated
only by tool implementations (single-writer discipline enforced by code
review + these tests). The Agent instance owns one SessionState across the
lifetime of a session; LLM never touches it directly.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payment_agent.session import (
    ConfirmationPending,
    LookupResult,
    PaymentState,
    SessionState,
    TerminalKind,
    VerificationState,
)


def _make_lookup(
    account_id: str = "ACC1001",
    full_name: str = "Nithin Jain",
    dob: date = date(1990, 5, 14),
    aadhaar_last4: str = "1234",
    pincode: str = "411001",
    balance: Decimal = Decimal("1250.75"),
) -> LookupResult:
    """Factory for LookupResult in tests — fills the on-file fields with
    realistic ACC1001 defaults so individual tests can override only what
    they care about."""
    return LookupResult(
        account_id=account_id,
        full_name=full_name,
        dob=dob,
        aadhaar_last4=aadhaar_last4,
        pincode=pincode,
        balance=balance,
    )


# ===========================================================================
# Enum / default values
# ===========================================================================


class TestTerminalKind:
    def test_has_all_seven_kinds(self):
        # Per DECISIONS_V2 V2-1 + tool returns: completed, verification_exhausted,
        # payment_exhausted, payment_unknown, account_not_found,
        # lookup_unresolvable, cancelled.
        assert {t.value for t in TerminalKind} == {
            "completed",
            "verification_exhausted",
            "payment_exhausted",
            "payment_unknown",
            "account_not_found",
            "lookup_unresolvable",
            "cancelled",
        }

    def test_is_string_enum(self):
        # StrEnum so JSON serialization is trivial.
        assert TerminalKind.COMPLETED == "completed"
        assert TerminalKind.CANCELLED.value == "cancelled"


class TestVerificationStateDefaults:
    def test_counter_starts_at_three(self):
        # Per DECISIONS_V2 V2-5: starts 3, decrements only on comparison fail.
        v = VerificationState()
        assert v.counter == 3

    def test_locked_name_is_none(self):
        v = VerificationState()
        assert v.locked_name is None

    def test_verified_is_false(self):
        v = VerificationState()
        assert v.verified is False


class TestPaymentStateDefaults:
    def test_counter_starts_at_five(self):
        # Per DECISIONS_V2 V2-5: starts 5, decrements only on typo-class api fail.
        p = PaymentState()
        assert p.counter == 5


# ===========================================================================
# LookupResult
# ===========================================================================


class TestLookupResult:
    def test_construction_with_required_fields(self):
        lr = _make_lookup()
        assert lr.account_id == "ACC1001"
        assert lr.full_name == "Nithin Jain"
        assert lr.dob == date(1990, 5, 14)
        assert lr.aadhaar_last4 == "1234"
        assert lr.pincode == "411001"
        assert lr.balance == Decimal("1250.75")
        assert lr.currency == "INR"  # default

    def test_currency_can_be_overridden(self):
        lr = LookupResult(
            account_id="ACC1001",
            full_name="x",
            dob=date(1990, 1, 1),
            aadhaar_last4="0000",
            pincode="000000",
            balance=Decimal("0"),
            currency="USD",
        )
        assert lr.currency == "USD"

    def test_holds_sensitive_on_file_fields(self):
        # The cached LookupResult must carry dob/aadhaar_last4/pincode so
        # submit_verification can run strict-compare against them. These
        # fields are sensitive *as agent output* — never echoed in user-
        # facing prose — but valid internal state.
        lr = _make_lookup()
        assert lr.dob == date(1990, 5, 14)
        assert lr.aadhaar_last4 == "1234"
        assert lr.pincode == "411001"


# ===========================================================================
# ConfirmationPending — binds (amount, last4) for process_payment
# ===========================================================================


class TestConfirmationPending:
    def test_construction(self):
        cp = ConfirmationPending(amount=Decimal("500"), last4="0366")
        assert cp.amount == Decimal("500")
        assert cp.last4 == "0366"

    def test_equality_by_value(self):
        # Same values → equal; this is what process_payment uses for matching.
        a = ConfirmationPending(amount=Decimal("500"), last4="0366")
        b = ConfirmationPending(amount=Decimal("500"), last4="0366")
        assert a == b

    def test_inequality_different_amount(self):
        a = ConfirmationPending(amount=Decimal("500"), last4="0366")
        b = ConfirmationPending(amount=Decimal("600"), last4="0366")
        assert a != b

    def test_inequality_different_last4(self):
        a = ConfirmationPending(amount=Decimal("500"), last4="0366")
        b = ConfirmationPending(amount=Decimal("500"), last4="9999")
        assert a != b


# ===========================================================================
# SessionState — defaults and substates
# ===========================================================================


class TestSessionStateDefaults:
    def test_lookup_is_none(self):
        s = SessionState()
        assert s.lookup is None

    def test_verification_initialized(self):
        s = SessionState()
        assert s.verification == VerificationState()

    def test_payment_initialized(self):
        s = SessionState()
        assert s.payment == PaymentState()

    def test_confirmation_pending_is_none(self):
        s = SessionState()
        assert s.confirmation_pending is None

    def test_terminal_is_none(self):
        s = SessionState()
        assert s.terminal is None

    def test_is_terminal_false_when_no_terminal(self):
        s = SessionState()
        assert s.is_terminal() is False

    def test_is_terminal_true_when_terminal_set(self):
        s = SessionState()
        s.terminal = TerminalKind.COMPLETED
        assert s.is_terminal() is True


class TestSessionStateIndependentInstances:
    """Each SessionState() must get FRESH substate instances (no shared state
    via the default_factory pattern). Regression guard against the classic
    mutable-default-argument trap.
    """

    def test_verification_not_shared_between_instances(self):
        a = SessionState()
        b = SessionState()
        a.verification.counter = 0
        assert b.verification.counter == 3

    def test_payment_not_shared_between_instances(self):
        a = SessionState()
        b = SessionState()
        a.payment.counter = 0
        assert b.payment.counter == 5


# ===========================================================================
# Snapshot — frozen dict view for eval introspection
# ===========================================================================


class TestSnapshotEmptySession:
    def test_returns_dict(self):
        s = SessionState()
        snap = s.snapshot()
        assert isinstance(snap, dict)

    def test_has_expected_keys(self):
        s = SessionState()
        snap = s.snapshot()
        assert set(snap.keys()) == {
            "lookup",
            "verification",
            "payment",
            "confirmation_pending",
            "terminal",
        }

    def test_lookup_is_none(self):
        s = SessionState()
        snap = s.snapshot()
        assert snap["lookup"] is None

    def test_verification_is_default_dict(self):
        s = SessionState()
        snap = s.snapshot()
        assert snap["verification"] == {
            "counter": 3,
            "locked_name": None,
            "verified": False,
        }

    def test_payment_is_default_dict(self):
        s = SessionState()
        snap = s.snapshot()
        assert snap["payment"] == {"counter": 5}

    def test_confirmation_pending_is_none(self):
        s = SessionState()
        snap = s.snapshot()
        assert snap["confirmation_pending"] is None

    def test_terminal_is_none(self):
        s = SessionState()
        snap = s.snapshot()
        assert snap["terminal"] is None


class TestSnapshotPopulatedSession:
    def test_lookup_serializes_as_dict(self):
        s = SessionState()
        s.lookup = _make_lookup()
        snap = s.snapshot()
        # Full LookupResult serializes verbatim. The forbidden-substring
        # sweep enforces "no DOB/Aadhaar/pincode in agent OUTPUT" — the
        # snapshot is internal eval state, not output, so it carries them.
        assert snap["lookup"] == {
            "account_id": "ACC1001",
            "full_name": "Nithin Jain",
            "dob": date(1990, 5, 14),
            "aadhaar_last4": "1234",
            "pincode": "411001",
            "balance": Decimal("1250.75"),
            "currency": "INR",
        }

    def test_verification_reflects_mutations(self):
        s = SessionState()
        s.verification.counter = 2
        s.verification.locked_name = "Nithin Jain"
        s.verification.verified = True
        snap = s.snapshot()
        assert snap["verification"] == {
            "counter": 2,
            "locked_name": "Nithin Jain",
            "verified": True,
        }

    def test_payment_reflects_mutations(self):
        s = SessionState()
        s.payment.counter = 3
        snap = s.snapshot()
        assert snap["payment"] == {"counter": 3}

    def test_confirmation_pending_serializes_as_dict(self):
        s = SessionState()
        s.confirmation_pending = ConfirmationPending(
            amount=Decimal("500"), last4="0366"
        )
        snap = s.snapshot()
        assert snap["confirmation_pending"] == {
            "amount": Decimal("500"),
            "last4": "0366",
        }

    def test_terminal_serializes_as_string_value(self):
        # Snapshot stringifies the enum so eval JSON-equality works without
        # importing TerminalKind everywhere.
        s = SessionState()
        s.terminal = TerminalKind.COMPLETED
        snap = s.snapshot()
        assert snap["terminal"] == "completed"


class TestSnapshotIsFrozen:
    """Snapshot should be a *copy*, not a live view. Mutating the returned
    dict must not affect the SessionState.
    """

    def test_mutating_snapshot_does_not_affect_session(self):
        s = SessionState()
        snap = s.snapshot()
        snap["verification"]["counter"] = 999
        assert s.verification.counter == 3

    def test_two_snapshots_are_independent(self):
        s = SessionState()
        snap1 = s.snapshot()
        snap2 = s.snapshot()
        snap1["verification"]["counter"] = 999
        assert snap2["verification"]["counter"] == 3


# ===========================================================================
# Substate mutation discipline (these are the contract tests for the
# "single-writer through tools" rule from V2-12; verified by code review
# at usage sites, but the *capability* exists at the dataclass layer)
# ===========================================================================


class TestMutationCapability:
    def test_can_mutate_verification_counter(self):
        s = SessionState()
        s.verification.counter -= 1
        assert s.verification.counter == 2

    def test_can_set_locked_name(self):
        s = SessionState()
        s.verification.locked_name = "Nithin Jain"
        assert s.verification.locked_name == "Nithin Jain"

    def test_can_set_verified(self):
        s = SessionState()
        s.verification.verified = True
        assert s.verification.verified is True

    def test_can_assign_lookup(self):
        s = SessionState()
        s.lookup = _make_lookup()
        assert s.lookup.account_id == "ACC1001"

    def test_can_set_and_clear_confirmation_pending(self):
        s = SessionState()
        s.confirmation_pending = ConfirmationPending(
            amount=Decimal("500"), last4="0366"
        )
        assert s.confirmation_pending is not None
        s.confirmation_pending = None
        assert s.confirmation_pending is None

    def test_can_set_terminal(self):
        s = SessionState()
        s.terminal = TerminalKind.CANCELLED
        assert s.terminal == TerminalKind.CANCELLED


# ===========================================================================
# Parameterized: every TerminalKind variant survives the snapshot round-trip
# ===========================================================================


@pytest.mark.parametrize("kind", list(TerminalKind))
def test_every_terminal_kind_serializes(kind: TerminalKind):
    s = SessionState()
    s.terminal = kind
    snap = s.snapshot()
    assert snap["terminal"] == kind.value
    assert s.is_terminal() is True
