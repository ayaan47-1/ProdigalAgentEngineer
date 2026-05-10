# Payment Collection Agent

A conversational agent that collects card payments end-to-end against a stubbed REST API. Take-home assignment for an Agent Engineer role.

> Architecture, key decisions, tradeoffs, and what's next: see [`DESIGN.md`](./DESIGN.md).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY
```

Python 3.11+ required.

## Run

### Interactive CLI

```bash
python3 -m payment_agent.cli
```

The agent greets on startup. Type messages to continue the conversation. `quit`, `exit`, or `Ctrl-D` to leave; `Ctrl-C` to interrupt.

Without `ANTHROPIC_API_KEY` set, the agent falls back to a deterministic regex-only extraction path (DECISIONS #18). Most natural-language inputs land as AMBIGUOUS and trigger re-prompts; structured inputs (`ACC1001`, `yes`, `no`) still work.

### Programmatic

```python
from payment_agent.agent import Agent

agent = Agent()  # reads ANTHROPIC_API_KEY from env
print(agent.next(""))         # bootstrap greeting
print(agent.next("ACC1001"))
# {"message": "I've found your account. To verify your identity, ..."}
```

The interface is exactly `Agent.next(user_input: str) -> dict[str, str]` per the spec. State persists across calls within an `Agent` instance; instantiate a fresh `Agent()` per session per DECISIONS #6 and #11.

## Tests

```bash
python3 -m pytest tests/ --cov=payment_agent --cov-report=term-missing
```

553 unit + integration tests, ~95% package coverage, runs in ~1 second.

## Eval

Two complementary eval surfaces.

### Persona-driven integration (7 personas, real LLM + real API stub)

```bash
set -a && source .env && set +a
python3 -m eval.runner                              # all 7
python3 -m eval.runner --persona happy_path         # one
python3 -m eval.runner --no-transcripts             # don't write sample_conversations/
```

Personas exercise full-stack behavior end-to-end. Captures markdown transcripts to `sample_conversations/`. Cost: ~$0.15 per full run.

### Extractor pass-bar eval (53-case corpus, real LLM)

```bash
python3 -m eval.extractor_eval
python3 -m eval.extractor_eval --subset critical
python3 -m eval.extractor_eval --case-id leap_year_iso_acc1004
```

Pass bars per DECISIONS #25: **100% on critical subset, 95% overall**. Currently 12/12 critical (100%) and 51/53 overall (96.2%). The 2 long-tail misses are documented in the corpus `notes` and discussed in the design doc.

## Sample conversations

Captured runs from the persona suite — real LLM extraction + real API responses, not hand-crafted (per DECISIONS #27).

- [`sample_conversations/happy_path.md`](./sample_conversations/happy_path.md) — successful payment cycle (ACC1001 → verify via DOB → ₹500)
- [`sample_conversations/verification_exhausted.md`](./sample_conversations/verification_exhausted.md) — 4 wrong DOBs, terminal exhaustion
- [`sample_conversations/leap_year_acc1004.md`](./sample_conversations/leap_year_acc1004.md) — leap-year DOB canary (ACC1004, DOB 1988-02-29)
- [`sample_conversations/insufficient_balance_recovery.md`](./sample_conversations/insufficient_balance_recovery.md) — payment fails (₹2000 > balance), user reduces to ₹500, succeeds
- [`sample_conversations/cancellation_during_verify.md`](./sample_conversations/cancellation_during_verify.md) — polite refusal routes to `terminal_cancelled`
- [`sample_conversations/dob_disambiguation_success.md`](./sample_conversations/dob_disambiguation_success.md) — ACC1003 + "10-08-1992" → v2 two-option prompt → user picks "August 10" → (m,d) match resolves with stored year → verify success
- [`sample_conversations/alternate_factor_recovery.md`](./sample_conversations/alternate_factor_recovery.md) — ACC1001 + ambiguous DOB → wrong reading → switch to pincode → wrong pincode → correct DOB → verify success → payment

## Project structure

```
.
├── DESIGN.md                           # architecture + tradeoffs writeup
├── README.md                           # this file
├── pyproject.toml
├── requirements.txt
├── .env.example
├── src/payment_agent/
│   ├── agent.py                        # public Agent class + per-turn orchestration
│   ├── state.py                        # 16-stage state machine + slot store + transitions
│   ├── extract.py                      # single-LLM-call slot + intent extraction
│   ├── llm.py                          # thin Anthropic SDK wrapper (only module that imports anthropic)
│   ├── api.py                          # HTTP client for /api/lookup-account, /api/process-payment
│   ├── validate.py                     # Luhn, expiry, DOB strict-parse, amount, account-id
│   ├── verify.py                       # strict-equality identity comparators
│   ├── templates.py                    # render(slots, message_key) → str
│   ├── redact.py                       # CVV/PAN/PII scrubbing
│   ├── errors.py                       # typed exceptions
│   ├── config.py                       # determinism contract + retry caps + timeouts
│   └── cli.py                          # interactive REPL
├── eval/
│   ├── harness.py                      # run_persona drives Agent through scripted dialog
│   ├── runner.py                       # CLI for persona suite
│   ├── extractor_eval.py               # corpus-based extractor pass-bar runner
│   ├── personas/                       # 7 JSON dialog scripts
│   ├── assertions/                     # 7 Python modules (state-based assertions)
│   └── corpus/extraction.json          # 53-case extraction corpus
├── sample_conversations/               # 7 captured transcripts
└── tests/                              # 553 tests across 11 files
```

## Configuration

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | recommended | Anthropic API key. Without it, extraction falls back to regex-only mode (DECISIONS #18). |

`.env.example` shows the full list. The model and retry caps are pinned in `src/payment_agent/config.py` per the determinism contract (DECISIONS #8 + #19 + #20) and intentionally not env-overridable.

## Hard rules enforced

- No payment without successful verification
- Strict (non-fuzzy) name match + one secondary factor (DOB / Aadhaar last-4 / pincode)
- All inputs validated locally before any API call
- Sensitive data (DOB, Aadhaar, pincode, full PAN, CVV, full name) never echoed in agent responses
- 3 verification retries → `terminal_verification_exhausted`
- 5 typo-class payment retries → `terminal_payment_exhausted`; `insufficient_balance` re-prompts unbounded
- Lookup retries: 1 silent transient (500ms backoff) + 3 user-visible
- `process_payment` is **never retried** on transient failure (no idempotency key — DECISIONS #13); routes to `terminal_payment_unknown`
- All documented error codes (`account_not_found`, `invalid_amount`, `insufficient_balance`, `invalid_card`, `invalid_cvv`, `invalid_expiry`) handled with actionable user copy

## Known limitations

See [`DESIGN.md`](./DESIGN.md) "Where the agent struggles" for the full writeup. Short version:

- The extractor has 2 long-tail misses: explicit Aadhaar factor selection and exact preservation of extra whitespace in a name. Both have zero functional impact because deterministic code infers the factor and canonicalizes names before verification.
- The live API stub appears to accept any well-formed CVV, so CVV typo recovery is covered by mocked integration tests rather than a live persona.
- Direct `Agent.next("hi")` skips the greeting prefix and goes straight to account-ID collection. The CLI avoids this by bootstrapping with `agent.next("")`.
- Two message-key drift bugs were caught only by persona transcripts, which is why the eval suite remains part of the submission even with high unit-test coverage.
