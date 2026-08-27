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

import re

from datetime import date
from decimal import Decimal
from typing import Any

from app.adapters.base import NormalizedOffer
from app.adapters.parsing import MIN_PLAUSIBLE_PRICE, parse_price
from app.core.errors import SchemaDriftError

MISSING = object()


#: One path segment that picks a list element by a field of its own, e.g.
#: ``pricing[adultCount={adults}]``. The value may be a literal or one of the
#: booking conditions -- see :func:`dig`.
_SELECTOR_RE = re.compile(r"^(?P<key>[^\[\]]+)\[(?P<field>[^=\[\]]+)=(?P<value>[^\[\]]*)\]$")

#: One path segment that fans out over every element of a list, e.g.
#: ``masterRooms[*].rooms``. See :func:`dig`.
_WILDCARD_RE = re.compile(r"^(?P<key>[^\[\]]+)\[\*\]$")


def dig(
    payload: Any,
    path: str | None,
    default: Any = MISSING,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    """Follow a dotted path into nested dicts and lists.

    An empty path returns the payload itself, which is what lets a config say
    "the room list IS the response body".

    A segment may also SELECT from a list by one of its fields:

        pricing[adultCount={adults}].priceForPax.0.priceBeforeTax

    which is the smallest addition that covers a shape the index language
    could not reach at all. Hotelzify -- Sterling Yelagiri's engine -- returns
    seventy-two pricing entries per room, one per rate plan x occupancy x date,
    and the nightly rate for two adults is inside the one whose adultCount is
    2. ``pricing.0`` is the SINGLE-occupancy rate: a real number, off by
    thousands, and impossible to notice in a stored series.

    Without this the only reachable price on that payload was ``defaultPrice``,
    a placeholder the engine leaves at 100.00 on rooms whose rate is 12,000 --
    which is what the monitor recorded, unchanged, for as long as it watched.

    ``{name}`` in the value is filled from ``params``: the booking conditions
    of the fetch, so one configuration serves every occupancy rather than
    hard-coding the one it was discovered at.
    """
    if path is None or path == "":
        return payload

    current = payload
    # Once a [*] segment has fanned out, ``current`` is a list of nodes and
    # every remaining segment is applied to each of them.
    fanned = False

    for part in path.split("."):
        if current is None:
            return _or_default(default, path)

        if (wildcard := _WILDCARD_RE.match(part)) is not None:
            branches = _fan_out(current, wildcard["key"], fanned)
            if branches is _MISS:
                return _or_default(default, path)
            current, fanned = branches, True
            continue

        if fanned:
            gathered = []
            for node in current:
                value = _step(node, part, params, path)
                if value is _MISS:
                    continue
                # Flattened rather than nested: "every room under every group"
                # is a list of rooms, not a list of lists, and a caller asking
                # for rooms_path should not have to know how deep the grouping
                # went.
                gathered.extend(value) if isinstance(value, list) else gathered.append(value)
            current = gathered
            continue

        if (selector := _SELECTOR_RE.match(part)) is not None:
            current = _select(current, selector, params, default, path)
            if current is _MISS:
                return _or_default(default, path)
            continue

        current = _step(current, part, params, path)
        if current is _MISS:
            return _or_default(default, path)
    return current


def _step(current: Any, part: str, params, path: str) -> Any:
    """One ordinary path segment: a list index or a dict key."""
    if (selector := _SELECTOR_RE.match(part)) is not None:
        return _select(current, selector, params, _MISS, path)
    if isinstance(current, list):
        try:
            return current[int(part)]
        except (ValueError, IndexError):
            return _MISS
    if isinstance(current, dict):
        return current.get(part, _MISS)
    return _MISS


def _fan_out(current: Any, key: str, already_fanned: bool) -> Any:
    """Resolve ``key`` to a list and hand back its elements to walk in parallel.

    A second ``[*]`` flattens into the first rather than nesting, so
    ``a[*].b[*].c`` reads as "every c under every b under every a".
    """
    sources = current if already_fanned else [current]
    out = []
    for node in sources:
        if not isinstance(node, dict):
            continue
        value = node.get(key)
        if isinstance(value, list):
            out.extend(value)
        elif value is not None:
            out.append(value)
    return out if out or already_fanned else _MISS


#: Distinct from ``None``, which a payload may legitimately hold.
_MISS = object()


def _select(current: Any, selector: re.Match, params, default, path: str) -> Any:
    """The ``key[field=value]`` step of a path. See :func:`dig`."""
    key, field, wanted = selector["key"], selector["field"], selector["value"]

    if isinstance(current, dict):
        if key not in current:
            return _MISS
        current = current[key]
    else:
        return _MISS

    if not isinstance(current, list):
        return _MISS

    if wanted.startswith("{") and wanted.endswith("}"):
        name = wanted[1:-1]
        if not params or name not in params:
            # A config asking for a condition this fetch does not carry is a
            # configuration error, not a missing price, and guessing at it
            # would file one occupancy's rate under another's.
            raise SchemaDriftError(
                f"Field path {path!r} selects on {wanted}, which this fetch "
                f"does not provide. Known conditions: "
                f"{', '.join(sorted(params or {})) or 'none'}.",
                context={"path": path},
            )
        wanted = params[name]

    for item in current:
        if not isinstance(item, dict) or field not in item:
            continue
        # Compared as text, and case-folded: JSON carries 2 and a config
        # carries "2", and a mapping that worked until someone quoted a number
        # is not a mapping.
        #
        # Folding case is what makes BOOLEANS work. Python renders True as
        # "True" and every JSON payload and config spells it "true", so a
        # selector like priceBreakDownItems[isTotal=true] matched nothing and
        # returned the path's default -- silently, because a default is
        # indistinguishable from a field that was legitimately absent.
        if str(item[field]).strip().lower() == str(wanted).strip().lower():
            return item
    return _MISS


def filter_rooms(nodes: list, spec: dict | None) -> list:
    """Drop rows a source returns that are not the row we asked for.

    WHY A SOURCE RETURNS ROWS YOU DID NOT ASK FOR
    =============================================
    Agoda answers a 2-adult search with one row per occupancy variant of each
    room, and both carry the same room name::

        Family Room   Max 2 adults   13,500   isFit=true    <- the 2-adult rate
        Family Room   Max 4 adults   14,400   isFit=false

    Both are real prices for a real room. Only the first is the answer to the
    question that was asked. Without this the pair reaches the pipeline with
    one identity, the second is dropped as a collision, and the hotel is
    monitored as however many rooms happened to survive -- which is what the
    "shared an identity with another offer" alert is reporting.

    Filtering here rather than teaching the offer key about occupancy is
    deliberate: the 4-adult rate is not a different PRODUCT to be tracked
    separately, it is an answer to a different question, and storing it would
    put a price nobody searched for into a series someone reads.

    ``{"field": "isFit", "equals": true}`` keeps rows whose field matches;
    ``not_equals`` inverts it. Compared as text, for the same reason
    :func:`_select` is: JSON carries ``true`` and a config carries ``"true"``,
    and a filter that stopped working because someone quoted a boolean is not
    a filter.
    """
    if not spec or not isinstance(spec, dict):
        return nodes
    field = spec.get("field")
    if not field:
        return nodes

    if "equals" in spec:
        wanted, keep_when_equal = spec["equals"], True
    elif "not_equals" in spec:
        wanted, keep_when_equal = spec["not_equals"], False
    else:
        return nodes

    target = str(wanted).strip().lower()
    kept = [
        node for node in nodes
        if isinstance(node, dict)
        and (str(node.get(field)).strip().lower() == target) is keep_when_equal
    ]

    # A filter that removes everything is a filter that has gone stale -- the
    # field was renamed, or the source stopped publishing it. Keeping the
    # unfiltered rows lets the collision alert fire and name the problem,
    # which beats reporting a sell-out the site never declared.
    return kept or nodes


def dedupe_offers(offers: list, keep: str | None) -> list:
    """One offer per room when a source sells the same room several times over.

    WHY A SOURCE SELLS THE SAME ROOM TWICE
    ======================================
    An OTA is a marketplace. Agoda returns one row per SUPPLIER, so the same
    room at the same occupancy arrives more than once at different prices::

        Deluxe   supplierId 332    breakfast included    4,950
        Deluxe   supplierId 3038   room only             5,500

    Room name, occupancy and board together still do not identify one of
    these, so no selector can tell them apart -- which is why the "add a
    meal_plan or refundable selector" advice cannot fix this shape. And note
    the cheaper row is the one WITH breakfast: whatever these differ by, it is
    not something a guest is choosing between on price.

    Left alone, the pair reaches the pipeline with one identity and one is
    dropped as a collision -- silently, and not always the same one, so the
    stored series can step between suppliers and show a price change nobody
    made.

    ``keep="cheapest"`` records the lowest, which is the figure the listing
    leads with and the one a competitor comparison rests on. ``"dearest"``
    exists for symmetry. The choice is deliberate and recorded here rather
    than left to whichever row happened to arrive first.

    Grouped on the full offer identity, not just the name, so a source that
    DOES sell genuinely different products -- a room-only and a breakfast rate
    it labels properly -- keeps both.
    """
    if keep not in ("cheapest", "dearest"):
        return offers

    best: dict[tuple, Any] = {}
    order: list[tuple] = []
    for offer in offers:
        identity = (
            _norm(offer.raw_room_name),
            _norm(offer.meal_plan),
            offer.refundable,
        )
        price = offer.price_inclusive if offer.price_inclusive is not None else offer.price_exclusive
        incumbent = best.get(identity)
        if incumbent is None:
            best[identity] = offer
            order.append(identity)
            continue

        held = incumbent.price_inclusive if incumbent.price_inclusive is not None else incumbent.price_exclusive
        # An offer with no price never displaces one that has a price: a
        # sold-out row and a priced row for the same room are not competing
        # quotes, and preferring the empty one would record a sell-out the
        # site never declared.
        if price is None:
            continue
        if held is None or (price < held if keep == "cheapest" else price > held):
            best[identity] = offer

    return [best[identity] for identity in order]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def booking_conditions(context: Any) -> dict[str, Any]:
    """The values a ``key[field={name}]`` selector may be filled from.

    Deliberately only the conditions the fetch was made UNDER. A selector that
    could reach anything else would be choosing a price by something other than
    what was asked for, which is the failure this whole path language exists to
    avoid.
    """
    return {
        "adults": context.adults,
        "children": context.children,
        "rooms": context.rooms,
        "check_in": context.check_in.isoformat(),
        "check_out": context.check_out.isoformat(),
        "currency": context.currency,
    }


def _or_default(default: Any, path: str) -> Any:
    if default is MISSING:
        raise SchemaDriftError(
            f"Path {path!r} is missing from the response. The endpoint's shape "
            f"has changed, or adapter_config points at the wrong key."
        )
    return default


#: ``{check_in}`` or ``{check_in:%d-%m-%Y}``. The format is an strftime pattern
#: and applies only to dates; anything else ignores it.
_PLACEHOLDER_RE = re.compile(r"\{(?P<key>[a-z_]+)(?::(?P<fmt>[^{}]+))?\}")


def render_template(template: str, **values: Any) -> str:
    """Substitute ``{check_in}``-style placeholders in a URL template.

    ``str.format`` would raise on a stray brace in a real URL and would happily
    evaluate attribute access, so substitution is done by explicit replacement.

    A placeholder may carry a DATE FORMAT, because not every engine speaks ISO.
    bookingsmaker asks for ``gindate=03-09-2026``; rendering that as 2026-09-03
    produces a URL the site does not understand, and rendering it without a
    placeholder at all pins the source to whichever night the operator happened
    to be looking at. ``{check_in:%d-%m-%Y}`` says which night AND in which
    dialect.

    An unknown key is left exactly as written, so a stray brace in a real URL
    survives untouched.
    """
    def substitute(match: re.Match[str]) -> str:
        key = match.group("key")
        if key not in values:
            return match.group(0)
        value = values[key]
        fmt = match.group("fmt")
        if fmt and isinstance(value, date):
            return value.strftime(fmt)
        return _stringify(value)

    return _PLACEHOLDER_RE.sub(substitute, template)


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
    params: dict[str, Any] | None = None,
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

    value = dig(node, path, None, params=params)
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
    params: dict[str, Any] | None = None,
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
    raw_name = dig(node, mapping.get("room_name"), None, params=params)
    if not raw_name or not str(raw_name).strip():
        raise SchemaDriftError(
            "A room node carried no name. Without a name the offer cannot be "
            "matched to a room type, and an unnamed price is not storable.",
            context={"mapping_key": mapping.get("room_name")},
        )

    available = _truthy(
        dig(node, mapping["available"], True, params=params)
        if mapping.get("available")
        else True
    )

    inclusive = _money_field(node, mapping, "price_inclusive", params=params)
    exclusive = _money_field(node, mapping, "price_exclusive", params=params)
    # Taxes may legitimately be zero, and are frequently smaller than the
    # minimum plausible ROOM price, so they get their own floor.
    taxes = _money_field(node, mapping, "taxes_fees", allow_zero=True, params=params)

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

    rooms_left = (
        dig(node, mapping["rooms_left"], None, params=params)
        if mapping.get("rooms_left") else None
    )
    try:
        rooms_left = int(rooms_left) if rooms_left is not None else None
    except (TypeError, ValueError):
        rooms_left = None

    refundable_raw = (
        dig(node, mapping["refundable"], None, params=params)
        if mapping.get("refundable") else None
    )

    return NormalizedOffer(
        raw_room_name=str(raw_name).strip(),
        price_inclusive=inclusive,
        price_exclusive=exclusive,
        taxes_fees=taxes,
        currency=str(
            (dig(node, mapping["currency"], default_currency, params=params)
             if mapping.get("currency")
             else default_currency) or default_currency
        )[:3].upper(),
        meal_plan=_clean(
            dig(node, mapping["meal_plan"], None, params=params)
            if mapping.get("meal_plan") else None
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
