"""Tests for llm.py — the determinism contract.

Per DECISIONS #8 + #19 + #20:
  - model="claude-sonnet-4-6" (the dateless 4.6+ format IS the pinned snapshot)
  - temperature=0
  - thinking={"type": "disabled"} (and explicitly NOT "enabled" or "adaptive")
  - no extended-thinking budget (no budget_tokens key sneaking in)
  - no top_k / top_p / random kwargs that affect determinism

Per DECISIONS #18 fail-soft:
  - missing ANTHROPIC_API_KEY → return None without invoking the SDK

These assertions run on every kwarg of every SDK call. The whole point of
this test file is that the determinism contract is asserted in code, not
just in docs — config drift (e.g., someone flipping a default) fails CI
loudly.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from payment_agent import config, llm
from payment_agent.errors import LlmCallFailed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(tool_args: dict[str, Any] | None = None):
    """Construct a minimal Anthropic Message-shaped response with a tool_use block."""
    if tool_args is None:
        tool_args = {"slots": {}, "intent": "ambiguous"}
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="extract_turn",
                input=tool_args,
            )
        ],
        stop_reason="tool_use",
    )


def _tool_def() -> dict:
    """A minimal tool schema for testing — extract.py owns the real one."""
    return {
        "name": "extract_turn",
        "description": "Extract slots and intent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {"type": "object"},
                "intent": {"type": "string"},
            },
            "required": ["slots", "intent"],
        },
    }


@pytest.fixture
def mock_anthropic(monkeypatch):
    """Patch anthropic.Anthropic to return a MagicMock whose messages.create
    returns the canned _mock_response(). Yields the create-mock so tests can
    inspect call args."""
    mock_client_class = MagicMock()
    mock_create = MagicMock(return_value=_mock_response())
    mock_client_class.return_value.messages.create = mock_create
    monkeypatch.setattr(llm, "Anthropic", mock_client_class)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return mock_create


# ===========================================================================
# Section 1 — Determinism contract: every kwarg on every call
# ===========================================================================


class TestDeterminismContract:
    def test_model_pinned_to_claude_sonnet_4_6(self, mock_anthropic) -> None:
        llm.call_extract(
            system_prompt="extract test",
            user_message="hi",
            tool=_tool_def(),
            tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"

    def test_temperature_is_zero(self, mock_anthropic) -> None:
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert kwargs["temperature"] == 0
        # Defensive: not 0.0001, not "0", not None.
        assert isinstance(kwargs["temperature"], (int, float))
        assert kwargs["temperature"] == 0

    def test_thinking_explicitly_disabled(self, mock_anthropic) -> None:
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert "thinking" in kwargs
        assert kwargs["thinking"] == {"type": "disabled"}

    def test_thinking_type_is_not_enabled(self, mock_anthropic) -> None:
        # DECISIONS #20: explicitly guard against accidental "enabled"
        # (manual thinking with budget_tokens) on Sonnet 4.6.
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert kwargs["thinking"]["type"] != "enabled"

    def test_thinking_type_is_not_adaptive(self, mock_anthropic) -> None:
        # DECISIONS #20: also guard against "adaptive" — Anthropic's
        # recommended mode for Sonnet 4.6 when thinking is desired.
        # We want neither.
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert kwargs["thinking"]["type"] != "adaptive"

    def test_no_budget_tokens_in_thinking(self, mock_anthropic) -> None:
        # `budget_tokens` only appears under thinking={"type":"enabled"}.
        # Its presence with type="disabled" is a config bug.
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert "budget_tokens" not in kwargs["thinking"]

    def test_max_tokens_pinned(self, mock_anthropic) -> None:
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert kwargs["max_tokens"] == config.LLM_MAX_TOKENS

    def test_no_top_k_or_top_p(self, mock_anthropic) -> None:
        # top_k/top_p affect sampling; they must not be passed since
        # temp=0 alone doesn't fully eliminate variance if top_p drifts.
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert "top_k" not in kwargs
        assert "top_p" not in kwargs

    def test_kwargs_match_config_aggregate(self, mock_anthropic) -> None:
        # The whole determinism aggregate is in config.LLM_CALL_KWARGS;
        # llm.py splats it. If anyone adds a new key to LLM_CALL_KWARGS
        # and forgets to splat, this test fails.
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        for key, expected_value in config.LLM_CALL_KWARGS.items():
            assert kwargs.get(key) == expected_value, (
                f"kwarg {key!r} drift: expected {expected_value!r}, "
                f"got {kwargs.get(key)!r}"
            )

    def test_kwarg_set_is_exhaustively_whitelisted(self, mock_anthropic) -> None:
        # Whitelist-completeness: catches "someone adds top_k=10 (or any
        # other kwarg) directly at the call site, bypassing config." The
        # other tests catch value drift and missing splats; only this test
        # catches additions at the call site that aren't in config.
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        expected_keys = {
            # Determinism kwargs (from config.LLM_CALL_KWARGS):
            "model", "temperature", "thinking", "max_tokens",
            # Structural kwargs (per-call inputs):
            "system", "messages", "tools", "tool_choice",
        }
        assert set(kwargs.keys()) == expected_keys, (
            f"kwarg set drift: extra={set(kwargs.keys()) - expected_keys!r}, "
            f"missing={expected_keys - set(kwargs.keys())!r}"
        )


# ===========================================================================
# Section 2 — Tool-use dispatch (forced extraction)
# ===========================================================================


class TestToolDispatch:
    def test_tool_choice_forces_named_tool(self, mock_anthropic) -> None:
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": "extract_turn"}

    def test_tools_list_passed_through(self, mock_anthropic) -> None:
        tool = _tool_def()
        llm.call_extract(
            system_prompt="x", user_message="y",
            tool=tool, tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert kwargs["tools"] == [tool]

    def test_system_prompt_passed_through(self, mock_anthropic) -> None:
        prompt = "You are a slot extractor. Extract account_id, full_name, etc."
        llm.call_extract(
            system_prompt=prompt, user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        assert kwargs["system"] == prompt

    def test_user_message_in_messages_list(self, mock_anthropic) -> None:
        llm.call_extract(
            system_prompt="x", user_message="my account is ACC1001",
            tool=_tool_def(), tool_name="extract_turn",
        )
        kwargs = mock_anthropic.call_args.kwargs
        # Single-turn extraction architecture (DECISIONS framing): no prior
        # message history is passed.
        assert kwargs["messages"] == [
            {"role": "user", "content": "my account is ACC1001"}
        ]


# ===========================================================================
# Section 3 — Response parsing: tool_use block → dict
# ===========================================================================


class TestResponseParsing:
    def test_returns_tool_use_input_dict(self, mock_anthropic) -> None:
        mock_anthropic.return_value = _mock_response(
            {"slots": {"account_id": "ACC1001"}, "intent": "ambiguous"}
        )
        result = llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        assert result == {
            "slots": {"account_id": "ACC1001"},
            "intent": "ambiguous",
        }

    def test_response_without_tool_use_block_raises(self, mock_anthropic) -> None:
        # If the model fails to use the forced tool, that's an SDK contract
        # violation — surface as LlmCallFailed rather than silently returning {}.
        mock_anthropic.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="no tool call here")],
            stop_reason="end_turn",
        )
        with pytest.raises(LlmCallFailed, match="tool_use"):
            llm.call_extract(
                system_prompt="x", user_message="y",
                tool=_tool_def(), tool_name="extract_turn",
            )

    def test_empty_content_raises(self, mock_anthropic) -> None:
        mock_anthropic.return_value = SimpleNamespace(
            content=[], stop_reason="end_turn"
        )
        with pytest.raises(LlmCallFailed):
            llm.call_extract(
                system_prompt="x", user_message="y",
                tool=_tool_def(), tool_name="extract_turn",
            )

    def test_tool_use_block_with_non_dict_input_raises_distinct_error(
        self, mock_anthropic
    ) -> None:
        # MEDIUM #2 collapsed into HIGH #2: tool_use block IS present (correct
        # type+name), but block.input is not a dict (e.g., None, list, str).
        # Must raise with a "non-dict input" message — NOT the misleading
        # "missing tool_use block" message.
        mock_anthropic.return_value = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", name="extract_turn", input=None,
                )
            ],
            stop_reason="tool_use",
        )
        with pytest.raises(LlmCallFailed, match="non-dict input"):
            llm.call_extract(
                system_prompt="x", user_message="y",
                tool=_tool_def(), tool_name="extract_turn",
            )

    def test_tool_use_block_with_list_input_raises_distinct_error(
        self, mock_anthropic
    ) -> None:
        # Same shape as above with a list (different malformed value).
        mock_anthropic.return_value = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", name="extract_turn",
                    input=["not", "a", "dict"],
                )
            ],
            stop_reason="tool_use",
        )
        with pytest.raises(LlmCallFailed, match="non-dict input"):
            llm.call_extract(
                system_prompt="x", user_message="y",
                tool=_tool_def(), tool_name="extract_turn",
            )


# ===========================================================================
# Section 4 — Fail-soft: missing ANTHROPIC_API_KEY (DECISIONS #18)
# ===========================================================================


class TestNoKeyFailSoft:
    def test_returns_none_when_env_var_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Spy: ensure the SDK is not even instantiated when no key is present.
        mock_client_class = MagicMock()
        monkeypatch.setattr(llm, "Anthropic", mock_client_class)

        result = llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        assert result is None
        mock_client_class.assert_not_called()

    def test_returns_none_when_env_var_empty(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        mock_client_class = MagicMock()
        monkeypatch.setattr(llm, "Anthropic", mock_client_class)

        result = llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
        )
        assert result is None
        mock_client_class.assert_not_called()

    def test_explicit_api_key_arg_overrides_missing_env(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_client_class = MagicMock()
        mock_client_class.return_value.messages.create = MagicMock(
            return_value=_mock_response()
        )
        monkeypatch.setattr(llm, "Anthropic", mock_client_class)

        result = llm.call_extract(
            system_prompt="x", user_message="y",
            tool=_tool_def(), tool_name="extract_turn",
            api_key="explicit-key",
        )
        assert result is not None
        # SDK was instantiated with the explicit key, not from env.
        mock_client_class.assert_called_once_with(api_key="explicit-key")


# ===========================================================================
# Section 5 — SDK error handling
# ===========================================================================


class TestSdkErrors:
    def test_sdk_exception_becomes_llm_call_failed(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        # Simulate a transient API error.
        mock_client_class = MagicMock()
        mock_client_class.return_value.messages.create.side_effect = (
            RuntimeError("connection reset")
        )
        monkeypatch.setattr(llm, "Anthropic", mock_client_class)

        with pytest.raises(LlmCallFailed, match="connection reset"):
            llm.call_extract(
                system_prompt="x", user_message="y",
                tool=_tool_def(), tool_name="extract_turn",
            )
