"""What the operator is told when a page never opened at all.

THE MESSAGE THIS REPLACES
=========================
Pasting a MakeMyTrip link into "Detect and attach" produced:

    Could not inspect that page: Error. If it showed a CAPTCHA or a bot
    wall, that is a refusal and this hotel needs manual entry instead.

Both halves were wrong. "Error" is the name Playwright gives EVERY navigation
failure, so the class name said nothing; and the page had shown nothing at all
-- MakeMyTrip's edge closes the connection before serving a byte, so there was
no CAPTCHA to go and look for. The operator was sent to check a page they
could not open, for a wall that was not there.

The reason is in the exception's MESSAGE, as a ``net::ERR_*`` code, and that is
what these read. The distinction that matters is not which code it is: it is
whether trying again could ever help.
"""
from __future__ import annotations

import pytest

from app.adapters.discovery import _navigation_failure
from app.core.errors import BlockedError, ErrorClass, NetworkError

#: Verbatim from Playwright, call log and all, because that is the shape the
#: parser actually receives.
MAKEMYTRIP = (
    "Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at "
    "https://www.makemytrip.com/hotels/hotel-details/?hotelId=202309131850084425\n"
    "Call log:\n"
    '  - navigating to "https://www.makemytrip.com/hotels/hotel-details/", '
    'waiting until "domcontentloaded"\n'
)

URL = "https://www.makemytrip.com/hotels/hotel-details/?hotelId=202309131850084425"


class TestASiteThatClosesTheConnection:
    """The observed case: no page, no challenge, just a dropped socket."""

    def test_it_is_a_refusal_and_not_a_flaky_network(self):
        error = _navigation_failure(URL, Exception(MAKEMYTRIP))
        assert isinstance(error, BlockedError)
        assert error.error_class is ErrorClass.BLOCKED
        # The whole point of the class: nothing retries a refusal.
        assert error.is_transient is False

    def test_it_names_the_site_and_the_reason(self):
        error = _navigation_failure(URL, Exception(MAKEMYTRIP))
        assert "www.makemytrip.com" in str(error)
        assert "ERR_HTTP2_PROTOCOL_ERROR" in str(error)
        assert error.context["net_error"] == "ERR_HTTP2_PROTOCOL_ERROR"

    def test_it_does_not_send_anyone_looking_for_a_captcha(self):
        """There was no page. Telling someone to check one wastes their time."""
        text = str(_navigation_failure(URL, Exception(MAKEMYTRIP))).lower()
        assert "captcha" not in text

    def test_it_says_another_link_will_not_help(self):
        """Every address on that domain fails identically, robots.txt included.

        Without this the next thing anyone tries is a different URL from the
        same site, which costs another minute of browser time to fail the same
        way.
        """
        text = str(_navigation_failure(URL, Exception(MAKEMYTRIP)))
        assert "different link will not help" in text
        assert "manual entry" in text


class TestOrdinaryConnectivityFailures:
    """These say something about the network, not about the site's willingness."""

    @pytest.mark.parametrize(
        "code,expected_phrase",
        [
            ("ERR_NAME_NOT_RESOLVED", "no such host"),
            ("ERR_CONNECTION_REFUSED", "nothing is listening"),
            ("ERR_CONNECTION_TIMED_OUT", "did not answer in time"),
            ("ERR_TOO_MANY_REDIRECTS", "redirects in a loop"),
            ("ERR_CERT_DATE_INVALID", "certificate has expired"),
        ],
    )
    def test_the_reason_is_given_in_words(self, code, expected_phrase):
        error = _navigation_failure(
            "https://book.example.test/rooms", Exception(f"Page.goto: net::{code} at x")
        )
        assert isinstance(error, NetworkError)
        assert expected_phrase in str(error)
        assert code in str(error)

    def test_they_stay_retryable(self):
        """A typo'd host and a dropped packet are not the site saying no."""
        error = _navigation_failure(
            "https://book.example.test/", Exception("Page.goto: net::ERR_CONNECTION_RESET at x")
        )
        assert error.is_transient is True


class TestSomethingNobodyAnticipated:
    def test_chromium_is_quoted_rather_than_paraphrased(self):
        error = _navigation_failure(
            "https://book.example.test/", Exception("Page.goto: net::ERR_MADE_UP_THING at x")
        )
        assert "ERR_MADE_UP_THING" in str(error)

    def test_a_message_with_no_net_code_still_reads_as_a_sentence(self):
        error = _navigation_failure(
            "https://book.example.test/", Exception("Target page, context or browser has been closed")
        )
        assert "book.example.test" in str(error)
        assert "has been closed" in str(error)

    def test_the_call_log_is_left_out_of_the_message(self):
        """It belongs in the artifacts, not in a form field under an input."""
        error = _navigation_failure(URL, Exception(MAKEMYTRIP))
        assert "Call log" not in str(error)

    def test_an_empty_message_falls_back_to_the_class_name(self):
        """NotImplementedError from the event-loop policy bug arrived like this."""
        error = _navigation_failure("https://book.example.test/", NotImplementedError())
        assert "NotImplementedError" in str(error)
