"""Per-turn slot + intent extraction.

The single LLM call per turn (DECISIONS architecture). One forced tool-use
call that returns a uniform schema covering every possible slot plus the
3-way intent classification (AFFIRM / NEGATE / AMBIGUOUS).

Architecture commitments honored here:
- LLM consulted exactly once per turn for extraction (never for transitions,
  never for user-facing prose, never for API decisions).
- ``llm.py`` is the only module that imports ``anthropic``; this module
  consumes ``llm.call_extract``.
- Fail-soft per DECISIONS #18: when no API key, deterministic regex covers
  the most-structured inputs (yes/no, ACC1234 account-id pattern); anything
  else returns ``intent=AMBIGUOUS`` with empty slots so the orchestrator
  emits a clarification re-prompt.

Tool schema design (single, uniform across stages):
- The tool returns ALL possible slots as nullable. Stage-aware filtering is
  the orchestrator's job (``state.merge_extracted_slots`` honors lock-once-
  filled). Extractor reports what the user said this turn; the deterministic
  kernel decides what to do with it.
- The system prompt IS stage-aware (tells the model what's currently
  expected) so it can disambiguate intent correctly (e.g., a slot-fill while
  awaiting confirmation is still AMBIGUOUS intent — the user didn't actually
  say yes/no).

Cardholder-name rule (load-bearing for verification anchoring):
- ``name_on_card`` is filled ONLY when the user explicitly names a different
  cardholder ("the card is in my wife's name, Priya Kumar"). Silent-default
  cases leave it ``None`` — ``api.py`` then substitutes the verified
  ``full_name`` at the request boundary. This prevents the extractor from
  fabricating names into ``name_on_card`` when the user said nothing about
  it, which would cause verified-name vs. card-name divergence to be
  invisible to downstream code.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from payment_agent import llm, validate
from payment_agent.state import (
    ExtractedSlots,
    ExtractionResult,
    Intent,
    SlotStore,
    Stage,
)
from payment_agent.verify import SecondaryFactor

_TOOL_NAME = "record_extraction"

# Account-ID and yes/no patterns are the only deterministic-only fallback
# extractions. Anything natural-language requires the LLM.
_ACCOUNT_ID_PATTERN = re.compile(r"\bACC\d{4}\b", re.IGNORECASE)
_AFFIRM_PATTERN = re.compile(
    r"\b(yes|yeah|yep|yup|ok|okay|sure|confirm|confirmed|go\s*ahead|"
    r"proceed|do\s*it|continue|affirmative|right|correct|fine)\b",
    re.IGNORECASE,
)
_NEGATE_PATTERN = re.compile(
    r"\b(no|nope|nah|cancel|stop|abort|quit|exit|nevermind|never\s*mind|"
    r"don't|do\s*not|refuse|decline|negative)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

# Single Anthropic tool. The model is forced to call it via ``tool_choice``
# in ``llm.call_extract``. All slots are nullable; only ``intent`` is required.
_TOOL_SCHEMA: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": (
        "Record any payment-collection slots and the user's intent extracted "
        "from this single user turn. Set ONLY fields the user provided in this "
        "message; leave everything else null. Always classify intent."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["affirm", "negate", "ambiguous"],
                "description": (
                    "AFFIRM: explicit agreement (yes, ok, confirm, go ahead). "
                    "NEGATE: explicit decline or cancellation (no, stop, "
                    "cancel, refuse, 'I don't want to give my DOB'). "
                    "AMBIGUOUS: anything else, including bare slot-fills "
                    "without a yes/no, off-topic chatter, or unclear "
                    "responses. A slot-fill is NOT an AFFIRM."
                ),
            },
            "account_id": {
                "type": ["string", "null"],
                "description": (
                    "Account ID format: 'ACC' followed by 4 digits "
                    "(e.g., ACC1001). Uppercase the prefix."
                ),
            },
            "full_name": {
                "type": ["string", "null"],
                "description": (
                    "Full legal name. If the user gives a nickname AND a "
                    "different full name (e.g., 'call me Raja but my full "
                    "name is Rajarajeswari Balasubramaniam'), extract the "
                    "FULL name, not the nickname. Capture exactly as stated; "
                    "do not abbreviate, transliterate, or normalize case."
                ),
            },
            "dob": {
                "type": ["string", "null"],
                "description": (
                    "Date of birth in ISO YYYY-MM-DD. Default to DD-MM-YYYY "
                    "interpretation (Indian context). For unambiguous "
                    "formats like '1988-02-29' or 'Feb 29 1988' or "
                    "'29 February 1988', use the date directly. "
                    "AMBIGUITY RULE — apply strictly: a NUMERIC date with "
                    "three slash/dash/dot-delimited parts (e.g. '05-04-1990', "
                    "'10/08/1992', '07.08.1985') is AMBIGUOUS whenever BOTH "
                    "the DD-MM-YYYY and MM-DD-YYYY interpretations yield "
                    "valid dates within the plausible range (1925 to today). "
                    "When ambiguous, set dob to the DD-MM-YYYY reading AND "
                    "set dob_alternate to the MM-DD-YYYY reading. The "
                    "first numeric being <=12 does NOT mean it is the day; "
                    "if both readings are valid you MUST flag the alternate. "
                    "Examples requiring dob_alternate: '05-04-1990' "
                    "(DD-MM=Apr 5, MM-DD=May 4), '10-08-1992' "
                    "(DD-MM=Aug 10, MM-DD=Oct 8), '07/08/1985' "
                    "(DD-MM=Aug 7, MM-DD=Jul 8). Do NOT set dob_alternate "
                    "for unambiguous inputs like '13-04-1990' (no month 13) "
                    "or month-name dates. Do not invent dates."
                ),
            },
            "dob_alternate": {
                "type": ["string", "null"],
                "description": (
                    "ISO YYYY-MM-DD. Set ONLY when the user's numeric DOB "
                    "input is ambiguous between two plausible readings AND "
                    "you have set dob to the DD-MM reading. Otherwise null. "
                    "Never set this if dob is null. See dob field's "
                    "AMBIGUITY RULE for examples."
                ),
            },
            "aadhaar_last4": {
                "type": ["string", "null"],
                "description": (
                    "Last 4 digits of the Aadhaar number. Exactly 4 digits, "
                    "no spaces. Convert spelled-out digits if needed."
                ),
            },
            "pincode": {
                "type": ["string", "null"],
                "description": "Indian postal pincode. Exactly 6 digits.",
            },
            "selected_secondary_factor": {
                "type": ["string", "null"],
                "enum": ["dob", "aadhaar_last4", "pincode", None],
                "description": (
                    "Set ONLY when the user POSITIVELY chooses which "
                    "secondary factor to PROVIDE (e.g., 'I'll give you my "
                    "Aadhaar', 'I'd prefer to share my pincode'). "
                    "A REFUSAL of a specific factor is NOT a selection. "
                    "For inputs like 'I don't want to give my DOB' or 'I "
                    "won't share my Aadhaar', leave this null and set "
                    "intent=negate. If the user just provides a value "
                    "without naming which factor type it is, also leave "
                    "this null."
                ),
            },
            "amount": {
                "type": ["string", "null"],
                "description": (
                    "Payment amount as a decimal string, e.g., '500' or "
                    "'1234.56'. No currency symbol, no thousand-separators, "
                    "no commas. Use a period for decimals."
                ),
            },
            "pan": {
                "type": ["string", "null"],
                "description": (
                    "Card number digits only (13-19 digits), no spaces or "
                    "dashes. Convert spelled-out digits to actual digits "
                    "('four five three two zero one five one one two eight "
                    "three zero three six six' -> '4532015112830366')."
                ),
            },
            "cvv": {
                "type": ["string", "null"],
                "description": (
                    "Card verification value (3 or 4 digits). Convert "
                    "spelled-out digits to actual digits ('one two three' "
                    "-> '123')."
                ),
            },
            "expiry_month": {
                "type": ["integer", "null"],
                "description": "Card expiry month, 1-12 inclusive.",
            },
            "expiry_year": {
                "type": ["integer", "null"],
                "description": (
                    "Card expiry year. Convert 2-digit years to 4-digit "
                    "(27 -> 2027). 4-digit years pass through."
                ),
            },
            "name_on_card": {
                "type": ["string", "null"],
                "description": (
                    "ONLY set if the user EXPLICITLY names a different "
                    "cardholder than themselves (e.g., 'the card is in my "
                    "wife's name, Priya Kumar'). DO NOT fill this with the "
                    "user's own name when they just provide card details "
                    "without naming a different person -- the system "
                    "substitutes the verified account holder's name "
                    "downstream. Default: null."
                ),
            },
        },
        "required": ["intent"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Stage guidance for the system prompt
# ---------------------------------------------------------------------------

_STAGE_GUIDANCE: dict[Stage, str] = {
    Stage.GREETING: (
        "Greeting. The user may volunteer the account ID early or just say "
        "hello. Extract account_id if present; otherwise leave slots null."
    ),
    Stage.COLLECTING_ACCOUNT_ID: (
        "Awaiting the account ID. Extract account_id (format: ACC + 4 digits)."
    ),
    Stage.COLLECTING_IDENTITY: (
        "Awaiting identity verification. Expected: full_name and ONE secondary "
        "factor (dob, aadhaar_last4, or pincode). The user may name which "
        "factor they want to provide via selected_secondary_factor."
    ),
    Stage.DOB_DISAMBIGUATION: (
        "Awaiting confirmation that the primary DD-MM-YYYY reading of an "
        "ambiguous DOB is correct. AFFIRM means primary reading is correct. "
        "If user provides a different date, extract it as dob (no alternate)."
    ),
    Stage.COLLECTING_AMOUNT: (
        "Awaiting payment amount. Extract amount as a decimal string."
    ),
    Stage.COLLECTING_CARD: (
        "Awaiting card details: pan, cvv, expiry_month, expiry_year. The user "
        "may provide one or many in a single turn. Only set name_on_card if "
        "the user explicitly names a different cardholder."
    ),
    Stage.AWAITING_PAYMENT_CONFIRMATION: (
        "Awaiting yes/no confirmation of the pending charge. Most input "
        "should classify as AFFIRM, NEGATE, or AMBIGUOUS. Slot-fills here "
        "are off-script -- still extract them, but intent is AMBIGUOUS "
        "unless the user clearly said yes or no."
    ),
    Stage.FORCED_DISAMBIGUATION: (
        "Awaiting a clear yes (continue) or no (stop) after prior ambiguous "
        "responses. Slot-fills are off-script."
    ),
}


def _build_system_prompt(stage: Stage, slots: SlotStore) -> str:
    """Compose the stage-aware system prompt.

    The prompt is short and stable: instructions + stage label + stage
    guidance + a one-line summary of which slots are already filled (so the
    model doesn't fight with the orchestrator's lock-once-filled rule).
    """
    guidance = _STAGE_GUIDANCE.get(
        stage, "Extract any slots present and classify intent."
    )
    filled = _filled_slot_names(slots)
    filled_line = (
        f"Already known (do not re-extract these): {', '.join(filled)}."
        if filled
        else "Nothing extracted yet."
    )
    return (
        "You extract structured payment-collection slots and classify user "
        "intent for a deterministic state machine. The state machine -- not "
        "you -- decides transitions, validates slots, and composes user-"
        "facing prose. Your job is to faithfully record what the user said "
        "in this single turn.\n\n"
        f"Current stage: {stage.value}\n"
        f"{guidance}\n"
        f"{filled_line}\n\n"
        "Rules:\n"
        "- Always call the record_extraction tool exactly once.\n"
        "- Set ONLY fields the user provided IN THIS MESSAGE. Leave "
        "everything else null. Do not infer, default, or fabricate.\n"
        "- intent is mandatory. A bare slot-fill without explicit yes/no is "
        "AMBIGUOUS, not AFFIRM."
    )


def _filled_slot_names(slots: SlotStore) -> list[str]:
    """Names of slots already filled in the persistent SlotStore.

    Excludes derived fields (verified, lookup_response, etc.) and never
    leaks values -- only field names.
    """
    candidates = (
        "account_id",
        "full_name",
        "dob",
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
    return [name for name in candidates if getattr(slots, name) is not None]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_turn(
    user_input: str,
    stage: Stage,
    slots: SlotStore,
    *,
    api_key: str | None = None,
) -> ExtractionResult:
    """Extract slots + intent from one user turn.

    Returns ``ExtractionResult`` with all slots present in the user's message
    plus the 3-way intent classification. Stage-aware system prompt; uniform
    tool schema across stages. Fail-soft on missing API key per DECISIONS #18.

    Empty/whitespace input short-circuits to ``AMBIGUOUS`` with no slots
    without invoking the LLM. The Anthropic Messages API rejects empty
    ``user`` content with HTTP 400; deterministically there is nothing to
    extract from whitespace anyway, so the orchestrator's re-prompt path is
    the correct outcome.
    """
    if not user_input or not user_input.strip():
        return ExtractionResult(slots=ExtractedSlots(), intent=Intent.AMBIGUOUS)
    system_prompt = _build_system_prompt(stage, slots)
    raw = llm.call_extract(
        system_prompt=system_prompt,
        user_message=user_input,
        tool=_TOOL_SCHEMA,
        tool_name=_TOOL_NAME,
        api_key=api_key,
    )
    if raw is None:
        # No-key fail-soft path (DECISIONS #18). The SDK was never invoked.
        return _fail_soft_extract(user_input, stage)
    return _parse_tool_input(raw)


# ---------------------------------------------------------------------------
# Parsing the LLM tool_use args dict into a typed ExtractionResult
# ---------------------------------------------------------------------------


def _parse_tool_input(args: dict[str, Any]) -> ExtractionResult:
    """Convert the tool_use args dict to a typed ``ExtractionResult``.

    Conversions are defensive: a malformed value for any single field is
    dropped to ``None`` rather than aborting the whole extraction. The model
    occasionally emits a wrong type or an invalid date; the orchestrator
    handles missing slots by re-prompting, which is a strictly better
    outcome than crashing.

    The one strict path is ``intent``: an unrecognized value falls back to
    ``AMBIGUOUS`` (which routes to a clarification re-prompt downstream).
    """
    intent = _coerce_intent(args.get("intent"))
    slots = ExtractedSlots(
        account_id=_coerce_account_id(args.get("account_id")),
        full_name=_coerce_string(args.get("full_name")),
        dob=_coerce_date(args.get("dob")),
        dob_alternate=_coerce_date(args.get("dob_alternate")),
        aadhaar_last4=_coerce_digits(args.get("aadhaar_last4"), exact_len=4),
        pincode=_coerce_digits(args.get("pincode"), exact_len=6),
        selected_secondary_factor=_coerce_secondary_factor(
            args.get("selected_secondary_factor")
        ),
        amount=_coerce_amount(args.get("amount")),
        pan=_coerce_pan(args.get("pan")),
        cvv=_coerce_cvv(args.get("cvv")),
        expiry_month=_coerce_int(args.get("expiry_month")),
        expiry_year=_coerce_year(args.get("expiry_year")),
        name_on_card=_coerce_string(args.get("name_on_card")),
    )
    # Defensive: dob_alternate without dob is meaningless; drop it so the
    # orchestrator never sees an inconsistent pair.
    if slots.dob is None and slots.dob_alternate is not None:
        slots = slots.model_copy(update={"dob_alternate": None})
    return ExtractionResult(slots=slots, intent=intent)


def _coerce_intent(value: Any) -> Intent:
    if isinstance(value, str):
        try:
            return Intent(value.lower())
        except ValueError:
            pass
    return Intent.AMBIGUOUS


def _coerce_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_account_id(value: Any) -> str | None:
    s = _coerce_string(value)
    if s is None:
        return None
    return s.upper()


def _coerce_date(value: Any) -> date | None:
    s = _coerce_string(value)
    if s is None:
        return None
    try:
        return validate.parse_iso_date(s)
    except ValueError:
        return None


def _coerce_digits(value: Any, *, exact_len: int) -> str | None:
    s = _coerce_string(value)
    if s is None:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) != exact_len:
        return None
    return digits


def _coerce_secondary_factor(value: Any) -> SecondaryFactor | None:
    if not isinstance(value, str):
        return None
    try:
        return SecondaryFactor(value.lower())
    except ValueError:
        return None


def _coerce_amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            return None
    else:
        return None
    if not d.is_finite():
        return None
    return d


def _coerce_pan(value: Any) -> str | None:
    s = _coerce_string(value)
    if s is None:
        return None
    digits = re.sub(r"\D", "", s)
    if not (13 <= len(digits) <= 19):
        return None
    return digits


def _coerce_cvv(value: Any) -> str | None:
    s = _coerce_string(value)
    if s is None:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) not in (3, 4):
        return None
    return digits


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_year(value: Any) -> int | None:
    """Coerce expiry year to 4-digit form. 2-digit years map to 20YY."""
    n = _coerce_int(value)
    if n is None:
        return None
    if 0 <= n <= 99:
        return 2000 + n
    return n


# ---------------------------------------------------------------------------
# Fail-soft deterministic-only path (no API key, DECISIONS #18)
# ---------------------------------------------------------------------------


def _fail_soft_extract(user_input: str, stage: Stage) -> ExtractionResult:
    """Regex-only extraction for the no-API-key case.

    Covers only the most-structured input patterns where regex is unambiguous:
    yes/no for AFFIRM/NEGATE classification, and the ACC#### account-id
    pattern. Natural-language slot-fills (DOB, names, amounts, card numbers)
    return ``intent=AMBIGUOUS`` with empty slots so the orchestrator re-
    prompts. This is degraded-mode; extraction quality is the LLM's job.
    """
    intent = _fail_soft_classify_intent(user_input)
    slots_kwargs: dict[str, Any] = {}

    # Account-ID pattern is unambiguous enough to extract without LLM help,
    # which keeps the most common entry point usable in degraded mode.
    if stage in (Stage.GREETING, Stage.COLLECTING_ACCOUNT_ID):
        match = _ACCOUNT_ID_PATTERN.search(user_input)
        if match is not None:
            slots_kwargs["account_id"] = match.group(0).upper()

    return ExtractionResult(
        slots=ExtractedSlots(**slots_kwargs),
        intent=intent,
    )


def _fail_soft_classify_intent(user_input: str) -> Intent:
    """Cheap regex AFFIRM/NEGATE classifier.

    Order matters: NEGATE checked first because phrases like "no yes please
    don't" should land NEGATE on the conservative-safe principle (worst case
    is an extra clarification re-prompt). Default AMBIGUOUS.
    """
    if _NEGATE_PATTERN.search(user_input):
        return Intent.NEGATE
    if _AFFIRM_PATTERN.search(user_input):
        return Intent.AFFIRM
    return Intent.AMBIGUOUS
