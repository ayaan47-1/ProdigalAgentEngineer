"""Tool-use loop orchestrator — the v2 ``Agent`` class.

Spec interface: ``Agent.next(user_input: str) -> {"message": str}``.
Locked from v1; the surrounding implementation is rebuilt for LLM-driven
orchestration per DECISIONS_V2.

Per-turn lifecycle (V2-14):
  1. Terminal short-circuit — if ``_session.terminal`` is set, return
     the canonical session_closed message without invoking the LLM.
  2. Append the user message to ``_history`` (with a sentinel for the
     empty-input bootstrap, V2-17).
  3. Tool-use loop: call the LLM, execute any tool_use blocks the model
     emits (mutating ``_session`` via tool handlers), append the
     tool_result blocks, iterate. Bounded by ``ITERATION_CAP`` (V2-16).
  4. Return ``{"message": last assistant text block}``.

After any ``process_payment`` tool call, V2-15 PII scrubbing runs over
``_history`` to redact raw card data from past user messages and the
LLM's tool_use ``card`` argument.

Single-writer discipline (V2-12): only tool handlers mutate
``_session``. The orchestrator reads ``_session.terminal`` and
constructs the system prompt + tools list, but never writes to
SessionState.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

import httpx

from payment_agent import config, llm, tools
from payment_agent.errors import LlmCallFailed
from payment_agent.prompt import (
    EMPTY_INPUT_SENTINEL,
    SESSION_START_SENTINEL,
    SYSTEM_PROMPT,
)
from payment_agent.session import SessionState

_LOG = logging.getLogger(__name__)


# Regex for PAN-shaped digit groups (12-19 digits, optional spaces/dashes
# between digits). Used by the V2-15 history scrub.
_PAN_REGEX = re.compile(r"\b(?:\d[ -]?){11,18}\d\b")

# Regex for CVV-context patterns ("CVV 123", "cvv: 1234", "security code 999").
# Captures the prefix so we can preserve it in the replacement.
_CVV_REGEX = re.compile(
    r"\b(CVV|cvv|cv2|csc|sec(?:urity)?\s+code)\s*[:\-]?\s*\d{3,4}\b",
    re.IGNORECASE,
)

_CARD_REDACTED: str = "[card details redacted]"
_CVV_REDACTED: str = r"\1 [REDACTED]"


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for Decimal and date inside tool results."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return str(obj)


class Agent:
    """Conversational payment-collection agent (v2 — LLM-orchestrated).

    Construct one ``Agent`` per session. One payment per ``Agent``
    instance (DECISIONS #6); a new payment requires ``Agent()`` again.

    Test seam: pass ``llm_call`` to inject a fake LLM in unit tests.
    Defaults to ``llm.call_messages`` which talks to the real Anthropic
    SDK with the determinism contract from ``config.py``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_client: httpx.Client | None = None,
        llm_call: Callable[..., Any] | None = None,
    ):
        self._api_key: str | None = (
            api_key or os.environ.get(config.ANTHROPIC_API_KEY_ENV) or None
        )
        self._http_client: httpx.Client | None = http_client
        self._llm_call: Callable[..., Any] = llm_call or llm.call_messages
        self._history: list[dict[str, Any]] = []
        self._session: SessionState = SessionState()

    # -----------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------

    def next(self, user_input: str) -> dict[str, str]:
        """Process one user turn and return ``{"message": str}``.

        The interface and return shape are spec-locked. Internal
        complexity (LLM call, tool dispatch, history scrubbing) is
        invisible to callers.
        """
        if self._session.is_terminal():
            result = tools.render_canonical_message_handler(
                self._session, {"kind": "session_closed", "slots": {}}
            )
            return {"message": result.get("message", "")}

        self._append_user_message(user_input)
        reply = self._tool_use_loop()
        return {"message": reply}

    def snapshot(self) -> dict[str, Any]:
        """Return a frozen-dict view of kernel state plus a recent-tool-
        calls summary, for eval introspection (V2-13).
        """
        snap = self._session.snapshot()
        snap["last_tool_calls"] = self._summarize_recent_tool_calls()
        return snap

    # -----------------------------------------------------------------
    # User-message append (with empty-input bootstrap)
    # -----------------------------------------------------------------

    def _append_user_message(self, user_input: str) -> None:
        """Append a user message to history.

        Empty input on an empty history is the bootstrap pattern (the
        CLI calls ``agent.next("")`` once before the input loop). We
        substitute ``SESSION_START_SENTINEL`` which the system prompt
        directs the LLM to greet on. Mid-conversation empty input is
        unusual but possible from direct callers; use the empty-input
        sentinel so the prompt can distinguish.

        SECURITY: sentinel rewriting fires ONLY when ``user_input`` is
        empty or whitespace. Any non-empty input — including a literal
        user-typed ``"<session_start>"`` or ``"<empty_input>"`` —
        passes through verbatim as ordinary user content. This is the
        load-bearing invariant that prevents a user from triggering
        the bootstrap branch on demand. See the SECURITY notes on the
        sentinel constants in ``prompt.py``.
        """
        content = user_input
        if not content.strip():
            # Empty/whitespace input → orchestrator-controlled sentinel.
            if not self._history:
                content = SESSION_START_SENTINEL
            else:
                content = EMPTY_INPUT_SENTINEL
        # else: pass user content through verbatim, even if it happens
        # to equal a sentinel string. Do NOT add a special-case rewrite
        # for that — it would weaken the agent-controlled invariant.
        self._history.append({"role": "user", "content": content})

    # -----------------------------------------------------------------
    # Tool-use loop
    # -----------------------------------------------------------------

    def _tool_use_loop(self) -> str:
        """Iterate up to ``ITERATION_CAP`` rounds of LLM call → tool
        execution → re-call, returning the final assistant text.
        """
        last_text = ""

        for _iteration in range(config.ITERATION_CAP):
            try:
                response = self._llm_call(
                    system_prompt=SYSTEM_PROMPT,
                    messages=self._history,
                    tools=tools.TOOL_SCHEMAS,
                    api_key=self._api_key,
                )
            except LlmCallFailed as e:
                _LOG.warning("LLM call failed: %s", e)
                return (
                    "I'm having trouble responding right now. "
                    "Please try again."
                )

            assistant_content = _normalize_assistant_content(response.content)
            self._history.append(
                {"role": "assistant", "content": assistant_content}
            )

            text_parts = [
                block["text"]
                for block in assistant_content
                if block.get("type") == "text"
            ]
            if text_parts:
                last_text = "\n".join(text_parts)

            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "end_turn":
                break

            if stop_reason == "tool_use":
                self._execute_tool_use_blocks(assistant_content)
                continue

            # max_tokens / stop_sequence / unexpected — stop with whatever
            # text we have. Don't loop further.
            break

        return last_text or "(no response)"

    # -----------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------

    def _execute_tool_use_blocks(
        self, assistant_content: list[dict[str, Any]]
    ) -> None:
        """Dispatch every tool_use block in the latest assistant turn,
        append a tool_result block (user role) per Anthropic's tool-use
        protocol, and scrub history if process_payment fired.
        """
        tool_results: list[dict[str, Any]] = []
        process_payment_fired = False

        for block in assistant_content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            block_id = block.get("id", "")
            block_input = block.get("input") or {}

            handler = tools.TOOL_HANDLERS.get(name)
            if handler is None:
                result: dict[str, Any] = {
                    "error": "UNKNOWN_TOOL",
                    "message": f"Unknown tool: {name!r}",
                }
            else:
                result = handler(
                    self._session,
                    block_input,
                    http_client=self._http_client,
                )

            if name == tools.TOOL_PROCESS_PAYMENT:
                process_payment_fired = True

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block_id,
                    "content": json.dumps(result, default=_json_default),
                }
            )

        if tool_results:
            self._history.append(
                {"role": "user", "content": tool_results}
            )

        if process_payment_fired:
            self._scrub_card_data_from_history()

    # -----------------------------------------------------------------
    # PII scrubbing (V2-15)
    # -----------------------------------------------------------------

    def _scrub_card_data_from_history(self) -> None:
        """Walk history and redact raw card data after process_payment.

        - User messages with string content: regex-replace card-shaped
          digit groups with ``[card details redacted]`` and CVV-context
          patterns with ``[REDACTED]``.
        - Assistant tool_use blocks named ``process_payment``: replace
          the ``card`` field of ``input`` with ``{"redacted": True}``.
        - Assistant text blocks: same regex scrub as user messages.
        """
        for msg in self._history:
            content = msg.get("content")
            role = msg.get("role")
            if role == "user" and isinstance(content, str):
                scrubbed = _PAN_REGEX.sub(_CARD_REDACTED, content)
                scrubbed = _CVV_REGEX.sub(_CVV_REDACTED, scrubbed)
                if scrubbed != content:
                    msg["content"] = scrubbed
            elif role == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if (
                        btype == "tool_use"
                        and block.get("name") == tools.TOOL_PROCESS_PAYMENT
                    ):
                        inp = block.get("input")
                        if isinstance(inp, dict) and "card" in inp:
                            inp["card"] = {"redacted": True}
                    elif btype == "text" and isinstance(
                        block.get("text"), str
                    ):
                        text = block["text"]
                        scrubbed = _PAN_REGEX.sub(_CARD_REDACTED, text)
                        scrubbed = _CVV_REGEX.sub(_CVV_REDACTED, scrubbed)
                        if scrubbed != text:
                            block["text"] = scrubbed

    # -----------------------------------------------------------------
    # Eval introspection helper
    # -----------------------------------------------------------------

    def _summarize_recent_tool_calls(self) -> list[dict[str, Any]]:
        """Return a compact list of tool calls made this session.

        Walks history in order and emits one dict per tool_use block:
        ``{name, input_keys, result}``. ``input_keys`` is the set of
        top-level argument keys (avoids surfacing raw card data even
        pre-scrub). ``result`` is the subset of well-known fields eval
        typically asserts on (success/found/verified/stage/error_class/
        terminal).
        """
        results_by_id: dict[str, dict[str, Any]] = {}
        for msg in self._history:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                ):
                    tool_id = block.get("tool_use_id", "")
                    raw = block.get("content")
                    parsed: dict[str, Any]
                    try:
                        parsed = (
                            json.loads(raw) if isinstance(raw, str) else {}
                        )
                    except (json.JSONDecodeError, TypeError):
                        parsed = {}
                    results_by_id[tool_id] = parsed

        summaries: list[dict[str, Any]] = []
        for msg in self._history:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    not isinstance(block, dict)
                    or block.get("type") != "tool_use"
                ):
                    continue
                inp = block.get("input") or {}
                input_keys = (
                    sorted(inp.keys()) if isinstance(inp, dict) else []
                )
                result = results_by_id.get(block.get("id", ""), {})
                result_summary = {
                    k: result[k]
                    for k in (
                        "success",
                        "found",
                        "verified",
                        "stage",
                        "error_class",
                        "terminal",
                    )
                    if k in result
                }
                summaries.append(
                    {
                        "name": block.get("name", ""),
                        "input_keys": input_keys,
                        "result": result_summary,
                    }
                )
        return summaries


# ===========================================================================
# Helpers
# ===========================================================================


def _normalize_assistant_content(content: Any) -> list[dict[str, Any]]:
    """Convert SDK Pydantic content blocks (or already-dicts) into plain
    dict form for storage in history.

    Anthropic SDK returns Pydantic models for content blocks. Normalizing
    early so the rest of the orchestrator (and the scrubber) can treat
    history as plain JSON-serializable dicts uniformly. Test fakes can
    produce dict content directly and this path passes through.
    """
    if not isinstance(content, list):
        return []
    normalized: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict):
            normalized.append(dict(block))
            continue
        btype = getattr(block, "type", None)
        if btype == "text":
            normalized.append(
                {"type": "text", "text": getattr(block, "text", "")}
            )
        elif btype == "tool_use":
            normalized.append(
                {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": dict(getattr(block, "input", {}) or {}),
                }
            )
    return normalized
