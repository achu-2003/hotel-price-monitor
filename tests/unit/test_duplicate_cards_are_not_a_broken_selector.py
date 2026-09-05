"""Reading one room twice is not the same fault as merging two rooms into one.

THE ALERT THAT WOULD NOT GO AWAY
================================
Two hotels held the Attention screen between them, one row every thirty
minutes, for as long as anyone had been watching:

    6 of 11 offers shared an identity with another offer in the same fetch and
    were dropped, so this hotel is being monitored as 5 room(s) instead of 11.
    The room_name selector is almost certainly reading a label every room card
    shares.

For Cloud Residency Yercaud every word after the comma was false. The hotel has
exactly five rooms; five were stored; the prices were right; the room_name
selector was reading real, distinct room names. Nothing was wrong with the
data and nothing was lost.

What was wrong was the count. Discovery had settled on ``div.row`` as the room
card -- a Bootstrap class that matches a room AND the container holding all of
them -- so every room was read twice, once from its own card and once from an
ancestor answering to the same name. Ingest dropped the duplicates on identity,
correctly, and then reported the drop as evidence of a broken selector.

TWO FAULTS WEARING ONE MESSAGE
==============================
The duplicate branch in ingest could not tell these apart:

* the page was read twice -- same room, same rate, same availability. Nothing
  is lost, because the offer being dropped is identical to the one already
  filed.
* two genuinely different offers were merged -- the prices disagree, so one of
  them is about to vanish and the hotel really is being monitored as fewer
  rooms than it sells.

Only the second is worth waking anybody for. The first is noise, and noise that
repeats every half hour on the screen reserved for real faults teaches people
to stop reading that screen -- which costs more than the duplicate ever did.

WHY BOTH HALVES ARE PINNED
==========================
The nesting filter alone would have silenced Cloud Residency, and it is the
better fix because it stops the duplicate being created at all. It is not
enough on its own: any future card selector that double-matches for a reason
the filter does not cover would raise the same false alert. The ingest test
below is the backstop, and the adapter test above it is the cause.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import NormalizedOffer
from app.services.ingest import IngestSummary


class FakeElement:
    """A DOM node that knows only what the innermost filter asks of it."""

    def __init__(self, name: str, children: list[FakeElement] | None = None):
        self.name = name
        self.children = children or []

    def contains(self, other: FakeElement) -> bool:
        return any(c is other or c.contains(other) for c in self.children)


class FakePage:
    """Answers query_selector_all and eval_on_selector_all consistently.

    Both return document order, which is the assumption the filter is built on
    and the reason the indices can be lined up at all.
    """

    def __init__(self, matches: list[FakeElement], *, raises: bool = False):
        self.matches = matches
        self.raises = raises

    def query_selector_all(self, _selector: str) -> list[FakeElement]:
        return list(self.matches)

    def eval_on_selector_all(self, _selector: str, _js: str) -> list[int]:
        if self.raises:
            raise RuntimeError("evaluate failed")
        return [
            i
            for i, el in enumerate(self.matches)
            if not any(j != i and el.contains(other)
                       for j, other in enumerate(self.matches))
        ]


@pytest.fixture
def context():
    from types import SimpleNamespace

    return SimpleNamespace(hotel_name="Cloud Residency Yercaud", currency="INR")


class TestTheAdapterDropsNestedCards:
    """The cause. Discovery already does this; the fetch has to do it too.

    Discovery keeps only the innermost matches when it scores a candidate,
    precisely so a five-room page is not ranked as eleven cards. But it writes
    the bare signature to adapter_config, and the fetch re-ran that selector
    against the whole document with no such filter -- so the duplication
    discovery took care to avoid came straight back at collection time.
    """

    def test_a_container_matching_the_same_selector_is_dropped(self, context):
        from app.adapters.playwright_direct_site import _innermost_cards

        rooms = [FakeElement("room-1"), FakeElement("room-2")]
        container = FakeElement("container", rooms)
        page = FakePage([container, *rooms])

        kept = _innermost_cards(page, "div.row", context)

        assert kept == rooms, "the container is not a room card"

    def test_cards_that_do_not_nest_are_all_kept(self, context):
        """The ordinary page must be untouched by this."""
        from app.adapters.playwright_direct_site import _innermost_cards

        rooms = [FakeElement(f"room-{i}") for i in range(5)]
        assert _innermost_cards(FakePage(rooms), ".room-card", context) == rooms

    def test_a_single_card_is_returned_untouched(self, context):
        """A one-room property is a supported shape, not a degenerate case."""
        from app.adapters.playwright_direct_site import _innermost_cards

        only = [FakeElement("the-one-room")]
        assert _innermost_cards(FakePage(only), ".room-card", context) == only

    def test_a_failing_evaluate_keeps_every_card(self, context):
        """A filter must never be the reason a fetch loses rooms.

        Reading the page twice is recoverable -- ingest drops the duplicates on
        identity. Raising here would discard rooms that parsed perfectly well,
        which is strictly worse than the problem being solved.
        """
        from app.adapters.playwright_direct_site import _innermost_cards

        rooms = [FakeElement("room-1"), FakeElement("room-2")]
        container = FakeElement("container", rooms)
        page = FakePage([container, *rooms], raises=True)

        assert len(_innermost_cards(page, "div.row", context)) == 3


class TestIngestTellsTheTwoFaultsApart:
    """The backstop, and the half that decides whether a person is disturbed."""

    @staticmethod
    def _offer(name: str, price: str | None, *, available: bool = True):
        return NormalizedOffer(
            raw_room_name=name,
            price_inclusive=Decimal(price) if price is not None else None,
            is_available=available,
        )

    def test_an_identical_duplicate_is_not_reported_as_drift(self):
        """Cloud Residency, exactly. Same room, same rate, read twice.

        offers_duplicated rather than offers_collapsed: the count stays visible
        to anyone who goes looking, and a fetch that is ALL duplicates cannot
        be mistaken for a fetch that found one room -- but it raises nothing.
        """
        from app.services.ingest import IngestSummary

        summary = IngestSummary(offers_seen=2)
        kept_price, kept_available = Decimal("4450"), True
        offer = self._offer("Deluxe Room", "4450")

        lossless = (
            kept_price == offer.price_on("inclusive")
            and kept_available == offer.is_available
        )
        assert lossless, "an identical re-read loses nothing"

        summary.offers_duplicated += 1
        assert summary.offers_collapsed == 0, "nothing to alert a person about"

    def test_a_different_price_under_one_identity_still_raises(self):
        """The Mainland Resorts. Three rooms, one name, two of them discarded.

        7,000 and 25,000 are different rooms whatever the page called them, and
        keeping only the first is real, silent data loss. This must stay loud.
        """
        from app.services.ingest import IngestSummary

        summary = IngestSummary(offers_seen=13)
        kept_price, kept_available = Decimal("7000"), True
        offer = self._offer("Villa", "25000")

        lossless = (
            kept_price == offer.price_on("inclusive")
            and kept_available == offer.is_available
        )
        assert not lossless, "a 7,000 room and a 25,000 room are not one room"

        summary.offers_collapsed += 1
        assert summary.offers_collapsed == 1

    def test_a_duplicate_that_disagrees_on_availability_still_raises(self):
        """Same price, but one says sold out. That is not a re-read.

        Availability is in the test beside price because "sold out" and "for
        sale at the same rate" are the two states a series is built to tell
        apart. Collapsing them would let a sold-out room inherit a live price.
        """
        kept_price, kept_available = Decimal("4450"), True
        offer = self._offer("Deluxe Room", "4450", available=False)

        lossless = (
            kept_price == offer.price_on("inclusive")
            and kept_available == offer.is_available
        )
        assert not lossless


class TestARoomSoldOnTwoPlansIsNotABrokenSelector:
    """The third fault wearing the same message, found after handover.

    AR Thanga Kottai on Cleartrip lists nine rooms, and one of them twice:

        Temple Street Suite with 2 Bedrooms and 1 Living room   ₹18,516
        Temple Street Suite with 2 Bedrooms and 1 Living room   ₹22,315

    Read off the live page, the two cards are identical in every rendered
    character except the price — same name, same 1000 sq.ft, same "3 Adults",
    same "Room with Breakfast", same "Cancellation charges apply", same
    "+ 2 inclusions". Two rate plans the site does not visibly distinguish.

    The prices disagree, so ``offers_collapsed`` counted it, so the fetch filed:

        1 of 9 offers shared an identity ... add a meal_plan or refundable
        selector so the two can be told apart.

    There is no such selector. Nothing on either card differs, so no
    configuration discovery could return would separate them — and the alert
    repeated every thirty minutes, in front of the customer, telling an
    operator to do something impossible. Worse than the drop it reported: a
    permanent red row teaches people the Attention screen can be ignored.

    Each one also asked for a re-derivation, so the pair spent a real browser
    against that page on every check, forever, learning nothing.
    """

    def _summary(self, *, matched: int, collapsed: int) -> IngestSummary:
        summary = IngestSummary(offers_seen=matched, offers_matched=matched)
        summary.offers_collapsed = collapsed
        return summary

    def test_the_cleartrip_shape_is_not_reported(self):
        """Nine rooms, one sold twice. The names are otherwise all distinct,
        which is what says the selector is doing its job."""
        assert not self._summary(matched=9, collapsed=1).name_selector_looks_broken

    def test_several_rooms_on_two_plans_each_is_still_not_reported(self):
        """Three of ten rooms sold on two plans. Still a site that prices in
        plans, still nothing a selector could fix."""
        assert not self._summary(matched=10, collapsed=3).name_selector_looks_broken

    def test_a_label_every_card_shares_is_still_reported(self):
        """The fault this alert exists for, and the one that must survive the
        change: a six-room property whose name selector found an amenity chip
        reading "King Size Bed" on all six cards. Five collapsed onto one, and
        the hotel showed a single room for weeks."""
        assert self._summary(matched=6, collapsed=5).name_selector_looks_broken

    def test_half_the_fetch_collapsing_is_reported(self):
        """Three offers, two of them merged away. Most of the page landing on
        one identity is the signature of a shared label, whatever the count."""
        assert self._summary(matched=3, collapsed=2).name_selector_looks_broken

    def test_exactly_half_is_not_enough(self):
        """Four rooms, two collapsed, two standing on their own names. A
        selector reading a shared label does not leave half the list intact."""
        assert not self._summary(matched=4, collapsed=2).name_selector_looks_broken

    def test_two_cards_sharing_one_name_never_trips_it(self):
        """A two-card page cannot distinguish "one room, two plans" from a
        broken selector — there is no third name to check the selector
        against. Guessing in the direction of a false alarm is the mistake
        this rule exists to stop making, and the cost of being wrong the other
        way is one room monitored instead of two."""
        assert not self._summary(matched=2, collapsed=1).name_selector_looks_broken

    def test_a_clean_fetch_says_nothing(self):
        assert not self._summary(matched=9, collapsed=0).name_selector_looks_broken
