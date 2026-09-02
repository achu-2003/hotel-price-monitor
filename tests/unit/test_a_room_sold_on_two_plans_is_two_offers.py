"""A room sold on two rate plans is two offers, not one offer and a warning.

THE FAILURE THIS EXISTS TO STOP
===============================
Downstream a room's identity is (name, meal plan, refundability). Discovery
wrote a ``room_name`` and a ``price`` selector and nothing else, so the last
two were empty on every offer -- and one suite sold room-only and again with
breakfast arrived as ONE identity. The second card was dropped, and the fetch
said so every thirty minutes::

    1 of 12 offers shared an identity with another offer in the same fetch AT
    A DIFFERENT PRICE, so the cheaper or dearer of each pair was dropped and
    this hotel is being monitored as 11 room(s). Either the room_name selector
    is reading a label several cards share, or the site sells these rooms under
    rate plans the config does not capture -- add a meal_plan or refundable
    selector so the two can be told apart.

The advice was sound and unreachable. ``playwright_direct_site`` reads
``selectors.meal_plan`` when it is there; nothing could put it there. So the
repair re-derived a byte-identical config, "a repair must actually differ"
refused to write it, and the alert came back on the next check. Forever.

The price that survived was not the right one either: the winner is whichever
card comes first in the DOM, which is not the figure the listing leads with,
and is not stable -- a site that reorders its two rate rows walks the stored
series between them and reports a price change nobody made. That is what "A R
Thanga Kottai shows the wrong price" was.

WHAT MUST NOT HAPPEN
====================
A plan selector rewrites the offer key of every room on the page, so it is not
something to hand out on suspicion. Two guards bound it, and both are tested
here: it is derived ONLY where the cards are already colliding, and only from a
label that will read the same tomorrow.
"""
from __future__ import annotations

import pytest

from app.adapters.discovery import _candidate_from_dom
from app.adapters.dom_discovery import find_room_cards


def _card(title: str, plan: str, price: str, left: int) -> str:
    """One Cleartrip-shaped room: a title, a rate plan, an urgency counter.

    The counter is the trap. It separates the colliding pair exactly as well as
    the plan does, and it is different on every check.
    """
    return f"""
      <div class="stacked">
        <h4 class="title">{title}</h4>
        <div class="rate">
          <h4 class="plan room--inclusions--header">{plan}</h4>
          <div class="urgency">{left} rooms left</div>
          <h5 class="amt">&#8377; {price}</h5>
        </div>
      </div>"""


def _page(rows) -> str:
    return "<html><body><div class='wrap'>" + "".join(
        _card(*row) for row in rows
    ) + "</div></body></html>"


# The production shape: one suite sold twice at two prices, everything else
# named once. 1 of 12 offers collided; 11 rooms were monitored.
COLLIDING = _page(
    [("Temple Street Suite with 2 Bedrooms", "Room with Breakfast", "8,995", 3),
     ("Temple Street Suite with 2 Bedrooms", "Room with Breakfast & Dinner", "10,495", 2)]
    + [(f"Garden Villa {i}", "Room with Breakfast", f"{5 + i},200", i + 1)
       for i in range(6)]
)

# The same page with nothing colliding. Every room is named once, so nothing
# here needs a plan and nothing may be given one.
CLEAN = _page([(f"Room {i}", "Room with Breakfast", f"{3 + i},000", i + 1)
               for i in range(5)])

# Colliding, and the ONLY thing that separates the pair is the counter.
VOLATILE = """<html><body><div class="wrap">
  <div class="stacked"><h4 class="title">Deluxe Room</h4>
    <div class="rate"><div class="left">2 rooms left</div>
      <h5 class="amt">&#8377; 3,200</h5></div></div>
  <div class="stacked"><h4 class="title">Deluxe Room</h4>
    <div class="rate"><div class="left">5 rooms left</div>
      <h5 class="amt">&#8377; 3,800</h5></div></div>
</div></body></html>"""


def _scan(html):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment without playwright
        pytest.skip("playwright is not installed")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content(html)
                return find_room_cards(page)
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


class TestThePageThatWasCollapsing:
    @pytest.fixture(scope="class")
    def best(self):
        cards = _scan(COLLIDING)
        if not cards:
            pytest.fail("the scan found no room list on a page full of rooms")
        return cards[0]

    def test_the_rate_plan_is_found(self, best):
        """The whole fix in one assertion: the config now HAS a meal_plan."""
        assert best["plan_selector"] == "h4.plan.room--inclusions--header", (
            best["plan_selector"]
        )

    def test_the_colliding_pair_comes_away_with_two_plans(self, best):
        """Which is what stops one of them being dropped."""
        pair = [best["plans"][i] for i, name in enumerate(best["names"])
                if name.startswith("Temple Street")]
        assert len(pair) == 2, best["names"]
        assert len(set(pair)) == 2, pair

    def test_the_room_is_still_named_after_the_room(self, best):
        """The plan is a second label, never a replacement for the first.

        Taking the varying label as the NAME is the other way to make this
        page stop colliding, and it names the rooms after their board basis.
        """
        assert best["name_selector"] == "h4.title", best["name_selector"]
        for name in best["names"]:
            assert "breakfast" not in name.lower(), best["names"]

    def test_the_hotel_is_counted_by_what_it_will_be_filed_under(self, best):
        """`rooms` is the ranking's estimate of what survives ingest.

        With a plan selector there is nothing left to estimate: the offers are
        filed under (name, plan), so the count of those IS the count.
        """
        assert best["identities"] == best["matched"], (
            best["names"], best["plans"]
        )
        assert best["rooms"] == best["identities"]

    def test_the_urgency_counter_is_not_the_rate_plan(self, best):
        """It separates the pair perfectly and changes on every check.

        Stored, it would file each fetch under a brand new offer key: the price
        history restarts twice an hour and every room reports itself sold out.
        """
        assert "urgency" not in best["plan_selector"], best["plan_selector"]
        for plan in best["plans"]:
            assert not any(c.isdigit() for c in plan), best["plans"]


class TestThePagesThatMustNotChange:
    def test_a_room_list_with_no_collision_gets_no_plan(self):
        """The blast radius, asserted.

        A plan selector rewrites the offer key of every room on the page. On a
        healthy source that is a hotel's entire price history restarting for
        nothing, so the derivation is scoped to pages that are already losing
        offers -- and this page is not one of them.
        """
        best = _scan(CLEAN)[0]
        assert best["plan_selector"] == "", best["plan_selector"]
        assert best["plans"] == []
        assert best["identities"] == best["distinct"] == len(best["names"])

    def test_a_collision_nothing_stable_separates_is_left_alerting(self):
        """Refusing is the right answer even though it leaves the alert open.

        A person reading "add a meal_plan selector" can look at the page and
        decide. A volatile key cannot be undone by anybody.
        """
        best = _scan(VOLATILE)[0]
        assert best["plan_selector"] == "", best["plan_selector"]


class TestWhatReachesTheConfig:
    """``_candidate_from_dom`` is what turns a scan hit into something stored."""

    def _hit(self, **overrides):
        hit = {
            "card": "div.stacked",
            "name_selector": "h4.title",
            "price_selector": "h5.amt",
            "plan_selector": "h4.plan",
            "names": ["Suite", "Suite"],
            "plans": ["Room Only", "With Breakfast"],
            "prices": [3200, 3800],
            "count": 2,
            "matched": 2,
            "name_trusted": True,
        }
        hit.update(overrides)
        return hit

    def test_the_plan_becomes_a_meal_plan_selector(self):
        candidate = _candidate_from_dom(self._hit(), "https://example.test/h")
        assert candidate.fields["meal_plan"] == "h4.plan"
        config = candidate.as_adapter_config("")
        assert config["selectors"]["meal_plan"] == "h4.plan"

    def test_a_scan_without_one_writes_no_meal_plan_key(self):
        """An older scan result, and every page that needed no plan.

        The key must be ABSENT rather than empty: the adapter tests the
        selector for truthiness, but a repair compares configs key by key and
        a stray empty string would read as a change on every run.
        """
        candidate = _candidate_from_dom(
            self._hit(plan_selector="", plans=[], names=["Suite", "Villa"]),
            "https://example.test/h",
        )
        assert "meal_plan" not in candidate.fields
        assert candidate.sample_plans == []

    def test_the_plan_is_refused_when_it_is_the_name_or_the_price(self):
        """A config naming one element as both has never once been right."""
        for clash in ("h4.title", "h5.amt"):
            candidate = _candidate_from_dom(
                self._hit(plan_selector=clash), "https://example.test/h"
            )
            assert "meal_plan" not in candidate.fields, clash

    def test_names_and_plans_stay_paired_when_a_name_is_blank(self):
        """Filtering one list and not the other shifts every plan by one.

        The room above would then be verified against the plan belonging to
        the room below it -- and the pairing is what ``is_verified`` reads.
        """
        candidate = _candidate_from_dom(
            self._hit(
                names=["", "Suite", "Villa"],
                plans=["Room Only", "With Breakfast", "Half Board"],
                prices=[3200, 3800, 4500],
            ),
            "https://example.test/h",
        )
        assert candidate.sample_names == ["Suite", "Villa"]
        assert candidate.sample_plans == ["With Breakfast", "Half Board"]

    def test_verification_asks_what_the_offers_will_be_filed_under(self):
        """A repeated name is only a collapse while nothing separates it.

        This is the untrusted case -- a bare div, no heading -- which
        ``is_verified`` refuses on repetition alone. With a plan selector the
        repetition is not a collapse, and refusing it would reject a candidate
        that is about to work perfectly.
        """
        from decimal import Decimal

        from app.adapters.discovery import Candidate

        def candidate(plans):
            return Candidate(
                source_url="https://example.test/h",
                rooms_path="div.stacked",
                fields={"room_name": "div.n", "price": "div.p"},
                kind="dom",
                sample_names=["Suite", "Suite"],
                sample_plans=plans,
                sample_prices=[Decimal("3200"), Decimal("3800")],
                corroborated=2,
                corroborated_marked=2,
                name_trusted=False,
            )

        assert candidate([]).is_verified is False
        assert candidate(["Room Only", "With Breakfast"]).is_verified is True
        # Separated on paper only: the same plan on both cards leaves the pair
        # exactly where it was.
        assert candidate(["Room Only", "Room Only"]).is_verified is False


class TestThePlanIsBoundedBeforeItIsStored:
    """``price_series.meal_plan`` is sixty characters wide.

    While only a person could add a meal_plan selector, an over-long label was
    their problem to notice. Discovery derives one now, so the bound has to
    hold on its own -- and the failure it prevents is not a graceful one: the
    INSERT raises and takes down the whole fetch, every room in it included.

    Bounded in the constructor because the plan is also HASHED into the offer
    key. Trimming in one place and not the other would produce a key that no
    longer describes the row it belongs to.
    """

    def _offer(self, plan):
        from app.adapters.base import NormalizedOffer

        return NormalizedOffer(raw_room_name="Suite", meal_plan=plan)

    def test_a_long_label_is_cut_to_the_column(self):
        from app.adapters.base import NormalizedOffer

        assert len(self._offer("Room with " + "everything " * 20).meal_plan) == (
            NormalizedOffer.MEAL_PLAN_MAX
        )

    def test_whitespace_is_collapsed_so_a_reflow_is_not_a_new_offer(self):
        """A label that gains a line break on the next deploy is the same
        plan, and must not open a second price series for the same offer."""
        assert self._offer("  Room with\n  Breakfast ").meal_plan == (
            "Room with Breakfast"
        )

    def test_an_empty_label_stays_absent(self):
        """"Plan unknown" and "plan is the empty string" are different offers
        in the key, and a selector that matched an empty element means the
        first."""
        assert self._offer("   ").meal_plan is None
        assert self._offer(None).meal_plan is None
