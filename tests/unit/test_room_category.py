"""Room names sort into the categories people actually compare across.

Every name in ``TestTheSheet`` is a real one, taken from the room-category
sheet the team kept by hand for the monitored properties. That sheet is the
specification for this classifier, so the cases that it and the classifier
DISAGREE on are written down here too (``TestWhatTheNameCannotSay``) rather
than left as a surprise: they are the price of deriving the category from the
name instead of storing a mapping per hotel.
"""
from __future__ import annotations

import pytest

from app.services.room_category import (
    CLASSIC,
    DELUXE,
    OTHER,
    POOL_SUITE,
    POOL_VIEW,
    SUITE,
    VILLA_2BR,
    VILLA_3BR,
    classify,
    is_category,
    label_for,
)


class TestTheSheet:
    """The real names, filed where the sheet files them."""

    @pytest.mark.parametrize("name", [
        "Classic Room",
        "Classic Room with Balcony",
        "Mountain View Classic Room",
        "Club Room",
        "Club Twin Room",
        "Standard Double Room",
        "Compact Room",
    ])
    def test_the_entry_level_room(self, name):
        assert classify(name) == CLASSIC

    @pytest.mark.parametrize("name", [
        "Deluxe",
        "Deluxe Room",
        "Deluxe Room with Balcony",
        "Deluxe Twin Room",
        "Superior Room",
        "Palace Room",
    ])
    def test_the_tier_above_it(self, name):
        assert classify(name) == DELUXE

    @pytest.mark.parametrize("name", [
        "Grander Suite",
        "Junior Suite",
        "Silver Suite",
        "Duplex Family Suite",
        "Heritage Experience Suite",
        "Family Room",
        "Cottage - King & Sofa Bed Sitout",
        "Villa with Garden View",
        "Penthouse",
    ])
    def test_a_suite_is_anything_sold_as_more_than_a_room(self, name):
        assert classify(name) == SUITE

    @pytest.mark.parametrize("name", [
        "One Bed Room Pool Villa",
        "Two Bed Room Pool Villa",
        "Pool Villa",
        "Swimming Pool Suite",
        "Villa with Private Pool",
        "Plunge Pool Suite",
    ])
    def test_a_pool_that_comes_with_the_unit(self, name):
        assert classify(name) == POOL_SUITE

    @pytest.mark.parametrize("name", [
        "Pool View Room",
        "Pool Facing Deluxe Room",
        "Poolside Room",
    ])
    def test_a_pool_you_can_only_look_at(self, name):
        assert classify(name) == POOL_VIEW

    @pytest.mark.parametrize("name", [
        "2 Bed Room Villa",
        "Two Bedroom Villa",
        "Heritage - 2 Bed Room Suite",
        "2 BHK Apartment",
        "Connecting Room",
    ])
    def test_two_bedrooms(self, name):
        assert classify(name) == VILLA_2BR

    @pytest.mark.parametrize("name", [
        "3 Bed Room Villa",
        "Villa 3 Bed Room with Living Room",
        "Three Bedroom Villa",
        "3 BHK",
    ])
    def test_three_bedrooms(self, name):
        assert classify(name) == VILLA_3BR


class TestWhereTheRulesOverlap:
    """Order decides these, and the order is the point of the module."""

    def test_a_pool_villa_is_a_pool_suite_however_many_bedrooms_it_has(self):
        """The pool is what is being sold, so it outranks the bedroom count.

        Both of these are two-bedroom units. Only one of them is compared
        against the other pool villas.
        """
        assert classify("Two Bed Room Pool Villa") == POOL_SUITE
        assert classify("2 Bed Room Villa") == VILLA_2BR

    def test_a_pool_view_outranks_neither_the_bedrooms_nor_the_unit(self):
        """"Pool View Rooms" is a column of ROOMS.

        A cottage that happens to overlook the pool competes with the other
        cottages -- it is a unit at several times the price, and the view is a
        line in its description rather than what it is. Only when the name has
        nothing bigger to say does the view decide the category.
        """
        assert classify("Cottage - King & Sofa Bed, Pool View Sitout") == SUITE
        assert classify("Cottage - Two Queen Bed, Pool View Sitout") == VILLA_2BR
        assert classify("Cottage - 2 Bed Room, Pool View Sit Out") == VILLA_2BR
        assert classify("Pool Facing Deluxe Room") == POOL_VIEW

    def test_a_suite_wins_over_the_words_that_dress_it_up(self):
        assert classify("Heritage Experience Suite") == SUITE
        assert classify("Grand Deluxe Room") == DELUXE


class TestBedroomsAreNotBeds:
    """The riskiest rule in the file, so it gets its own class.

    A twin room silently filed as a villa is exactly the kind of wrong that
    this system refuses elsewhere: it would be compared against real villas
    and would drag the cheapest-villa figure down by ten thousand rupees with
    nothing on the page suggesting why.
    """

    def test_two_beds_in_one_room_is_not_a_two_bedroom_unit(self):
        assert classify("Deluxe Room with 2 Queen Beds") == DELUXE
        assert classify("Standard Room - 2 Single Beds") == CLASSIC

    def test_the_commonest_room_name_on_the_indian_otas_is_not_a_villa(self):
        """"Double" counts occupants, not bedrooms. Reading it as a count
        would file half the rooms in the system as two-bedroom villas."""
        assert classify("Standard Double Bed Room") == CLASSIC
        assert classify("Deluxe Double Bedroom") == DELUXE

    def test_a_stated_bedroom_counts(self):
        assert classify("Deluxe 2 Bed Room Villa") == VILLA_2BR

    def test_the_largest_count_in_the_name_wins(self):
        """A name can carry two numbers; the unit is as big as its biggest."""
        assert classify("Villa 3 Bed Room with 1 Living Room") == VILLA_3BR


class TestAWholeDwellingCountedInBeds:
    """The exception, and the guard that makes it safe.

    Inside a cottage or a penthouse, "2 king bed" is how a booking engine
    writes a two-bedroom unit; inside a room it is how it writes a twin. So
    beds are counted ONLY where the name also says the thing being sold is a
    whole dwelling, which is what keeps ``TestBedroomsAreNotBeds`` above true.

    Every name here is real, from MGM Whispering Winds, and the team's sheet
    files all three under 2 Bed Room Villa. They used to land in Suite --
    beside that property's own one-bedroom cottage, at half the price.
    """

    @pytest.mark.parametrize("name", [
        "Cottage - 2 Queen Bed - Pool view - Sitout",
        "Cottage - 2 King Bed - Pool view - Sitout",
        "Cottage - Living Room & 2 King Bed - Pool View Sit Out",
    ])
    def test_a_cottage_that_states_its_size_in_beds(self, name):
        assert classify(name) == VILLA_2BR

    def test_the_largest_count_wins_and_the_counts_are_not_added(self):
        """"Penthouse - 2 King and 1 Single Bed" is the two-bedroom unit on
        the sheet, not a three-bedroom one. The single bed is the child's bed
        in the second room."""
        assert classify("Penthouse - 2 King and 1 Single Bed") == VILLA_2BR

    def test_a_size_carries_the_count_when_the_word_bed_arrives_later(self):
        """THE BUG THIS PREVENTS: requiring the numeral to sit next to the
        word "bed" reads that penthouse as a ONE-bed unit, because the only
        numeral touching the word "bed" in it is the 1."""
        assert classify("Penthouse - 2 King and 1 Single Bed") != SUITE

    def test_a_unit_with_no_number_claims_nothing(self):
        assert classify("Cottage - King and Sofa Bed - Sitout") == SUITE
        assert classify("Private Cottage - King Bed - Pool view") == SUITE

    def test_a_room_is_never_promoted_by_the_beds_inside_it(self):
        """The guard, stated as its own case. Without the whole-dwelling
        requirement every one of these becomes a two-bedroom villa."""
        assert classify("Deluxe Room - 2 King Bed") == DELUXE
        assert classify("Club Room - 2 Queen Beds") == CLASSIC
        assert classify("Premium Suite, 2 Double Beds") == SUITE
        assert classify("Traditional Duplex, 2 Double Beds") == SUITE

    def test_a_stated_bedroom_is_not_overruled_by_a_bed(self):
        """A stated bedroom count is the better evidence, so it wins. A
        one-bedroom villa with two beds in it is a one-bedroom villa."""
        assert classify("Villa 1 Bedroom, 2 Double Beds") == SUITE
        assert classify("Villa 3 Bedroom with 2 King Bed") == VILLA_3BR

    def test_a_pool_villa_is_still_a_pool_suite(self):
        """The order is unchanged: the pool attached to the unit outranks
        however the unit states its size."""
        assert classify("Pool Villa - 2 King Bed") == POOL_SUITE


class TestWhatTheNameCannotSay:
    """Known, deliberate disagreements with the hand-kept sheet.

    Each of these is a judgement about ONE property that the words do not
    carry. They are recorded as tests so that a later change to the rules has
    to state whether it meant to move them, rather than moving them quietly.
    """

    def test_an_entry_level_room_named_deluxe_is_read_as_deluxe(self):
        """One property's cheapest room is called "Deluxe" and its next tier
        up is called "Superior". By name alone that is a deluxe room, and only
        that property's rate card says otherwise.
        """
        assert classify("Deluxe") == DELUXE

    def test_a_pool_view_room_that_does_not_say_so_is_not_one(self):
        """"Compact Room" is the pool-view room at one property. Nothing in
        those two words says pool."""
        assert classify("Compact Room") == CLASSIC

    def test_a_one_bedroom_villa_is_read_as_a_suite(self):
        """The sheet files "Villa 1 Bed Room" as that property's entry-level
        room. It is still a villa, and at every other property a villa is not
        the cheapest thing on sale."""
        assert classify("Villa 1 Bed Room") == SUITE


class TestTheUnrecognisable:
    def test_a_name_that_says_nothing_is_other_not_a_guess(self):
        """``other`` is a visible gap. A room quietly filed as "Classic" would
        be compared against real classic rooms and nobody would know."""
        assert classify("Sunrise") == OTHER
        assert classify("") == OTHER
        assert classify("   ") == OTHER

    def test_punctuation_and_accents_do_not_change_the_answer(self):
        assert classify("Suíte Máster") == SUITE
        assert classify("DELUXE ROOM (NON-AC)") == DELUXE
        assert classify("3-Bed-Room Villa") == VILLA_3BR


class TestTheSlugsThePageUses:
    def test_a_slug_off_a_bookmark_is_recognised(self):
        assert is_category(VILLA_3BR)

    def test_anything_else_is_not(self):
        assert not is_category("")
        assert not is_category(None)
        assert not is_category("villa-9br")

    def test_every_category_has_a_label(self):
        for slug in (CLASSIC, DELUXE, SUITE, POOL_SUITE, POOL_VIEW,
                     VILLA_2BR, VILLA_3BR, OTHER):
            assert label_for(slug) != slug
