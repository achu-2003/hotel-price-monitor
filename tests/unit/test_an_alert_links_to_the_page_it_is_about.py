"""The link at the bottom of an alert, and everything it must not break.

An alert says a rate moved; the reader's next question is "against whom". The
link is the bridge to /comparison, and it travels in the LAST template
parameter rather than in a WhatsApp button — see ``_stamp_with_link`` for why
that is a decision about Meta's approval queue and not a compromise.

WHAT THESE TESTS ARE REALLY GUARDING
====================================
Riding in a parameter puts the link inside the one URL the whole message
travels in, against the 2,048-byte cap that produced a bare 404 from the
reseller on 9 September. Two things follow, and both are here:

  * the parameter COUNT must not change, because Meta rejects a wrong count
    and charges for it;
  * the link must survive ``_fit_to_query_budget``, which trims the longest
    parameter when a busy window overflows. A message that silently loses its
    last twenty characters is a link that 404s at the other end.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.notifications.base import ChangeLine
from app.notifications.providers import whatsapp_mydreams
from app.notifications.render import (
    _LINK_LABEL,
    _stamp_with_link,
    render_digest,
    render_summary,
)
from app.workers import tasks_notify

# The dispatcher harness, reused rather than rebuilt: one person, two of our
# properties, a move on each, and a FakeSession wired into tasks_notify. A
# second copy of it here would drift from the one the summary tests use, and
# the point of the last two tests is that this path behaves as it does there.
from tests.unit.test_a_summary_that_counts_the_window_it_covers import world  # noqa: F401

WHEN = datetime(2026, 9, 10, 9, 30, tzinfo=UTC)
LINK = "https://monitor.example.com/c/8Kd2xQ1mZpVr4tLa"


def _line(hotel="Thanga Kottai", room="Club Room", old="7139", new="6890"):
    return ChangeLine(
        hotel_name=hotel,
        room_name=room,
        old_price=Decimal(old),
        new_price=Decimal(new),
        delta=Decimal(new) - Decimal(old),
        delta_pct=Decimal("-3.5"),
        direction="decrease",
        currency="INR",
        check_in="2026-09-11",
        check_out="2026-09-12",
    )


# -- the shape of it -------------------------------------------------
def test_no_link_configured_leaves_every_message_exactly_as_it_was():
    """The default, and it has to be invisible.

    ``public_base_url`` is empty until somebody publishes the deployment on a
    domain. Until then the messages must be byte-identical to what they were,
    or this feature has changed what goes out to a paying template on every
    deployment that has not adopted it.
    """
    without = render_summary([_line()], window_hours=2, when=WHEN)
    with_none = render_summary([_line()], window_hours=2, when=WHEN, link=None)

    assert without.text == with_none.text
    assert without.template_params == with_none.template_params
    assert without.html == with_none.html
    assert _LINK_LABEL not in without.text


def test_the_link_is_last_and_the_count_is_unchanged():
    """Meta rejects a wrong parameter count, and charges for the attempt.

    The whole reason the link rides in the stamp is that the stamp is already
    the last variable of both approved templates. If this ever pushes the count
    up by one, the message is paid for and lands wrong.
    """
    plain = render_summary([_line()], window_hours=2, when=WHEN)
    linked = render_summary([_line()], window_hours=2, when=WHEN, link=LINK)

    assert len(linked.template_params) == len(plain.template_params)
    assert LINK in linked.template_params[-1]
    # Nowhere else: a link duplicated into a slot would cost a property its
    # place in the message.
    assert sum(LINK in p for p in linked.template_params) == 1


def test_the_per_change_alert_carries_it_too():
    """Both messages, not just the summary.

    "Every alert message" was the requirement. The immediate alert is the one
    read at the moment a rate moves, which is exactly when somebody wants to
    know where that leaves them.
    """
    message = render_digest("Thanga Kottai", [_line()], when=WHEN, link=LINK)

    assert message.text.rstrip().endswith(LINK)
    assert LINK in message.template_params[-1]
    assert len(message.template_params) == 7


def test_it_goes_under_the_prices_and_not_over_them():
    """Position is not decoration.

    The message is read for its numbers. A link above them is an instruction to
    go elsewhere before you have seen anything; under them it is the next step
    for a reader who has already decided the figures are interesting.
    """
    text = render_summary([_line()], window_hours=2, when=WHEN, link=LINK).text

    assert text.index("Club Room") < text.index(_LINK_LABEL)
    assert text.rstrip().endswith(LINK)


def test_the_label_says_what_tapping_it_does():
    """A bare URL under a bare timestamp reads as something left in by mistake."""
    stamped = _stamp_with_link("9:30 AM IST", LINK)

    assert _LINK_LABEL in stamped
    assert stamped.startswith("9:30 AM IST")
    assert stamped.endswith(LINK)


# -- the constraints the transport imposes ---------------------------
def test_a_link_never_contains_the_character_the_reseller_splits_on():
    """The provider joins parameters with commas and the reseller splits on them.

    A URL carrying one would arrive as two parameters, and the message would be
    refused for its count -- after being paid for. The tokens are base64url,
    whose alphabet is letters, digits, hyphen and underscore, so this holds by
    construction; the test is here because the construction is one line in a
    different module and nothing else would notice it changing.
    """
    from app.services.comparison_links import _new_token

    for _ in range(200):
        token = _new_token()
        assert "," not in token
        assert token.isascii()


def test_a_busy_window_trims_a_property_and_never_the_link():
    """The regression that matters, and the one that would be silent.

    ``_fit_to_query_budget`` trims the LONGEST parameter until the encoded URL
    fits. A truncated property line is a message that reads slightly short; a
    truncated link is a 404 at the other end, for every reader, with nothing
    anywhere reporting it.

    Twelve properties of long-named rooms is well over the budget -- the same
    shape as the message that came back 404 on 9 September.
    """
    moved = [
        _line(
            hotel=f"A Rather Long Property Name Number {n}",
            room="Anthapuram Suite with 2 Bedrooms, 1 Living Room and Private Pool",
        )
        for n in range(12)
    ]
    message = render_summary(moved, window_hours=2, when=WHEN, param_count=5, link=LINK)

    params = [whatsapp_mydreams._param_safe(p) for p in message.template_params]
    fixed = len("LicenseNumber=x&APIKey=y&Contact=919999999999&Template=t&Param=")
    fitted = whatsapp_mydreams._fit_to_query_budget(
        params, whatsapp_mydreams._MAX_QUERY_CHARS - fixed
    )

    # Something was cut -- otherwise this test proves nothing about trimming.
    assert sum(map(len, fitted)) < sum(map(len, params))
    # And it was not the link.
    assert LINK in fitted[-1]


def test_a_newline_separates_the_link_from_the_stamp():
    """Not a space.

    A parameter CAN carry a newline -- measured on the live reseller path on
    9 September -- and without one the URL runs on from the tax note as a
    single wrapped line, which on a phone reads as part of the sentence and
    stops looking tappable.
    """
    stamped = _stamp_with_link("9:30 AM IST · prices incl. tax", LINK)

    assert "\n" in stamped
    assert " " + LINK not in stamped


# -- email gets the affordance WhatsApp cannot afford ----------------
def test_email_gets_a_real_button():
    """The one channel with no approved template to keep in step with.

    WhatsApp gets a bare URL because a button would mean editing the template.
    Email has no such constraint, so it gets the button -- and it degrades to
    plain blue link text in a client that ignores the styling, which is the
    fallback rather than a failure.
    """
    html = render_summary([_line()], window_hours=2, when=WHEN, link=LINK).html

    assert f'href="{LINK}"' in html
    assert "See the full comparison" in html


@pytest.mark.parametrize("renderer", ["digest", "summary"])
def test_html_without_a_link_grows_no_empty_row(renderer):
    """An empty <tr> in an Outlook table is a visible gap, not nothing."""
    if renderer == "digest":
        html = render_digest("Thanga Kottai", [_line()], when=WHEN).html
    else:
        html = render_summary([_line()], window_hours=2, when=WHEN).html

    assert "<a href" not in html


# -- the path that was never running ---------------------------------
def test_the_dispatcher_itself_puts_a_link_in_the_message(world, linked_settings):
    """End to end through ``market_summary``, not through the renderer.

    THE TEST THIS FILE MOST NEEDED. Every assertion above hands ``render_summary``
    a link and checks where it lands, which says nothing about whether anything
    ever calls it with one. For a while nothing did under test: with
    PUBLIC_BASE_URL empty, ``_comparison_url`` returned before touching the
    session, so the mint-and-append path never ran and 1638 tests passed
    regardless. Setting the address broke twenty of them instantly on a fake
    with no ``scalar``.

    So this drives the real task with a published deployment configured, and
    asserts the link reached the body a recipient would read.
    """
    tasks_notify.market_summary()

    bodies = [
        n.body_rendered
        for n in world.tables.get("notifications", [])
        if n.body_rendered
    ]
    assert bodies, "the summary produced no notification to inspect"
    assert any(linked_settings in body for body in bodies)
    assert any(_LINK_LABEL in body for body in bodies)


def test_one_link_row_is_reused_across_a_dispatch(world, linked_settings):
    """Not one per message, per recipient, or per hotel.

    Four alert numbers on a two-hourly summary across ten hotels is a row every
    few minutes, every one of them opening the identical page. ``ensure_link``
    is keyed on (owner, night, occupancy, baseline) precisely so a dispatch
    mints at most one -- and a reader who kept this morning's message still
    lands on a live URL this afternoon.
    """
    tasks_notify.market_summary()

    minted = world.tables.get("comparison_links", [])
    assert len(minted) == 1, f"expected one link row, got {len(minted)}"
    assert minted[0].owner_user_id == 42
