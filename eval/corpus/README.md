# Extractor eval corpus

Hand-curated input cases for the per-turn slot+intent extractor
(`payment_agent.extract.extract_turn`). The corpus is the substrate for
`eval/extractor_eval.py`; each case is an `(input, stage, prior-slots) →
expected (slots, intent)` triple plus a tag for the pass-bar subset.

Pass-bar (PLAN §4 + DECISIONS #25):
- **`critical` subset must be 100%.** Failing any of these is a
  rubric-visible defect — they cover the spec's stated edge cases:
  - Leap-year strict parse (DOB 1988-02-29 valid; 1989-02-29 not)
  - DOB DD-MM/MM-DD ambiguity routing (both readings valid)
  - Spelled-out card digits ("four five three two..." → "4532...")
  - Nickname-vs-full-name extraction (extract the FULL name, not the
    nickname)
  - Polite-refusal NEGATE classification ("I'd rather not share my DOB"
    → intent=NEGATE, not AMBIGUOUS)
- **Overall pass-bar: 95% across all cases.**

## Case schema

Each case in `extraction.json` has these fields:

```json
{
  "id": "leap_year_acc1004",
  "subset": "critical",
  "stage": "collecting_identity",
  "slots_in": { "account_id": "ACC1004", "full_name": "Vivek Joshi" },
  "input": "my date of birth is the twenty ninth of February nineteen eighty eight",
  "expected": {
    "intent": "ambiguous",
    "slots": { "dob": "1988-02-29" }
  },
  "notes": "leap-year strict-parse canary; year 1988 is a leap year"
}
```

- `id`: unique snake_case identifier; surfaces in failure logs.
- `subset`: `critical` or `long_tail`.
- `stage`: a `Stage` enum value (lowercase string, e.g. `collecting_amount`).
  Required because the extractor's prompt is stage-aware — the same input
  produces different `(slots, intent)` results depending on stage context
  (e.g., "ACC1001" during `collecting_account_id` extracts `account_id`;
  during `awaiting_payment_confirmation` it's still extracted but
  `intent=ambiguous` because the user didn't say yes/no).
- `slots_in`: partial `SlotStore` representing what's already known when the
  extractor is called. Missing fields default to `None` / unset. Used to
  build the "Already known: ..." line in the system prompt so the model
  doesn't fight with lock-once-filled.
- `input`: the user message for this turn.
- `expected.intent`: one of `affirm`, `negate`, `ambiguous`.
- `expected.slots`: partial `ExtractedSlots`. **Only fields the user
  actually provided in this turn.** Anything omitted MUST be `None` in the
  extractor's output.
- `notes`: free-form rationale; not asserted.

## Annotation rules (load-bearing)

These rules prevent the extractor from learning the wrong behavior via
biased ground truth.

1. **`name_on_card`: silent-default cases MUST annotate as `None`, NOT as
   the verified `full_name`.** When the user provides card details without
   explicitly naming a different cardholder ("my card number is 4532...,
   CVV 123"), the ground truth is `name_on_card: null`. The
   `name_on_card or full_name` substitution happens at the `api.py`
   request boundary — NOT at extraction. If silent cases annotated
   `name_on_card` to the verified name, the extractor would learn to
   fabricate cardholder names and we'd never notice mismatches.

2. **Off-script slot fills are still extracted.** During
   `awaiting_payment_confirmation`, an input like "my account is ACC1001"
   should extract `account_id` AND classify `intent=ambiguous` (the user
   didn't say yes/no). The orchestrator's lock-once-filled rule then
   no-ops the slot merge. This keeps the extractor's job uniform across
   stages.

3. **DOB unambiguity matters.** For `13-04-1990`, only the DD-MM-YYYY
   reading is valid (no month 13), so `dob: "1990-04-13"` and
   `dob_alternate: null`. For `04-05-1990`, both DD-MM (May 4) and
   MM-DD (April 5) are valid, so `dob: "1990-05-04"` (DD-MM default)
   and `dob_alternate: "1990-04-05"`.

4. **Polite refusal of a SPECIFIC field is NEGATE, not AMBIGUOUS.**
   "I don't want to give my DOB" routes to `terminal_cancelled` per
   DECISIONS #10 — the cancel guard treats stage-relevant NEGATE as
   cancellation. AMBIGUOUS would re-prompt indefinitely.

5. **Spelled-out digits get normalized.** "four five three two..." →
   "4532..." for PAN; "one two three" → "123" for CVV. The extractor
   handles this in the LLM call; deterministic kernel only validates
   length and digit-only after.

## File map

- `extraction.json` — the corpus (~50 cases).
- `README.md` — this file.

## How cases were chosen

- **Critical** cases come from the BRIEF's "non-obvious constraints worth
  flagging" section + the PDF's explicit edge-case examples. Each pattern
  has 2-3 variants to prevent overfitting to a single phrasing.
- **Long-tail** cases cover: clean happy paths (one per slot type),
  out-of-order info, off-script behavior at gates, partial fills,
  affirm/negate phrasing variety, off-topic chatter, empty input.
