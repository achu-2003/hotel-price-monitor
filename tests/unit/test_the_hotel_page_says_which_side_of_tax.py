"""The hotel page's price column says the basis its numbers are SHOWN on.

On 25 Sep Peters Park's Double Room read "PRICE EXCL. TAX ... 5,250": the
page showed 5,000 + 250 because Settings asks for prices with tax, while the
header described what Booking.com published, which is pre-tax. A header that
disagrees with the figure beneath it is worse than none.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.dashboard.routes import templates
from app.services.price_display import Shown


def _page(show_with_tax: bool) -> str:
    series = SimpleNamespace(
        offer_key="k", meal_plan="Room Only", adults=2, currency="INR", is_available=True,
        current_price=Decimal("5000"), pending_price=None, pending_count=0,
        last_checked_at=datetime(2026, 9, 25, 6, 37, tzinfo=UTC),
        last_price_basis=SimpleNamespace(value="exclusive"),
    )
    shown = Decimal("5250") if show_with_tax else Decimal("5000")
    return templates.get_template("hotel_detail.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/hotels/13")),
        user=SimpleNamespace(username="ops", full_name="Ops"), is_admin=True,
        attention={"total": 0},
        hotel=SimpleNamespace(id=13, name="Peters Park", slug="peters-park", location=None,
                              notes=None, is_active=True, is_own_property=False),
        sources=[], rooms=[], runs=[], targets=[], series=[], unmatched=[], errors=[],
        recipients=[], assignments={}, channels=["email"], has_source=True,
        alert_defaults=SimpleNamespace(min_delta_abs=50, min_delta_pct=2, confirm_checks=2),
        prices=[(series, "Double Room")], shown_prices={"k": Shown(shown)},
        unlisted_keys=set(), show_with_tax=show_with_tax,
    )


def _header(html: str) -> str:
    th = re.search(r'<th class="num">Price(.*?)</th>', html, re.S).group(1)
    return " ".join(re.sub(r"<[^>]+>", " ", th).split())


def test_prices_shown_with_tax_are_headed_incl_tax():
    html = _page(show_with_tax=True)
    assert _header(html) == "incl. tax"
    assert "5,250" in html


def test_prices_shown_before_tax_are_headed_excl_tax():
    html = _page(show_with_tax=False)
    assert _header(html) == "excl. tax"
    assert "5,000" in html
