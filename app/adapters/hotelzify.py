"""Hotelzify booking engine -- a first-class adapter.

Hotelzify (``api.hotelzify.com``) powers the direct booking pages of a number
of Indian chains, Sterling among them. It answers the same JSON its own
booking page consumes, over plain HTTP, in about half a second -- against
roughly twenty-five seconds to drive a browser at the same page.

WHY THIS IS NOT `playwright_direct_site` + CONFIG
=================================================
It was, and it recorded prices that were nearly three times too high.

The generic mapping language can select a list element by one of its own
fields, which is how the previous configuration read::

    pricing[adultCount={adults}].priceForPax.0.priceBeforeTax

Three things that cannot express, each of which is a wrong number rather than
a missing one:

**1. The rate plan is not a field of the entry.** ``pricing`` is a full matrix
of adults x children x infants x rate plan -- seventy-two entries for a
three-room property -- and every entry names its plan only by a uuid in
``ratePlanCode``. The human name lives in a SIBLING dict, ``ratePlans``, and
then inside a JSON string in ``ratePlanConfig``. A selector matching on
``adultCount`` alone takes whichever entry happens to come first, which on
this engine is the dearest plan on the room::

    Mountain View Classic Room, 27 Aug, 2 adults
        Room with Breakfast, Lunch & Dinner   10,093   <- what we recorded
        Room with Breakfast, Lunch or Dinner   8,593
        Room with Breakfast                    7,093
        Room Only                              5,593   <- what the page leads with

**2. Occupancy is three fields, not one.** Matching ``adultCount`` while
ignoring ``childCount`` and ``infantCount`` got the right row by ordering
rather than by logic -- the child-free entry simply sorts first today.

**3. The published price is the RACK rate.** ``priceBeforeTax`` is the
struck-through number. The discount a guest actually receives comes from a
DIFFERENT endpoint, ``promotions/get-promotion``, and is applied in the
browser. On the night this was found, Sterling ran a 31% "Last Minute Deal"::

    5,593 x 0.69 = 3,859      exactly the price printed on the page

This is the same trap ``app/adapters/aiosell.py`` documents for a different
engine: two numbers are both "the price", and only one is what a competitor
decision can rest on. We record what the guest is charged and keep the rack
rate and the promotion in ``raw_payload``, so a discount starting or ending
stays diagnosable -- otherwise a promotion expiring looks identical to the
hotel raising its rates.

WHICH PLAN IS RECORDED
======================
One offer per room, on the board named by ``config["board"]`` (default
``Room Only``): the cheapest, most comparable product, and the one the page
leads with. ``meal_plan`` carries the plan actually used and is part of the
offer key, so if a room does not sell that board the fallback opens its own
series instead of quietly contaminating the Room Only one.

BOARD CODES ARE NOT USABLE HERE
===============================
``ratePlanConfig.settings.board.code`` reads ``"RO"`` on every plan this
engine returns, including ``Room with Breakfast, Lunch & Dinner``. The field
exists and is uniformly wrong, so the plan NAME is matched instead. Anything
keyed on that board code would look principled and pick full board.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.adapters.base import FetchContext, FetchResult, NormalizedOffer
from app.adapters.playwright_base import build_user_agent
from app.adapters.robots import RobotsChecker
from app.config import get_settings
from app.core.errors import (
    AdapterConfigError,
    AuthError,
    BlockedError,
    HttpStatusError,
    NetworkError,
    RateLimitedError,
    SchemaDriftError,
    TimeoutError_,
)
from app.core.logging import get_logger

log = get_logger("adapter.hotelzify")

_API_ROOT = "https://api.hotelzify.com"
_AVAILABILITY = f"{_API_ROOT}/hotel/v2/hotel/availability"
_PROMOTIONS = f"{_API_ROOT}/hotel/v2/promotions/get-promotion"
#: The hotel's OWN tax table, banded by room price. A third endpoint, and the
#: reason a Hotelzify rate can be shown inclusive of tax at all -- the
#: availability response quotes "plus taxes" and carries no amount.
_TAXES = f"{_API_ROOT}/payments/v1/tax/list"
_TIMEOUT = 20.0

#: How long a fetched tax table is reused. A hotel's GST bands change when the
#: law does, not between one check and the next, so re-asking every half hour
#: for every property on the engine is load with no information in it.
_TAX_CACHE_SECONDS = 3600

#: Booking URLs look like https://booking.<brand>.com/rooms/5171/<in>/<out>/2/0
_ID_FROM_URL = re.compile(r"/rooms/(\d+)")

#: The board recorded when the config does not say otherwise.
_DEFAULT_BOARD = "Room Only"

#: Money is stored to two places; see _Discount.apply.
_CENTS = Decimal("0.01")

#: Promotions that are not offered to an anonymous visitor pricing a room.
#: Counting them would report a discount no ordinary guest can obtain.
_RESTRICTED_FLAGS = (
    "isPrivate",
    "isAgentPromo",
    "isMembership",
    "isRetargeting",
    "isReturningMember",
)


#: hotel_id -> (expires_at, schedule). Process-local and deliberately small:
#: one entry per Hotelzify property this worker has fetched.
_TAX_CACHE: dict[str, tuple[float, "_TaxSchedule | None"]] = {}


@dataclass(frozen=True, slots=True)
class _TaxBand:
    """One row of the hotel's tax table."""

    tax: Decimal
    is_percentage: bool
    price_from: Decimal
    price_to: Decimal | None

    def covers(self, price: Decimal) -> bool:
        if price < self.price_from:
            return False
        return self.price_to is None or price <= self.price_to

    def on(self, price: Decimal) -> Decimal:
        amount = price * self.tax / 100 if self.is_percentage else self.tax
        return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class _TaxSchedule:
    """What the hotel says it charges on top of a room, by price band.

    NOT A RATE WE CHOSE
    ===================
    This is the property's own table, published by the engine that takes the
    booking: Sterling declares 0% under 1,000, 5% to 7,500 and 18% above it.
    Reading it is reporting, and it is the only reason a Hotelzify rate can be
    shown with tax at all -- the availability response prints "plus taxes"
    beside every rate and carries no amount anywhere.

    That distinction is the whole point. Applying a GST rate WE picked would
    produce a number the hotel never quoted, and the band boundaries are
    exactly where such a guess goes wrong: a 7,400 room and a 7,600 room are
    taxed at 5% and 18% by this table, and no single assumed rate is right for
    both.

    ALL MATCHING BANDS, SUMMED
    ==========================
    Bands here are mutually exclusive by price, so the sum is the one that
    matches. It is a sum rather than a first-match because a property is free
    to file a service charge as a second active row over the same range, and
    taking only the first would quietly under-report the bill.
    """

    bands: tuple[_TaxBand, ...]

    def on(self, price: Decimal) -> Decimal | None:
        """The tax on ``price``, or None when the table does not cover it.

        None, never zero. A gap in the table means the hotel has not said what
        it charges at this price, which is a different claim from "nothing" --
        and stored as zero it would render as a total identical to the rate,
        indistinguishable from a genuinely all-inclusive quote.
        """
        matching = [b for b in self.bands if b.covers(price)]
        if not matching:
            return None
        return sum((b.on(price) for b in matching), Decimal("0"))


def _tax_schedule(payload: Any) -> _TaxSchedule | None:
    """Parse the tax table, or refuse it whole.

    REFUSED WHOLE, NOT ROW BY ROW
    =============================
    A row this code cannot read -- an unknown ``taxType``, a band with no
    usable rate -- discards the ENTIRE schedule rather than being skipped.
    Skipping it would silently drop one component of a bill and produce a
    total that looks complete and is short, which is worse than the honest
    "excl. tax" the display falls back to.

    Only active, room-level rows are considered. A tax filed at another level
    is not a per-room charge and adding it to a nightly rate would overstate
    every room on the property.
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return None

    bands: list[_TaxBand] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        if not row.get("isActive"):
            continue
        if str(row.get("level") or "").strip().lower() != "room":
            continue

        kind = str(row.get("taxType") or "").strip().lower()
        if kind not in ("percentage", "percent", "fixed", "amount", "flat"):
            log.warning("hotelzify_tax_type_unknown", tax_type=kind)
            return None

        rate = _as_decimal(row.get("tax"))
        low = _as_decimal(row.get("priceFrom"))
        if rate is None or low is None:
            return None
        high = row.get("priceTo")
        bands.append(
            _TaxBand(
                tax=rate,
                is_percentage=kind in ("percentage", "percent"),
                price_from=low,
                # null is the open-ended top band, not a missing value.
                price_to=None if high is None else _as_decimal(high),
            )
        )

    return _TaxSchedule(tuple(bands)) if bands else None


class HotelzifyAdapter:
    """Reads sellable rates from Hotelzify's own booking API."""

    adapter_key = "hotelzify"
    queue = "http"

    def fetch(self, context: FetchContext) -> FetchResult:
        settings = get_settings()
        hotel_id = self._hotel_id(context)

        user_agent = build_user_agent(settings.browser_user_agent_suffix)
        # The same politeness rules as the browser path: robots.txt is checked
        # against the API host before anything is requested from it.
        RobotsChecker(
            user_agent, cache=_cache_or_none(), enabled=settings.respect_robots_txt
        ).assert_allowed(_AVAILABILITY)

        started = time.monotonic()

        availability = self._request(
            _AVAILABILITY,
            {
                "id": hotel_id,
                "checkInDate": context.stay.check_in.isoformat(),
                "checkOutDate": context.stay.check_out.isoformat(),
                "currency": context.currency,
            },
            user_agent,
        )
        promotions = self._promotions(hotel_id, context, user_agent)
        taxes = self._tax_schedule_for(hotel_id, user_agent)
        offers = self._to_offers(availability, promotions, context, taxes)

        return FetchResult(
            offers=offers,
            # Rooms returned but none sellable is a genuine sell-out. Distinct
            # from an empty response, which is treated as drift below.
            sold_out_detected=bool(offers) and not any(o.is_available for o in offers),
            duration_ms=int((time.monotonic() - started) * 1000),
            source_url=(
                f"{_AVAILABILITY}?id={hotel_id}"
                f"&checkInDate={context.stay.check_in.isoformat()}"
                f"&checkOutDate={context.stay.check_out.isoformat()}"
            ),
        )

    # -- inputs ------------------------------------------------------
    def _hotel_id(self, context: FetchContext) -> str:
        """The property's Hotelzify id.

        Taken from ``external_id`` when set, otherwise lifted out of the
        booking URL, so attaching a hotel needs only the URL an operator can
        copy from their address bar.
        """
        if context.external_id:
            return str(context.external_id).strip()

        explicit = (context.config or {}).get("hotel_id")
        if explicit:
            return str(explicit).strip()

        if context.url and (match := _ID_FROM_URL.search(context.url)):
            return match.group(1)

        raise AdapterConfigError(
            "Hotelzify needs the property id. Set external_id on the hotel "
            "source, or give a booking URL of the form "
            "https://booking.<brand>.com/rooms/<id>/<check-in>/<check-out>/2/0."
        )

    def _promotions(
        self, hotel_id: str, context: FetchContext, user_agent: str
    ) -> list[dict]:
        """The live discounts, or an empty list.

        A failure here is deliberately NOT fatal. The rack rate is a real
        number and recording it beats recording nothing; what would be
        indefensible is recording it while claiming it is the sell price, so
        the offer carries ``promotion: null`` and the caller can see the
        difference in ``raw_payload``.

        Dates are dd/mm/yyyy on this endpoint and ISO on the other one. That
        is the engine's inconsistency, not ours.
        """
        try:
            payload = self._request(
                _PROMOTIONS,
                {
                    "hotelId": hotel_id,
                    "checkInDate": context.stay.check_in.strftime("%d/%m/%Y"),
                    "checkOutDate": context.stay.check_out.strftime("%d/%m/%Y"),
                },
                user_agent,
            )
        except (NetworkError, TimeoutError_, HttpStatusError, SchemaDriftError) as exc:
            log.warning(
                "hotelzify_promotions_unavailable",
                hotel_id=hotel_id,
                error=str(exc),
            )
            return []

        data = payload.get("data") if isinstance(payload, dict) else None
        return [p for p in (data or []) if isinstance(p, dict)]

    def _tax_schedule_for(self, hotel_id: str, user_agent: str) -> _TaxSchedule | None:
        """The property's tax table, cached, and never fatal.

        WHY THIS IS OPTIONAL
        ====================
        The rate is the thing being monitored; the tax is what lets it be
        DISPLAYED inclusive. A third endpoint failing must not turn a fetch
        that has already read every room into a failed check -- five of those
        open the circuit and take the hotel off monitoring altogether, which
        would be a steep price for a cosmetic setting.

        So a failure returns None and the offers carry no tax, exactly as they
        did before this existed: the matrix shows the pre-tax rate marked
        "excl. tax", which is true.

        The None is cached alongside a real schedule. A property with no tax
        table configured would otherwise be re-asked on every single fetch,
        forever, to be told nothing again.
        """
        now = time.monotonic()
        cached = _TAX_CACHE.get(hotel_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        try:
            payload = self._request(
                _TAXES,
                {
                    "hotelId": hotel_id,
                    "page": "1",
                    "pageSize": "100",
                    # The property's OWN bands. The engine will otherwise fold
                    # in its platform defaults, which are not what this hotel
                    # has told its guests it charges.
                    "getDefaultTaxes": "false",
                },
                user_agent,
            )
        except (NetworkError, TimeoutError_, HttpStatusError, SchemaDriftError,
                RateLimitedError, AuthError, BlockedError) as exc:
            log.warning("hotelzify_taxes_unavailable", hotel_id=hotel_id, error=str(exc))
            # NOT cached. A refusal or an outage is a state to retry, unlike a
            # property that answered and simply has no table.
            return None

        schedule = _tax_schedule(payload)
        _TAX_CACHE[hotel_id] = (now + _TAX_CACHE_SECONDS, schedule)
        log.info(
            "hotelzify_tax_schedule",
            hotel_id=hotel_id,
            bands=len(schedule.bands) if schedule else 0,
        )
        return schedule

    # -- transport ---------------------------------------------------
    def _request(self, url: str, params: dict[str, str], user_agent: str) -> Any:
        try:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                response = client.get(
                    url,
                    params=params,
                    headers={"User-Agent": user_agent, "Accept": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise TimeoutError_(f"Timed out calling {url}", context={"url": url}) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc), context={"url": url}) from exc

        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimitedError(
                "Hotelzify rate limited us",
                retry_after_seconds=int(retry_after) if (retry_after or "").isdigit() else None,
                context={"url": url},
            )
        if status in (401, 403):
            # 403 on an API is the machine-readable form of "not welcome".
            raise (AuthError if status == 401 else BlockedError)(
                f"Hotelzify returned {status} for {url}", context={"url": url}
            )
        if status >= 500:
            raise HttpStatusError(
                f"Hotelzify returned {status}", status_code=status, context={"url": url}
            )
        if status >= 400:
            raise SchemaDriftError(
                f"Hotelzify returned {status}. The property id or the endpoint "
                f"has probably changed.",
                context={"url": url, "status_code": status},
            )

        try:
            return response.json()
        except ValueError as exc:
            raise SchemaDriftError(
                "Hotelzify did not return JSON.",
                context={"body_head": response.text[:300]},
            ) from exc

    # -- shaping -----------------------------------------------------
    def _to_offers(
        self,
        payload: Any,
        promotions: list[dict],
        context: FetchContext,
        taxes: _TaxSchedule | None = None,
    ) -> list[NormalizedOffer]:
        rooms = _dig_rooms(payload)
        if not rooms:
            # Genuinely ambiguous: no rooms at all could be a closed-out date
            # or a changed endpoint. Refusing to guess means no price is
            # written, which is the correct outcome for an unexplained
            # empty response.
            raise SchemaDriftError(
                "Hotelzify returned no rooms. Either the property is closed "
                "for these dates or the endpoint has changed; refusing to "
                "record this as a sell-out without evidence."
            )

        discount = _best_promotion(promotions, context)
        board = str((context.config or {}).get("board") or _DEFAULT_BOARD)

        offers: list[NormalizedOffer] = []
        for room in rooms:
            if isinstance(room, dict):
                offer = self._offer_for_room(room, board, discount, context, taxes)
                if offer is not None:
                    offers.append(offer)

        if not offers:
            raise SchemaDriftError(
                f"Hotelzify returned {len(rooms)} room(s) but no priceable "
                f"rate for {context.adults} adult(s) and {context.children} "
                f"child(ren). The pricing matrix or its field names have "
                f"probably changed."
            )
        return offers

    def _offer_for_room(
        self,
        room: dict,
        board: str,
        discount: _Discount | None,
        context: FetchContext,
        taxes: _TaxSchedule | None = None,
    ) -> NormalizedOffer | None:
        name = _clean(room.get("roomName"))
        if not name:
            return None

        # A room that cannot HOLD the party is not a room that is sold out.
        # Conflating them would fire a "became unavailable" alert about a
        # two-person suite every time someone watched it for four guests, and
        # would keep firing it forever.
        if not _fits(room, context):
            return None

        rooms_left = _as_int(room.get("availableRooms"))
        plan_names = _plan_names(room)
        priced = _priced_plans(room, plan_names, context)

        if not priced:
            # It fits and it has no open rate: that is a genuine close-out,
            # and it is reported rather than skipped -- a room that vanishes
            # from the list looks identical to a room that was never there.
            return NormalizedOffer(
                raw_room_name=name,
                is_available=False,
                currency=context.currency,
                rooms_left=0,
                raw_payload={"reason": "no open rate plan for this occupancy"},
            )

        chosen = _pick_board(priced, board)
        rack = chosen.price
        sell = discount.apply(rack) if discount else rack

        # Hotelzify quotes tax-exclusive: the page prints "plus taxes" beside
        # every rate, and the availability response carries no amount. The
        # figure comes from the property's OWN banded table on a third
        # endpoint (_tax_schedule_for), so this is still the hotel's number
        # rather than a rate we picked -- which matters most exactly where a
        # guess goes wrong, at the band edges: this property taxes a 7,400
        # room at 5% and a 7,600 room at 18%.
        #
        # Applied to the SELL price, not the rack rate. Tax is charged on what
        # the guest actually pays, and on a promoted room those differ.
        #
        # None when the table could not be read or does not reach this price.
        # Left unset then, exactly as before: the display shows the pre-tax
        # rate marked "excl. tax", which is true, rather than a total that is
        # short by an unknown amount.
        tax = taxes.on(sell) if taxes is not None and sell is not None else None

        return NormalizedOffer(
            raw_room_name=name,
            price_exclusive=sell,
            taxes_fees=tax,
            currency=context.currency,
            meal_plan=chosen.plan,
            is_available=rooms_left is None or rooms_left > 0,
            rooms_left=rooms_left,
            raw_payload={
                "rack_price": str(rack),
                "sell_price": str(sell),
                "rate_plan": chosen.plan,
                "rate_plan_code": chosen.code,
                "board_requested": board,
                "board_matched": chosen.plan.strip().lower() == board.strip().lower(),
                "promotion": discount.describe() if discount else None,
                "plans_seen": {p.plan: str(p.price) for p in priced},
                "tax_on_sell": None if tax is None else str(tax),
            },
        )


# -- payload helpers -------------------------------------------------
def _dig_rooms(payload: Any) -> list:
    """``data[0].HotelRooms``, defensively."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return []
    first = data[0]
    rooms = first.get("HotelRooms") if isinstance(first, dict) else None
    return rooms if isinstance(rooms, list) else []


def _fits(room: dict, context: FetchContext) -> bool:
    """Whether the party could occupy this room at all.

    Absent limits do not exclude: a payload that stops publishing
    ``maxAdultCount`` must not silently drop every room from the results.
    """
    limits = (
        (room.get("maxAdultCount"), context.adults),
        (room.get("maxChildCount"), context.children),
    )
    for published, wanted in limits:
        cap = _as_int(published)
        if cap is not None and wanted > cap:
            return False
    return True


def _plan_names(room: dict) -> dict[str, str]:
    """``ratePlanCode -> human name``.

    The name is inside ``ratePlanConfig``, which this engine serves as a JSON
    STRING rather than an object. It is parsed here rather than in the mapping
    language because a config that has to parse its own values is a config
    nobody can debug.
    """
    names: dict[str, str] = {}
    for code, plan in (room.get("ratePlans") or {}).items():
        config = plan.get("ratePlanConfig") if isinstance(plan, dict) else None
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = None
        name = config.get("name") if isinstance(config, dict) else None
        names[str(code)] = _clean(name) or str(code)
    return names


class _Priced:
    """One rate plan priced for the requested occupancy."""

    __slots__ = ("plan", "code", "price")

    def __init__(self, plan: str, code: str, price: Decimal):
        self.plan, self.code, self.price = plan, code, price


def _priced_plans(
    room: dict, plan_names: dict[str, str], context: FetchContext
) -> list[_Priced]:
    """Every open plan whose occupancy matches the fetch, cheapest first.

    All three occupancy fields are compared. Matching on ``adultCount`` alone
    is what let a child-inclusive rate be filed as the 2-adult price on any
    payload that happened to order them differently.
    """
    out: list[_Priced] = []
    seen: set[str] = set()

    for entry in room.get("pricing") or []:
        if not isinstance(entry, dict):
            continue
        if _as_int(entry.get("adultCount")) != context.adults:
            continue
        if _as_int(entry.get("childCount")) not in (None, context.children):
            continue
        if _as_int(entry.get("infantCount")) not in (None, 0):
            continue
        if entry.get("isOpen") is False or entry.get("meetsMLOS") is False:
            continue

        price = _stay_total(entry)
        if price is None:
            continue

        code = str(entry.get("ratePlanCode") or "")
        plan = plan_names.get(code, code or "unknown")
        # The matrix repeats each plan across infant counts; the first match
        # for a plan is the one that fits, and later duplicates are the same
        # product priced for a party we did not ask about.
        if plan in seen:
            continue
        seen.add(plan)
        out.append(_Priced(plan, code, price))

    out.sort(key=lambda p: p.price)
    return out


def _stay_total(entry: dict) -> Decimal | None:
    """The whole stay, not one night.

    ``priceForPax`` carries one row per night. Reading ``.0`` -- as the old
    configuration did -- prices a three-night stay at one night's rate, which
    is a plausible number and wrong by a factor of three.
    """
    rows = entry.get("priceForPax")
    if not isinstance(rows, list) or not rows:
        return None

    total = Decimal(0)
    found = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _as_decimal(row.get("priceBeforeTax"))
        if value is not None:
            total += value
            found = True
    return total if found else None


def _pick_board(priced: list[_Priced], board: str) -> _Priced:
    """The requested board, or the cheapest plan the room does sell.

    The fallback does not corrupt the requested board's history: ``meal_plan``
    is part of the offer key, so a room with no Room Only rate opens its own
    series under the plan it actually sells rather than writing a full-board
    price into a Room Only series.
    """
    wanted = board.strip().lower()
    for candidate in priced:
        if candidate.plan.strip().lower() == wanted:
            return candidate
    return priced[0]


# -- promotions ------------------------------------------------------
class _Discount:
    __slots__ = ("name", "kind", "amount", "cap")

    def __init__(self, name: str, kind: str, amount: Decimal, cap: Decimal | None):
        self.name, self.kind, self.amount, self.cap = name, kind, amount, cap

    def apply(self, price: Decimal) -> Decimal:
        if self.kind == "percentage":
            off = price * self.amount / Decimal(100)
        else:
            off = self.amount
        if self.cap is not None:
            off = min(off, self.cap)
        # Never below zero, and never above the price: a misconfigured
        # promotion must not turn into a negative or free room, which the
        # change detector would read as a catastrophic price drop.
        net = max(Decimal(0), price - max(Decimal(0), off))
        # Quantised because the engine publishes rates as floats: 4824.019047
        # ... discounted becomes a twenty-digit repeating decimal, which the
        # price column would silently truncate anyway and which would make two
        # equal prices compare unequal depending on how they were reached.
        return net.quantize(_CENTS, rounding=ROUND_HALF_UP)

    def describe(self) -> dict:
        return {"name": self.name, "type": self.kind, "amount": str(self.amount)}


def _best_promotion(
    promotions: list[dict], context: FetchContext, now: datetime | None = None
) -> _Discount | None:
    """The largest discount an anonymous guest would actually be offered.

    Largest rather than combined: every promotion this engine returns carries
    ``isStack: false``, so the page applies one. Adding them would invent a
    price no guest is ever shown.
    """
    tz = ZoneInfo(context.timezone)
    now = now or datetime.now(tz)
    today = now.date()

    best: _Discount | None = None
    best_off = Decimal(-1)

    for promo in promotions:
        if not _is_offered(promo, context, now, today):
            continue

        kind = str(promo.get("discountType") or "").lower()
        amount = _as_decimal(promo.get("discount"))
        if amount is None or amount <= 0:
            continue
        if kind not in ("percentage", "flat", "fixed", "amount"):
            continue

        discount = _Discount(
            name=_clean(promo.get("name")) or "promotion",
            kind="percentage" if kind == "percentage" else "flat",
            amount=amount,
            cap=_as_decimal(promo.get("maxDiscountAmount")),
        )
        # Ranked on a reference amount so a percentage and a flat sum are
        # comparable at all. 10,000 is arbitrary and only ever used for the
        # ordering, never for a recorded price.
        off = Decimal(10_000) - discount.apply(Decimal(10_000))
        if off > best_off:
            best, best_off = discount, off

    return best


def _is_offered(promo: dict, context: FetchContext, now: datetime, today: date) -> bool:
    """Whether this promotion applies to this fetch."""
    if promo.get("isActive") is False or promo.get("isManuallyDisabled") is True:
        return False
    if any(promo.get(flag) is True for flag in _RESTRICTED_FLAGS):
        return False

    check_in = context.stay.check_in
    nights = max(1, (context.stay.check_out - check_in).days)

    if not _within(promo.get("startDate"), promo.get("endDate"), today):
        return False
    if not _within(promo.get("stayStartDate"), promo.get("stayEndDate"), check_in):
        return False

    # A "last minute" deal is live only inside a window of hours on the day,
    # and only for stays starting within its cutoff. Ignoring either would
    # record a discount the page stops showing at 23:30.
    start, end = promo.get("timeWindowStart"), promo.get("timeWindowEnd")
    if start and end:
        clock = now.time()
        first, last = _as_time(start), _as_time(end)
        if first is not None and last is not None and not (first <= clock <= last):
            return False

    # cutoffDays means the OPPOSITE thing either side of this branch, and
    # reading it one way for both is a wrong price rather than a missing one:
    #
    #   late  "Last Minute Deal, cutoff 0"    book WITHIN 0 days of check-in
    #   early "Book 15 Days Ahead, cutoff 15" book AT LEAST 15 days ahead
    #
    # Applying the late rule to an early promotion rejected Sterling's 30%
    # "Monsoon Saver" on a stay sixteen days out, and recorded the 26%
    # fallback: 5,550 against the 5,250 printed on the page.
    cutoff_days = promo.get("cutoffDays")
    if cutoff_days is not None:
        lead_days = (check_in - today).days
        cutoff = _as_int(cutoff_days, 0)
        kind = str(promo.get("type") or promo.get("subType") or "").lower()
        if kind == "early":
            if lead_days < cutoff:
                return False
        elif lead_days > cutoff:
            return False

    minimum = _as_int(promo.get("lengthOfStay"))
    if minimum is not None and nights < minimum:
        return False
    maximum = _as_int(promo.get("maximumNights"))
    if maximum is not None and nights > maximum:
        return False

    return True


def _within(start: Any, end: Any, moment: date) -> bool:
    """An open-ended window: a null bound does not constrain."""
    first, last = _as_date(start), _as_date(end)
    if first is not None and moment < first:
        return False
    if last is not None and moment > last:
        return False
    return True


# -- scalars ---------------------------------------------------------
def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip())
    except (TypeError, ValueError, ArithmeticError):
        return None


def _as_date(value: Any) -> date | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _as_time(value: Any):
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _cache_or_none():
    """Redis for the robots cache, or nothing if it is unreachable.

    A robots.txt cache miss costs one extra request; a hard failure here would
    cost the fetch entirely.
    """
    try:
        from app.core.ratelimit import get_redis

        return get_redis()
    except Exception:  # noqa: BLE001
        return None


# Deferred so the module reads top-down. ``_Discount`` is referenced by
# ``_offer_for_room``'s annotation, which ``from __future__ import
# annotations`` keeps as a string until something asks for it.
_ = _Discount
