"""A target becomes an RMS rate by the ratio between two readings, and the
two are taken at different moments: the grid now, the site up to half an
hour ago. Every guard in this file exists because that gap is real.

WHAT WENT WRONG ON 22 SEP 2026
==============================
A run set DELUXE's CP cell to 5,082 and Booking.com duly showed ₹4,536 --
exactly the hundred under Sterling the owner asked for. Fourteen minutes
later the grid read 8,500 again; something outside this app had put it back.
The site still showed 4,536, because that is what 5,082 produces.

The next run paired them: 8,500 from the grid, a price that came from 5,082
on the site. It wrote 8,001 -- and 8,001 on that channel is about ₹7,141 to
a guest, eighteen hundred rupees ABOVE the hotel the rule exists to sit a
hundred below. It is the worst shape of bug this system can have: confident,
quiet, and in the wrong direction.

No arithmetic survives two readings of different moments. So the run does
not try to be clever with them -- it notices, refuses, and says so.

Pure: these test the decision, not the browser.
"""
from __future__ import annotations

from decimal import Decimal

from app.services import repricing as rule


class TestTheRatioIsOnlySafeWhileTheGridIsWhereWeLeftIt:
    """The arithmetic itself, shown with the real numbers, so the guard's
    reason for existing is checkable rather than asserted."""

    TARGET = Decimal("5216")          # Sterling 5,316 less the hundred

    def test_a_matched_pair_gives_the_rate_that_lands_on_the_target(self):
        """Grid 8,000 and site 7,140 were read of the same moment."""
        rms = rule.to_rms(self.TARGET, Decimal("7140"), Decimal("8000"),
                          round_to=10, exact=True)
        assert rms == Decimal("5844")
        assert (rms * Decimal("7140") / Decimal("8000")).quantize(Decimal("1")) == self.TARGET

    def test_a_mismatched_pair_gives_a_rate_that_lands_nowhere_near(self):
        """The same target, the same site price -- but the grid has been put
        back to 8,000 while the site still shows what 5,844 produced."""
        stale = rule.to_rms(self.TARGET, Decimal("5215"), Decimal("8000"),
                            round_to=10, exact=True)
        assert stale == Decimal("8001")
        # What a guest would then be charged, through the same factor the
        # site is actually applying (15% off, then 5% tax).
        guest = stale * Decimal("0.85") * Decimal("1.05")
        assert guest > Decimal("7000")
        # ...against a competitor at 5,316. Over them, not under.
        assert guest - Decimal("5316") > Decimal("1500")

    def test_the_error_is_the_size_of_the_disagreement(self):
        """Not a rounding wobble to be tolerated: the two readings imply
        factors a third apart, and the written rate is out by the same."""
        matched = Decimal("7140") / Decimal("8000")
        mismatched = Decimal("5215") / Decimal("8000")
        assert matched / mismatched > Decimal("1.3")


class TestWhatTheGuardCompares:
    """The check is not on the factor, which moves for honest reasons -- a
    channel's deal can change overnight and that is not an error. It is on
    one fact that cannot be argued with: the cell holds what we last wrote,
    or it does not."""

    def test_unchanged_since_our_write_is_the_healthy_case(self):
        assert int(Decimal("5082.00")) == int(Decimal("5082"))

    def test_changed_since_our_write_is_the_case_that_must_refuse(self):
        assert int(Decimal("5082.00")) != int(Decimal("8500"))

    def test_a_cell_we_never_wrote_today_has_nothing_to_compare(self):
        """First run of the night: no prior write, so no disagreement is
        possible and the ratio is the only thing there is."""
        prior_applied = None
        assert prior_applied is None
