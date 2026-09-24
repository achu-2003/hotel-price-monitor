"""One move per room, when a room repriced on several boards at once.

WHY A ROOM IS NOT A RATE PLAN
=============================
Booking.com sells one room on three boards -- room only, breakfast, breakfast
and dinner -- and a reprice moves all three together. Each is its own offer
and its own ``price_changes`` row, correctly so: they are three different
prices and the history needs all three.

They are not three things that happened. On 23 Sep 2026 ASG's Deluxe Double
Room moved on all three boards in one cycle, and the Changes table printed

    Deluxe Double Room    8,228 -> 6,612
    Deluxe Double Room    6,800 -> 4,967
    Deluxe Double Room    6,375 -> 4,539

three times over, with no column naming the board. That does not read as one
room on three boards; it reads as a bug in the detector, and it was reported
as one. The same four changes became ONE WhatsApp saying "Deluxe Double Room
+3 more" -- the price-change template has a single room slot, so the Standard
Double Room that also moved was never named at all.

WHICH BOARD SURVIVES
====================
The one the comparison is pinned to (``RepricingSettings.benchmark_meal_plan``),
falling back to room-only for a room that is not sold on it. That is not a new
rule: it is exactly what :func:`app.services.repricing.propose` already does
with ``fallback_board``, and reusing it means the room named in an alert is the
room, and the price, the Repricing page is about. On the night this was
written that picked

    Deluxe Double Room      Breakfast    6,800 -> 4,967
    Standard Double Room    Room Only    7,225 -> 4,320

-- breakfast for the room that sells it, room-only for the room that does not.

A ROOM IS NEVER DROPPED
=======================
A third rank exists for rooms that moved on neither the pinned board nor
room-only -- a half-board-only rate, or a plan this codebase could not
classify. Preferring a board must not mean losing a move that really happened:
the reader would see silence and read it as "nothing changed". So the best
available move is reported, and only the duplicates of a room already spoken
for are dropped.

PURE
====
Tuples in, tuples out. No database, no settings read; the caller supplies the
board it read. Both callers -- the Changes page and the dispatcher -- go
through here so the screen and the alert can never disagree about how many
rooms moved.
"""
from __future__ import annotations

from collections.abc import Hashable, Iterable
from typing import TypeVar

from app.services.meal_plan import ROOM_ONLY

T = TypeVar("T")

#: Worse than any real rank, so an unseen room always takes its first move.
_UNRANKED = 99


def _rank(plan: str | None, board: str | None, fallback: str | None) -> int:
    """How much this board is wanted: lower wins.

    ``board`` is skipped when nothing is pinned, rather than matched against
    ``None`` -- a rate whose board could not be classified is ``None`` too
    (see :mod:`app.services.meal_plan`), and letting those two match would
    make an unreadable plan beat a known room-only one.
    """
    if board is not None and plan == board:
        return 0
    if fallback is not None and plan == fallback:
        return 1
    return 2


def one_per_room(
    rows: Iterable[tuple[Hashable, str | None, T]],
    *,
    board: str | None = None,
    fallback: str | None = ROOM_ONLY,
) -> list[T]:
    """Keep one item per room: the pinned board, else room-only, else the rest.

    Args:
        rows: ``(room_key, meal_plan, item)`` triples. ``room_key`` identifies
            the room -- use ``(hotel_id, room_type_id)`` rather than the name,
            because "Classic Room" is a name half the market uses and two
            properties repricing must not collapse into one.
        board: the pinned meal plan, or ``None`` when none is set.
        fallback: the board a room not sold on ``board`` drops to.

    Returns:
        The kept items, in the order their room was first seen. Callers pass
        rows newest-first and a room therefore keeps the position of its most
        recent move, which is where a reader scanning the top of the page
        expects to find it.

        Ties -- two moves on the same board for the same room inside one
        window -- keep the FIRST, so a newest-first caller reports the latest
        price and not the one it has already been superseded by.
    """
    best: dict[Hashable, tuple[int, T]] = {}
    order: list[Hashable] = []

    for room_key, plan, item in rows:
        rank = _rank(plan, board, fallback)
        current = best.get(room_key, (_UNRANKED, None))
        if room_key not in best:
            order.append(room_key)
        if rank < current[0]:
            best[room_key] = (rank, item)

    return [best[key][1] for key in order]


__all__ = ["one_per_room"]
