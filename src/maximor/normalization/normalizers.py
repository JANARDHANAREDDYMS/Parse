"""Parse primitive commercial values deterministically.

Inputs are bounded raw strings from trusted commercial-fact records. Functions
return normalized Decimal/date/string values or safe typed failures; they do
not access storage, databases, PDFs, or LLMs and never infer missing values.
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from maximor.normalization.errors import NormalizationValueError
from maximor.normalization.schemas import MoneyValue

_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y")


def _decimal(raw: str) -> Decimal:
    """Parse one finite decimal token without using binary floating point."""
    token = raw.strip().replace(",", "")
    try:
        value = Decimal(token)
    except (InvalidOperation, ValueError):
        raise NormalizationValueError("invalid_decimal", "The numeric value is invalid.") from None
    if not value.is_finite():
        raise NormalizationValueError("invalid_decimal", "The numeric value is not finite.")
    return value


def normalize_currency(raw: str) -> str:
    """Normalize an explicit ISO-like currency code; symbols alone are ambiguous."""
    value = raw.strip().upper()
    match = _CURRENCY_RE.fullmatch(value)
    if not match:
        raise NormalizationValueError("unsupported_currency", "An explicit currency code is required.")
    return match.group(1)


def normalize_money(raw: str, *, currency_code: str | None = None) -> MoneyValue:
    """Parse one raw monetary amount with an explicit, unambiguous currency."""
    numbers = _NUMBER_RE.findall(raw)
    if len(numbers) != 1:
        raise NormalizationValueError("ambiguous_money", "The monetary amount is ambiguous.")
    currency = currency_code or next((m.group(1) for m in _CURRENCY_RE.finditer(raw.upper())), None)
    if not currency:
        raise NormalizationValueError("unsupported_currency", "The monetary value has no explicit currency.")
    return MoneyValue(amount=_decimal(numbers[0]), currency_code=normalize_currency(currency))


def normalize_quantity(raw: str) -> Decimal:
    """Parse a positive decimal quantity; no default quantity is invented."""
    numbers = _NUMBER_RE.findall(raw)
    if len(numbers) != 1:
        raise NormalizationValueError("ambiguous_quantity", "The quantity is ambiguous.")
    value = _decimal(numbers[0])
    if value <= 0:
        raise NormalizationValueError("invalid_quantity", "The quantity must be positive.")
    return value


def normalize_date(raw: str) -> date:
    """Parse ISO or unambiguous written dates; ambiguous numeric formats are rejected."""
    value = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise NormalizationValueError("invalid_or_ambiguous_date", "The date format is unsupported or ambiguous.")


def normalize_payment_terms(raw: str) -> str:
    """Normalize payment terms conservatively while preserving their wording."""
    value = " ".join(raw.split())
    if not value:
        raise NormalizationValueError("invalid_payment_terms", "Payment terms are empty.")
    return value


def normalize_billing_enum(raw: str, *, allowed: frozenset[str]) -> str:
    """Normalize a billing/invoicing label only when it is in an explicit allowlist."""
    value = " ".join(raw.lower().split())
    if value not in allowed:
        raise NormalizationValueError("unsupported_billing_enum", "The billing value is unsupported.")
    return value


def normalize_period_label(raw: str) -> str:
    """Normalize whitespace in a bounded price-period label without changing meaning."""
    value = " ".join(raw.split())
    if not value:
        raise NormalizationValueError("invalid_period_label", "The price period label is empty.")
    return value
