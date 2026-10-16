"""A with-tax target becomes an RMS rate on the PRE-TAX figures.

WHAT WOULD HAVE GONE WRONG ON 16 OCT 2026
=========================================
GST on a hotel night is 5% up to a 7,500 tariff and 18% above it, and
Booking.com applies it exactly so. Our Standard Double (RMS DELUXE CP 8,500)
read 5,355 + 268 = 5,623; Sterling's Mountain View Classic Room read
8,060 + 1,451 = 9,511. A hundred under that is 9,411.

The ratio was taken over the with-tax prices -- 9,411 x 8,500 / 5,623 =
14,226 -- as if the tax were proportional. Booking.com shows 14,226 at
8,962 + 18% = 10,576: a thousand rupees OVER Sterling, from a rule set to sit
a hundred under. Every price we had ever followed sat in the 5% slab, where
the tax cancels out of the ratio, so it had never shown.

Pure: the arithmetic, checked against the channel's own.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.services import repricing as rule

D = Decimal


def _booking(rms: Decimal, deal: Decimal) -> Decimal:
    """What Booking.com quotes for an RMS rate: its deal, then GST by slab."""
    pre_tax = (rms * deal).quantize(D("1"), rounding=ROUND_HALF_UP)
    tax = (pre_tax * rule.gst_rate(pre_tax)).quantize(D("1"), rounding=ROUND_HALF_UP)
    return pre_tax + tax


class TestTheSixteenthOfOctober:
    DELUXE_RMS, DELUXE_DEAL, DELUXE_GUEST = D("8500"), D("0.63"), D("5623")
    CLASSIC_RMS, CLASSIC_DEAL, CLASSIC_GUEST = D("8000"), D("0.70"), D("5880")

    def test_the_channel_model_reproduces_todays_prices(self):
        assert _booking(self.DELUXE_RMS, self.DELUXE_DEAL) == self.DELUXE_GUEST
        assert _booking(self.CLASSIC_RMS, self.CLASSIC_DEAL) == self.CLASSIC_GUEST

    def test_a_hundred_under_sterling_across_the_slab_lands_a_hundred_under(self):
        rms = rule.to_rms(D("9411"), self.DELUXE_GUEST, self.DELUXE_RMS,
                          round_to=10, exact=True, with_tax=True)
        assert rms == D("12658")
        shown = _booking(rms, self.DELUXE_DEAL)
        assert D("9409") <= shown <= D("9411")

    def test_the_old_with_tax_ratio_would_have_landed_over_sterling(self):
        """The record of what the bug was, so nobody drops ``with_tax``."""
        rms = rule.to_rms(D("9411"), self.DELUXE_GUEST, self.DELUXE_RMS, round_to=10, exact=True)
        assert rms == D("14226")
        assert _booking(rms, self.DELUXE_DEAL) > D("9511")

    def test_a_target_inside_one_slab_is_unchanged(self):
        """Classic Room, 7,768 from 5,880: both 5%, the tax cancels, and the
        rate is the one the page already proposed."""
        rms = rule.to_rms(D("7768"), self.CLASSIC_GUEST, self.CLASSIC_RMS,
                          round_to=10, exact=True, with_tax=True)
        assert rms == D("10568")
        assert _booking(rms, self.CLASSIC_DEAL) == D("7768")

    def test_the_page_shows_what_the_guest_will_actually_see(self):
        rms = D("12658")
        seen = rule.guest_sees(rms, self.DELUXE_GUEST, self.DELUXE_RMS, with_tax=True)
        assert abs(seen - _booking(rms, self.DELUXE_DEAL)) <= 2


class TestTheGapInTheSlab:
    """No tariff shows a guest anything from 7,876 to 8,850."""

    OUR_GUEST, RMS, DEAL = D("5623"), D("8500"), D("0.63")

    def test_a_target_in_the_gap_takes_the_top_of_the_lower_slab(self):
        """The 50% step capped tonight's move at 8,430 -- in the gap."""
        rms = rule.to_rms(D("8430"), self.OUR_GUEST, self.RMS,
                          round_to=10, exact=True, with_tax=True)
        shown = _booking(rms, self.DEAL)
        assert shown <= D("7875")
        assert shown >= D("7870")

    def test_before_gst_never_gives_a_tariff_whose_price_is_over_the_target(self):
        for guest in range(7000, 10000, 7):
            pre_tax = rule.before_gst(D(guest))
            assert pre_tax * (1 + rule.gst_rate(pre_tax)) <= D(guest) + D("0.01")


class TestRoundingNeverCrossesTheSlab:
    def test_rounding_up_is_pulled_back_under_7500(self):
        """7,874 is 7,499 before tax; the nearest 25 above the ratio's answer
        is a tariff of 7,507, which is 18% and 8,858 to the guest."""
        rms = rule.to_rms(D("7874"), D("5880"), D("8000"), round_to=25, with_tax=True)
        assert rms % 25 == 0
        assert _booking(rms, D("0.70")) <= D("7875")

    def test_without_tax_nothing_changes(self):
        assert rule.to_rms(D("6000"), D("5610"), D("7500"), round_to=10) == D("8020")
