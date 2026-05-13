"""Tests for the v2 ``Agent`` orchestrator.

Covers the tool-use loop, terminal short-circuit, bootstrap behavior,
PII scrubbing, error handling, and the snapshot interface. The LLM is
mocked via the ``llm_call`` constructor injection — no real Anthropic
API calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from payment_agent import config, tools
from payment_agent.agent import Agent
from payment_agent.api import (
    LookupEffectResult,
    LookupOutcome,
    LookupResponse,
    PaymentEffectResult,
    PaymentOutcome,
)
from payment_agent.errors import LlmCallFailed
from payment_agent.prompt import SESSION_START_SENTINEL
from payment_agent.session import (
    ConfirmationPending,
    LookupResult,
    SessionState,
    TerminalKind,
)


# ===========================================================================
# Fake LLM infrastructure
# ===========================================================================


@dataclass
class _Block:
    """Mimics the Pydantic ContentBlock from the Anthropic SDK closely
    enough for the orchestrator's normalizer to handle. Uses attribute
    access (not dict access) so we cover the SDK path of the normalizer.
    """

    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeResponse:
    content: list[_Block]
    stop_reason: str


class _FakeLlm:
    """Records call args; returns scripted responses in order.

    Pass a list of ``_FakeResponse`` objects to the constructor. Each
    call consumes one. Raise on exhaustion so we catch tests that don't
    script enough turns.
    """

    def __init__(self, responses: list[_FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        api_key: str | None = None,
    ) -> _FakeResponse:
        # Deep-ish copy of messages so the test sees what the LLM was
        # called with at this point in time (history grows after).
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "messages": json.loads(json.dumps(messages, default=str)),
                "tools": tools,
                "api_key": api_key,
            }
        )
        if not self.responses:
            raise AssertionError(
                "FakeLlm out of scripted responses — test under-scripted."
            )
        return self.responses.pop(0)


def _text(text: str) -> _Block:
    return _Block(type="text", text=text)


def _tool_use(
    name: str, input_args: dict[str, Any], block_id: str = "tu_1"
) -> _Block:
    return _Block(type="tool_use", id=block_id, name=name, input=input_args)


def _resp_text(text: str) -> _FakeResponse:
    return _FakeResponse(content=[_text(text)], stop_reason="end_turn")


def _resp_tool(
    name: str, input_args: dict[str, Any], block_id: str = "tu_1"
) -> _FakeResponse:
    return _FakeResponse(
        content=[_tool_use(name, input_args, block_id)],
        stop_reason="tool_use",
    )


# ===========================================================================
# Construction
# ===========================================================================


class TestAgentInit:
    def test_empty_history(self):
        agent = Agent(api_key="x", llm_call=_FakeLlm([]))
        assert agent._history == []

    def test_fresh_session_state(self):
        agent = Agent(api_key="x", llm_call=_FakeLlm([]))
        assert isinstance(agent._session, SessionState)
        assert agent._session.verification.counter == 3
        assert agent._session.payment.counter == 5

    def test_env_api_key_used_when_not_passed(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-xyz")
        agent = Agent(llm_call=_FakeLlm([]))
        assert agent._api_key == "env-key-xyz"

    def test_explicit_api_key_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        agent = Agent(api_key="explicit", llm_call=_FakeLlm([]))
        assert agent._api_key == "explicit"


# ===========================================================================
# snapshot()
# ===========================================================================


class TestSnapshot:
    def test_returns_session_snapshot_keys(self):
        agent = Agent(api_key="x", llm_call=_FakeLlm([]))
        snap = agent.snapshot()
        # SessionState's keys plus the eval-helper one.
        assert {
            "lookup",
            "verification",
            "payment",
            "confirmation_pending",
            "terminal",
            "last_tool_calls",
        } <= set(snap.keys())

    def test_last_tool_calls_empty_initially(self):
        agent = Agent(api_key="x", llm_call=_FakeLlm([]))
        snap = agent.snapshot()
        assert snap["last_tool_calls"] == []

    def test_snapshot_is_independent_copy(self):
        agent = Agent(api_key="x", llm_call=_FakeLlm([]))
        snap = agent.snapshot()
        snap["verification"]["counter"] = 999
        assert agent._session.verification.counter == 3


# ===========================================================================
# Terminal short-circuit
# ===========================================================================


class TestTerminalShortCircuit:
    def test_returns_session_closed_message(self):
        fake = _FakeLlm([])
        agent = Agent(api_key="x", llm_call=fake)
        agent._session.terminal = TerminalKind.COMPLETED
        result = agent.next("anything")
        assert "session has ended" in result["message"].lower()

    def test_does_not_invoke_llm(self):
        fake = _FakeLlm([])
        agent = Agent(api_key="x", llm_call=fake)
        agent._session.terminal = TerminalKind.CANCELLED
        agent.next("hello?")
        assert fake.calls == []

    def test_does_not_mutate_history(self):
        agent = Agent(api_key="x", llm_call=_FakeLlm([]))
        agent._session.terminal = TerminalKind.VERIFICATION_EXHAUSTED
        agent.next("anything")
        assert agent._history == []

    @pytest.mark.parametrize(
        "kind",
        [
            TerminalKind.COMPLETED,
            TerminalKind.CANCELLED,
            TerminalKind.VERIFICATION_EXHAUSTED,
            TerminalKind.PAYMENT_EXHAUSTED,
            TerminalKind.PAYMENT_UNKNOWN,
            TerminalKind.ACCOUNT_NOT_FOUND,
            TerminalKind.LOOKUP_UNRESOLVABLE,
        ],
    )
    def test_every_terminal_kind_short_circuits(self, kind):
        agent = Agent(api_key="x", llm_call=_FakeLlm([]))
        agent._session.terminal = kind
        result = agent.next("ping")
        assert result["message"]  # non-empty


# ===========================================================================
# Bootstrap (empty input)
# ===========================================================================


class TestBootstrap:
    def test_empty_input_empty_history_uses_session_start_sentinel(self):
        fake = _FakeLlm([_resp_text("Hello!")])
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("")
        # The LLM saw a user message with the sentinel.
        first_call_messages = fake.calls[0]["messages"]
        assert first_call_messages[0]["role"] == "user"
        assert first_call_messages[0]["content"] == SESSION_START_SENTINEL

    def test_empty_input_non_empty_history_uses_empty_input_sentinel(self):
        # Simulate a history with one prior turn.
        fake = _FakeLlm([_resp_text("OK"), _resp_text("Done")])
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hello")  # populates history
        agent.next("")  # empty mid-conversation
        last_call = fake.calls[-1]["messages"]
        # The most recent user message is the sentinel.
        user_messages = [m for m in last_call if m["role"] == "user"]
        assert user_messages[-1]["content"] == "<empty_input>"

    def test_normal_input_appended_verbatim(self):
        fake = _FakeLlm([_resp_text("OK")])
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("ACC1001")
        assert agent._history[0]["role"] == "user"
        assert agent._history[0]["content"] == "ACC1001"


# ===========================================================================
# Tool-use loop
# ===========================================================================


class TestTextOnlyResponse:
    def test_returns_text(self):
        fake = _FakeLlm([_resp_text("Hi there!")])
        agent = Agent(api_key="x", llm_call=fake)
        result = agent.next("hello")
        assert result == {"message": "Hi there!"}

    def test_single_llm_call_for_text_only(self):
        fake = _FakeLlm([_resp_text("Hi there!")])
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hello")
        assert len(fake.calls) == 1

    def test_assistant_text_appended_to_history(self):
        fake = _FakeLlm([_resp_text("Hi there!")])
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hello")
        # History: user msg, assistant msg
        assert len(agent._history) == 2
        assert agent._history[1]["role"] == "assistant"
        assert agent._history[1]["content"][0]["type"] == "text"
        assert agent._history[1]["content"][0]["text"] == "Hi there!"


class TestToolUseDispatch:
    def test_render_canonical_message_dispatches(self, monkeypatch):
        # LLM emits a tool_use, then a text response after seeing the result.
        fake = _FakeLlm(
            [
                _resp_tool(
                    "render_canonical_message",
                    {"kind": "greeting", "slots": {}},
                ),
                _resp_text("Hello! ..."),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        result = agent.next("")
        # The tool was dispatched: tool_result block appended to history.
        tool_result_blocks = [
            b
            for msg in agent._history
            if msg["role"] == "user" and isinstance(msg["content"], list)
            for b in msg["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_result_blocks) == 1
        parsed = json.loads(tool_result_blocks[0]["content"])
        assert parsed["kind"] == "greeting"
        assert "Hello!" in parsed["message"]
        # Final text response returned to caller
        assert result["message"] == "Hello! ..."

    def test_unknown_tool_returns_error_result(self):
        fake = _FakeLlm(
            [
                _resp_tool("frobnicate", {"x": 1}),
                _resp_text("(recovered)"),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hi")
        tool_result = next(
            b
            for msg in agent._history
            if msg["role"] == "user" and isinstance(msg["content"], list)
            for b in msg["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        )
        parsed = json.loads(tool_result["content"])
        assert parsed["error"] == "UNKNOWN_TOOL"

    def test_lookup_account_dispatches_and_caches(self, monkeypatch):
        # Patch api.lookup_account so the lookup tool returns SUCCESS.
        def fake_lookup(account_id: str, *, client: Any = None):
            return LookupEffectResult(
                outcome=LookupOutcome.SUCCESS,
                account_data=LookupResponse(
                    account_id="ACC1001",
                    full_name="Nithin Jain",
                    dob=date(1990, 5, 14),
                    aadhaar_last4="1234",
                    pincode="411001",
                    balance=Decimal("1250.75"),
                ),
            )

        monkeypatch.setattr(tools.api, "lookup_account", fake_lookup)
        fake = _FakeLlm(
            [
                _resp_tool("lookup_account", {"account_id": "ACC1001"}),
                _resp_text("Found your account."),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("ACC1001")
        assert agent._session.lookup is not None
        assert agent._session.lookup.account_id == "ACC1001"


class TestIterationCap:
    def test_cap_enforced(self):
        # Script ITERATION_CAP+2 responses, all tool_use, so the loop
        # would run forever without the cap.
        responses = [
            _resp_tool(
                "render_canonical_message",
                {"kind": "greeting", "slots": {}},
                block_id=f"tu_{i}",
            )
            for i in range(config.ITERATION_CAP + 2)
        ]
        fake = _FakeLlm(responses)
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hi")
        # LLM should have been called exactly ITERATION_CAP times.
        assert len(fake.calls) == config.ITERATION_CAP

    def test_loop_breaks_on_end_turn_before_cap(self):
        fake = _FakeLlm(
            [
                _resp_tool(
                    "render_canonical_message",
                    {"kind": "greeting", "slots": {}},
                ),
                _resp_text("Done."),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hi")
        # Only 2 calls — loop broke on end_turn after the second.
        assert len(fake.calls) == 2

    def test_max_tokens_stop_reason_breaks(self):
        fake = _FakeLlm(
            [
                _FakeResponse(
                    content=[_text("partial output")],
                    stop_reason="max_tokens",
                ),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        result = agent.next("hi")
        assert result["message"] == "partial output"
        assert len(fake.calls) == 1


# ===========================================================================
# PII scrubbing (V2-15)
# ===========================================================================


def _build_verified_agent(
    monkeypatch: pytest.MonkeyPatch, payment_outcome: PaymentOutcome
) -> tuple[Agent, _FakeLlm]:
    """Construct an Agent with session in verified+confirmed state, plus a
    scripted FakeLlm that will fire ONE process_payment tool_use then end.
    """
    # Patch api.process_payment to return the requested outcome.
    def fake_process_payment(**kwargs: Any):
        return PaymentEffectResult(
            outcome=payment_outcome,
            transaction_id=(
                "txn_test"
                if payment_outcome == PaymentOutcome.SUCCESS
                else None
            ),
        )

    monkeypatch.setattr(tools.api, "process_payment", fake_process_payment)

    fake = _FakeLlm(
        [
            _resp_tool(
                "process_payment",
                {
                    "account_id": "ACC1001",
                    "amount": 500,
                    "card": {
                        "number": "4532015112830366",
                        "cvv": "123",
                        "expiry_month": 12,
                        "expiry_year": 2099,
                    },
                },
            ),
            _resp_text("OK."),
        ]
    )

    agent = Agent(api_key="x", llm_call=fake)
    agent._session.lookup = LookupResult(
        account_id="ACC1001",
        full_name="Nithin Jain",
        dob=date(1990, 5, 14),
        aadhaar_last4="1234",
        pincode="411001",
        balance=Decimal("1250.75"),
    )
    agent._session.verification.locked_name = "nithin jain"
    agent._session.verification.verified = True
    agent._session.confirmation_pending = ConfirmationPending(
        amount=Decimal("500"), last4="0366"
    )

    # Plant a prior user message with raw card details so the scrub can
    # operate on something realistic.
    agent._history.append(
        {
            "role": "user",
            "content": (
                "My card is 4532 0151 1283 0366, CVV 123, expires 12/27"
            ),
        }
    )
    return agent, fake


class TestPiiScrubbing:
    def test_pan_redacted_in_user_message(self, monkeypatch):
        agent, _ = _build_verified_agent(monkeypatch, PaymentOutcome.SUCCESS)
        agent.next("yes")
        # The pre-existing user message containing the PAN should be
        # redacted after process_payment ran.
        card_msg = next(
            m
            for m in agent._history
            if m["role"] == "user"
            and isinstance(m["content"], str)
            and "card" in m["content"].lower()
        )
        assert "4532 0151 1283 0366" not in card_msg["content"]
        assert "4532015112830366" not in card_msg["content"]
        assert "[card details redacted]" in card_msg["content"]

    def test_cvv_pattern_redacted(self, monkeypatch):
        agent, _ = _build_verified_agent(monkeypatch, PaymentOutcome.SUCCESS)
        agent.next("yes")
        card_msg = next(
            m
            for m in agent._history
            if m["role"] == "user"
            and isinstance(m["content"], str)
            and "[REDACTED]" in m["content"]
        )
        assert "CVV 123" not in card_msg["content"]
        assert "[REDACTED]" in card_msg["content"]

    def test_tool_use_card_argument_redacted(self, monkeypatch):
        agent, _ = _build_verified_agent(monkeypatch, PaymentOutcome.SUCCESS)
        agent.next("yes")
        # Find the assistant tool_use block for process_payment.
        tool_use_block = next(
            b
            for msg in agent._history
            if msg["role"] == "assistant" and isinstance(msg["content"], list)
            for b in msg["content"]
            if isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("name") == "process_payment"
        )
        assert tool_use_block["input"]["card"] == {"redacted": True}

    def test_scrub_runs_on_payment_failure_too(self, monkeypatch):
        # PII scrubbing runs regardless of outcome — INVALID_CARD path.
        agent, _ = _build_verified_agent(
            monkeypatch, PaymentOutcome.INVALID_CARD
        )
        agent.next("yes")
        card_msg = next(
            m
            for m in agent._history
            if m["role"] == "user"
            and isinstance(m["content"], str)
            and "card" in m["content"].lower()
        )
        assert "[card details redacted]" in card_msg["content"]

    def test_scrub_does_not_run_for_non_payment_tools(self, monkeypatch):
        # No process_payment in this turn → no scrubbing.
        fake = _FakeLlm(
            [
                _resp_tool(
                    "render_canonical_message",
                    {"kind": "greeting", "slots": {}},
                ),
                _resp_text("Hi."),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        # Plant a PAN-shaped string in a user message.
        agent._history.append(
            {"role": "user", "content": "My number is 4532 0151 1283 0366"}
        )
        agent.next("hello")
        # The pre-existing PAN string should NOT be scrubbed (no process_payment ran).
        survives = any(
            "4532 0151 1283 0366" in m["content"]
            for m in agent._history
            if isinstance(m.get("content"), str)
        )
        assert survives


# ===========================================================================
# Error handling
# ===========================================================================


class TestErrorHandling:
    def test_llm_call_failed_returns_fallback_message(self):
        def raising_llm(**kwargs):
            raise LlmCallFailed("simulated SDK error")

        agent = Agent(api_key="x", llm_call=raising_llm)
        result = agent.next("hello")
        assert "trouble responding" in result["message"].lower()

    def test_llm_call_failed_does_not_set_terminal(self):
        def raising_llm(**kwargs):
            raise LlmCallFailed("network blip")

        agent = Agent(api_key="x", llm_call=raising_llm)
        agent.next("hello")
        # LLM failures degrade gracefully but the session is not
        # terminal — user can try again on the next turn.
        assert agent._session.terminal is None


# ===========================================================================
# SDK content normalization
# ===========================================================================


class TestNormalizeAssistantContent:
    def test_handles_attribute_access_blocks(self):
        # _Block uses attribute access (mimics SDK Pydantic).
        fake = _FakeLlm([_resp_text("hello")])
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hi")
        normalized = agent._history[1]["content"]
        assert normalized == [{"type": "text", "text": "hello"}]

    def test_handles_dict_blocks_passthrough(self):
        # FakeResponse with dict-shaped content (not _Block) should pass through.
        dict_response = _FakeResponse(
            content=[{"type": "text", "text": "from dict"}],
            stop_reason="end_turn",
        )
        fake = _FakeLlm([dict_response])
        agent = Agent(api_key="x", llm_call=fake)
        result = agent.next("hi")
        assert result["message"] == "from dict"

    def test_tool_use_block_normalized_with_id_name_input(self):
        fake = _FakeLlm(
            [
                _resp_tool(
                    "render_canonical_message",
                    {"kind": "greeting", "slots": {}},
                    block_id="abc_123",
                ),
                _resp_text("Done."),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hi")
        tu_block = next(
            b
            for msg in agent._history
            if msg["role"] == "assistant" and isinstance(msg["content"], list)
            for b in msg["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"
        )
        assert tu_block["id"] == "abc_123"
        assert tu_block["name"] == "render_canonical_message"
        assert tu_block["input"] == {"kind": "greeting", "slots": {}}


# ===========================================================================
# snapshot.last_tool_calls
# ===========================================================================


class TestSnapshotLastToolCalls:
    def test_summarizes_tool_calls_in_order(self, monkeypatch):
        # Patch lookup so the tool returns successfully.
        def fake_lookup(account_id: str, *, client: Any = None):
            return LookupEffectResult(
                outcome=LookupOutcome.SUCCESS,
                account_data=LookupResponse(
                    account_id="ACC1001",
                    full_name="Nithin Jain",
                    dob=date(1990, 5, 14),
                    aadhaar_last4="1234",
                    pincode="411001",
                    balance=Decimal("1250.75"),
                ),
            )

        monkeypatch.setattr(tools.api, "lookup_account", fake_lookup)
        fake = _FakeLlm(
            [
                _resp_tool(
                    "render_canonical_message",
                    {"kind": "greeting", "slots": {}},
                    block_id="tu_1",
                ),
                _resp_tool(
                    "lookup_account",
                    {"account_id": "ACC1001"},
                    block_id="tu_2",
                ),
                _resp_text("Found."),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        agent.next("hi")
        snap = agent.snapshot()
        names = [c["name"] for c in snap["last_tool_calls"]]
        assert names == ["render_canonical_message", "lookup_account"]
        # lookup_account result is summarized
        lookup_summary = next(
            c
            for c in snap["last_tool_calls"]
            if c["name"] == "lookup_account"
        )
        assert lookup_summary["result"]["found"] is True
        assert lookup_summary["result"]["stage"] == "api_response"

    def test_input_keys_redact_sensitive_values(self, monkeypatch):
        # input_keys lists keys without values, so raw card data never
        # appears in snapshot even pre-scrub.
        def fake_process_payment(**kwargs: Any):
            return PaymentEffectResult(
                outcome=PaymentOutcome.SUCCESS, transaction_id="txn_x"
            )

        monkeypatch.setattr(
            tools.api, "process_payment", fake_process_payment
        )
        fake = _FakeLlm(
            [
                _resp_tool(
                    "process_payment",
                    {
                        "account_id": "ACC1001",
                        "amount": 500,
                        "card": {
                            "number": "4532015112830366",
                            "cvv": "123",
                            "expiry_month": 12,
                            "expiry_year": 2099,
                        },
                    },
                    block_id="tu_p",
                ),
                _resp_text("Paid."),
            ]
        )
        agent = Agent(api_key="x", llm_call=fake)
        agent._session.lookup = LookupResult(
            account_id="ACC1001",
            full_name="Nithin Jain",
            dob=date(1990, 5, 14),
            aadhaar_last4="1234",
            pincode="411001",
            balance=Decimal("1250.75"),
        )
        agent._session.verification.locked_name = "nithin jain"
        agent._session.verification.verified = True
        agent._session.confirmation_pending = ConfirmationPending(
            amount=Decimal("500"), last4="0366"
        )
        agent.next("yes")
        snap = agent.snapshot()
        pp = next(
            c
            for c in snap["last_tool_calls"]
            if c["name"] == "process_payment"
        )
        assert pp["input_keys"] == ["account_id", "amount", "card"]
        # No raw PAN/CVV anywhere in the snapshot.
        snap_str = json.dumps(snap, default=str)
        assert "4532015112830366" not in snap_str
        assert "4532 0151 1283 0366" not in snap_str
