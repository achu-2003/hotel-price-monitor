"""Turning what a page displays into a number we can trust.

Pure functions, so they can be tested against the real strings collected
during the source spike without a browser.

The guiding principle is **refuse rather than guess**. Every parsed price is
bounds-checked, and anything implausible raises instead of being stored. A
missing price shows up as a visible gap that gets fixed; a wrong price silently
poisons a series and fires false alerts for weeks.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.core.errors import SchemaDriftError

# Sanity bounds for an Indian hotel room, per night, in INR.
# A parse landing outside this is a parsing bug, not a bargain.
MIN_PLAUSIBLE_PRICE = Decimal("100")
MAX_PLAUSIBLE_PRICE = Decimal("500000")

_CURRENCY_SYMBOLS = {
    "₹": "INR",  # rupee sign
    "rs.": "INR",
    "rs": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "£": "GBP",
}

# Strips currency symbols, thin/non-breaking spaces and stray words, keeping
# only digits and separators.
_NUMBER_RE = re.compile(r"[-+]?\d[\d,.   ]*\d|\d")
_SOLD_OUT_MARKERS = (
    "sold out", "soldout", "no rooms", "not available", "unavailable",
    "fully booked", "no availability", "houseful",
)

# Plenty of booking pages say in words which side of the tax their headline
# number sits on -- "Room Rates Exclusive of Tax Rs 3,200.00" is a real
# example. Read literally it is free, reliable information about a figure we
# would otherwise have to label by guesswork.
_TAX_EXCLUSIVE_MARKERS = (
    "exclusive of tax", "excluding tax", "excluding taxes", "excl. tax",
    "excl tax", "before tax", "plus tax", "plus taxes", "+ tax", "+ taxes",
    "tax extra", "taxes extra", "extra taxes",
)
_TAX_INCLUSIVE_MARKERS = (
    "inclusive of tax", "including tax", "including taxes", "incl. tax",
    "incl tax", "tax included", "taxes included", "all inclusive",
    "inclusive of all taxes",
)


def detect_currency(text: str, default: str = "INR") -> str:
    lowered = text.lower()
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in lowered:
            return code
    return default


def looks_sold_out(text: str) -> bool:
    """Whether a page fragment is announcing no availability.

    Used to distinguish "the hotel says no rooms" (a business fact worth
    recording) from "we found nothing" (a bug worth alerting on).
    """
    lowered = " ".join(text.lower().split())
    return any(marker in lowered for marker in _SOLD_OUT_MARKERS)


def _normalise_number(raw: str) -> str:
    """Handle Indian, European and plain number formats.

    "2,500" / "2.500" / "2 500" all mean 2500; "2,500.50" means 2500.50.
    Indian grouping ("1,23,456") is handled by simply dropping the separators.
    """
    cleaned = raw.strip().replace(" ", "").replace(" ", "").replace(" ", "")

    has_comma, has_dot = "," in cleaned, "." in cleaned
    if has_comma and has_dot:
        # Whichever appears last is the decimal separator.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        # A single comma with exactly two trailing digits is a decimal comma;
        # anything else is thousands grouping.
        cleaned = (
            cleaned.replace(",", ".")
            if re.fullmatch(r"\d+,\d{2}", cleaned)
            else cleaned.replace(",", "")
        )
    elif has_dot and re.fullmatch(r"\d{1,3}(\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")  # European grouping, not a decimal
    return cleaned


def parse_price(
    text: str | None,
    *,
    field_name: str = "price",
    min_value: Decimal = MIN_PLAUSIBLE_PRICE,
    max_value: Decimal = MAX_PLAUSIBLE_PRICE,
) -> Decimal:
    """Extract a price, or raise ``SchemaDriftError``.

    Raising rather than returning ``None`` is deliberate: a page that loaded
    but no longer shows a parseable price is almost always a redesign, and that
    needs a human, not a retry.
    """
    if not text or not text.strip():
        raise SchemaDriftError(f"No text to parse a {field_name} from")

    match = _NUMBER_RE.search(text)
    if not match:
        raise SchemaDriftError(
            f"No number found in {field_name}: {text[:80]!r}",
            context={"raw": text[:200]},
        )

    try:
        value = Decimal(_normalise_number(match.group()))
    except (InvalidOperation, ValueError) as exc:
        raise SchemaDriftError(
            f"Could not parse {field_name} from {text[:80]!r}",
            context={"raw": text[:200]},
        ) from exc

    if not (min_value <= value <= max_value):
        # The classic failure this catches: picking up a review count, a room
        # number, or a discount percentage instead of the nightly rate.
        raise SchemaDriftError(
            f"Parsed {field_name} {value} is outside the plausible range "
            f"{min_value}-{max_value}; the selector is probably matching the "
            f"wrong element",
            context={"raw": text[:200], "parsed": str(value)},
        )
    return value


def parse_price_or_none(text: str | None, **kwargs) -> Decimal | None:
    """Non-raising variant, for genuinely optional fields such as taxes."""
    try:
        return parse_price(text, min_value=Decimal("0"), **kwargs)
    except SchemaDriftError:
        return None


def parse_rooms_left(text: str | None) -> int | None:
    """Extract "only 2 rooms left" style urgency counts.

    Purely informational: it is never used in a price comparison, because
    these counters are frequently a marketing device rather than real stock.
    """
    if not text:
        return None
    if (match := re.search(r"\b(\d{1,3})\b", text)) and looks_urgency(text):
        value = int(match.group(1))
        return value if 0 < value <= 100 else None
    return None


def looks_urgency(text: str) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in ("left", "remaining", "last", "only"))


def declared_tax_basis(text: str | None) -> str | None:
    """Which side of the tax a page says its headline price is on.

    Returns ``"exclusive"``, ``"inclusive"`` or ``None`` when the page does not
    say. Only stated intent counts -- nothing is inferred from the number
    itself, because a rate that merely looks round is not evidence.

    Both phrasings on one card means the page is describing two different
    figures ("Rs 3,200 exclusive of tax, Rs 3,360 inclusive"), and the scraper
    has only captured one of them. Which one is unknowable from here, so the
    honest answer is ``None``: the caller keeps its existing behaviour instead
    of labelling a number on a coin toss.
    """
    if not text:
        return None
    lowered = " ".join(text.lower().split())
    exclusive = any(marker in lowered for marker in _TAX_EXCLUSIVE_MARKERS)
    inclusive = any(marker in lowered for marker in _TAX_INCLUSIVE_MARKERS)
    if exclusive and not inclusive:
        return "exclusive"
    if inclusive and not exclusive:
        return "inclusive"
    return None
