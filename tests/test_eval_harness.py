"""Wiring tests for eval/harness.py and eval/runner.py.

Scope per user calibration: 5-7 tests. Focus on harness correctness +
redaction + runner exit-code semantics. Persona quality is not tested here
(personas exercise the live system; this layer just plumbs).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from eval import harness, runner
from payment_agent import agent as agent_module
from payment_agent.state import (
    ExtractedSlots,
    ExtractionResult,
    Intent,
    LookupEffectResult,
    LookupOutcome,
    LookupResponse,
    PaymentEffectResult,
)


# ---------------------------------------------------------------------------
# Fixtures: stub out extract + api so harness tests don't need network.
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed_agent_io(monkeypatch):
    """Patch extract_turn + api.lookup_account + api.process_payment with
    canned outputs. Returns a builder."""

    def build(
        extractions: list[ExtractionResult],
        lookups: list[LookupEffectResult] | None = None,
        payments: list[PaymentEffectResult] | None = None,
    ) -> None:
        ex_iter = iter(extractions)
        lookup_iter = iter(lookups or [])
        payment_iter = iter(payments or [])

        def fake_extract(user_input, stage, slots, *, api_key=None):
            try:
                return next(ex_iter)
            except StopIteration:
                return ExtractionResult(
                    slots=ExtractedSlots(), intent=Intent.AMBIGUOUS,
                )

        def fake_lookup(account_id, *, client=None):
            return next(lookup_iter)

        def fake_payment(*, client=None, **kwargs):
            return next(payment_iter)

        monkeypatch.setattr(agent_module, "extract_turn", fake_extract)
        monkeypatch.setattr(agent_module.api, "lookup_account", fake_lookup)
        monkeypatch.setattr(agent_module.api, "process_payment", fake_payment)

    return build


# ---------------------------------------------------------------------------
# harness.run_persona
# ---------------------------------------------------------------------------


def test_run_persona_includes_bootstrap_turn_then_dialog(stubbed_agent_io):
    """Bootstrap (auto-greet) + each dialog input each become a TurnTrace."""
    stubbed_agent_io(extractions=[
        ExtractionResult(slots=ExtractedSlots(account_id="ACC1001"),
                         intent=Intent.AMBIGUOUS),
    ], lookups=[LookupEffectResult(
        outcome=LookupOutcome.SUCCESS,
        account_data=LookupResponse(
            account_id="ACC1001", full_name="Nithin Jain",
            dob=date(1990, 5, 14), aadhaar_last4="1234",
            pincode="560001", balance=Decimal("1250.75"),
        ),
    )])
    persona = {"id": "test", "dialog": ["ACC1001"]}
    result = harness.run_persona(persona)
    # Bootstrap + 1 dialog turn = 2 turns.
    assert len(result.turns) == 2
    assert result.turns[0].user_input == ""    # bootstrap
    assert result.turns[1].user_input == "ACC1001"
    # Each turn captures the post-turn stage.
    assert result.turns[1].end_stage == "collecting_identity"


def test_snapshot_redacts_cvv_and_masks_pan_to_last4(stubbed_agent_io):
    """DECISIONS #13: CVV never written anywhere; PAN logged only as last-4.
    The harness snapshot honors this for any persona transcript."""
    from payment_agent.state import SlotStore
    slots = SlotStore(pan="4532015112830366", cvv="123", full_name="X")
    snap = harness._snapshot_slots(slots)
    assert "cvv" not in snap
    assert "pan" not in snap          # raw key dropped
    assert snap.get("pan_masked") == "**** **** **** 0366"
    # full_name kept (assertion modules need it; runner doesn't write it).
    assert snap.get("full_name") == "X"


def test_snapshot_drops_lookup_response_to_prevent_pii_in_transcript():
    """LookupResponse contains DOB / Aadhaar / pincode / full_name. Even
    though those are technically permitted in the snapshot dict, dropping
    the whole nested object prevents accidental leak when a future change
    starts serializing snapshots."""
    from payment_agent.state import SlotStore
    slots = SlotStore(
        lookup_response=LookupResponse(
            account_id="ACC1001", full_name="Nithin Jain",
            dob=date(1990, 5, 14), aadhaar_last4="1234",
            pincode="560001", balance=Decimal("1250.75"),
        ),
    )
    snap = harness._snapshot_slots(slots)
    assert "lookup_response" not in snap


# ---------------------------------------------------------------------------
# runner._format_transcript: redaction + structure
# ---------------------------------------------------------------------------


def test_format_transcript_writes_messages_only_no_slot_dump():
    """Sample-conversation markdown contains user inputs + agent messages
    only — no slot dumps, no counter dumps. Agent messages already exclude
    sensitive data per templates / DECISIONS #7."""
    persona = {"id": "demo", "description": "demo persona"}
    result = harness.PersonaResult(
        persona_id="demo",
        turns=[
            harness.TurnTrace(
                user_input="", agent_message="Hello!",
                end_stage="greeting", slots_snapshot={},
                counters_snapshot={},
            ),
            harness.TurnTrace(
                user_input="ACC1001", agent_message="Looking up...",
                end_stage="collecting_identity",
                slots_snapshot={"pan_masked": "**** **** **** 0366",
                                "full_name": "Nithin Jain"},
                counters_snapshot={"verification_retries": 0},
            ),
        ],
        final_stage="collecting_identity",
    )
    md = runner._format_transcript(persona, result)
    # Messages present.
    assert "Hello!" in md
    assert "Looking up..." in md
    assert "ACC1001" in md
    # Slot snapshot fields MUST NOT appear in the transcript markdown.
    assert "pan_masked" not in md
    assert "**** **** **** 0366" not in md
    assert "Nithin Jain" not in md
    assert "verification_retries" not in md


# ---------------------------------------------------------------------------
# runner CLI: exit-code semantics
# ---------------------------------------------------------------------------


def test_runner_exits_zero_when_all_personas_pass(monkeypatch, tmp_path):
    """Aggregate pass → exit 0. Stub harness to short-circuit live runs."""
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "demo.json").write_text(json.dumps(
        {"id": "demo", "dialog": []}
    ))
    monkeypatch.setattr(runner, "PERSONA_DIR", persona_dir)

    monkeypatch.setattr(
        runner, "run_persona",
        lambda persona, *, api_key=None: harness.PersonaResult(
            persona_id=persona["id"], final_stage="terminal_completed",
        ),
    )
    monkeypatch.setattr(
        runner, "_load_assertion",
        lambda pid: lambda result: [],   # no failures
    )
    rc = runner.main(["--no-transcripts"])
    assert rc == 0


def test_runner_exits_one_when_any_persona_fails(monkeypatch, tmp_path):
    """Any persona's assertion failure → exit 1."""
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "demo.json").write_text(json.dumps(
        {"id": "demo", "dialog": []}
    ))
    monkeypatch.setattr(runner, "PERSONA_DIR", persona_dir)
    monkeypatch.setattr(
        runner, "run_persona",
        lambda persona, *, api_key=None: harness.PersonaResult(
            persona_id=persona["id"], final_stage="closed",
        ),
    )
    monkeypatch.setattr(
        runner, "_load_assertion",
        lambda pid: lambda result: ["expected terminal_completed"],
    )
    rc = runner.main(["--no-transcripts"])
    assert rc == 1


def test_runner_exits_two_when_persona_filter_matches_nothing(
    monkeypatch, tmp_path,
):
    """--persona X where no persona has id X → exit code 2 (distinct from
    1 for assertion failures). Reviewer M2: prevents a CI false-positive
    where a typo in the persona name silently runs zero personas and exits 0."""
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "real.json").write_text(json.dumps(
        {"id": "real", "dialog": []}
    ))
    monkeypatch.setattr(runner, "PERSONA_DIR", persona_dir)
    rc = runner.main(["--no-transcripts", "--persona", "does_not_exist"])
    assert rc == 2


def test_runner_persona_filter_runs_only_named_persona(monkeypatch, tmp_path):
    """--persona foo runs only foo, not the rest of the corpus."""
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    for pid in ("a", "b", "c"):
        (persona_dir / f"{pid}.json").write_text(json.dumps(
            {"id": pid, "dialog": []}
        ))
    monkeypatch.setattr(runner, "PERSONA_DIR", persona_dir)

    seen_ids: list[str] = []

    def fake_run(persona, *, api_key=None):
        seen_ids.append(persona["id"])
        return harness.PersonaResult(persona_id=persona["id"],
                                     final_stage="terminal_completed")

    monkeypatch.setattr(runner, "run_persona", fake_run)
    monkeypatch.setattr(
        runner, "_load_assertion", lambda pid: lambda r: [],
    )
    rc = runner.main(["--no-transcripts", "--persona", "b"])
    assert rc == 0
    assert seen_ids == ["b"]
