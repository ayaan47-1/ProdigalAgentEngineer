"""Thin Anthropic SDK wrapper.

The whole point of this module is the determinism contract: every call to
the Messages API uses the pinned model + temperature=0 + thinking disabled.
The contract is asserted in `test_llm_kwargs.py`; this module is its only
implementation.

Per DECISIONS:
  - #18 fail-soft: missing ANTHROPIC_API_KEY → return None without
    invoking the SDK. Caller (extract.py) handles None by trying a
    deterministic-only extraction or asking the user to clarify.
  - #19 model pin: ``claude-sonnet-4-6`` (dateless = pinned snapshot).
  - #20 thinking disabled: explicitly ``{"type": "disabled"}`` (NOT
    ``"enabled"`` and NOT ``"adaptive"`` — both deprecated/wrong here).

Architecture commit: this is the ONLY module that imports anthropic.
extract.py (Step 8) consumes `call_extract`; agent.py (Step 9) never sees
the SDK directly.

Tool-use mechanism: the model is forced to call a single named extraction
tool via ``tool_choice={"type":"tool","name":...}``. The tool's input
arguments are returned as a dict for extract.py to parse into
``ExtractionResult``. extract.py owns the tool schema and prompt content;
this module just dispatches the call and unpacks the response.
"""
from __future__ import annotations

import os
from typing import Any, cast

from anthropic import Anthropic

from payment_agent import config
from payment_agent.errors import LlmCallFailed


def call_extract(
    *,
    system_prompt: str,
    user_message: str,
    tool: dict[str, Any],
    tool_name: str,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Run a single forced-tool-use extraction call.

    Returns the dict of arguments the model passed to the named tool, or
    ``None`` if the no-key fail-soft path was taken (DECISIONS #18).

    Raises ``LlmCallFailed`` on any SDK exception OR on a response that
    doesn't include a tool_use block matching ``tool_name`` — both are
    bugs from the caller's perspective and should surface, not be silently
    treated as empty extractions.
    """
    resolved_key = api_key or os.environ.get(config.ANTHROPIC_API_KEY_ENV) or ""
    if not resolved_key:
        # Fail-soft per DECISIONS #18. The SDK is never instantiated.
        return None

    # Per-call client construction is intentional for the dual-path design
    # (env key vs. explicit api_key arg). A module-level cached client
    # initialized at import time would silently use the wrong key when
    # api_key is passed explicitly. Production fix: factory/DI pattern with
    # a cached client; out of scope for this submission.
    client = Anthropic(api_key=resolved_key)

    try:
        # The Anthropic SDK exposes precisely-typed TypedDicts for `tools`,
        # `tool_choice`, `messages`, and `thinking`. We accept dicts at this
        # boundary (extract.py owns the tool schema content); cast at the
        # SDK call site keeps both Pyright and runtime happy.
        response = client.messages.create(
            **cast(Any, config.LLM_CALL_KWARGS),
            system=system_prompt,
            messages=cast(
                Any, [{"role": "user", "content": user_message}]
            ),
            tools=cast(Any, [tool]),
            tool_choice=cast(Any, {"type": "tool", "name": tool_name}),
        )
    except Exception as e:  # noqa: BLE001 — wrap any SDK error as LlmCallFailed
        # NOTE: This flattens SDK error subclasses (anthropic.AuthenticationError,
        # RateLimitError, APIConnectionError, etc.) into a single LlmCallFailed.
        # Auth (401) is unrecoverable; rate-limit (429) is retryable; transient
        # network errors are retryable. The orchestrator (agent.py, Step 9)
        # currently treats all LLM failures uniformly. If granular handling is
        # needed, re-raise specific subtypes or carry an error-class enum on
        # LlmCallFailed. Deferred to Step 9 per Step-7 review.
        raise LlmCallFailed(f"Anthropic SDK call failed: {e}") from e

    return _extract_tool_input(response, tool_name)


def _extract_tool_input(response: Any, tool_name: str) -> dict[str, Any]:
    """Pull the named tool_use block's `input` dict from a Messages response.

    Two distinct failure modes are surfaced separately so eval-debug
    diagnostics aren't misleading:
      - Block absent (SDK ignored ``tool_choice``): "missing tool_use block"
      - Block present but ``input`` is not a dict (malformed): "non-dict input"

    Both raise ``LlmCallFailed`` — surfacing prevents the agent from
    silently treating an absent or malformed tool call as "no slots."
    """
    content = getattr(response, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "tool_use" and (
            getattr(block, "name", None) == tool_name
        ):
            args = getattr(block, "input", None)
            if not isinstance(args, dict):
                raise LlmCallFailed(
                    f"tool_use block for {tool_name!r} has non-dict input: "
                    f"{args!r}"
                )
            return args
    raise LlmCallFailed(
        f"Anthropic response missing tool_use block for {tool_name!r}"
    )
