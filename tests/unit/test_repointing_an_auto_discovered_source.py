"""Correcting the link on a source that no engine profile describes.

THE DEAD END THIS CLOSES
========================
``replace-url`` is the only safe way to change a source's link: it re-detects,
so the dates come back as placeholders instead of being pinned to whatever
night was in the address bar. It refused any URL that ``detect`` did not
recognise.

Auto-discovered sources are exactly the URLs ``detect`` does not recognise --
that is what "auto-discovered" means. They were attached by inspecting the
page, no profile was ever written for them, and so the one endpoint that can
correct a link would not touch them.

A swiftbook hotel landed in that gap. Its URL keeps its parameters in the
fragment, which nothing parameterised at the time, so it was stored with
25 August baked in and every check re-read that one night. Automatic repair
could not help: it rewrites ``adapter_config`` and never ``url``. Nothing short
of deleting the source and losing its history could change the link.

Safe to allow without inspecting the page, because this endpoint changes only
the URL -- no selector is re-derived, so nothing is invented for a site nobody
has looked at. The same-domain condition is what keeps it honest.
"""
from __future__ import annotations

from app.adapters.engines import detect
from app.api.v1.hotels import _detection_for_discovery

SWIFTBOOK = ("https://www.swiftbook.io/inst/#home"
             "?propertyId=362NTRtI6w1gSaEHad4Im74Q1Njg=&JDRN=Y"
             "&checkIn=2026-08-25&checkOut=2026-08-26&currency=INR"
             "&adult=2&child=0")


def test_no_engine_profile_recognises_this_url():
    """The premise. If this ever becomes false the fallback is dead code."""
    assert detect(SWIFTBOOK) is None


def test_the_fallback_produces_a_usable_detection():
    detection = _detection_for_discovery(SWIFTBOOK)
    assert detection.profile.adapter_key == "playwright_direct_site"
    assert detection.profile.domains == ("www.swiftbook.io",)


def test_the_replacement_link_can_price_a_specific_night():
    """The whole reason to re-detect rather than store the string as pasted."""
    detection = _detection_for_discovery(SWIFTBOOK)
    assert detection.is_complete is True
    assert "checkIn={check_in}" in detection.url_template
    assert "2026-08-25" not in detection.url_template


def test_it_keeps_the_adapter_the_stored_source_already_uses():
    """The endpoint compares this against the source's adapter_key.

    A mismatch reads as "different engine" and is refused with a 409, so a
    fallback that guessed a different adapter would replace one dead end with
    another.
    """
    assert _detection_for_discovery(SWIFTBOOK).profile.adapter_key == (
        _detection_for_discovery("https://www.swiftbook.io/inst/#home?checkIn=2026-01-01")
        .profile.adapter_key
    )


def test_the_property_is_not_read_as_having_changed():
    """external_id stays None, as it was on attach.

    If the fallback invented one, replace-url would see the property change,
    demand `discard_history`, and delete the source's baselines to fix a typo
    in its link.
    """
    assert _detection_for_discovery(SWIFTBOOK).external_id is None


# -- a standing-rate source may keep its date-less link ----------------
#
# A hotel whose booking page carries no dates -- pripgo, and every small site
# that prices from its homepage -- is attached by LABELLING it a standing rate,
# not by refusing it: attach_source_from_url sets standing_rate = not
# detection.is_complete, and create_target then refuses a future-dated window
# on such a source, which is the only way the missing dates could produce a
# wrong number.
#
# replace-url did not know that, and refused every URL without a check-in
# parameter. The effect was a hotel that could be attached from its homepage
# and then never have its link corrected, because the only URL that site has
# is the one being refused -- with a warning about prices going stale that
# describes a per-night source, not this one.

from app.api.v1.hotels import _may_carry_no_dates  # noqa: E402


class _Detection:
    def __init__(self, complete: bool):
        self.is_complete = complete


class TestAStandingRateSource:
    def test_it_may_be_repointed_at_another_date_less_url(self):
        """The dead end: this is the only kind of URL the site has."""
        assert _may_carry_no_dates(_Detection(False), was_standing_rate=True) is True

    def test_a_dated_url_is_still_accepted(self):
        """An upgrade, not a downgrade -- and the endpoint clears the flag so
        the hotel can be watched ahead."""
        assert _may_carry_no_dates(_Detection(True), was_standing_rate=True) is True


class TestAPerNightSource:
    def test_a_date_less_url_is_still_refused(self):
        """The guard's real job. Pinning a source that follows dates to one
        night is silent: every check re-reads it and the dashboard goes on
        presenting the number as current."""
        assert _may_carry_no_dates(_Detection(False), was_standing_rate=False) is False

    def test_a_dated_url_is_accepted(self):
        assert _may_carry_no_dates(_Detection(True), was_standing_rate=False) is True
