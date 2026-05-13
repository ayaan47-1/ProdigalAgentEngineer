"""Eval harness — drives Agent through a persona script and collects
the result + assertions.

A persona is a JSON file describing a scripted dialog. The harness
loads it, runs a fresh ``Agent`` through the turns N times (3 for
critical, 1 otherwise per V2-28), and combines two kinds of checks:

  - Forbidden-substring sweep (rule #7 + #12 from the layering map):
    no on-file DOB / Aadhaar / pincode and no PAN- or CVV-shaped
    pattern in any agent reply.
  - Persona-specific assertion module: optional Python module at
    ``eval/assertions/<persona_id>.py`` exporting an
    ``assert_persona(snapshot, transcript) -> list[str]`` function.
    Returns failure strings; empty list = pass.

The two layers are combined in ``RunResult.failures``; a run passes
iff that list is empty. ``PersonaResult.overall_passed`` is True iff
ALL runs passed (the V2-28 3-of-3 bar for critical).
"""
from __future__ import annotations

import importlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from payment_agent.agent import Agent

from eval.account_fixtures import (
    fixture_for,
    forbidden_substrings_for_account,
)
from eval.forbidden_substrings import sweep_transcript


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class TurnRecord:
    """One user-input → agent-message exchange in a persona run."""

    user_input: str
    agent_message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class RunResult:
    """Outcome of one run of a persona (a persona may be re-run N times)."""

    persona_id: str
    run_idx: int
    passed: bool
    failures: list[str]
    snapshot: dict[str, Any]
    transcript: list[TurnRecord] = field(default_factory=list)


@dataclass
class PersonaResult:
    """Combined outcome across all runs of one persona."""

    persona_id: str
    tier: str
    critical: bool
    runs: list[RunResult]
    overall_passed: bool

    def n_passed(self) -> int:
        return sum(1 for r in self.runs if r.passed)

    def n_total(self) -> int:
        return len(self.runs)


# ---------------------------------------------------------------------------
# Persona loading
# ---------------------------------------------------------------------------


REQUIRED_FIELDS: tuple[str, ...] = (
    "persona_id",
    "tier",
    "critical",
    "turns",
)


def load_persona(path: Path | str) -> dict[str, Any]:
    """Load and structurally validate a persona JSON file.

    Raises ``ValueError`` if required fields are missing or have the
    wrong type. Persona format:

      {
        "persona_id": str,
        "tier": "functionality" | "compliance",
        "critical": bool,
        "description": str (optional),
        "account_id": str (optional; used for forbidden-substring sweep),
        "turns": [
          {"user_input": str},
          ...
        ]
      }
    """
    p = Path(path)
    with p.open("r") as f:
        data = json.load(f)

    for key in REQUIRED_FIELDS:
        if key not in data:
            raise ValueError(
                f"persona {p}: missing required field {key!r}"
            )

    if data["tier"] not in ("functionality", "compliance"):
        raise ValueError(
            f"persona {p}: tier must be 'functionality' or 'compliance', "
            f"got {data['tier']!r}"
        )
    if not isinstance(data["critical"], bool):
        raise ValueError(
            f"persona {p}: 'critical' must be a bool, "
            f"got {type(data['critical']).__name__}"
        )
    if not isinstance(data["turns"], list):
        raise ValueError(f"persona {p}: 'turns' must be a list")
    for i, turn in enumerate(data["turns"]):
        if not isinstance(turn, dict) or "user_input" not in turn:
            raise ValueError(
                f"persona {p}: turn {i} must be a dict with 'user_input'"
            )

    return data


def discover_personas(
    root: Path | str = "eval/personas",
    *,
    tier: str | None = None,
) -> list[dict[str, Any]]:
    """Scan ``root`` recursively for ``*.json`` persona files; return
    loaded persona dicts.

    Filters by ``tier`` ("functionality" / "compliance" / None for all).
    Personas are ordered by (tier, persona_id) for deterministic output.
    """
    base = Path(root)
    personas: list[dict[str, Any]] = []
    for json_path in sorted(base.rglob("*.json")):
        data = load_persona(json_path)
        if tier is not None and data["tier"] != tier:
            continue
        personas.append(data)
    personas.sort(key=lambda p: (p["tier"], p["persona_id"]))
    return personas


# ---------------------------------------------------------------------------
# Assertion module dispatch
# ---------------------------------------------------------------------------


def load_assertion_fn(persona_id: str) -> Callable | None:
    """Dynamically import ``eval.assertions.<persona_id>`` and return
    its ``assert_persona`` function, or ``None`` if no module exists.

    Signature expected:
      assert_persona(snapshot: dict, transcript: list[dict]) -> list[str]
    """
    module_name = f"eval.assertions.{persona_id}"
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    fn = getattr(module, "assert_persona", None)
    if not callable(fn):
        return None
    return fn


# ---------------------------------------------------------------------------
# Run a persona
# ---------------------------------------------------------------------------


def run_persona(
    persona: dict[str, Any],
    *,
    agent_factory: Callable[[], Agent] | None = None,
    n_runs: int | None = None,
    assertion_fn: Callable | None = None,
) -> PersonaResult:
    """Drive a fresh Agent through ``persona["turns"]`` ``n_runs`` times.

    Defaults:
      - ``agent_factory``: ``Agent`` (uses real Anthropic SDK from env)
      - ``n_runs``: 3 if persona is critical else 1 (V2-28)
      - ``assertion_fn``: looked up via ``load_assertion_fn(persona_id)``

    Each run:
      1. Construct a fresh agent.
      2. Step through turns; collect (user_input, agent_message) into
         a transcript.
      3. Capture ``Agent.snapshot()``.
      4. Run forbidden-substring sweep using
         ``persona.get("account_id")`` to look up account_fixtures.
      5. Run the persona-specific assertion function if available.
      6. Aggregate failures into a ``RunResult``.

    Returns a ``PersonaResult`` with ``overall_passed`` = all runs passed.
    """
    factory: Callable[[], Agent] = (
        agent_factory if agent_factory is not None else Agent
    )

    if n_runs is None:
        n_runs = 3 if persona["critical"] else 1

    if assertion_fn is None:
        assertion_fn = load_assertion_fn(persona["persona_id"])

    account_id = persona.get("account_id")
    if account_id:
        fixture = fixture_for(account_id) or {}
        account_dob = fixture.get("dob") if fixture.get("dob") != "NEEDS_VERIFY" else None
        account_aadhaar = (
            fixture.get("aadhaar_last4")
            if fixture.get("aadhaar_last4") != "NEEDS_VERIFY"
            else None
        )
        account_pincode = (
            fixture.get("pincode")
            if fixture.get("pincode") != "NEEDS_VERIFY"
            else None
        )
        account_specific = forbidden_substrings_for_account(account_id)
    else:
        account_dob = None
        account_aadhaar = None
        account_pincode = None
        account_specific = []

    runs: list[RunResult] = []
    for run_idx in range(n_runs):
        agent = factory()
        transcript: list[TurnRecord] = []
        for turn in persona["turns"]:
            user_input = turn["user_input"]
            response = agent.next(user_input)
            transcript.append(
                TurnRecord(
                    user_input=user_input,
                    agent_message=response.get("message", ""),
                )
            )

        snapshot = agent.snapshot()

        transcript_dicts = [t.to_dict() for t in transcript]
        failures: list[str] = []

        failures.extend(
            sweep_transcript(
                transcript_dicts,
                account_dob=account_dob,
                account_aadhaar=account_aadhaar,
                account_pincode=account_pincode,
                account_specific=account_specific,
            )
        )

        if assertion_fn is not None:
            try:
                assertion_failures = assertion_fn(snapshot, transcript_dicts)
            except Exception as e:  # noqa: BLE001
                assertion_failures = [
                    f"assertion module raised {type(e).__name__}: {e}"
                ]
            if assertion_failures:
                failures.extend(assertion_failures)

        runs.append(
            RunResult(
                persona_id=persona["persona_id"],
                run_idx=run_idx,
                passed=not failures,
                failures=failures,
                snapshot=snapshot,
                transcript=transcript,
            )
        )

    return PersonaResult(
        persona_id=persona["persona_id"],
        tier=persona["tier"],
        critical=persona["critical"],
        runs=runs,
        overall_passed=all(r.passed for r in runs),
    )
