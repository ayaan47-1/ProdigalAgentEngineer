"""Persona-driven integration harness.

Drives a fresh ``Agent`` instance through a scripted dialog (JSON), captures
the per-turn (message, end-of-turn state, slot snapshot, counters) trace,
and returns a typed result for assertion modules to evaluate.

Per DECISIONS #14 + #27:
- Eval assertions hit STATE + side-effects, not message text equality (text
  is template-owned and asserted on properties in `test_templates.py`).
- Sample conversations are *captured runs* from these personas, not hand-
  crafted dialogue. ``run_persona`` produces the trace; ``runner.py``
  writes the user-readable transcript to ``sample_conversations/``.

Sensitive-data handling per DECISIONS #13:
- CVV is dropped from every slot snapshot (never written anywhere).
- PAN is replaced with ``mask_pan(pan)`` before the snapshot is recorded.
- DOB / Aadhaar / pincode / full_name are kept in snapshots for assertion
  use only — assertion modules must not echo them into transcripts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from payment_agent.agent import Agent
from payment_agent.redact import mask_pan
from payment_agent.state import Counters, SlotStore


@dataclass(frozen=True)
class TurnTrace:
    """One turn of a persona dialog: user input → agent message + state.

    ``user_input`` is the literal string the persona script supplies; the
    bootstrap turn (auto-greet) records ``user_input=""``.
    """

    user_input: str
    agent_message: str
    end_stage: str
    slots_snapshot: dict[str, Any]      # CVV stripped, PAN -> last-4 only
    counters_snapshot: dict[str, int]


@dataclass
class PersonaResult:
    """Aggregate result of running a persona end-to-end."""

    persona_id: str
    turns: list[TurnTrace] = field(default_factory=list)
    final_stage: str = ""
    final_slots_snapshot: dict[str, Any] = field(default_factory=dict)
    final_counters_snapshot: dict[str, int] = field(default_factory=dict)


def run_persona(
    persona: dict[str, Any], *, api_key: str | None = None
) -> PersonaResult:
    """Drive a fresh ``Agent`` through the persona's scripted dialog.

    The persona JSON shape is::

        {
          "id": "happy_path",
          "description": "ACC1001 user pays ₹500 successfully",
          "dialog": ["Hi", "ACC1001", "Nithin Jain, DOB 14-05-1990", ...]
        }

    ``run_persona`` invokes the auto-greet bootstrap (``agent.next("")``)
    BEFORE the first scripted turn so transcripts open with the agent's
    greeting — matches CLI behavior. Each scripted user input is plumbed
    to ``agent.next``; the per-turn trace records the message, end-of-turn
    stage, and redacted slot/counter snapshots.
    """
    agent = Agent(api_key=api_key)
    result = PersonaResult(persona_id=persona["id"])

    # Bootstrap (auto-greet via agent's GREETING-empty branch).
    bootstrap_response = agent.next("")
    result.turns.append(_capture_turn("", bootstrap_response, agent))

    for user_input in persona["dialog"]:
        response = agent.next(user_input)
        result.turns.append(_capture_turn(user_input, response, agent))

    result.final_stage = agent._stage.value
    result.final_slots_snapshot = _snapshot_slots(agent._slots)
    result.final_counters_snapshot = _snapshot_counters(agent._counters)
    return result


def _capture_turn(
    user_input: str, response: dict[str, str], agent: Agent
) -> TurnTrace:
    return TurnTrace(
        user_input=user_input,
        agent_message=response["message"],
        end_stage=agent._stage.value,
        slots_snapshot=_snapshot_slots(agent._slots),
        counters_snapshot=_snapshot_counters(agent._counters),
    )


def _snapshot_slots(slots: SlotStore) -> dict[str, Any]:
    """Return a JSON-friendly snapshot of slot state with sensitive fields
    redacted per DECISIONS #13.

    - CVV is dropped (never recorded).
    - PAN is replaced with ``mask_pan(pan)`` (last-4 only).
    - LookupResponse is dropped wholesale — its fields are sensitive
      (full_name, dob, aadhaar_last4, pincode) and assertions don't need
      them; assertion modules read from the original slots if needed.
    - Other fields (full_name, dob, aadhaar_last4, pincode) are kept so
      assertion modules can validate verification flow. They MUST NOT be
      copied verbatim into transcripts by the runner.
    """
    d = slots.model_dump(mode="json")
    d.pop("cvv", None)
    pan = d.pop("pan", None)
    # `is not None` so that an empty-string PAN doesn't silently skip
    # masking (validators should prevent it from reaching here, but the
    # harness sees intermediate state and shouldn't depend on that).
    if pan is not None:
        try:
            d["pan_masked"] = mask_pan(pan)
        except ValueError:
            d["pan_masked"] = "[invalid]"
    d.pop("lookup_response", None)
    return d


def _snapshot_counters(counters: Counters) -> dict[str, int]:
    return counters.model_dump()
