"""Tonight's platform prices, for another system to fetch.

The push (workers/tasks_price_feed.py) sends this list to Sales Daddy when it
changes. This is the same list on request: for their developers to build and
test against before their receiver exists, and for a receiver that was down
to catch up without waiting for the next change. docs/SALESDADDY_PRICE_FEED.md
is the contract.

Not a user endpoint: no login, one shared key (Settings.price_feed_api_key)
in ``X-Api-Key`` -- or none at all with Settings.price_feed_public, since
these are prices every platform visitor already sees. With neither set the
route answers 404, so a deployment that never turned the feed on does not
advertise it.
"""
from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import select

from app.api.deps import DbSession
from app.config import get_settings
from app.db.models import Hotel
from app.services import price_feed
from app.services.dates import local_today

router = APIRouter(prefix="/price-feed", tags=["price-feed"])


@router.get("")
async def read_price_feed(
    session: DbSession,
    x_api_key: str | None = Header(default=None),
    hotel_id: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    expected = settings.price_feed_api_key
    if not settings.price_feed_public:
        if expected is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
        if not x_api_key or not hmac.compare_digest(x_api_key.encode(), expected.get_secret_value().encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_key")

    night = local_today(settings.timezone)
    statement = select(Hotel).where(Hotel.is_own_property.is_(True), Hotel.is_active.is_(True)).order_by(Hotel.id)
    if hotel_id is not None:
        statement = statement.where(Hotel.id == hotel_id)
    hotels = list((await session.scalars(statement)).all())
    if hotel_id is not None and not hotels:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "hotel_not_found")

    lists = []
    for hotel in hotels:
        rows = price_feed.to_rows((await session.execute(price_feed.rows_stmt(hotel.id, night))).all())
        lists.append(price_feed.payload(hotel, night, price_feed.items(hotel.id, rows)))
    return {"version": price_feed.VERSION, "night": night.isoformat(), "hotels": lists}
