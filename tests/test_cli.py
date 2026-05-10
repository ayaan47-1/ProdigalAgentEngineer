"""Wiring tests for the CLI REPL.

Scope per user calibration: 6 tests covering (1) auto-greet on startup;
(2) clean exit on EOF; (3) clean exit on quit/exit token; (4) SIGINT
exit code; (5) empty mid-conversation input is skipped; (6) each user
input is plumbed to agent.next and the response is printed.

The CLI is intentionally omitted from coverage gating per pyproject.toml
(thin REPL covered by manual smoke); these tests still exercise it for
wiring validation.
"""
from __future__ import annotations

from typing import Iterator

import pytest

from payment_agent import cli as cli_module


class _StubAgent:
    """Minimal agent stub. Records every call; returns canned messages."""

    def __init__(self, messages: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self._messages: list[str] = messages or []
        self._iter: Iterator[str] = iter(self._messages)

    def next(self, user_input: str) -> dict[str, str]:
        self.calls.append(user_input)
        try:
            msg = next(self._iter)
        except StopIteration:
            msg = f"[stub-default for input {user_input!r}]"
        return {"message": msg}


@pytest.fixture
def cli(monkeypatch):
    """Returns a builder: ``cli(stub, inputs)`` patches Agent + builtins.input
    and returns the stub for assertion."""

    def build(
        stub: _StubAgent, inputs: list[str | type[BaseException]],
    ) -> _StubAgent:
        # Replace the Agent class so cli.main() uses our stub.
        monkeypatch.setattr(cli_module, "Agent", lambda: stub)

        # Replace builtins.input to feed scripted inputs. Each item is
        # either a string (returned) or an exception class (raised).
        input_iter = iter(inputs)

        def fake_input(prompt: str = "") -> str:
            try:
                item = next(input_iter)
            except StopIteration as exc:
                # Default to EOF when scripted inputs run out, so tests
                # don't hang if a case forgets the terminator.
                raise EOFError() from exc
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            return item

        monkeypatch.setattr("builtins.input", fake_input)
        return stub

    return build


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_main_auto_greets_on_startup_via_empty_agent_next(cli, capsys):
    """Before any user input, main() calls agent.next('') once and prints
    the result. Matches natural chat feel — user sees the assistant speak
    first via the orchestrator's GREETING-bootstrap branch."""
    stub = _StubAgent(messages=["Hello! Please share your account ID."])
    cli(stub, [EOFError])  # immediate EOF after the greet
    rc = cli_module.main()
    out = capsys.readouterr().out
    assert "Hello! Please share your account ID." in out
    # First call MUST be the empty-input bootstrap, not user-driven.
    assert stub.calls == [""]
    assert rc == 0


def test_main_returns_zero_on_eof(cli):
    """Ctrl+D / end-of-input cleanly exits with code 0."""
    stub = _StubAgent(messages=["greet"])
    cli(stub, [EOFError])
    assert cli_module.main() == 0


@pytest.mark.parametrize("token", ["quit", "exit", "QUIT", "  exit  "])
def test_main_returns_zero_on_quit_token(cli, token):
    """Token 'quit' or 'exit' (case-insensitive, whitespace tolerated)
    exits cleanly without invoking agent.next on that input."""
    stub = _StubAgent(messages=["greet"])
    cli(stub, [token])
    rc = cli_module.main()
    assert rc == 0
    # Only the bootstrap call; the quit token does NOT reach agent.next.
    assert stub.calls == [""]


def test_main_returns_130_on_keyboard_interrupt(cli, capsys):
    """Ctrl+C surfaces as SIGINT-convention exit code 130. Stderr gets a
    visible '[interrupted]' marker so the user sees why."""
    stub = _StubAgent(messages=["greet"])
    cli(stub, [KeyboardInterrupt])
    rc = cli_module.main()
    assert rc == 130
    err = capsys.readouterr().err
    assert "interrupted" in err.lower()


def test_main_skips_empty_mid_conversation_input_silently(cli):
    """An empty/whitespace-only input mid-conversation is skipped (no
    agent.next call) — keeps the REPL responsive. The bootstrap empty
    call before the loop is the one exception."""
    stub = _StubAgent(messages=["greet", "stage-prompt"])
    cli(stub, ["", "   ", "ACC1001", EOFError])
    cli_module.main()
    # First call is the bootstrap (""); second is "ACC1001" — the two
    # empty/whitespace inputs in between are skipped.
    assert stub.calls == ["", "ACC1001"]


def test_main_plumbs_each_input_to_agent_next_and_prints_response(cli, capsys):
    """Each non-empty, non-quit user input gets handed to agent.next; the
    returned message is printed to stdout."""
    stub = _StubAgent(messages=[
        "GREET",
        "RESPONSE-1",
        "RESPONSE-2",
    ])
    cli(stub, ["ACC1001", "Nithin Jain", EOFError])
    cli_module.main()
    out = capsys.readouterr().out
    assert "GREET" in out
    assert "RESPONSE-1" in out
    assert "RESPONSE-2" in out
    # Bootstrap + 2 user inputs.
    assert stub.calls == ["", "ACC1001", "Nithin Jain"]
