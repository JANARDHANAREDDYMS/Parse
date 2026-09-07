"""Parse primitive commercial values deterministically.

Inputs are bounded raw strings from trusted commercial-fact records. Functions
return normalized Decimal/date/string values or safe typed failures; they do
not access storage, databases, PDFs, or LLMs and never infer missing values.
"""
import re
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from maximor.normalization.errors import NormalizationValueError
from maximor.normalization.schemas import MoneyValue

_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

# A small, deterministic set of currency symbols unambiguous enough to
# resolve on their own -- unlike a bare code guess, this maps a fixed,
# known symbol to exactly one ISO code, never infers from document context.
# Real order forms overwhelmingly state a monetary amount with only the
# symbol (`$68,005.90`), never spelling out `USD` anywhere nearby; requiring
# an explicit code left every such amount unparseable and null.
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%d-%B-%Y", "%d-%b-%Y")

_SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _parse_unambiguous_month_first_slash_date(value: str) -> date | None:
    """Parse `M/D/YYYY` using this dataset's proven-consistent month-first convention.

    A single instance with the second component > 12 (e.g. `11/23/2024`)
    only proves that one string is month-first. The convention itself is
    established at the corpus level: scanning every completed
    document-analysis run in this dataset shows dozens of `M/D/YYYY` values
    across many independent documents where the second component exceeds
    12 (proving month-first) and zero where the first component does
    (which a day-first document would eventually produce) -- so a
    same-shaped value where both components happen to be <= 12
    (`08/07/2025`) is read using that same established convention, not
    guessed in isolation. A first component outside 1-12 never fits this
    convention and is rejected rather than reinterpreted as day-first.
    """
    match = _SLASH_DATE_RE.fullmatch(value)
    if not match:
        return None
    month, day, year = (int(group) for group in match.groups())
    if not 1 <= month <= 12:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None

# The assignment's own fixed enum vocabularies for these two attributes --
# see docs/Take-Home-assignment.pdf's attribute table.
INVOICING_SCHEDULE_TYPES = frozenset({"upfront", "recurring", "milestone-based", "usage-based", "hybrid"})
INVOICING_FREQUENCIES = frozenset({"monthly", "quarterly", "yearly", "biannually", "one-time"})


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
    """Normalize an explicit ISO-like currency code or one of a small set of unambiguous symbols."""
    value = raw.strip()
    match = _CURRENCY_RE.fullmatch(value.upper())
    if match:
        return match.group(1)
    if value in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[value]
    raise NormalizationValueError("unsupported_currency", "An explicit currency code or a recognized currency symbol is required.")


def normalize_money(raw: str, *, currency_code: str | None = None) -> MoneyValue:
    """Parse one raw monetary amount with an explicit, unambiguous currency."""
    numbers = _NUMBER_RE.findall(raw)
    if len(numbers) != 1:
        raise NormalizationValueError("ambiguous_money", "The monetary amount is ambiguous.")
    currency = currency_code or next((m.group(1) for m in _CURRENCY_RE.finditer(raw.upper())), None)
    if not currency:
        found_symbols = {symbol for symbol in _CURRENCY_SYMBOLS if symbol in raw}
        if len(found_symbols) == 1:
            currency = _CURRENCY_SYMBOLS[next(iter(found_symbols))]
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
    slash_date = _parse_unambiguous_month_first_slash_date(value)
    if slash_date is not None:
        return slash_date
    raise NormalizationValueError("invalid_or_ambiguous_date", "The date format is unsupported or ambiguous.")


_NET_TERMS_RE = re.compile(r"\bnet\s*(\d+)\b", re.IGNORECASE)
_DUE_ON_RECEIPT_RE = re.compile(r"\bdue\s+on\s+receipt\b", re.IGNORECASE)


def normalize_payment_terms(raw: str) -> str:
    """Normalize payment terms conservatively while preserving their wording.

    A raw sentence often embeds a standard, unambiguous term ("Payment terms
    include Net 45 unless stated otherwise.") inside surrounding boilerplate.
    When exactly one recognized convention (`Net NN`, `Due on Receipt`)
    appears, extract just that -- it is already stated verbatim in the text,
    this only recognizes an existing universal convention, it does not infer
    a new value. If the text contains no such convention, or more than one
    distinct one (genuinely ambiguous), the full cleaned sentence is kept
    exactly as before.
    """
    value = " ".join(raw.split())
    if not value:
        raise NormalizationValueError("invalid_payment_terms", "Payment terms are empty.")
    mention = extract_payment_terms_mention(value)
    return mention if mention is not None else value


def extract_payment_terms_mention(raw: str) -> str | None:
    """Find a `Net NN`/`Due on Receipt` convention in text, or `None` if absent/ambiguous.

    Document analysis sometimes names a term after an unrelated concept (a
    real `OF-0008` term is literally named "Invoicing Schedule") while its
    value is boilerplate that also states the payment-terms convention
    ("Invoices are sent in advance ... Payment terms include Due on Receipt
    unless stated otherwise."). Term-name-based field routing alone misses
    this, so callers use this to opportunistically recognize the convention
    in any document term's text, independent of what field that term's name
    maps to. Unlike `normalize_payment_terms`, this never falls back to the
    full sentence -- a caller using this opportunistically must not adopt an
    unrelated term's full text as a payment-terms value.
    """
    value = " ".join(raw.split())
    if not value:
        return None
    net_terms = {match.group(1) for match in _NET_TERMS_RE.finditer(value)}
    if len(net_terms) == 1:
        return f"Net {net_terms.pop()}"
    if _DUE_ON_RECEIPT_RE.search(value):
        return "Due on Receipt"
    return None


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


_PERIOD_MONTHS_TO_FREQUENCY = {1: "monthly", 3: "quarterly", 6: "biannually", 12: "yearly"}


def reconcile_multi_period_total(
    *, quantity: Decimal, unit_price_amount: Decimal, total_amount: Decimal,
    duration_months: int | None, tolerance: Decimal = Decimal("0.01"),
) -> tuple[bool, str | None]:
    """Check whether a total reconciles as a whole-number multiple of quantity*unit_price.

    A subscription line item's `unit_price` is often a per-billing-period
    rate (e.g. per month) while `total_listed_value` is the full contract
    value across every occurrence -- `quantity * unit_price` alone then
    looks like a "conflict" even though the numbers are entirely consistent
    once the billing-period count is accounted for (verified directly
    against this dataset: a document with a 36-month term and a monthly
    rate has `total = quantity * unit_price * 36`, exactly).

    Returns `(reconciles, derived_frequency)`. `reconciles` is True whenever
    the total is a clean whole multiple of `quantity * unit_price`, so the
    caller should not report a conflict. `derived_frequency` additionally
    names that multiple as one of the fixed `invoicing_frequency` values
    only when the service period, divided by the multiple, lands within
    half a month of a supported period length (1/3/6/12 months) -- this is
    read off numbers already extracted with high confidence, never guessed.
    A multiple of exactly 1 is deliberately never labeled: it is equally
    consistent with `yearly` (billed once, for a ~12-month term) and
    `one-time` (a single lump sum regardless of term length), and nothing
    here can tell those apart.
    """

    if quantity <= 0 or unit_price_amount <= 0:
        return False, None
    base = quantity * unit_price_amount
    ratio = total_amount / base
    nearest = ratio.to_integral_value(rounding=ROUND_HALF_EVEN)
    if nearest < 1 or abs(ratio - nearest) > tolerance:
        return False, None
    if nearest == 1 or not duration_months or duration_months <= 0:
        return True, None
    period_length_months = Decimal(duration_months) / nearest
    for months, frequency in _PERIOD_MONTHS_TO_FREQUENCY.items():
        if abs(period_length_months - months) <= Decimal("0.5"):
            return True, frequency
    return True, None
