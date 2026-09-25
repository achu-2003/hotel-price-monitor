"""A competitor can be read at the price its pasted link shows.

Peters Park's link came from Google Hotels and Booking.com showed it a 12%
deal: 4,400 on that link, 5,000 on the public page the app reads. The owner
wanted the app to match the link. The public rate stays the default for every
site (``test_a_campaign_link_is_not_the_public_price``); this is the per-site
exception, and it is refused on your own property, whose price the repricing
rule turns into an RMS rate.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

from app.adapters.playwright_direct_site import KEEP_LINK_DEAL, PlaywrightDirectSiteAdapter
from app.api.v1 import hotels as hotels_api
from app.schemas.hotels import LinkDeal
from app.services.dates import StayWindow

LINK = ("https://www.booking.com/hotel/in/peters-park.en-gb.html?aid=1288252"
        "&label=metagha-link-MRI&checkin={check_in}&checkout={check_out}&group_adults={adults}")


def _url(config: dict) -> dict:
    context = SimpleNamespace(
        url=LINK, check_in=date(2026, 9, 25), check_out=date(2026, 9, 26),
        stay=StayWindow(check_in=date(2026, 9, 25), check_out=date(2026, 9, 26)),
        adults=2, children=0, rooms=1, currency="INR", external_id=None,
    )
    return parse_qs(urlparse(PlaywrightDirectSiteAdapter()._resolve_url(context, config)).query)


class TestTheReader:
    def test_by_default_it_reads_the_public_price(self):
        query = _url({})
        assert "aid" not in query and "label" not in query

    def test_switched_on_it_reads_the_link_as_pasted(self):
        query = _url({KEEP_LINK_DEAL: True})
        assert query["aid"] == ["1288252"] and query["label"] == ["metagha-link-MRI"]

    def test_the_night_is_filled_in_either_way(self):
        assert _url({KEEP_LINK_DEAL: True})["checkin"] == ["2026-09-25"]


class _Session:
    def __init__(self):
        self.committed = False

    async def get(self, model, key):
        return SimpleNamespace(code="www-booking-com", adapter_key="playwright_direct_site")

    async def commit(self):
        self.committed = True


def _switch(monkeypatch, *, own: bool, enabled: bool, config=None):
    row = SimpleNamespace(id=14, hotel_id=13, source_id=2, url=LINK, external_id=None,
                          currency="INR", adapter_config=dict(config or {"selectors": {"price": "x"}}),
                          is_active=True, last_verified_at=None)

    async def found(*a, **k):
        return row

    async def owned(*a, **k):
        return SimpleNamespace(id=13, is_own_property=own)

    async def audit(*a, **k):
        return None

    monkeypatch.setattr(hotels_api, "get_object_or_404", found)
    monkeypatch.setattr(hotels_api, "owned_hotel_or_404", owned)
    monkeypatch.setattr(hotels_api, "record_audit", audit)
    session = _Session()
    asyncio.run(hotels_api.set_link_deal(14, LinkDeal(enabled=enabled), None, session, None))
    return row, session


class TestTheSwitch:
    def test_it_sets_one_key_and_keeps_the_rest(self, monkeypatch):
        row, session = _switch(monkeypatch, own=False, enabled=True)
        assert row.adapter_config == {"selectors": {"price": "x"}, KEEP_LINK_DEAL: True}
        assert session.committed

    def test_switching_it_off_removes_the_key(self, monkeypatch):
        row, _ = _switch(monkeypatch, own=False, enabled=False,
                         config={"selectors": {}, KEEP_LINK_DEAL: True})
        assert KEEP_LINK_DEAL not in row.adapter_config

    def test_it_is_refused_on_your_own_property(self, monkeypatch):
        with pytest.raises(HTTPException) as refused:
            _switch(monkeypatch, own=True, enabled=True)
        assert refused.value.status_code == 409

    def test_your_own_property_can_always_be_switched_back_off(self, monkeypatch):
        row, _ = _switch(monkeypatch, own=True, enabled=False, config={KEEP_LINK_DEAL: True})
        assert KEEP_LINK_DEAL not in row.adapter_config


def test_a_repair_keeps_the_choice():
    """It is a person's setting, not discovery's, so a repaired config keeps it."""
    from app.services.rediscovery import merge_config
    merged = merge_config({KEEP_LINK_DEAL: True, "selectors": {"price": "old"}},
                          {"selectors": {"price": "new"}, "room_card": "tr"})
    assert merged[KEEP_LINK_DEAL] is True
