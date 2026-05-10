"""Extractor eval runner + scorer.

Runs every case in ``eval/corpus/extraction.json`` through
``payment_agent.extract.extract_turn`` and reports pass rates against the
PLAN §4 + DECISIONS #25 pass-bar:

- Critical subset: 100%
- Overall: 95%

Exits 0 if both bars met, 1 otherwise. Used as a manual quality gate
during prompt iteration; not run by pytest (extractor quality is not
unit-testable per DECISIONS #14).

Usage:
    ANTHROPIC_API_KEY=sk-... python -m eval.extractor_eval
    python -m eval.extractor_eval --subset critical
    python -m eval.extractor_eval --case-id leap_year_iso_acc1004
    python -m eval.extractor_eval --quiet  # summary only

Without ``ANTHROPIC_API_KEY``, the extractor takes the fail-soft path
(regex-only) and most cases will fail — this is informational for the
degraded-mode scenario, not a quality measurement.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from payment_agent.extract import extract_turn
from payment_agent.redact import REDACTED_PLACEHOLDER, mask_pan
from payment_agent.state import (
    ExtractedSlots,
    ExtractionResult,
    Intent,
    SlotStore,
    Stage,
)
from payment_agent.verify import SecondaryFactor

CORPUS_PATH = Path(__file__).parent / "corpus" / "extraction.json"

CRITICAL_PASS_BAR = 1.0   # 100%
OVERALL_PASS_BAR = 0.95   # 95%


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    subset: str
    passed: bool
    diffs: list[str]
    expected_intent: Intent
    actual_intent: Intent


def _load_corpus(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        return json.load(f)


def _build_slot_store(slots_in: dict[str, Any]) -> SlotStore:
    """Construct a SlotStore from a corpus ``slots_in`` dict.

    Type-coerces fields that arrive as strings (dob, amount, factor enum)
    so the SlotStore validates. Unknown fields raise a Pydantic
    ``ValidationError`` (``SlotStore`` inherits ``_StrictModel`` with
    ``extra="forbid"``) — corpus author typos surface as exception-class
    failures on the first run, not as silent no-ops. The eval's per-case
    ``except`` in ``_run_case`` carries that error into the failure diff.
    """
    coerced: dict[str, Any] = {}
    for key, value in slots_in.items():
        coerced[key] = _coerce_slot_value(key, value)
    return SlotStore(**coerced)


def _coerce_slot_value(field: str, value: Any) -> Any:
    """Coerce JSON-friendly types to the SlotStore's typed fields."""
    if value is None:
        return None
    if field == "dob" and isinstance(value, str):
        return date.fromisoformat(value)
    if field == "amount" and isinstance(value, (str, int, float)):
        return Decimal(str(value))
    if field == "selected_secondary_factor" and isinstance(value, str):
        return SecondaryFactor(value)
    return value


def _build_expected_slots(expected_slots: dict[str, Any]) -> ExtractedSlots:
    """Same coercion logic as _build_slot_store but for ExtractedSlots."""
    coerced: dict[str, Any] = {}
    for key, value in expected_slots.items():
        if value is None:
            coerced[key] = None
            continue
        if key in ("dob", "dob_alternate") and isinstance(value, str):
            coerced[key] = date.fromisoformat(value)
        elif key == "amount" and isinstance(value, (str, int, float)):
            coerced[key] = Decimal(str(value))
        elif key == "selected_secondary_factor" and isinstance(value, str):
            coerced[key] = SecondaryFactor(value)
        else:
            coerced[key] = value
    return ExtractedSlots(**coerced)


# Slot fields the extractor can populate. Unannotated fields in expected.slots
# must be None in the actual result — otherwise the extractor fabricated.
_EXTRACTABLE_SLOT_FIELDS = (
    "account_id",
    "full_name",
    "dob",
    "dob_alternate",
    "aadhaar_last4",
    "pincode",
    "selected_secondary_factor",
    "amount",
    "pan",
    "cvv",
    "expiry_month",
    "expiry_year",
    "name_on_card",
)


# PAN and CVV must never appear verbatim in any developer-visible output
# per DECISIONS #13 ("CVV never written to logs in any form. PAN logged
# only as last-4."). The eval is a dev tool but the architectural rule
# holds; redaction routes through redact.py to keep the format canonical.
_PAN_FIELDS: frozenset[str] = frozenset({"pan"})
_FULL_REDACT_FIELDS: frozenset[str] = frozenset({"cvv"})


def _redact_for_diff(field: str, value: Any) -> Any:
    """Redact field values destined for a printable diff line.

    PAN -> mask_pan() canonical format ("**** **** **** 0366").
    CVV -> REDACTED_PLACEHOLDER.
    None -> passes through (so "expected '4532...' got None" still reads
    sensibly: only the present value is masked).
    """
    if value is None:
        return None
    if field in _PAN_FIELDS and isinstance(value, str):
        try:
            return mask_pan(value)
        except ValueError:
            # Too-short/garbled PAN -> can't expose digits at all.
            return REDACTED_PLACEHOLDER
    if field in _FULL_REDACT_FIELDS:
        return REDACTED_PLACEHOLDER
    return value


def _diff_result(
    expected: ExtractionResult, actual: ExtractionResult
) -> list[str]:
    """Compare expected vs actual; return human-readable diff lines.

    A field passes when expected and actual agree. An unannotated field in
    ``expected.slots`` is implicitly expected to be None — fabricating a
    value is a defect. PAN and CVV are redacted in the diff output (see
    _redact_for_diff and DECISIONS #13).
    """
    diffs: list[str] = []
    if expected.intent is not actual.intent:
        diffs.append(
            f"intent: expected {expected.intent.value!r}, "
            f"got {actual.intent.value!r}"
        )
    for field in _EXTRACTABLE_SLOT_FIELDS:
        exp_val = getattr(expected.slots, field)
        act_val = getattr(actual.slots, field)
        if exp_val != act_val:
            diffs.append(
                f"{field}: expected {_redact_for_diff(field, exp_val)!r}, "
                f"got {_redact_for_diff(field, act_val)!r}"
            )
    return diffs


def _run_case(case: dict[str, Any], *, api_key: str | None) -> CaseResult:
    stage = Stage(case["stage"])
    slot_store = _build_slot_store(case.get("slots_in") or {})
    expected = ExtractionResult(
        slots=_build_expected_slots(case["expected"].get("slots") or {}),
        intent=Intent(case["expected"]["intent"]),
    )
    try:
        actual = extract_turn(
            case["input"], stage, slot_store, api_key=api_key,
        )
    except Exception as e:  # noqa: BLE001 — surface as case failure, not abort
        # Eval is a runner; a single LLM hiccup or schema parse failure must
        # not kill the whole pass. Carry the error class + message into the
        # diff so it shows up in failure detail.
        return CaseResult(
            case_id=case["id"],
            subset=case["subset"],
            passed=False,
            diffs=[f"EXCEPTION {type(e).__name__}: {e}"],
            expected_intent=expected.intent,
            actual_intent=Intent.AMBIGUOUS,
        )
    diffs = _diff_result(expected, actual)
    return CaseResult(
        case_id=case["id"],
        subset=case["subset"],
        passed=not diffs,
        diffs=diffs,
        expected_intent=expected.intent,
        actual_intent=actual.intent,
    )


def _format_summary(results: list[CaseResult]) -> tuple[str, bool]:
    """Build the summary block. Returns (text, both_bars_met)."""
    by_subset: dict[str, list[CaseResult]] = {}
    for r in results:
        by_subset.setdefault(r.subset, []).append(r)

    lines: list[str] = ["", "=" * 60, "SUMMARY", "=" * 60]
    overall_pass = sum(1 for r in results if r.passed)
    overall_total = len(results)
    overall_rate = overall_pass / overall_total if overall_total else 0.0

    crit_results = by_subset.get("critical", [])
    crit_pass = sum(1 for r in crit_results if r.passed)
    crit_total = len(crit_results)
    crit_rate = crit_pass / crit_total if crit_total else 0.0

    lt_results = by_subset.get("long_tail", [])
    lt_pass = sum(1 for r in lt_results if r.passed)
    lt_total = len(lt_results)
    lt_rate = lt_pass / lt_total if lt_total else 0.0

    crit_status = "PASS" if crit_rate >= CRITICAL_PASS_BAR else "FAIL"
    overall_status = "PASS" if overall_rate >= OVERALL_PASS_BAR else "FAIL"

    lines.append(
        f"Critical:  {crit_pass}/{crit_total} = {crit_rate:.1%} "
        f"[bar: {CRITICAL_PASS_BAR:.0%}] {crit_status}"
    )
    lines.append(
        f"Long-tail: {lt_pass}/{lt_total} = {lt_rate:.1%}"
    )
    lines.append(
        f"Overall:   {overall_pass}/{overall_total} = {overall_rate:.1%} "
        f"[bar: {OVERALL_PASS_BAR:.0%}] {overall_status}"
    )

    both_met = (
        crit_rate >= CRITICAL_PASS_BAR and overall_rate >= OVERALL_PASS_BAR
    )
    lines.append("")
    lines.append("RESULT: " + ("BOTH BARS MET" if both_met else "BARS NOT MET"))
    return "\n".join(lines), both_met


def _format_failure(result: CaseResult) -> str:
    diff_lines = "\n    ".join(result.diffs)
    return (
        f"  FAIL [{result.subset}] {result.case_id}\n"
        f"    {diff_lines}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extractor eval runner")
    parser.add_argument(
        "--subset",
        choices=["critical", "long_tail"],
        help="Run only one subset",
    )
    parser.add_argument(
        "--case-id",
        help="Run a single case by id (overrides --subset)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only summary; suppress per-failure detail",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=CORPUS_PATH,
        help=f"Corpus path (default: {CORPUS_PATH})",
    )
    args = parser.parse_args(argv)

    cases = _load_corpus(args.corpus)
    if args.case_id is not None:
        cases = [c for c in cases if c["id"] == args.case_id]
        if not cases:
            print(f"No case matched id {args.case_id!r}", file=sys.stderr)
            return 2
    elif args.subset is not None:
        cases = [c for c in cases if c["subset"] == args.subset]

    # api_key=None defers to env via llm.call_extract; no key triggers fail-soft.
    results = [_run_case(case, api_key=None) for case in cases]

    failures = [r for r in results if not r.passed]
    if failures and not args.quiet:
        print("FAILURES")
        print("-" * 60)
        for r in failures:
            print(_format_failure(r))

    summary, both_met = _format_summary(results)
    print(summary)

    return 0 if both_met else 1


if __name__ == "__main__":
    raise SystemExit(main())
