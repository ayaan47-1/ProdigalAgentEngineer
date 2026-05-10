"""Sensitive-data scrubbing helpers.

Imported by api.py for log writes. All functions are pure (no I/O, no mutation
of inputs).

Per DECISIONS hard constraints:
  - CVV never written to logs in any form.
  - PAN logged only as last-4.
  - DOB, Aadhaar last-4, pincode, full name never exposed in logs.

The canonical API log schema (DECISIONS #13 implications) is:
  {account_id, amount, attempted_at, error_class, last4}
constructed via :func:`build_api_log_record`. :func:`scrub_sensitive` is the
defensive backstop for any other payload that might be handed to a logger.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# Keys whose presence anywhere in a payload means the value must be dropped
# entirely from logs. Compared case-insensitively.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"cvv", "dob", "aadhaar_last4", "pincode", "full_name"}
)

# Keys whose value is a PAN (full card number); replaced with last-4 in logs.
_PAN_KEYS: frozenset[str] = frozenset({"pan", "card_number"})

# Single canonical placeholder for fields that must be elided entirely (no
# partial info) when they appear in any developer-visible diagnostic. Used by
# eval/extractor_eval.py for CVV in failure diffs; production log paths drop
# the field via _SENSITIVE_KEYS rather than substituting this placeholder.
REDACTED_PLACEHOLDER: str = "[REDACTED]"


def last4(pan: str) -> str:
    """Return the last four digits of a PAN.

    Strips spaces and dashes before extracting digits. Raises ``ValueError``
    if the input contains no digits or has fewer than four digits.
    """
    digits = "".join(ch for ch in pan if ch.isdigit())
    if not digits:
        raise ValueError("PAN contains no digits")
    if len(digits) < 4:
        raise ValueError("PAN is too short")
    return digits[-4:]


def mask_pan(pan: str) -> str:
    """Return a display-masked PAN like ``**** **** **** 0366``."""
    return f"**** **** **** {last4(pan)}"


def scrub_sensitive(payload: Any) -> Any:
    """Return a deep-copied payload with sensitive fields stripped or masked.

    Removes any key in :data:`_SENSITIVE_KEYS` (case-insensitive). Replaces
    values under any key in :data:`_PAN_KEYS` with the last-4 of the PAN.
    Recursively descends into nested dicts and lists. Does not mutate input.
    Non-container values are returned unchanged.
    """
    if isinstance(payload, dict):
        out: dict[Any, Any] = {}
        for key, value in payload.items():
            key_lower = key.lower() if isinstance(key, str) else key
            if isinstance(key_lower, str) and key_lower in _SENSITIVE_KEYS:
                continue
            if isinstance(key_lower, str) and key_lower in _PAN_KEYS:
                # PAN keys NEVER recurse — even on non-string values.
                # In production api.py types pan: str, but a defensive
                # scrubber must not log a raw int / None / dict that
                # somehow ended up under a PAN key. Replace with the
                # canonical placeholder; preserves the "key was present"
                # signal for debugging without leaking the value.
                if isinstance(value, str):
                    out[key] = last4(value)
                else:
                    out[key] = REDACTED_PLACEHOLDER
                continue
            out[key] = scrub_sensitive(value)
        return out
    if isinstance(payload, list):
        return [scrub_sensitive(item) for item in payload]
    return payload


def build_api_log_record(
    *,
    account_id: str,
    amount: int | float,
    attempted_at: datetime,
    error_class: str | None,
    last4: str,
) -> dict[str, Any]:
    """Construct the canonical API log record.

    The schema is fixed by DECISIONS #13 implications. Callers pass only the
    five permitted fields; sensitive data has no path into this record by
    construction. ``attempted_at`` must be timezone-aware — naive datetimes
    produce ambiguous ISO strings in distributed logs and so are rejected.
    """
    if attempted_at.tzinfo is None:
        raise ValueError("attempted_at must be timezone-aware")
    return {
        "account_id": account_id,
        "amount": amount,
        "attempted_at": attempted_at.isoformat(),
        "error_class": error_class,
        "last4": last4,
    }
