"""A hotel belongs to the account that added it, and to nobody else.

Every screen and endpoint that reads hotel data filters on
``hotels.owner_user_id``. These pin the two halves of that rule which are
easiest to get wrong later:

* the SQL actually carries the predicate -- a helper that silently returned
  the statement unchanged would leave every listing global while looking
  scoped at the call site;
* an owner-less hotel matches nobody. ``NULL = 1`` is NULL in SQL, not false
  and certainly not true, and a helper written with ``IS NOT DISTINCT FROM``
  or a Python-side ``or`` would quietly make the pre-ownership hotels visible
  to whichever account asked first.

The role checks are deliberately NOT retested here. Role decides what you may
change and ownership decides what you may see; they are separate rules, and
conflating them in a test is how one of them ends up enforcing the other.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from app.db.models import Hotel, PriceSeries
from app.services.ownership import (
    owned_hotel_ids,
    owns,
    scope_by_hotel_id,
    scope_hotels,
)


def _user(user_id: int):
    return SimpleNamespace(id=user_id)


def _where(statement) -> str:
    """The WHERE clause alone, as SQL.

    Asserting against the whole statement would pass on an UNSCOPED
    ``select(Hotel)``, because ``owner_user_id`` is one of the columns it
    selects -- the predicate has to be looked for where it would actually do
    something.
    """
    clause = statement.whereclause
    return "" if clause is None else str(clause)


class TestTheScopedStatementCarriesThePredicate:
    def test_scope_hotels_adds_an_owner_comparison(self):
        assert "hotels.owner_user_id" in _where(scope_hotels(select(Hotel), _user(4)))

    def test_an_unscoped_statement_has_no_predicate_at_all(self):
        """The guard against a scope helper that quietly does nothing."""
        assert _where(select(Hotel)) == ""

    def test_scope_by_hotel_id_adds_a_subquery_rather_than_a_join(self):
        where = _where(
            scope_by_hotel_id(select(PriceSeries), _user(4), PriceSeries.hotel_id)
        )
        assert "IN (SELECT" in " ".join(where.upper().split())
        assert "hotels.owner_user_id" in where

    def test_owned_hotel_ids_selects_ids_only(self):
        """It composes into an IN clause, so it must be a single column."""
        assert [c.name for c in owned_hotel_ids(_user(4)).selected_columns] == ["id"]


class TestAnOwnerlessHotelBelongsToNobody:
    """Hotels added before ownership existed have a NULL owner.

    They are deliberately visible to no account at all -- not to the first one
    that asks, and not to admins.
    """

    def test_owns_is_false_for_a_null_owner(self):
        assert owns(SimpleNamespace(owner_user_id=None), _user(1)) is False

    def test_owns_is_false_for_a_different_account(self):
        assert owns(SimpleNamespace(owner_user_id=2), _user(1)) is False

    def test_owns_is_true_for_the_owner(self):
        assert owns(SimpleNamespace(owner_user_id=1), _user(1)) is True

    def test_owns_is_false_for_a_missing_hotel(self):
        """A 404 path hands in None; it must not raise on the way past."""
        assert owns(None, _user(1)) is False

    def test_the_predicate_is_an_equality_not_a_null_safe_comparison(self):
        """``NULL = 1`` is NULL, which is what keeps unowned rows invisible.

        ``IS NOT DISTINCT FROM`` would treat two NULLs as equal, and a Python
        ``or`` fallback would make the row match everyone. Either would hand
        the pre-ownership hotels to whichever account looked first.
        """
        where = _where(scope_hotels(select(Hotel), _user(4))).upper()
        assert "IS NOT DISTINCT FROM" not in where
        assert "IS NULL" not in where
