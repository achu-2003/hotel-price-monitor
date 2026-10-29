"""Sales Daddy's WhatsApp AI is fed tonight's prices for our own hotel.

It quotes only from its own product list, so ``price_feed.push`` PUTs one
product per room per platform there: the cheapest offer the platform showed
on our last check, or the room marked sold out. These pin what one product
looks like, which offer it is, and when a list is (and is not) sent.

respx stands in for Sales Daddy and a dict for Redis.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.services import price_feed
from app.services.price_feed import Row
from app.workers import tasks_price_feed

NIGHT = date(2026, 10, 29)
SEEN = datetime(2026, 10, 29, 8, 35, tzinfo=UTC)
URL = "https://api.salesdaddy.in/v1/ext/products"


def row(room_type_id=12, room="Deluxe Double Room", code="booking_com", channel="Booking.com",
        price="5600", with_tax="5880", meal_plan=None, available=True, seen_at=SEEN) -> Row:
    return Row(room_type_id=room_type_id, room=room, source_code=code, channel=channel,
               meal_plan=meal_plan, price=Decimal(price) if price else None,
               price_with_tax=Decimal(with_tax) if with_tax else None,
               currency="INR", available=available, seen_at=seen_at)


class TestOneProductPerRoomPerPlatform:
    def test_a_product_carries_what_the_platform_printed(self):
        [item] = price_feed.items(7, [row()])
        assert item == {
            "external_id": "h7-r12-booking_com",
            "name": "Deluxe Double Room – Booking.com – tonight",
            "room": "Deluxe Double Room",
            "channel": "Booking.com",
            "available": True,
            "price": 5600,
            "price_with_tax": 5880,
            "meal_plan": None,
            "currency": "INR",
            "description": "Tonight on Booking.com, 2 adults, room only: ₹5,880 including taxes.",
            "seen_at": SEEN.isoformat(),
        }

    def test_the_same_room_on_three_platforms_is_three_products(self):
        out = price_feed.items(7, [
            row(code="booking_com", channel="Booking.com"),
            row(code="agoda", channel="Agoda", price="5500"),
            row(code="goibibo", channel="Goibibo", price="5450"),
        ])
        assert [i["external_id"] for i in out] == ["h7-r12-booking_com", "h7-r12-agoda", "h7-r12-goibibo"]

    def test_of_a_room_s_boards_the_cheapest_on_sale_is_quoted(self):
        [item] = price_feed.items(7, [
            row(price="6400", meal_plan="breakfast"),
            row(price="5600", meal_plan="room only"),
            row(price="5100", meal_plan="room only", available=False),
        ])
        assert (item["price"], item["meal_plan"]) == (5600, "room only")

    def test_a_room_with_nothing_on_sale_is_sent_as_sold_out(self):
        """Dropped instead, Sales Daddy would go on quoting its last price."""
        [item] = price_feed.items(7, [row(available=False)])
        assert item["available"] is False
        assert item["price"] is None and item["price_with_tax"] is None


class TestFingerprint:
    def test_a_new_check_at_the_same_prices_is_not_a_change(self):
        hotel = SimpleNamespace(id=7, name="ASG")
        a = price_feed.payload(hotel, NIGHT, price_feed.items(7, [row()]))
        b = price_feed.payload(hotel, NIGHT, price_feed.items(7, [row(seen_at=datetime(2026, 10, 29, 9, tzinfo=UTC))]))
        assert price_feed.fingerprint(a) == price_feed.fingerprint(b)

    def test_a_price_move_is(self):
        hotel = SimpleNamespace(id=7, name="ASG")
        a = price_feed.payload(hotel, NIGHT, price_feed.items(7, [row()]))
        b = price_feed.payload(hotel, NIGHT, price_feed.items(7, [row(price="5500")]))
        assert price_feed.fingerprint(a) != price_feed.fingerprint(b)


class FakeRedis:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value.encode()


@pytest.fixture
def wired(monkeypatch):
    """The task with its database, clock, Redis and key replaced."""
    rows = [row(), row(code="agoda", channel="Agoda", price="5500")]
    hotel = SimpleNamespace(id=7, name="ASG", is_own_property=True)

    class Session:
        def get(self, _model, _id):
            return hotel

        def execute(self, _stmt):
            return SimpleNamespace(all=lambda: [])

    @contextmanager
    def session():
        yield Session()

    redis = FakeRedis()
    settings = SimpleNamespace(
        price_feed_push_key=SecretStr("sd_partner_key"), price_feed_push_url=URL,
        price_feed_resend_hours=6, timezone="Asia/Kolkata",
    )
    monkeypatch.setattr(tasks_price_feed, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks_price_feed, "sync_session", session)
    monkeypatch.setattr(tasks_price_feed, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks_price_feed, "local_today", lambda _tz: NIGHT)
    monkeypatch.setattr(tasks_price_feed.price_feed, "to_rows", lambda _r: rows)
    return SimpleNamespace(settings=settings, redis=redis, rows=rows, hotel=hotel)


class TestPush:
    @respx.mock
    def test_the_list_is_put_with_the_partner_key(self, wired):
        route = respx.put(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        assert tasks_price_feed.push_prices.run(7) == {"status": "sent", "items": 2}
        sent = route.calls.last.request
        assert sent.headers["X-Api-Key"] == "sd_partner_key"
        body = json.loads(sent.content)
        assert (body["source"], body["night"], body["hotel"]) == ("rms", "2026-10-29", "ASG")
        assert [i["channel"] for i in body["items"]] == ["Booking.com", "Agoda"]

    @respx.mock
    def test_the_same_list_twice_is_sent_once(self, wired):
        route = respx.put(URL).mock(return_value=httpx.Response(200))
        tasks_price_feed.push_prices.run(7)
        assert tasks_price_feed.push_prices.run(7) == {"status": "unchanged"}
        assert route.call_count == 1

    @respx.mock
    def test_a_moved_price_is_sent_again(self, wired):
        route = respx.put(URL).mock(return_value=httpx.Response(200))
        tasks_price_feed.push_prices.run(7)
        wired.rows[1] = row(code="agoda", channel="Agoda", price="5300")
        tasks_price_feed.push_prices.run(7)
        assert route.call_count == 2

    @respx.mock
    def test_sales_daddy_down_is_retried_and_not_remembered(self, wired):
        respx.put(URL).mock(return_value=httpx.Response(503))
        with pytest.raises(tasks_price_feed.Retryable):
            tasks_price_feed.push_prices.run(7)
        assert wired.redis.data == {}

    @respx.mock
    def test_a_refused_list_is_not_retried(self, wired):
        respx.put(URL).mock(return_value=httpx.Response(401, json={"error": "invalid_key"}))
        assert tasks_price_feed.push_prices.run(7) == {"status": "refused", "code": 401}
        assert wired.redis.data == {}

    def test_without_the_key_nothing_is_sent(self, wired):
        wired.settings.price_feed_push_key = None
        assert tasks_price_feed.push_prices.run(7) == {"status": "off"}

    def test_a_competitor_is_never_sent(self, wired):
        wired.hotel.is_own_property = False
        assert tasks_price_feed.push_prices.run(7) == {"status": "not_own"}


class TestTheContract:
    """docs/SALESDADDY_PRICE_FEED.md: what the receiver is promised."""

    def test_the_list_says_its_version_and_that_it_is_complete(self):
        body = price_feed.payload(SimpleNamespace(id=7, name="ASG"), NIGHT, price_feed.items(7, [row()]))
        assert (body["version"], body["complete"], body["hotel_id"]) == (1, True, 7)

    def test_each_product_carries_a_sentence_the_ai_can_repeat(self):
        [item] = price_feed.items(7, [row(meal_plan="breakfast")])
        assert item["description"] == "Tonight on Booking.com, 2 adults, breakfast: ₹5,880 including taxes."

    def test_a_sold_out_product_says_so(self):
        [item] = price_feed.items(7, [row(available=False)])
        assert item["description"] == "Sold out tonight on Booking.com."

    def test_without_a_tax_figure_the_price_does_not_claim_one(self):
        [item] = price_feed.items(7, [row(with_tax=None)])
        assert item["description"].endswith("₹5,600 (taxes may be extra).")


class TestTheReadEndpoint:
    """GET /api/v1/price-feed: the same list, fetched instead of pushed."""

    @pytest.fixture
    def api(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.api.deps import db_session
        from app.api.v1 import price_feed as module
        from app.main import create_app

        hotel = SimpleNamespace(id=7, name="ASG")
        rows = [row(), row(code="agoda", channel="Agoda", price="5500")]

        class Session:
            async def scalars(self, _stmt):
                return SimpleNamespace(all=lambda: [hotel])

            async def execute(self, _stmt):
                return SimpleNamespace(all=lambda: [])

        async def _session():
            yield Session()

        settings = SimpleNamespace(price_feed_api_key=SecretStr("feed-key"), timezone="Asia/Kolkata")
        monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(module, "local_today", lambda _tz: NIGHT)
        monkeypatch.setattr(module.price_feed, "to_rows", lambda _r: rows)
        app = create_app()
        app.dependency_overrides[db_session] = _session
        return SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), settings=settings)

    def test_the_right_key_gets_the_list(self, api):
        response = api.client.get("/api/v1/price-feed", headers={"X-Api-Key": "feed-key"})
        assert response.status_code == 200
        body = response.json()
        assert (body["version"], body["night"]) == (1, "2026-10-29")
        [hotel] = body["hotels"]
        assert [i["channel"] for i in hotel["items"]] == ["Booking.com", "Agoda"]

    def test_a_wrong_or_missing_key_is_refused(self, api):
        assert api.client.get("/api/v1/price-feed", headers={"X-Api-Key": "nope"}).status_code == 401
        assert api.client.get("/api/v1/price-feed").status_code == 401

    def test_without_the_setting_the_route_is_not_there(self, api):
        api.settings.price_feed_api_key = None
        assert api.client.get("/api/v1/price-feed", headers={"X-Api-Key": "feed-key"}).status_code == 404
