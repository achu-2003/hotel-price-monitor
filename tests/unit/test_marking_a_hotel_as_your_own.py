"""Correcting "this is my own property" after the hotel was added.

THE GAP THIS CLOSES
===================
``is_own_property`` could be set in exactly one place: the tick on the Add
hotel form. It is the easiest thing on that form to miss -- one checkbox among
the name, the location and the booking URL -- and once missed there was no way
back. The hotel page PRINTED the answer, as a "yours" pill, next to nothing
that could change it.

What being wrong costs is quiet, which is why it went unnoticed. The matrix
highlights your own row and marks it "yours"; that is what makes a
competitor's number readable as above or below yours. A property filed as a
competitor is drawn as one more line among them, and every comparison read off
that screen is made against the wrong baseline — with nothing about the screen
looking broken.

WHY A CHECKBOX ON ITS OWN WOULD HAVE BEEN WORSE THAN NOTHING
============================================================
``HotelUpdate`` did not declare the field, and ORMModel does not forbid extras,
so a PATCH carrying ``is_own_property`` was accepted, ignored, and answered
200. The form would have reported "Saved.", reloaded, and shown the old value
— a control that lies about having worked, which is harder to diagnose than
one that is missing. The schema half is pinned first below for that reason.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.dashboard.routes import templates
from app.db.models import Hotel
from app.schemas.hotels import HotelCreate, HotelUpdate


class TestTheUpdateSchemaCarriesIt:
    """The half that makes the control real rather than decorative."""

    def test_turning_it_on_survives_the_model(self):
        sent = HotelUpdate(is_own_property=True).model_dump(exclude_unset=True)

        assert sent == {"is_own_property": True}

    def test_turning_it_off_survives_too(self):
        """The direction that a form built on FormData alone cannot express --
        an unchecked box sends no key at all. The page collects checkboxes
        explicitly for this reason, and the model has to accept the False it
        sends or a property could be marked yours and never unmarked."""
        sent = HotelUpdate(is_own_property=False).model_dump(exclude_unset=True)

        assert sent == {"is_own_property": False}

    def test_not_mentioning_it_leaves_it_alone(self):
        """The endpoint applies exactly what exclude_unset returns, so a PATCH
        that only renames a hotel must not carry a flag it never mentioned."""
        sent = HotelUpdate(name="Sunrise Resort").model_dump(exclude_unset=True)

        assert "is_own_property" not in sent

    def test_the_column_it_writes_to_exists(self):
        """setattr onto the ORM row is the whole of the endpoint's update, so
        a rename on either side fails silently rather than loudly."""
        assert hasattr(Hotel, "is_own_property")

    def test_create_and_update_agree_on_the_name(self):
        """Two spellings of one flag would let a hotel be added as yours and
        edited as something else."""
        assert "is_own_property" in HotelCreate.model_fields
        assert "is_own_property" in HotelUpdate.model_fields


def render(**overrides) -> str:
    hotel = SimpleNamespace(
        id=1, name="Sunrise Resort", slug="sunrise-resort", location="Yelagiri",
        notes=None, is_active=True, is_own_property=False,
    )
    for key, value in overrides.items():
        setattr(hotel, key, value)

    return templates.get_template("hotel_detail.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/hotels/1")),
        user=SimpleNamespace(username="ops", full_name="Ops"),
        is_admin=True,
        attention={"total": 0},
        hotel=hotel,
        sources=[], rooms=[], runs=[], targets=[], series=[], unmatched=[],
        errors=[], recipients=[], assignments={}, channels=["email"],
        has_source=False,
        alert_defaults=SimpleNamespace(min_delta_abs=50, min_delta_pct=2, confirm_checks=2),
    )


def details_form(page: str) -> str:
    """Just the form that PATCHes the hotel.

    The page carries other forms and the word "own" appears in prose, so a
    search of the whole thing would pass on markup that posts nowhere.
    """
    start = page.index('id="hotel-details"')
    return page[start:page.index("</form>", start)]


class TestTheControlOnTheHotelPage:
    def test_it_is_offered(self):
        assert 'name="is_own_property"' in details_form(render())

    def test_it_sits_in_the_form_that_saves_the_hotel(self):
        """In the panel that already PATCHes /hotels/{id}, so it is saved by
        the Save that is already there rather than by a second control with
        its own failure modes."""
        form = details_form(render())

        assert 'data-endpoint="/api/v1/hotels/1"' in form
        assert 'data-method="PATCH"' in form

    def test_a_competitor_shows_it_unticked(self):
        assert "checked" not in details_form(render(is_own_property=False))

    def test_your_own_property_shows_it_ticked(self):
        """The box has to reflect what is STORED. One that always renders
        empty reads as "not yours" and, saved, turns the flag off on the
        property it was supposed to confirm."""
        assert "checked" in details_form(render(is_own_property=True))

    def test_the_panel_says_the_setting_is_in_there(self):
        """The summary is all that is visible while the panel is closed, and
        somebody looking for this setting is looking at a closed panel."""
        page = render()
        summary = page[page.index('id="hotel-details"'):page.index("</summary>", page.index('id="hotel-details"'))]

        assert "whose it is" in summary

    def test_it_says_what_the_flag_actually_changes(self):
        """"My own property" does not explain itself. The matrix highlight is
        the entire consequence, and an operator deciding whether to tick it
        needs to know that is what they are deciding."""
        form = details_form(render())

        assert "matrix" in form
