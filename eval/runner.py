"""Eval runner: load personas, drive ``Agent``, evaluate assertions, write
sample conversation transcripts, report aggregate pass/fail.

Per DECISIONS #14: assertions hit STATE + side-effects, not message text
equality. Per DECISIONS #27: sample conversations are *captured runs*
from these personas — the transcripts written here are the artifacts the
submission's design doc + README cite, not hand-crafted dialogues.

Usage::

    ANTHROPIC_API_KEY=sk-... python -m eval.runner
    python -m eval.runner --persona happy_path
    python -m eval.runner --no-transcripts   # don't write sample_conversations/

Without ``ANTHROPIC_API_KEY``, ``extract_turn`` takes the deterministic-only
fail-soft path (regex extraction). Most personas will fail because natural-
language inputs land as AMBIGUOUS — informational, not a real measurement.

Exit code: 0 if all personas pass their assertions; 1 otherwise.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eval.harness import PersonaResult, run_persona

PERSONA_DIR = Path(__file__).parent / "personas"
ASSERTION_PKG = "eval.assertions"
TRANSCRIPT_DIR = Path(__file__).parent.parent / "sample_conversations"


def _load_personas(only: str | None = None) -> list[dict[str, Any]]:
    paths = sorted(PERSONA_DIR.glob("*.json"))
    personas: list[dict[str, Any]] = []
    for path in paths:
        with path.open() as f:
            persona = json.load(f)
        if only is not None and persona["id"] != only:
            continue
        personas.append(persona)
    return personas


def _load_assertion(
    persona_id: str,
) -> Callable[[PersonaResult], list[str]]:
    """Import ``eval.assertions.<persona_id>`` and return ``assert_persona``."""
    module = importlib.import_module(f"{ASSERTION_PKG}.{persona_id}")
    return module.assert_persona


def _format_transcript(persona: dict[str, Any], result: PersonaResult) -> str:
    """Render a captured run as a markdown transcript.

    Sensitive-data handling per DECISIONS #13: only emits user inputs and
    agent messages, both of which are already redaction-safe — extraction
    captures user-typed values (which the user typed; not new exposure)
    and templates own agent prose (which excludes sensitive fields per
    DECISIONS #7). The slot snapshots collected by the harness are NOT
    written here.
    """
    lines: list[str] = []
    lines.append(f"# Sample conversation: {persona['id']}")
    lines.append("")
    lines.append(f"_{persona.get('description', '').strip()}_")
    lines.append("")
    lines.append("---")
    lines.append("")
    for turn in result.turns:
        if turn.user_input:
            lines.append(f"**User:** {turn.user_input}")
        else:
            lines.append("**User:** _(session start; agent greets)_")
        lines.append("")
        lines.append(f"**Agent:** {turn.agent_message}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"**Final stage:** `{result.final_stage}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persona-driven eval runner")
    parser.add_argument(
        "--persona", help="Run only the persona with this id"
    )
    parser.add_argument(
        "--no-transcripts", action="store_true",
        help="Skip writing sample_conversations/ markdown",
    )
    args = parser.parse_args(argv)

    personas = _load_personas(only=args.persona)
    if not personas:
        print("No personas found", file=sys.stderr)
        return 2

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "[runner] ANTHROPIC_API_KEY not set — running in fail-soft "
            "regex-only mode; most personas will fail.",
            file=sys.stderr,
        )

    if not args.no_transcripts:
        TRANSCRIPT_DIR.mkdir(exist_ok=True)

    failures: dict[str, list[str]] = {}
    for persona in personas:
        pid = persona["id"]
        try:
            result = run_persona(persona, api_key=api_key)
        except Exception as e:  # noqa: BLE001 — surface as persona failure
            failures[pid] = [f"EXCEPTION {type(e).__name__}: {e}"]
            print(f"  ERR  {pid}: {e}")
            continue

        try:
            assert_fn = _load_assertion(pid)
            failure_msgs = assert_fn(result)
        except Exception as e:  # noqa: BLE001
            failure_msgs = [f"assertion module error: {type(e).__name__}: {e}"]

        if failure_msgs:
            failures[pid] = failure_msgs
            print(f"  FAIL {pid}: {len(failure_msgs)} assertion(s) failed")
            for msg in failure_msgs:
                print(f"       - {msg}")
        else:
            print(f"  PASS {pid}")

        if not args.no_transcripts:
            transcript_path = TRANSCRIPT_DIR / f"{pid}.md"
            transcript_path.write_text(_format_transcript(persona, result))

    print()
    total = len(personas)
    passed = total - len(failures)
    print(f"{passed}/{total} persona(s) passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
