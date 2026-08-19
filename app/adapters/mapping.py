"""Turning a site's own JSON into :class:`NormalizedOffer` from configuration.

Both the HTTP adapter and the Playwright adapter end up here: when a booking
engine exposes an availability endpoint, the shape of that JSON is the thing
that varies between hotels, and it varies far less often than CSS does. Putting
the shape in ``hotel_sources.adapter_config`` means a booking-engine change is
a row update, not a deploy.

The path language is deliberately tiny — dotted keys and integer indices, e.g.
``data.rooms.0.rates.0.total``. A real query language (JSONPath, jq) would
invite configuration nobody can debug at 2 AM; if a site needs more than this
it needs a hand-written adapter, and that is the honest answer.

Pure functions only, so the mapping for every hotel can be tested against a
recorded payload with no network.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.adapters.base import NormalizedOffer
from app.adapters.parsing import MIN_PLAUSIBLE_PRICE, parse_price
from app.core.errors import SchemaDriftError

MISSING = object()


def dig(payload: Any, path: str | None, default: Any = MISSING) -> Any:
    """Follow a dotted path into nested dicts and lists.

    An empty path returns the payload itself, which is what lets a config say
    "the room list IS the response body".
    """
    if path is None or path == "":
        return payload

    current = payload
    for part in path.split("."):
        if current is None:
            return _or_default(default, path)
        if isinstance(current, list):
            try:
                current = current[int(part)]
                continue
            except (ValueError, IndexError):
                return _or_default(default, path)
        if isinstance(current, dict):
            if part not in current:
                return _or_default(default, path)
            current = current[part]
            continue
        return _or_default(default, path)
    return current


def _or_default(default: Any, path: str) -> Any:
    if default is MISSING:
        raise SchemaDriftError(
            f"Path {path!r} is missing from the response. The endpoint's shape "
            f"has changed, or adapter_config points at the wrong key."
        )
    return default


def render_template(template: str, **values: Any) -> str:
    """Substitute ``{check_in}``-style placeholders in a URL template.

    ``str.format`` would raise on a stray brace in a real URL and would happily
    evaluate attribute access, so substitution is done by explicit replacement.
    """
    rendered = template
    for key, value in values.items():
        token = "{" + key + "}"
        if token in rendered:
            rendered = rendered.replace(token, _stringify(value))
    return rendered


def _stringify(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _money_field(
    node: Any,
    mapping: dict[str, Any],
    key: str,
    *,
    allow_zero: bool = False,
) -> Decimal | None:
    """Read one money field, distinguishing *absent* from *nonsense*.

    Two separate cases, and conflating them is how a wrong price gets stored:

    * **Not configured, or null in the response** — returns ``None``. Nothing
      is known about this component, which is legitimate.
    * **Present but unparseable or implausible** — raises ``SchemaDriftError``.
      A field that used to hold a rate and now holds a review count is exactly
      the failure the bounds check exists for, and a JSON endpoint is no more
      trustworthy than a CSS selector after a redesign.

    The ``key not in mapping`` check is load-bearing: ``dig`` with an empty
    path returns the node itself, so an unconfigured field would otherwise
    parse the string form of the whole room object and find a number in it.
    """
    path = mapping.get(key)
    if not path:
        return None

    value = dig(node, path, None)
    if value is None:
        return None

    return parse_price(
        str(value),
        field_name=key,
        min_value=Decimal("0") if allow_zero else MIN_PLAUSIBLE_PRICE,
    )


def offer_from_mapping(
    node: Any,
    mapping: dict[str, Any],
    *,
    default_currency: str = "INR",
) -> NormalizedOffer:
    """Build one offer from one room node, per the configured field mapping.

    Recognised keys in ``mapping`` (all values are dotted paths except the
    ``*_values`` sets, which are literal lists):

    ==================  =============================================
    ``room_name``       required — what the site calls this room
    ``price_inclusive`` all-in nightly rate
    ``price_exclusive`` rate before taxes
    ``taxes_fees``      the tax component
    ``currency``        ISO code, if the payload carries one
    ``meal_plan``       "Room Only", "Breakfast Included", ...
    ``refundable``      truthy/falsy field
    ``available``       truthy/falsy field
    ``rooms_left``      urgency counter, informational only
    ==================  =============================================
    """
    raw_name = dig(node, mapping.get("room_name"), None)
    if not raw_name or not str(raw_name).strip():
        raise SchemaDriftError(
            "A room node carried no name. Without a name the offer cannot be "
            "matched to a room type, and an unnamed price is not storable.",
            context={"mapping_key": mapping.get("room_name")},
        )

    available = _truthy(
        dig(node, mapping["available"], True) if mapping.get("available") else True
    )

    inclusive = _money_field(node, mapping, "price_inclusive")
    exclusive = _money_field(node, mapping, "price_exclusive")
    # Taxes may legitimately be zero, and are frequently smaller than the
    # minimum plausible ROOM price, so they get their own floor.
    taxes = _money_field(node, mapping, "taxes_fees", allow_zero=True)

    if available and inclusive is None and exclusive is None:
        # The room is offered but we found no price. That is drift, not a
        # sold-out room, and writing nothing is the correct response.
        raise SchemaDriftError(
            f"Room {str(raw_name)[:60]!r} is available but no price field "
            f"resolved. Check price_inclusive / price_exclusive in adapter_config.",
            context={"room": str(raw_name)[:120]},
        )

    if inclusive is None and exclusive is not None and taxes is not None:
        inclusive = exclusive + taxes
    # The mirror image, and it matters as much: a site that publishes an all-in
    # figure alongside its tax line has told us the pre-tax rate too, and that
    # is the number printed on the page. Without this the exclusive basis
    # silently falls back to the inclusive figure and the dashboard shows a
    # price the guest never sees.
    if exclusive is None and inclusive is not None and taxes is not None:
        exclusive = inclusive - taxes

    rooms_left = dig(node, mapping["rooms_left"], None) if mapping.get("rooms_left") else None
    try:
        rooms_left = int(rooms_left) if rooms_left is not None else None
    except (TypeError, ValueError):
        rooms_left = None

    refundable_raw = (
        dig(node, mapping["refundable"], None) if mapping.get("refundable") else None
    )

    return NormalizedOffer(
        raw_room_name=str(raw_name).strip(),
        price_inclusive=inclusive,
        price_exclusive=exclusive,
        taxes_fees=taxes,
        currency=str(
            (dig(node, mapping["currency"], default_currency) if mapping.get("currency")
             else default_currency) or default_currency
        )[:3].upper(),
        meal_plan=_clean(
            dig(node, mapping["meal_plan"], None) if mapping.get("meal_plan") else None
        ),
        refundable=None if refundable_raw is None else _truthy(refundable_raw),
        is_available=available,
        rooms_left=rooms_left,
        raw_payload=node if isinstance(node, dict) else {"value": node},
    )


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _truthy(value: Any) -> bool:
    """Interpret whatever a site uses for a boolean.

    Sites variously send ``true``, ``"Y"``, ``1``, ``"available"`` and
    ``"SOLD_OUT"``. Unknown strings are treated as TRUE, because the caller
    only reaches this for a room the site chose to list; a genuinely sold-out
    room is far more often signalled by an explicit falsy marker.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"false", "0", "n", "no", "sold_out", "soldout", "sold out",
                "unavailable", "not_available", "closed"}:
        return False
    return True
