"""Price identity.

The single most important function in the system. An ``offer_key`` answers
"are these two prices comparable?" — and because it is the primary key of
``price_series``, two prices with different booking conditions land in
different rows and CANNOT be compared by accident.

This is the structural answer to the requirement that a comparison must hold
constant: hotel, room type, check-in, check-out, guests, occupancy, meal plan
and currency.

Stability contract
------------------
The key must be stable across process restarts, deploys, and Python versions,
because yesterday's stored keys must still match today's computed ones.
Therefore:

* fields are serialised in a FIXED order with an explicit separator
* every field is normalised to a canonical form before hashing
* ``None`` has its own sentinel, distinct from an empty string
* sha256 is used rather than ``hash()``, which is randomised per process

Changing anything in this module invalidates every stored key and breaks every
price history. If a field must ever be added, bump ``KEY_VERSION`` and write a
migration that recomputes the keys.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

KEY_VERSION = "v1"
_SEP = "|"
_NULL = "~"  # distinct from an empty string, which is a real (if odd) value


def _norm_text(value: str | None) -> str:
    """Lowercase, collapse whitespace. ``None`` becomes the null sentinel.

    "Room Only", "room only" and "  ROOM  ONLY " are the same plan; a missing
    plan is something else entirely.
    """
    if value is None:
        return _NULL
    cleaned = " ".join(value.strip().lower().split())
    return cleaned or _NULL


def _norm_bool(value: bool | None) -> str:
    """Tri-state. "Unknown refundability" must not collide with "non-refundable"."""
    if value is None:
        return _NULL
    return "1" if value else "0"


@dataclass(frozen=True, slots=True)
class OfferIdentity:
    """The complete set of conditions that define one comparable price."""

    hotel_id: int
    source_id: int
    room_type_id: int
    check_in: date
    check_out: date
    adults: int
    children: int
    meal_plan: str | None
    refundable: bool | None
    currency: str

    def canonical_string(self) -> str:
        """The exact string that gets hashed. Useful in tests and debugging."""
        parts = [
            KEY_VERSION,
            str(self.hotel_id),
            str(self.source_id),
            str(self.room_type_id),
            self.check_in.isoformat(),
            self.check_out.isoformat(),
            str(self.adults),
            str(self.children),
            _norm_text(self.meal_plan),
            _norm_bool(self.refundable),
            self.currency.strip().upper(),
        ]
        return _SEP.join(parts)

    def key(self) -> str:
        return hashlib.sha256(self.canonical_string().encode("utf-8")).hexdigest()

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


def compute_offer_key(
    *,
    hotel_id: int,
    source_id: int,
    room_type_id: int,
    check_in: date,
    check_out: date,
    adults: int,
    children: int = 0,
    meal_plan: str | None = None,
    refundable: bool | None = None,
    currency: str = "INR",
) -> str:
    """Keyword-only by design.

    Positional arguments here would be a disaster waiting to happen: swapping
    ``adults`` and ``children``, or ``check_in`` and ``check_out``, would
    silently produce a valid-looking key for the wrong thing.
    """
    return OfferIdentity(
        hotel_id=hotel_id,
        source_id=source_id,
        room_type_id=room_type_id,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=children,
        meal_plan=meal_plan,
        refundable=refundable,
        currency=currency,
    ).key()
