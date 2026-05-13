# Payment Agent v2 — Locked Decisions

Companion to `DECISIONS.md` (v1). v2 reworks the orchestration layer
per reviewer feedback: the LLM drives the conversation and emits all
spec-API tool calls; the v1 kernel survives as the tool-mediated
guardrail layer. Hard rules from the brief — no payment without
verification, strict identity matching, retry caps, asymmetric
process_payment retry, sensitive-data redaction — are preserved
invariants enforced inside tool implementations.

Each decision below is a lock: it survives the rework cycle and binds
implementation. Format mirrors v1's `DECISIONS.md`.

---

## Architecture

### V2-1. Inversion: LLM orchestrator + kernel-mediated guardrails

The 16-stage FSM in `state.py` and the separate extraction call in
`extract.py` are retired. `agent.py` becomes a tool-use loop against
the Anthropic SDK. The LLM receives a system prompt, a tool catalog,
and the message history each turn; it decides what to say and which
tool to call. The kernel (validate / verify / api / redact, plus the
retry policy and strict-equality identity logic) is preserved as the
guardrail layer wrapped by tools.

**Why.** The reviewer asked for an LLM-orchestrated agent because that
is what production looks like for compliance-centric workloads where
ambiguous inputs need accommodation rather than rigid rejection.

**Considered and declined.** Keeping the FSM as a "safety net" behind
the LLM — would re-create the v1 architecture under a thin LLM veneer
and would not address the literal critique that the LLM should be
making the tool calls.

### V2-2. The kernel-as-tool-implementation pattern

Every hard guardrail is enforced inside a tool implementation, not in
the system prompt. The LLM emits arguments; the tool wrapper runs the
kernel function with those arguments and returns a structured result
the LLM must consume on the next inference pass. Counters,
preconditions, slot locks, asymmetric retry, sensitive-data sanitation
are all kernel-layer enforcements.

**Why.** Tool-layer enforcement is contractual; system-prompt
enforcement is statistical. Compliance-centric workloads need
contractual guarantees on the rules that, if violated, sink the agent.

---

## Tool catalog

### V2-3. Five tools, no more

`lookup_account`, `submit_verification`, `process_payment`,
`cancel_session`, `render_canonical_message`. Together they map 1-1 to
the spec API surface (lookup, process_payment), plus the one local
kernel operation (verification), plus structural cancellation, plus
canonical-message rendering.

**Considered and declined.** A `validate_card` preflight tool (rejected
— validation as a separate tool has no observable outcome; the
`stage: local_validation` discriminator inside `process_payment` is
richer). A `get_balance` tool (redundant — lookup returns balance). A
`check_retry_budget` query tool (redundant — counters surface in tool
returns). Coarse `collect_and_verify_identity` (rejected — hides the
orchestration the reviewer wants to see).

### V2-4. `submit_verification` is a pure comparator

One call per attempt with complete `(account_id, full_name,
secondary_factor)`. Incomplete inputs return `stage=local_validation`
without burning a retry; the LLM gathers across multiple turns
conversationally.

**Considered and declined.** Stateful `submit_verification` accepting
partial data and returning "still need X" — rebuilds a partial
state machine inside the tool layer.

### V2-5. Retry counters live inside tool implementations

`SessionState.verification.counter` (init 3) and
`SessionState.payment.counter` (init 5) are mutated only by their
respective tool implementations. The LLM sees `retries_remaining` in
tool returns as a read-only field and cannot manipulate it. The
orchestrator does not touch counters.

**Why.** Single-writer discipline: one entry point per state field,
debuggable when invariants desync from history. Matches v1's
state.py purity property at a new boundary.

### V2-6. `stage` discriminator on every operational tool return

Each operational tool (`lookup_account`, `submit_verification`,
`process_payment`) returns a `stage` field in
`{local_validation, preconditions, cycle_lock, api_call, api_response,
comparison}`. The stage governs whether the retry counter decrements
and whether the LLM should re-prompt the user with "wasn't submitted"
copy versus "the processor rejected this" copy.

**Why.** A single boolean `success` field is ambiguous when failure
mode shapes the recovery copy. The stage discriminator makes the
boundary between pre-network and api-response failures unambiguous.

### V2-7. Retry-burn rules

| Tool                  | Burns on                                                                  | Never burns on                                  |
|-----------------------|---------------------------------------------------------------------------|-------------------------------------------------|
| `lookup_account`      | none (lookup retries are kernel-internal, see V2-9)                       | n/a                                             |
| `submit_verification` | `stage=comparison + verified=false`                                       | local_validation, preconditions, cycle_lock     |
| `process_payment`     | `stage=api_response` + typo-class error (`INVALID_CARD`, `INVALID_CVV`, `INVALID_EXPIRY`, `INVALID_AMOUNT_SERVER`) | local_validation, preconditions, INSUFFICIENT_BALANCE, transient/unknown (terminal instead of burn) |

### V2-8. DOB disambiguation moves out of the kernel

`submit_verification` accepts only strict ISO `YYYY-MM-DD`. When a
user-supplied date string is ambiguous (e.g., "10-08-1992"), the LLM
resolves it conversationally before submitting. The dedicated
`dob_disambiguation` FSM stage from v1 is retired; the leap-year
canary still works via `datetime.date(y, m, d)` raising on invalid
dates.

**Why.** Modern LLMs handle conversational disambiguation reliably,
and a kernel-level two-option storage adds state that is no longer
necessary.

### V2-9. Lookup retries are kernel-internal

A single `lookup_account` tool call performs the full retry budget
transparently: 1 silent retry on transient (500ms backoff, per
DECISIONS #13) plus up to 3 user-visible retries — all inside one
tool call. The LLM sees only the final outcome: success,
`ACCOUNT_NOT_FOUND` (terminal), or `TRANSIENT_UNRESOLVABLE` (terminal).
`retries_remaining` is not exposed for this tool.

**Why.** The LLM has nothing useful to do with per-attempt visibility
on transient lookup failures.

### V2-10. Cycle name lock: hard reject `CYCLE_VIOLATION_NAME`

The first `full_name` submitted in a verification cycle locks the
slot for the remainder of the cycle. A subsequent
`submit_verification` call with a different name returns
`error_class=CYCLE_VIOLATION_NAME`, retries unchanged. The LLM must
either re-confirm with the user and resubmit with the locked name or
call `cancel_session`. Factor type is **not** locked — the LLM can
switch DOB → pincode → DOB within the budget.

**Why.** Structural defense against the (Alice, X) → (Bob, Y) pivot
attack. Retry budget alone would bound but not close this attack.

### V2-11. Confirmation gate is structural

A new canonical kind `confirmation_prompt` takes `{amount, last4,
expiry_month, expiry_year}` and emits the canonical confirmation
copy. Emitting it sets `_session.confirmation_pending = {amount,
last4}` (the binding values). `process_payment` requires the flag set
with **matching values**; mismatch returns `CONFIRMATION_MISMATCH`,
missing flag returns `NOT_CONFIRMED`. Neither burns a retry. The flag
is consumed (cleared) by any `process_payment` call.

**Why.** v1 had no kernel enforcement of "ask before charging" — it
was prompt-only. Binding the flag to specific values also closes
"confirm for X, charge Y" attacks.

---

## Conversation state

### V2-12. Two stores: `_history` and `_session`

`Agent._history` is the LLM message log in Anthropic SDK shape
(text/tool_use/tool_result blocks). `Agent._session` is the imperative
kernel mirror (`SessionState` dataclass: lookup, verification, payment,
confirmation_pending, terminal). Tools are the only mutators of
`_session`; the orchestrator never writes.

**Why.** History is the source of truth for the LLM; the imperative
mirror is the source of truth for the kernel. Two consumers, two
stores, one writer.

### V2-13. `Agent.snapshot()` public introspection

Returns a frozen dict view of `_session` plus a derived
`last_tool_calls` list. Used by the eval harness; doesn't change
the spec-locked `Agent.next(user_input) -> dict` return shape.

### V2-14. `next()` lifecycle

1. Terminal short-circuit. If `_session.terminal` is set, return the
   canonical session-closed message without LLM contact.
2. Append user message to `_history`.
3. Tool-use loop (cap from V2-16): call LLM, execute any `tool_use`
   blocks (mutating `_session`), append `tool_result` blocks, iterate
   until text-only response or cap hit.
4. Return `{"message": reply_text}`.

### V2-15. PII scrubbing in `_history` after `process_payment`

After `process_payment` returns (success or failure), the orchestrator
walks back through `_history` and redacts the user message that
contained raw card details (card-shaped substrings replaced with
`[card details redacted]`) **and** the LLM's `tool_use` block (the
`card` argument object replaced with `{redacted: true}`). CVV is
never persisted in `_session`, in tool results, or in logs. Recap
copy sources `last4` from the tool return, never from history.

**Considered and declined.** Verbatim history (v1's posture) — PII
remains in past user messages for the session lifetime. Scrubbing
the user message only without the `tool_use` args — leaves the
structured card object live in history.

### V2-16. Tool-use iteration cap: 6 per turn

Bounds runaway. The expected maximum per turn is 3 (e.g., recap turn:
`process_payment` + `render_canonical_message(payment_success_recap)`
= 2; defensive ceiling of 6 leaves headroom).

### V2-17. Bootstrap greeting is LLM-driven

First `next("")` with empty `_history` invokes the LLM, which calls
`render_canonical_message(kind="greeting")` per system prompt
instruction. The greeting lands in `_history` as a normal assistant
message.

**Why.** Consistent with the "LLM drives" rebuild narrative.

---

## Slot extraction

### V2-18. LLM inline extraction (no separate extractor)

The LLM reads raw user messages and constructs tool arguments inline.
No pre-processing pipeline. Deterministic validation lives inside
tools at `stage=local_validation` and bounces malformed slots back
to the LLM with structured errors.

**Why.** Modern tool-use loops with strict input schemas have
replaced the separate-extractor-then-orchestrate pattern. Schema
validation at the SDK boundary plus tool-side `stage=local_validation`
gives the safety without the parallel error surface.

**Considered and declined.** Hybrid extraction (regex for high-
confidence patterns, LLM elsewhere) — bookkeeping overhead exceeds
the safety win at this scope. Pure separate-extractor (v1's pattern)
— precisely the architecture the reviewer asked us to invert.

### V2-19. Account-ID silent-correction guard: system-prompt echo rule

The system prompt instructs the LLM to echo the extracted account ID
in its user-facing reply before/while calling `lookup_account` (e.g.,
"Got it — looking up ACC1001 for you..."). If the LLM mis-extracted
(e.g., `ACC1OO1` → `ACC1001`), the user corrects on the next turn
before lookup completes. No new canonical kind, no structural
backstop. Risk acknowledged in "What I'd improve."

### V2-20. Canonicalization inside the tool

`submit_verification` runs `verify._canonicalize_name` (whitespace
collapse + case-fold) on `full_name` before strict compare. The LLM
passes whatever it extracted; the tool normalizes silently. Other
fields are format-validated at `stage=local_validation` and
canonicalized before compare.

**Why.** v1's documented `name_with_extra_whitespace` extraction
miss had zero functional impact precisely because the kernel
canonicalized before compare. Preserving this in v2 is robust.

---

## Canonical messages

### V2-21. `render_canonical_message` owns 10 kinds

`greeting`, `account_not_found`, `verify_success_with_balance`,
`confirmation_prompt`, `payment_success_recap`,
`verification_exhausted`, `payment_exhausted`, `payment_unknown`,
`cancelled`, `session_closed`. Spec-required exact strings live
inside the tool; the LLM is instructed to use the returned string
in its reply.

### V2-22. Decoration policy: per-kind

| Kind                            | Eval bar          | Decoration allowed? |
|---------------------------------|-------------------|---------------------|
| `greeting`                      | substring         | Yes (warmth prefix) |
| `verify_success_with_balance`   | substring         | Yes (e.g., "Thanks, Nithin! ") |
| `confirmation_prompt`           | substring         | Yes (short acknowledgment) |
| `payment_success_recap`         | exact-match       | No |
| `verification_exhausted`        | exact-match       | No |
| `payment_exhausted`             | exact-match       | No |
| `payment_unknown`               | exact-match       | No |
| `cancelled`                     | exact-match       | No |
| `session_closed`                | exact-match       | No |
| `account_not_found`             | exact-match       | No |

**Why.** Formal/closure moments need the canonical voice; warmth-
permitted moments benefit from LLM-composed acknowledgment prose
without weakening the canonical assertion.

---

## System prompt

### V2-23. System prompt is treated as code

Versioned with semver + sha256 header. Eight sections: role, tools,
flow (the 8-step brief flow), hard rules, style (decoration policy),
forbidden behaviors, edge cases (DOB disambiguation, cancellation,
off-topic, account-ID echo), 2–3 few-shot examples.

### V2-24. Few-shot examples included

2–3 short example exchanges covering: DOB disambiguation, scope-shift
refusal with `cancel_session`, identity-pivot refusal with
`CYCLE_VIOLATION_NAME` recovery. ~500 tokens budget added per LLM
call.

**Considered and declined.** Skipping few-shot for simpler prompt —
the model's behavior on the tricky moments is precisely what eval
flakes on, and few-shot is the cheapest intervention.

### V2-25. Compliance layering: every hard rule at ≥ 2 layers

The 13 hard rules from the brief are each enforced at at least two
of {system prompt, tool wrapper, eval suite}. The map is the
load-bearing exhibit in DESIGN_V2.md.

---

## Eval

### V2-26. Two-tier eval suite

**Tier 1 Functionality** (~15 personas, ~10 critical): happy paths
for each factor, verification failure/exhaustion, payment validation
failure, insufficient balance recovery, leap-year DOB, account-not-
found, cancellation, payment-unknown.

**Tier 2 Compliance** (~13 personas, ~11 critical): jailbreak, identity
pivot, PAN/DOB/Aadhaar/pincode exfiltration, retry-cap bypass,
confirmation skip, scope shift, prompt injection in name, account
enumeration, verification skip, double payment.

### V2-27. Pass bars

- Tier 1 critical: **100%** (3-of-3 runs required)
- Tier 1 overall: **95%** (1 run)
- Tier 2 critical: **100%** (3-of-3 runs required)
- Tier 2 overall: **90%** (1 run, soft "refusal politeness"
  assertions allowed to be logged-not-gating)

### V2-28. Multi-run stability: 3-of-3 with 4-of-5 fallback

Critical personas across both tiers run 3 times; all must pass to
count green. Non-critical run once. Pre-committed fallback if Tier 2
critical flakes at 3-of-3: drop to **4-of-5**, adding 2 additional
runs on affected cases. Default framing in DESIGN_V2 is "3-of-3 with
4-of-5 documented as the resilient bar when LLM variance is observed."

### V2-29. Programmatic assertions only (no LLM-as-judge)

Hard assertions: `Agent.snapshot()` fields (terminal, counters,
verified), tool-call sequence and ordering, canonical-message
presence per V2-22 policy, forbidden-substring regex sweep on full
transcript (no raw DOB / Aadhaar / pincode / full PAN / CVV in any
assistant message). Soft assertions (conversational quality,
refusal politeness): logged but not pass-gating.

**Why.** Same rationale as v1 DECISIONS #9 — LLM-as-judge adds a
probabilistic layer to an evaluation meant to give *deterministic*
signal about hard rules.

### V2-30. Eval corpus size: ~28 personas

15 functionality + 13 compliance. Reviewer-credible breadth without
exceeding 48–72hr maintenance budget. Each persona ~50 lines JSON
script + ~30 lines Python assertion module.

---

## Model and determinism

### V2-31. Model pin: `claude-sonnet-4-6`

Preserves v1's choice. Balance of cost and capability; proven on v1's
tool-use loop work. Cost target ~$0.50 per full eval pass (3×
critical + 1× overall).

### V2-32. Determinism contract preserved

`temperature=0`, `thinking={"type":"disabled"}`, single provider
(Anthropic), single model id. Eval assertions are picked for
behavioral determinism — Anthropic does not guarantee bit-exact
determinism at temperature=0, and the eval suite acknowledges this
explicitly. The closed-kwarg-whitelist test from v1
(`tests/test_llm_kwargs.py`) survives, extended with `tools` and
`tool_choice` for the tool-use loop.

---

## v1 kernel preservation

### V2-33. Files that survive intact (with minor modifications)

`api.py` (asymmetric retry policy verbatim), `validate.py` (pure
validators verbatim), `verify.py` (strict-equality compare verbatim;
trim DOB disambiguation logic per V2-8), `redact.py` (extend with
history-scrub helper per V2-15), `llm.py` (extend kwargs whitelist),
`config.py` (modify retry/timeout constants, add `ITERATION_CAP=6`),
`errors.py` (trim FSM-specific classes), `cli.py` (unchanged).

### V2-34. Files that are replaced

`agent.py` (tool-use loop replaces FSM orchestrator). `state.py`
(SessionState dataclass replaces 16-stage FSM + slot store + transitions).

### V2-35. Files that are deleted

`extract.py` (LLM extracts inline). `templates.py` (canonical strings
move into `render_canonical_message`; free-form prose is LLM-composed).

### V2-36. New files in v2

`prompt.py` (system prompt as versioned Python string).
`tools.py` (single ~400-line file with all five tool implementations
and JSON schemas).
`session.py` (`SessionState` and substates).

---

## Module layout

### V2-37. Tools live in a single file

`tools.py` houses all five tool wrappers and their JSON schema
definitions. Re-evaluate at >800 lines.

**Considered and declined.** A `tools/` package with one file per
tool — overkill at 5 tools; loses cross-tool greppability.

---

## Design doc narrative

### V2-38. DESIGN_V2.md opens with a literal trace

Turns 0–5 of the ACC1001 happy path, showing user messages → LLM
tool calls with arguments → tool returns → LLM replies. Directly
answers the reviewer's two TLDR points (LLM drives the conversation;
LLM emits `lookup_account` and `process_payment`).

### V2-39. Load-bearing exhibit: rules-to-layers map

The 13-rule-by-3-layer table from compliance layering (V2-25). This
is the exhibit that makes the "tool-mediated guardrails" claim
concrete and inspectable.

### V2-40. Explicit "Differences from v1" section near the end

~10 lines naming the orchestrator inversion, the tool-boundary
guardrail surface, the eval restructure, and the kernel preservation.
Anchors the rework against v1 directly. No defense of v1's design;
no apologetic framing.

---

## Risks acknowledged

### V2-41. Most likely v2-specific failure: compliance regression invisible to eval

Prompt controls hundreds of behavior dimensions; eval covers ~28.
Silent over-correction of inputs, ambiguity collapse, decoration
drift, edge-case scope-creep are all v2-class failures v1 did not
have. Mitigated by prompt rigour, layered defense, and explicit
"what we couldn't test" framing in DESIGN_V2.md.

### V2-42. Time budget

~44–48 hours of work in a 48–72 hour window. ~4–28 hour buffer.
Phases: tool implementations + tests (8h), orchestrator + history
scrub (6h), system prompt v1 (4h), eval harness retarget (2h), Tier 1
personas (5h), Tier 2 personas (5h), eval iteration + flake diagnosis
+ prompt iteration (10h), design doc (4h).
