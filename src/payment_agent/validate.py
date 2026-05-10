"""Pure validation primitives — Luhn, expiry, leap-year strict-parse, amount,
account-id format. No I/O, no orchestration.

DECISIONS hard constraints honored here:
  - Leap-year strict-parse via ``datetime.date(y, m, d)`` (BRIEF #3 calls out
    ACC1004 = 1988-02-29 valid; 1989-02-29 invalid). Never use ``dateutil.parser``
    for DOB — it would silently coerce.
  - Amount validation is complete (zero / negative / >2 decimals all rejected).
    DECISIONS #4: an ``invalid_amount`` API error is a bug; this module is
    where the bug is prevented.
  - DOB format defaults to DD-MM-YYYY (Indian context per DECISIONS #15);
    when both DD-MM and MM-DD readings yield plausible dates, both are
    returned for orchestrator-level disambiguation.

All functions are pure and raise ``ValueError`` on bad input. They never
mutate their arguments.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PAN_LEN_MIN: int = 13
_PAN_LEN_MAX: int = 19
_CVV_LEN_MIN: int = 3
_CVV_LEN_MAX: int = 4

# Plausible DOB lower bound. Upper bound is "today" (passed in or computed).
_DOB_PLAUSIBLE_MIN: date = date(1925, 1, 1)

_ACC_ID_PATTERN: re.Pattern[str] = re.compile(r"^ACC\d{4}$")
# DOB format: three numeric components separated by a *consistent* delimiter
# (one of - / .). Backreference \2 requires the second separator to match the
# first, so "29-02.1988" is rejected as a mixed-separator string.
_DOB_FORMAT_PATTERN: re.Pattern[str] = re.compile(r"^(\d+)([-/.])(\d+)\2(\d+)$")
_EXPIRY_SEP_PATTERN: re.Pattern[str] = re.compile(r"[/-]")
_ISO_DATE_PATTERN: re.Pattern[str] = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Luhn
# ---------------------------------------------------------------------------

def luhn_check(digits: str) -> bool:
    """Pure Luhn arithmetic. Length and content checks live in ``validate_pan``."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ---------------------------------------------------------------------------
# PAN
# ---------------------------------------------------------------------------

def validate_pan(text: str) -> str:
    """Strip spaces/dashes, verify Luhn + length, return the digit-only PAN."""
    cleaned = text.replace(" ", "").replace("-", "")
    if not cleaned:
        raise ValueError("PAN is empty")
    if not cleaned.isdigit():
        raise ValueError("PAN contains non-digit characters")
    if not _PAN_LEN_MIN <= len(cleaned) <= _PAN_LEN_MAX:
        raise ValueError(
            f"PAN length must be {_PAN_LEN_MIN}-{_PAN_LEN_MAX} digits "
            f"(got {len(cleaned)})"
        )
    if not luhn_check(cleaned):
        raise ValueError("PAN failed Luhn check")
    return cleaned


# ---------------------------------------------------------------------------
# CVV
# ---------------------------------------------------------------------------

def validate_cvv(text: str) -> str:
    """Strip whitespace, verify 3-4 digits, return digit-only CVV."""
    cleaned = text.strip()
    if not cleaned.isdigit():
        raise ValueError("CVV must be digits only")
    if not _CVV_LEN_MIN <= len(cleaned) <= _CVV_LEN_MAX:
        raise ValueError(
            f"CVV length must be {_CVV_LEN_MIN}-{_CVV_LEN_MAX} digits "
            f"(got {len(cleaned)})"
        )
    return cleaned


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def parse_expiry(text: str) -> tuple[int, int]:
    """Parse ``MM/YY`` or ``MM/YYYY`` (also dash-separated) into (month, year_4digit)."""
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Expiry is empty")
    parts = _EXPIRY_SEP_PATTERN.split(cleaned)
    if len(parts) != 2:
        raise ValueError(f"Expiry must be MM/YY or MM/YYYY (got {text!r})")
    try:
        month = int(parts[0].strip())
        year = int(parts[1].strip())
    except ValueError as e:
        raise ValueError(f"Expiry components must be numeric (got {text!r})") from e
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid expiry month: {month}")
    if year < 100:
        year += 2000
    return month, year


def validate_expiry(
    text: str, *, today: date | None = None
) -> tuple[int, int]:
    """Parse and verify the card has not expired (cards expire at end of month)."""
    today = today if today is not None else date.today()
    month, year = parse_expiry(text)
    if year < today.year or (year == today.year and month < today.month):
        raise ValueError(f"Card expired ({month:02d}/{year})")
    return month, year


# ---------------------------------------------------------------------------
# Date / DOB
# ---------------------------------------------------------------------------

def parse_iso_date(text: str) -> date:
    """Parse ``YYYY-MM-DD`` strictly via ``datetime.date``."""
    cleaned = text.strip()
    if not _ISO_DATE_PATTERN.match(cleaned):
        raise ValueError(f"Not ISO YYYY-MM-DD format: {text!r}")
    y, m, d = cleaned.split("-")
    return date(int(y), int(m), int(d))


def parse_dob_strict(*, year: int, month: int, day: int) -> date:
    """Construct a ``date`` from components. Raises ``ValueError`` on invalid
    (leap-year, month, day-of-month)."""
    return date(year, month, day)


def parse_dob_with_ambiguity(
    text: str, *, today: date | None = None
) -> tuple[date, date | None]:
    """Parse a delimited DD-MM-YYYY string, returning (primary, alternate).

    Per DECISIONS #15, the default reading is DD-MM-YYYY (Indian context).
    When the alternate reading (MM-DD-YYYY) also produces a plausible date,
    both are returned so the orchestrator can offer disambiguation. When only
    one reading is plausible, the alternate is ``None``. When neither is
    plausible, raises ``ValueError``.

    "Plausible" means ``date >= 1925-01-01`` and ``date <= today``.
    """
    today = today if today is not None else date.today()
    match = _DOB_FORMAT_PATTERN.match(text.strip())
    if match is None:
        raise ValueError(
            f"Date must be 3 numeric components with a consistent "
            f"separator (- / .): {text!r}"
        )
    a, b, year = int(match.group(1)), int(match.group(3)), int(match.group(4))

    def _try(year_: int, month_: int, day_: int) -> date | None:
        try:
            d = date(year_, month_, day_)
        except ValueError:
            return None
        if d < _DOB_PLAUSIBLE_MIN or d > today:
            return None
        return d

    primary = _try(year, b, a)      # DD-MM-YYYY (default)
    alternate = _try(year, a, b)    # MM-DD-YYYY

    if primary is not None and alternate is not None and primary != alternate:
        return primary, alternate
    if primary is not None:
        return primary, None
    if alternate is not None:
        return alternate, None
    raise ValueError(f"Could not parse {text!r} as a plausible DOB")


# ---------------------------------------------------------------------------
# Amount
# ---------------------------------------------------------------------------

def validate_amount(value: str | int | float | Decimal) -> Decimal:
    """Validate a payment amount (rupees, max 2 decimal places, positive)."""
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Amount is empty")
        try:
            d = Decimal(cleaned)
        except InvalidOperation as e:
            raise ValueError(f"Amount not numeric: {value!r}") from e
    elif isinstance(value, bool):
        # bool is a subclass of int in Python; reject explicitly.
        raise ValueError("Amount must be numeric, not bool")
    elif isinstance(value, (int, Decimal)):
        d = Decimal(value)
    elif isinstance(value, float):
        # Convert via str() to avoid float-repr surprises.
        d = Decimal(str(value))
    else:
        raise ValueError(f"Unsupported amount type: {type(value).__name__}")

    # Reject NaN, sNaN, Infinity, -Infinity in one guard before the orderable
    # comparison. Decimal('NaN') constructs without InvalidOperation, so the
    # try/except above does not catch it; without this guard, ``d <= 0`` would
    # raise InvalidOperation (NaN is not orderable) and ``Decimal('Infinity')``
    # would silently pass all checks because its ``.as_tuple().exponent`` is
    # the string ``'F'``, not an int.
    if not d.is_finite():
        raise ValueError(f"Amount must be a finite number (got {value!r})")

    if d <= 0:
        raise ValueError("Amount must be positive")

    exponent = d.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValueError("Amount cannot have more than 2 decimal places")

    return d


# ---------------------------------------------------------------------------
# Account ID
# ---------------------------------------------------------------------------

def validate_account_id(text: str) -> str:
    """Verify ``ACC`` followed by exactly 4 digits. Strict — case matters."""
    cleaned = text.strip()
    if not _ACC_ID_PATTERN.match(cleaned):
        raise ValueError(
            f"Account ID must match ACC followed by 4 digits (got {text!r})"
        )
    return cleaned
