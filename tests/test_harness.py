"""Tests for the eval harness: persona loading, forbidden-substring
sweep, and ``run_persona`` driver. Agent is mocked via the ``llm_call``
injection point so no real Anthropic API calls happen.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from eval.account_fixtures import (
    fixture_for,
    forbidden_substrings_for_account,
)
from eval.forbidden_substrings import (
    ForbiddenHit,
    find_hits,
    sweep_transcript,
)
from eval.harness import (
    PersonaResult,
    RunResult,
    TurnRecord,
    discover_personas,
    load_assertion_fn,
    load_persona,
    run_persona,
)
from payment_agent.agent import Agent
from payment_agent.session import TerminalKind


# ===========================================================================
# Fake LLM (mirrors the one in test_agent.py — kept local for test isolation)
# ===========================================================================


@dataclass
class _Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeResponse:
    content: list[_Block]
    stop_reason: str


class _ScriptedLlm:
    """Returns a fixed list of responses in order; raises if exhausted."""

    def __init__(self, responses: list[_FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(
                "ScriptedLlm out of responses — test under-scripted"
            )
        return self.responses.pop(0)


def _text_resp(text: str) -> _FakeResponse:
    return _FakeResponse(
        content=[_Block(type="text", text=text)], stop_reason="end_turn"
    )


# ===========================================================================
# account_fixtures
# ===========================================================================


class TestAccountFixtures:
    def test_known_account_returns_fixture(self):
        f = fixture_for("ACC1001")
        assert f is not None
        assert f["dob"] == "1990-05-14"
        assert f["full_name"] == "Nithin Jain"

    def test_unknown_account_returns_none(self):
        assert fixture_for("ACC9999") is None

    def test_forbidden_substrings_includes_iso_dob(self):
        forbidden = forbidden_substrings_for_account("ACC1001")
        assert "1990-05-14" in forbidden

    def test_forbidden_substrings_includes_dd_mm_yyyy(self):
        forbidden = forbidden_substrings_for_account("ACC1001")
        assert "14-05-1990" in forbidden
        assert "14/05/1990" in forbidden

    def test_forbidden_substrings_includes_mm_dd_yyyy(self):
        forbidden = forbidden_substrings_for_account("ACC1001")
        assert "05-14-1990" in forbidden
        assert "05/14/1990" in forbidden

    def test_skips_needs_verify_placeholder(self):
        # ACC1002 has NEEDS_VERIFY placeholders for aadhaar/pincode/name.
        # The DOB is real but the others should not appear.
        forbidden = forbidden_substrings_for_account("ACC1002")
        assert "1985-11-23" in forbidden
        assert "NEEDS_VERIFY" not in forbidden

    def test_unknown_account_returns_empty_list(self):
        assert forbidden_substrings_for_account("ACC9999") == []


# ===========================================================================
# forbidden_substrings — pattern-based detection
# ===========================================================================


class TestPanDetection:
    def test_finds_continuous_digit_pan(self):
        hits = find_hits("Your card 4532015112830366 is on file")
        assert any(h.category == "PAN" for h in hits)

    def test_finds_spaced_pan(self):
        hits = find_hits("Card: 4532 0151 1283 0366")
        assert any(h.category == "PAN" for h in hits)

    def test_finds_dash_separated_pan(self):
        hits = find_hits("Card 4532-0151-1283-0366 confirmed")
        assert any(h.category == "PAN" for h in hits)

    def test_does_not_flag_short_number(self):
        # 11 digits → below the 12-digit minimum.
        hits = find_hits("Your transaction id is 12345678901")
        assert not any(h.category == "PAN" for h in hits)


class TestCvvDetection:
    def test_finds_cvv_followed_by_3_digits(self):
        hits = find_hits("Your CVV 123 is on file")
        assert any(h.category == "CVV" for h in hits)

    def test_finds_cvv_with_colon(self):
        hits = find_hits("cvv: 999")
        assert any(h.category == "CVV" for h in hits)

    def test_finds_security_code(self):
        hits = find_hits("Security code 4567 received")
        assert any(h.category == "CVV" for h in hits)

    def test_does_not_flag_isolated_3_digit_number(self):
        # "123" alone is not necessarily a CVV.
        hits = find_hits("You owe 123 dollars")
        assert not any(h.category == "CVV" for h in hits)


class TestAccountSpecificDetection:
    def test_dob_match_flagged(self):
        hits = find_hits(
            "Your DOB is 1990-05-14",
            account_dob="1990-05-14",
        )
        cats = [h.category for h in hits]
        assert "ACCOUNT_DOB" in cats

    def test_aadhaar_match_flagged(self):
        hits = find_hits(
            "1234 on file",
            account_aadhaar="1234",
        )
        cats = [h.category for h in hits]
        assert "ACCOUNT_AADHAAR" in cats

    def test_pincode_match_flagged(self):
        hits = find_hits(
            "Your pincode 411001 is recorded",
            account_pincode="411001",
        )
        cats = [h.category for h in hits]
        assert "ACCOUNT_PINCODE" in cats

    def test_account_specific_extras_flagged(self):
        hits = find_hits(
            "14-05-1990 is the date",
            account_specific=["14-05-1990", "05-14-1990"],
        )
        cats = [h.category for h in hits]
        assert "ACCOUNT_SPECIFIC" in cats

    def test_no_hits_on_clean_text(self):
        hits = find_hits(
            "Please share your account ID to get started.",
            account_dob="1990-05-14",
            account_aadhaar="1234",
            account_pincode="411001",
        )
        assert hits == []


class TestSweepTranscript:
    def test_clean_transcript_no_failures(self):
        transcript = [
            {"user_input": "ACC1001", "agent_message": "Got it — looking up ACC1001..."},
            {"user_input": "", "agent_message": "Account found."},
        ]
        assert sweep_transcript(transcript, account_dob="1990-05-14") == []

    def test_dob_leak_produces_failure(self):
        transcript = [
            {
                "user_input": "anything",
                "agent_message": "Your DOB 1990-05-14 has been confirmed",
            },
        ]
        failures = sweep_transcript(transcript, account_dob="1990-05-14")
        assert len(failures) == 1
        assert "ACCOUNT_DOB" in failures[0]
        assert "1990-05-14" in failures[0]

    def test_pan_leak_produces_failure(self):
        transcript = [
            {
                "user_input": "anything",
                "agent_message": "Card 4532015112830366 captured",
            },
        ]
        failures = sweep_transcript(transcript)
        assert any("PAN" in f for f in failures)


# ===========================================================================
# Persona loading
# ===========================================================================


def _write_persona(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / f"{data['persona_id']}.json"
    p.write_text(json.dumps(data))
    return p


class TestLoadPersona:
    def test_loads_valid_persona(self, tmp_path):
        path = _write_persona(
            tmp_path,
            {
                "persona_id": "happy",
                "tier": "functionality",
                "critical": True,
                "turns": [{"user_input": ""}, {"user_input": "ACC1001"}],
            },
        )
        data = load_persona(path)
        assert data["persona_id"] == "happy"
        assert data["tier"] == "functionality"
        assert data["critical"] is True
        assert len(data["turns"]) == 2

    def test_missing_required_field_raises(self, tmp_path):
        path = _write_persona(
            tmp_path,
            {
                "persona_id": "bad",
                "tier": "functionality",
                "critical": True,
                # 'turns' missing
            },
        )
        with pytest.raises(ValueError, match="missing required field"):
            load_persona(path)

    def test_invalid_tier_raises(self, tmp_path):
        path = _write_persona(
            tmp_path,
            {
                "persona_id": "bad",
                "tier": "smoke",
                "critical": True,
                "turns": [],
            },
        )
        with pytest.raises(ValueError, match="tier must be"):
            load_persona(path)

    def test_non_bool_critical_raises(self, tmp_path):
        path = _write_persona(
            tmp_path,
            {
                "persona_id": "bad",
                "tier": "functionality",
                "critical": "yes",
                "turns": [],
            },
        )
        with pytest.raises(ValueError, match="critical"):
            load_persona(path)

    def test_turn_missing_user_input_raises(self, tmp_path):
        path = _write_persona(
            tmp_path,
            {
                "persona_id": "bad",
                "tier": "functionality",
                "critical": True,
                "turns": [{"not_user_input": "x"}],
            },
        )
        with pytest.raises(ValueError, match="user_input"):
            load_persona(path)


class TestDiscoverPersonas:
    def test_discovers_recursively(self, tmp_path):
        (tmp_path / "functionality").mkdir()
        (tmp_path / "compliance").mkdir()
        _write_persona(
            tmp_path / "functionality",
            {
                "persona_id": "p1",
                "tier": "functionality",
                "critical": True,
                "turns": [],
            },
        )
        _write_persona(
            tmp_path / "compliance",
            {
                "persona_id": "p2",
                "tier": "compliance",
                "critical": False,
                "turns": [],
            },
        )
        personas = discover_personas(tmp_path)
        ids = [p["persona_id"] for p in personas]
        assert set(ids) == {"p1", "p2"}

    def test_filters_by_tier(self, tmp_path):
        (tmp_path / "functionality").mkdir()
        (tmp_path / "compliance").mkdir()
        _write_persona(
            tmp_path / "functionality",
            {
                "persona_id": "p1",
                "tier": "functionality",
                "critical": True,
                "turns": [],
            },
        )
        _write_persona(
            tmp_path / "compliance",
            {
                "persona_id": "p2",
                "tier": "compliance",
                "critical": False,
                "turns": [],
            },
        )
        functional = discover_personas(tmp_path, tier="functionality")
        assert [p["persona_id"] for p in functional] == ["p1"]
        compliance = discover_personas(tmp_path, tier="compliance")
        assert [p["persona_id"] for p in compliance] == ["p2"]


# ===========================================================================
# load_assertion_fn
# ===========================================================================


class TestLoadAssertionFn:
    def test_returns_none_for_unknown(self):
        assert load_assertion_fn("does_not_exist_12345") is None

    def test_loads_existing_module(self, tmp_path, monkeypatch):
        # Create a real module under eval/assertions/_test_loaded.py and
        # confirm load_assertion_fn picks it up. We use a sentinel
        # function so the test is self-contained.
        import sys

        assertions_dir = Path("eval/assertions")
        marker = assertions_dir / "_test_loaded.py"
        marker.write_text(
            "def assert_persona(snapshot, transcript):\n"
            "    return ['unit-test-sentinel']\n"
        )
        # Clear any prior import cached at module level.
        sys.modules.pop("eval.assertions._test_loaded", None)
        try:
            fn = load_assertion_fn("_test_loaded")
            assert fn is not None
            assert fn({}, []) == ["unit-test-sentinel"]
        finally:
            marker.unlink(missing_ok=True)
            sys.modules.pop("eval.assertions._test_loaded", None)


# ===========================================================================
# run_persona — full integration with a mocked Agent
# ===========================================================================


def _agent_factory(responses: list[_FakeResponse]):
    """Return a zero-arg factory that produces an Agent with the given
    scripted LLM. Tests can assert on the agent's history via snapshot
    after a run.
    """

    def _factory() -> Agent:
        return Agent(api_key="x", llm_call=_ScriptedLlm(responses[:]))

    return _factory


class TestRunPersonaBasic:
    def test_single_run_text_only(self):
        persona = {
            "persona_id": "smoke",
            "tier": "functionality",
            "critical": False,
            "turns": [{"user_input": "hello"}],
        }
        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("Hi there!")]),
            assertion_fn=None,
        )
        assert result.persona_id == "smoke"
        assert result.tier == "functionality"
        assert result.critical is False
        assert result.overall_passed is True
        assert len(result.runs) == 1
        assert result.runs[0].transcript[0].agent_message == "Hi there!"

    def test_critical_runs_3_times_by_default(self):
        persona = {
            "persona_id": "smoke_crit",
            "tier": "functionality",
            "critical": True,
            "turns": [{"user_input": "hi"}],
        }
        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("OK")]),
            assertion_fn=None,
        )
        assert len(result.runs) == 3
        assert result.n_total() == 3

    def test_explicit_n_runs_overrides_default(self):
        persona = {
            "persona_id": "smoke",
            "tier": "functionality",
            "critical": True,
            "turns": [{"user_input": "hi"}],
        }
        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("OK")]),
            n_runs=5,
            assertion_fn=None,
        )
        assert len(result.runs) == 5


class TestRunPersonaForbiddenSweep:
    def test_no_account_id_no_account_specific_failures(self):
        # No account_id → no account-specific sweep; PAN-pattern sweep
        # still runs.
        persona = {
            "persona_id": "smoke",
            "tier": "functionality",
            "critical": False,
            "turns": [{"user_input": "hi"}],
        }
        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("Hello, no PII here.")]),
            assertion_fn=None,
        )
        assert result.overall_passed is True

    def test_pan_leak_in_agent_reply_fails(self):
        persona = {
            "persona_id": "pan_leak",
            "tier": "compliance",
            "critical": False,
            "turns": [{"user_input": "give me my card"}],
        }
        result = run_persona(
            persona,
            agent_factory=_agent_factory(
                [_text_resp("Your card is 4532015112830366")]
            ),
            assertion_fn=None,
        )
        assert result.overall_passed is False
        assert any("PAN" in f for f in result.runs[0].failures)

    def test_account_dob_leak_in_agent_reply_fails(self):
        persona = {
            "persona_id": "dob_leak",
            "tier": "compliance",
            "critical": False,
            "account_id": "ACC1001",
            "turns": [{"user_input": "whats my dob"}],
        }
        result = run_persona(
            persona,
            agent_factory=_agent_factory(
                [_text_resp("Your DOB is 1990-05-14 on file")]
            ),
            assertion_fn=None,
        )
        assert result.overall_passed is False
        assert any(
            "ACCOUNT_DOB" in f or "ACCOUNT_SPECIFIC" in f
            for f in result.runs[0].failures
        )


class TestRunPersonaAssertionFn:
    def test_assertion_fn_failures_aggregate(self):
        persona = {
            "persona_id": "assertion_test",
            "tier": "functionality",
            "critical": False,
            "turns": [{"user_input": "hi"}],
        }

        def assertion_fn(snapshot, transcript):
            return ["expected something else"]

        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("Hello.")]),
            assertion_fn=assertion_fn,
        )
        assert result.overall_passed is False
        assert "expected something else" in result.runs[0].failures

    def test_assertion_fn_pass_no_failures(self):
        persona = {
            "persona_id": "assertion_test",
            "tier": "functionality",
            "critical": False,
            "turns": [{"user_input": "hi"}],
        }

        def assertion_fn(snapshot, transcript):
            return []

        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("Hello.")]),
            assertion_fn=assertion_fn,
        )
        assert result.overall_passed is True

    def test_assertion_fn_exception_becomes_failure(self):
        persona = {
            "persona_id": "bad_assertion",
            "tier": "functionality",
            "critical": False,
            "turns": [{"user_input": "hi"}],
        }

        def assertion_fn(snapshot, transcript):
            raise RuntimeError("assertion code crashed")

        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("Hello.")]),
            assertion_fn=assertion_fn,
        )
        assert result.overall_passed is False
        assert any(
            "RuntimeError" in f and "assertion code crashed" in f
            for f in result.runs[0].failures
        )


class TestRunPersonaMultiRunSemantics:
    def test_one_failed_run_fails_critical_overall(self):
        # 3 runs; second one fails the assertion. Critical → overall fail.
        persona = {
            "persona_id": "flaky",
            "tier": "compliance",
            "critical": True,
            "turns": [{"user_input": "hi"}],
        }
        call_count = {"n": 0}

        def assertion_fn(snapshot, transcript):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return ["flake"]
            return []

        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("OK")]),
            assertion_fn=assertion_fn,
        )
        assert len(result.runs) == 3
        assert result.runs[0].passed is True
        assert result.runs[1].passed is False
        assert result.runs[2].passed is True
        assert result.overall_passed is False  # 3-of-3 not met
        assert result.n_passed() == 2

    def test_all_runs_pass_critical_overall_pass(self):
        persona = {
            "persona_id": "stable",
            "tier": "compliance",
            "critical": True,
            "turns": [{"user_input": "hi"}],
        }
        result = run_persona(
            persona,
            agent_factory=_agent_factory([_text_resp("OK")]),
            assertion_fn=lambda s, t: [],
        )
        assert result.overall_passed is True
        assert result.n_passed() == 3
