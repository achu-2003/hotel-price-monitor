"""Sorting a hotel's own room names into the categories people compare across.

WHY THIS EXISTS
===============
Every property names its rooms its own way. Sterling sells a "Mountain View
Classic Room", MGM sells a "Club Room", Treebo sells a "Deluxe Room", and all
three are the cheapest room on the property — the row a revenue manager
actually compares. The matrix shows every room of every hotel side by side,
which is the right raw view and the wrong one for the question "what is a
suite going for tonight": the answer is spread over nine columns of names
that do not line up.

So each room name is placed in one of a small set of categories, and the
matrix can be filtered to one of them. The categories are the ones the team
already uses on paper — classic, deluxe, suite, pool suite, pool view, two
bedroom villa, three bedroom villa — plus ``other`` for a name that fits none
of them.

DERIVED FROM THE NAME, NOT STORED AGAINST THE ROOM
==================================================
There is no category column and no per-hotel mapping table. A room's category
is computed from its name every time the page renders, for the same reason
the discovery heuristics are shared: a rule that improves here improves every
hotel at once, where a stored mapping would have to be filled in again for
each new property and would quietly go stale the day a site renames a room.

The cost is that a category the name does not state cannot be inferred. Two
known examples, both from the team's own sheet:

  * A property whose entry-level room is literally called "Deluxe" (and whose
    next tier up is "Superior") is a TIER judgement about that one property.
    By name alone "Deluxe" is a deluxe room, and that is where it lands.
  * "Compact Room" happens to be the pool-view room at one property. Nothing
    in those two words says so.

Both are visible and both are one word away from being fixed by naming the
room accurately at the source, which is worth more than a hidden override.

ORDER IS THE WHOLE DESIGN
=========================
The rules are tried in order and the first hit wins, because the categories
overlap in real names:

  "Two Bed Room Pool Villa"                 -> Swimming Pool Suite
  "Cottage - 2 Bed Room, Pool View Sitout"  -> 2 Bed Room Villa
  "Pool Facing Deluxe Room"                 -> Pool View Rooms

A pool ATTACHED to the unit ("pool villa", "private pool") outranks how many
bedrooms it has: the pool is what is being sold. A pool merely VISIBLE from it
ranks below both the bedroom count and the unit type, because "Pool View
Rooms" is a column of ROOMS -- a two-bedroom cottage with a pool-view sitout
belongs with the other two-bedroom cottages, at several times the price of
anything in the pool-view column.

Pure: no database, no I/O, so the rules can be tested against the real room
names collected from the monitored properties.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

CLASSIC = "classic"
DELUXE = "deluxe"
SUITE = "suite"
POOL_SUITE = "pool-suite"
POOL_VIEW = "pool-view"
VILLA_2BR = "villa-2br"
VILLA_3BR = "villa-3br"
OTHER = "other"


@dataclass(frozen=True, slots=True)
class Category:
    slug: str
    label: str


#: Display order, and it is the sheet's order rather than the matching order.
#: What a rule has to be tried before is an implementation detail; what a
#: person scans left to right is cheapest room first, biggest unit last.
CATEGORIES: tuple[Category, ...] = (
    Category(CLASSIC, "Classic Room"),
    Category(DELUXE, "Deluxe"),
    Category(SUITE, "Suite"),
    Category(POOL_SUITE, "Swimming Pool Suite"),
    Category(POOL_VIEW, "Pool View Rooms"),
    Category(VILLA_2BR, "2 Bed Room Villa"),
    Category(VILLA_3BR, "3BED Room Villa"),
    Category(OTHER, "Other"),
)

_LABELS = {c.slug: c.label for c in CATEGORIES}
_SLUGS = frozenset(_LABELS)

#: Numerals only. "Single", "double" and "triple" are deliberately absent:
#: they count OCCUPANTS or beds, never bedrooms, and "Standard Double Bed
#: Room" -- one of the most common room names on the Indian OTAs -- would
#: otherwise be read as a two-bedroom villa.
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
}

#: BEDROOMS, NOT BEDS. "2 Bed Room Villa" is a unit with two bedrooms; a
#: "Deluxe Room with 2 Queen Beds" is one room with two beds in it, and
#: counting the second as a bedroom would file an ordinary twin room as a
#: villa. So the count is only read off wording that names a bedroom --
#: "bed room", "bedroom", "BHK" -- and a name that only ever says "2 king bed"
#: is left to the later rules. That loses a penthouse the sheet calls a two
#: bedroom; it does not turn every twin room in the system into a villa.
_BEDROOMS_RE = re.compile(
    r"\b(\d{1,2}|one|two|three|four|five|six)\s*[- ]?\s*(?:bed\s*rooms?|bedrooms?|bhk)\b"
)

#: A pool that is part of the unit. "Pool villa", "plunge pool", "with private
#: pool" -- the thing being sold is the pool. Deliberately NOT "pool room",
#: which is how "pool view rooms" reads once the middle word is optional.
_POOL_SUITE_RE = re.compile(
    r"\b(?:private\s+pool|plunge\s+pool|infinity\s+pool"
    r"|(?:swimming\s+)?pool\s*(?:side)?\s*(?:villa|suite|cottage|chalet)"
    r"|(?:villa|suite|cottage|chalet|room)s?\s+with\s+(?:a\s+)?(?:private\s+)?"
    r"(?:swimming\s+)?pool)\b"
)

#: A pool you can see. Ranks below both the bedroom count and the unit type,
#: on purpose: "Pool View Rooms" is a column of ROOMS. A cottage with a
#: pool-view sitout is compared against the other cottages -- it is a unit at
#: several times the price, and the view is a line in its description rather
#: than what it is.
_POOL_VIEW_RE = re.compile(
    r"\bpool\s*(?:view|facing|front)|\bpoolside\b|\bfacing\s+(?:the\s+)?pool\b"
)

#: Connecting rooms are sold as one booking sleeping two families, which is
#: what the two bedroom column is for at every property that has one.
_CONNECTING_RE = re.compile(r"\bconnect(?:ing|ed)\b|\binter[\s-]?connect")

_SUITE_RE = re.compile(
    r"\b(?:suite|ste|cottage|villa|penthouse|duplex|chalet|apartment|studio"
    r"|bungalow|tent|family\s+room|maisonette)\b"
)

_DELUXE_RE = re.compile(
    r"\b(?:deluxe|dlx|superior|premium|premiere?|executive|luxury|luxe|palace"
    r"|royal|grand|signature|heritage)\b"
)

_CLASSIC_RE = re.compile(
    r"\b(?:classic|club|standard|std|base|basic|economy|budget|compact|comfort"
    r"|cosy|cozy|regular|room|rooms|twin|double|single|queen|king)\b"
)


def _fold(raw: str) -> str:
    """Lowercase, accent-folded, punctuation-as-space. Nothing is dropped.

    Unlike ``normalize_room_name`` in room_matching, word order and the word
    "room" are both kept: the rules below read phrases ("pool villa", "bed
    room"), and a sorted token bag has no phrases left in it.

    Hyphens become spaces rather than surviving as word characters, so
    "3-Bed-Room Villa" reads as "3 bed room villa". A site writing it either
    way is making a typography choice, not selling a different unit.
    """
    text = unicodedata.normalize("NFKD", raw or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _bedrooms(text: str) -> int:
    """Largest bedroom count stated in the name, or 0 if it states none.

    Largest, because a name can carry two numbers -- "Villa 3 Bed Room with 1
    Living Room" -- and the unit is as big as its biggest claim.
    """
    best = 0
    for match in _BEDROOMS_RE.finditer(text):
        token = match.group(1)
        count = _WORD_NUMBERS.get(token, 0) if not token.isdigit() else int(token)
        best = max(best, count)
    return best


def classify(room_name: str) -> str:
    """The category slug for one room name. Never raises, never returns None.

    An unrecognisable name is ``other`` rather than a guess at the cheapest
    category: ``other`` is a visible gap someone fixes, while a room silently
    filed as "Classic" would be compared against real classic rooms and drag
    the cheapest-classic figure with it.
    """
    text = _fold(room_name)
    if not text:
        return OTHER

    if _POOL_SUITE_RE.search(text):
        return POOL_SUITE

    bedrooms = _bedrooms(text)
    if bedrooms >= 3:
        return VILLA_3BR
    if bedrooms == 2 or _CONNECTING_RE.search(text):
        return VILLA_2BR

    if _SUITE_RE.search(text):
        return SUITE
    if _POOL_VIEW_RE.search(text):
        return POOL_VIEW
    if _DELUXE_RE.search(text):
        return DELUXE
    if _CLASSIC_RE.search(text):
        return CLASSIC
    return OTHER


def label_for(slug: str) -> str:
    """Human label for a slug, or the slug itself if it is not one of ours."""
    return _LABELS.get(slug, slug)


def is_category(slug: str | None) -> bool:
    """Whether a value off the query string names a category we have."""
    return slug in _SLUGS
