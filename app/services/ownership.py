"""Which hotels an account can see.

A hotel belongs to the account that added it (``hotels.owner_user_id``), and
every screen and endpoint that reads hotel data filters on that. Role decides
what you may CHANGE; ownership decides what you may SEE. An admin is not
exempt -- an oversight account that quietly sees everyone's competitor set
would make the scoping decorative.

WHY A SUBQUERY AND NOT A LIST OF IDS
====================================
The obvious implementation is "fetch this user's hotel ids, then filter with
``in_(ids)``". That is one extra round trip per request, it goes stale between
the two queries, and it silently degrades into a multi-thousand-element IN
clause the day someone has that many properties.

:func:`owned_hotel_ids` returns an uncorrelated scalar SELECT instead. It
composes into any statement, the database plans it as a semi-join against
``ix_hotels_owner_user_id``, and there is exactly one query.

TWO WAYS TO APPLY IT
====================
* :func:`scope_hotels` -- when ``Hotel`` is already in the statement (the
  common case; almost every query here joins it for the name).
* :func:`scope_by_hotel_id` -- when it is not, and some other table's
  ``hotel_id`` is the only handle available.

They must not be mixed on one statement: the first adds a predicate on the
joined row, the second adds a semi-join, and applying both just makes the
planner do the same work twice.

NULL OWNERS ARE VISIBLE TO NOBODY
=================================
``owner_user_id`` is nullable for the hotels that predate the column. Both
helpers compare with ``==``, and in SQL ``NULL = anything`` is NULL, not true
-- so an unowned hotel matches no account at all. That is intended: see the
0006 migration for how to adopt one.
"""
from __future__ import annotations

from sqlalchemy import Select, select

from app.db.models import Hotel, User


def owned_hotel_ids(user: User) -> Select:
    """A scalar SELECT of the hotel ids this account owns.

    Composable into ``.where(Something.hotel_id.in_(owned_hotel_ids(user)))``
    without a second round trip.
    """
    return select(Hotel.id).where(Hotel.owner_user_id == user.id)


def scope_hotels(statement: Select, user: User) -> Select:
    """Restrict a statement that already selects from or joins ``Hotel``."""
    return statement.where(Hotel.owner_user_id == user.id)


def scope_by_hotel_id(statement: Select, user: User, column) -> Select:
    """Restrict a statement by some other table's ``hotel_id`` column.

    ``column`` is that column, e.g. ``PriceSeries.hotel_id``. Use this only
    when ``Hotel`` is absent from the statement; when it is present,
    :func:`scope_hotels` is one predicate instead of a semi-join.
    """
    return statement.where(column.in_(owned_hotel_ids(user)))


def owns(hotel: Hotel | None, user: User) -> bool:
    """Whether this account may see this already-loaded hotel."""
    return hotel is not None and hotel.owner_user_id == user.id
