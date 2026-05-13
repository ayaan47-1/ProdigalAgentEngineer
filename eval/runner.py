"""CLI for running eval personas against the real Agent.

Usage examples::

    # Run all personas (3-of-3 critical, 1 overall):
    python3 -m eval.runner

    # Run a single persona:
    python3 -m eval.runner --persona happy_path

    # Run one tier only:
    python3 -m eval.runner --tier functionality
    python3 -m eval.runner --tier compliance

    # Skip transcript capture (saves disk + write time):
    python3 -m eval.runner --no-transcripts

    # Bump critical to 5-of-5 (V2-28 fallback if 3-of-3 flakes):
    python3 -m eval.runner --multi-run-critical 5

Requires ``ANTHROPIC_API_KEY`` in the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from eval.harness import (
    PersonaResult,
    discover_personas,
    run_persona,
)


_TRANSCRIPTS_DIR: Path = Path("sample_conversations")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    personas = discover_personas(
        root="eval/personas", tier=(args.tier if args.tier != "all" else None)
    )

    if args.persona:
        personas = [p for p in personas if p["persona_id"] == args.persona]
        if not personas:
            print(f"error: no persona matched --persona={args.persona!r}")
            return 2

    if not personas:
        print(
            "no personas to run. add JSON files under "
            "eval/personas/functionality/ or eval/personas/compliance/."
        )
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. The agent will fail to call "
            "the LLM. Set the env var and re-run.",
            file=sys.stderr,
        )
        return 3

    results: list[PersonaResult] = []
    for idx, persona in enumerate(personas):
        if idx > 0 and args.inter_persona_sleep > 0:
            # Inter-persona backoff so a single eval pass stays under
            # Anthropic's per-minute rate limit. At Tier-1 (50 RPM) a
            # single happy_path persona is ~6-8 LLM calls; 3-of-3
            # critical sessions back-to-back can easily burst past
            # the limit. 2s default is conservative.
            time.sleep(args.inter_persona_sleep)
        n_runs = (
            args.multi_run_critical
            if persona["critical"]
            else args.multi_run_overall
        )
        print(
            f"running {persona['persona_id']} "
            f"({persona['tier']}, "
            f"{'critical' if persona['critical'] else 'overall'}, "
            f"n_runs={n_runs}) ..."
        )
        result = run_persona(persona, n_runs=n_runs)
        results.append(result)
        _print_result(result)
        if not args.no_transcripts:
            _write_transcript(result)

    _print_summary(results)
    return 0 if all(r.overall_passed for r in results) else 1


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eval.runner",
        description="Run payment-agent eval personas against the real Agent.",
    )
    parser.add_argument(
        "--persona",
        help="Run only the persona with this persona_id.",
    )
    parser.add_argument(
        "--tier",
        choices=("functionality", "compliance", "all"),
        default="all",
    )
    parser.add_argument(
        "--no-transcripts",
        action="store_true",
        help="Skip writing transcripts to sample_conversations/.",
    )
    parser.add_argument(
        "--multi-run-critical",
        type=int,
        default=3,
        help="Number of runs for critical personas (default 3; V2-28).",
    )
    parser.add_argument(
        "--multi-run-overall",
        type=int,
        default=1,
        help="Number of runs for non-critical personas (default 1).",
    )
    parser.add_argument(
        "--inter-persona-sleep",
        type=float,
        default=2.0,
        help=(
            "Seconds to sleep between persona runs to stay under "
            "Anthropic's per-minute rate limit (default 2)."
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _print_result(result: PersonaResult) -> None:
    pass_label = "PASS" if result.overall_passed else "FAIL"
    print(
        f"  [{pass_label}] {result.persona_id}: "
        f"{result.n_passed()}/{result.n_total()} runs passed"
    )
    if not result.overall_passed:
        for run in result.runs:
            if run.passed:
                continue
            print(f"    run {run.run_idx}:")
            for failure in run.failures:
                print(f"      - {failure}")


def _print_summary(results: list[PersonaResult]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.overall_passed)
    critical = [r for r in results if r.critical]
    critical_passed = sum(1 for r in critical if r.overall_passed)

    print()
    print("=== Summary ===")
    print(f"total: {passed}/{total} personas passed")
    if critical:
        print(
            f"critical: {critical_passed}/{len(critical)} passed "
            f"(target 100%)"
        )
    print()


def _write_transcript(result: PersonaResult) -> None:
    """Write a single markdown transcript of the first run to
    ``sample_conversations/<persona_id>.md``.

    Captures the first run only — subsequent runs of critical personas
    are for stability evidence, not separate transcripts. The pass/fail
    label and failure list are included so the file is useful even on
    failure.
    """
    _TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    first = result.runs[0] if result.runs else None
    if first is None:
        return

    lines: list[str] = []
    lines.append(f"# Sample conversation: {result.persona_id}")
    lines.append("")
    lines.append(
        f"_tier: {result.tier} | critical: {result.critical} | "
        f"result: {'PASS' if result.overall_passed else 'FAIL'} | "
        f"runs: {result.n_passed()}/{result.n_total()}_"
    )
    lines.append("")
    lines.append(f"_captured: {datetime.now(timezone.utc).isoformat()}_")
    lines.append("")
    lines.append("---")
    lines.append("")
    for turn in first.transcript:
        user_input = turn.user_input
        if not user_input.strip():
            lines.append("**User:** _(empty / bootstrap)_")
        else:
            lines.append(f"**User:** {user_input}")
        lines.append("")
        lines.append(f"**Agent:** {turn.agent_message}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        f"**Final snapshot.terminal:** "
        f"`{first.snapshot.get('terminal')}`"
    )
    if first.failures:
        lines.append("")
        lines.append("**Failures:**")
        for f in first.failures:
            lines.append(f"- {f}")

    out = _TRANSCRIPTS_DIR / f"{result.persona_id}.md"
    out.write_text("\n".join(lines) + "\n")


# Compact JSON dump utility, useful for piping to jq / CI parsers.
def dump_result_json(result: PersonaResult) -> str:
    """Serialize a PersonaResult to JSON. Used by tooling that wants
    machine-readable output."""
    payload = {
        "persona_id": result.persona_id,
        "tier": result.tier,
        "critical": result.critical,
        "overall_passed": result.overall_passed,
        "runs": [
            {
                "run_idx": r.run_idx,
                "passed": r.passed,
                "failures": r.failures,
                "snapshot": r.snapshot,
                "transcript": [t.to_dict() for t in r.transcript],
            }
            for r in result.runs
        ],
    }
    return json.dumps(payload, default=str, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
