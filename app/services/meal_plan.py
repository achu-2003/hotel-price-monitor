"""Which board a rate includes, from the sentence the site prints beside it.

WHY THE WORD "BREAKFAST" IS NOT THE ANSWER
==========================================
Booking.com prints both of these in the same cell, in the same style, for
two rows of the same room:

    Good breakfast ₹ 590        <- room only, breakfast sold separately
    Good breakfast included     <- breakfast in the rate

A match on "breakfast" calls them both breakfast and the owner's rate goes
out against the wrong one of the pair. The difference is roughly 1,200
rupees on the night this was written, which is twelve times the gap the
whole repricing rule is trying to hold.

So the rule is INCLUSION, not mention: a plan is only claimed when the text
says the meal comes with the room. A price beside the meal is the site
saying it does not.

THE THREE PLANS ARE THE THREE RMS ROWS
======================================
Not a coincidence and not a coding choice -- the property sells EP, CP and
MAP in its RMS grid and Booking.com lists one row per plan, in the same
order. :data:`ROOM_ONLY`, :data:`BREAKFAST` and :data:`HALF_BOARD` are those
three, so a rate read off the site can be lined up with the RMS row that
sets it (``RmsRoomMapping.rate_type_for``).

UNKNOWN IS A REAL ANSWER
========================
Text this cannot place returns ``None`` rather than a guess. A caller that
wanted breakfast and got ``None`` has learned something true -- we cannot
tell what this rate includes -- and can refuse, which is the right move for
a rule that sets real prices. Guessing ROOM_ONLY would be the same answer
with the doubt deleted.

PURE
====
A string in, a plan out. No page, no network, no config.
"""
from __future__ import annotations

import re

#: Room only: the rate buys the room and nothing else. Booking.com says so by
#: pricing the meal ("Good breakfast ₹ 590"), by calling it optional, or by
#: saying nothing about food at all.
ROOM_ONLY = "Room Only"
#: Breakfast in the rate. RMS calls this CP.
BREAKFAST = "Breakfast"
#: Breakfast and one more meal. RMS calls this MAP.
HALF_BOARD = "Breakfast and Dinner"

#: Everything that means "this meal is in the price". Kept as whole phrases
#: rather than a list of meal words, because the meal word on its own is
#: exactly what cannot be trusted -- see the module docstring.
_INCLUDED = re.compile(
    r"\b(?:included|include[sd]?\s+in|inclusive)\b"
    r"|\bincludes?\s+(?:a\s+)?(?:good\s+|continental\s+|buffet\s+|free\s+)?(?:breakfast|dinner|lunch)\b"
    r"|\bfree\s+breakfast\b",
    re.IGNORECASE,
)
#: A second meal named alongside breakfast. "Half board" is the trade term
#: for exactly this and appears on plenty of sites in place of the meals.
_SECOND_MEAL = re.compile(r"\b(?:dinner|lunch|half[- ]board|map\b)", re.IGNORECASE)
_BREAKFAST = re.compile(r"\bbreakfast\b", re.IGNORECASE)
#: The meal has a price next to it, so it is being sold, not given. Covers
#: "₹ 590", "INR 590", "Rs. 590" and a bare number after the meal word.
_PRICED = re.compile(
    r"breakfast[^.;|]{0,20}?(?:₹|rs\.?|inr)\s*[\d,]+"
    r"|breakfast\s+[\d,]{3,}"
    r"|\boptional\b"
    r"|\bnot\s+included\b"
    r"|\bexcluded\b",
    re.IGNORECASE,
)


#: Which RMS rate row sells each board. The property's grid has one row per
#: plan and the site lists one rate per plan, so this is a naming map and not
#: a judgement -- but it is the map that decides WHICH RMS number the site
#: price belongs to, and getting it wrong writes the right figure on the
#: wrong row. See ``RmsRoomMapping.anchor_for``.
RMS_PLAN = {ROOM_ONLY: "EP", BREAKFAST: "CP", HALF_BOARD: "MAP"}


def rms_plan(plan: str | None) -> str | None:
    """The RMS rate row that sells ``plan``, or None if it is not one of ours."""
    return RMS_PLAN.get(plan) if plan else None


def classify(text: str | None) -> str | None:
    """The board this rate includes, or ``None`` when the text does not say.

    ``text`` is whatever the site printed with the rate -- one cell, one
    list, or the whole row joined together. Order matters: a row can carry
    both "Good breakfast ₹ 590" and "Breakfast & dinner included" in
    different rates, so the priced form is checked for FIRST and only within
    the part of the text that is about breakfast.
    """
    if not text:
        return None
    blob = " ".join(str(text).split())
    if not blob:
        return None

    included = bool(_INCLUDED.search(blob))
    mentions_breakfast = bool(_BREAKFAST.search(blob))

    if included and _SECOND_MEAL.search(blob):
        return HALF_BOARD
    if included and mentions_breakfast:
        # "Good breakfast ₹ 590 | Free cancellation" mentions breakfast and
        # the word "included" may still appear further along about something
        # else entirely (a tax, a transfer). The priced form wins: a meal
        # with a price beside it is a meal being sold.
        return ROOM_ONLY if _PRICED.search(blob) else BREAKFAST
    if mentions_breakfast and _PRICED.search(blob):
        return ROOM_ONLY
    if included:
        # Something is included but no meal is named -- WiFi, taxes, a
        # transfer. That says nothing about board.
        return None
    return None


def wanted(plan: str | None, asked: str) -> bool:
    """Whether ``plan`` is the board the owner asked to be priced on.

    ``None`` is never a match. A rate whose board could not be read is not
    evidence that it is the one wanted, and a rule that treats it as one
    prices the room against whatever happened to be in that row.
    """
    return plan is not None and plan == asked
