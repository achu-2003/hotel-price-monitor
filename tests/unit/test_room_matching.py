"""Tests for room-name resolution.

The behaviour that matters most is the NEGATIVE one: when the system is not
confident, it must refuse to match. A gap is visible and gets fixed; a wrong
mapping is invisible and corrupts a price series forever.
"""
from __future__ import annotations

import pytest

from app.services import room_matching
from app.services.room_matching import (
    AUTO_MATCH_THRESHOLD,
    SUGGEST_THRESHOLD,
    normalize_room_name,
    resolve,
    score_similarity,
)

# A hotel's canonical rooms, as they would come from the database.
CANDIDATES = [
    (1, normalize_room_name("Standard Room")),
    (2, normalize_room_name("Deluxe Room")),
    (3, normalize_room_name("Premium Room")),
    (4, normalize_room_name("Suite")),
]


# ── normalisation ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Deluxe Room", "deluxe"),
        ("  DELUXE   ROOM  ", "deluxe"),
        ("Deluxe-Room", "deluxe"),
        ("Deluxe Room!", "deluxe"),
        ("The Deluxe Room", "deluxe"),
    ],
)
def test_cosmetic_differences_normalise_away(raw, expected):
    assert normalize_room_name(raw) == expected


def test_word_order_does_not_matter():
    """Room names are descriptive, not grammatical."""
    assert normalize_room_name("Deluxe Double") == normalize_room_name("Double Deluxe")


def test_abbreviations_expand():
    assert normalize_room_name("Dlx Dbl Room") == normalize_room_name("Deluxe Double Room")


def test_accents_are_folded():
    assert normalize_room_name("Suite") == normalize_room_name("Suîte")


def test_ac_and_non_ac_stay_distinct():
    """Different rooms at genuinely different prices. Merging them would be bad."""
    assert normalize_room_name("AC Deluxe Room") != normalize_room_name("Non AC Deluxe Room")


def test_empty_input_is_unmatchable_not_a_valid_key():
    assert normalize_room_name("") == ""
    assert normalize_room_name("   ") == ""
    assert normalize_room_name("room") == ""  # nothing but a stop word


# ── renames, and the reason they are NOT auto-resolved ───────────────
def test_a_rename_with_extra_words_goes_to_a_human():
    """"Deluxe Room" -> "Deluxe Double Room with Balcony" is NOT auto-matched.

    This was auto-matched once, and the mechanism that allowed it is the same
    one that merged "Super Deluxe Double Occupancy Room" into "Deluxe Double
    Occupancy Room" on a real property: token_set_ratio scores a superset as a
    perfect 100, so "add a word" and "different room" are indistinguishable.

    Two rooms at different rates sharing one price series is invisible and
    permanent. An unmatched row is visible and costs one click. The queue is
    the cheaper failure, so added words now suppress the automatic match and
    the near-miss is offered as a suggestion instead.
    """
    result = resolve(
        "Deluxe Double Room with Balcony", aliases={}, candidates=CANDIDATES
    )
    assert not result.matched
    assert result.score < AUTO_MATCH_THRESHOLD


def test_a_qualifier_word_never_collapses_two_rooms():
    """The failure this threshold exists to prevent."""
    candidates = [(1, normalize_room_name("Deluxe Double Occupancy Room"))]
    result = resolve(
        "Super Deluxe Double Occupancy Room", aliases={}, candidates=candidates
    )
    assert not result.matched, "Super Deluxe is a different, dearer room"
    # Still surfaced as a suggestion, so mapping it is one click if it IS right.
    assert result.suggestion is not None


def test_once_mapped_a_rename_resolves_forever():
    """The queue is a one-time cost, not a recurring one.

    A human maps the new name once; the alias then hits on the exact path and
    fuzzy matching is never consulted again.
    """
    aliases = {normalize_room_name("Deluxe Double Room with Balcony"): 2}
    result = resolve(
        "Deluxe Double Room with Balcony", aliases=aliases, candidates=CANDIDATES
    )
    assert result.room_type_id == 2
    assert result.is_exact


# ── exact alias path ─────────────────────────────────────────────────
def test_known_alias_matches_exactly():
    aliases = {normalize_room_name("Executive Cottage"): 3}
    result = resolve("Executive Cottage", aliases=aliases, candidates=CANDIDATES)
    assert result.room_type_id == 3
    assert result.is_exact
    assert result.score == 100.0


def test_a_manual_alias_beats_fuzzy_matching():
    """A human decision must always win over the algorithm.

    Here "Deluxe Garden View" was manually mapped to the Suite; fuzzy matching
    would have picked Deluxe. The operator knows something the string does not
    say, so the alias wins.
    """
    aliases = {normalize_room_name("Deluxe Garden View"): 4}
    result = resolve("Deluxe Garden View", aliases=aliases, candidates=CANDIDATES)
    assert result.room_type_id == 4
    assert result.is_exact


# ── refusing to guess ────────────────────────────────────────────────
def test_an_unrelated_name_is_left_unmatched():
    result = resolve("Tent Camping Pitch", aliases={}, candidates=CANDIDATES)
    assert not result.matched
    assert result.room_type_id is None


def test_unmatched_names_still_carry_a_normalised_form():
    """The caller writes this to unmatched_offers for a human to map once."""
    result = resolve("Tree House Villa", aliases={}, candidates=CANDIDATES)
    assert not result.matched
    assert result.normalized == "house tree villa"


def test_a_near_miss_is_offered_as_a_suggestion_but_not_applied():
    """The dashboard can offer one-click mapping without the system guessing."""
    result = resolve(
        "Standard", aliases={}, candidates=CANDIDATES,
        auto_threshold=101.0,  # force the auto-match off
        suggest_threshold=50.0,
    )
    assert not result.matched
    assert result.suggestion is not None
    assert result.suggestion.room_type_id == 1


def test_empty_name_never_matches_anything():
    result = resolve("", aliases={}, candidates=CANDIDATES)
    assert not result.matched
    assert result.normalized == ""


def test_no_candidates_means_no_match():
    result = resolve("Deluxe Room", aliases={}, candidates=[])
    assert not result.matched


# ── similarity scoring ───────────────────────────────────────────────
def test_identical_strings_score_full_marks():
    assert score_similarity("deluxe", "deluxe") == 100.0


def test_extra_words_lower_the_score_below_the_automatic_threshold():
    """The score is min(token_set_ratio, token_sort_ratio).

    token_set_ratio alone returns 100 here, because every token of "deluxe"
    appears in "balcony deluxe double". Taking the minimum makes the extra
    words cost something, which is what keeps two genuinely different rooms
    apart.
    """
    score = score_similarity("balcony deluxe double", "deluxe")
    assert score < AUTO_MATCH_THRESHOLD
    # Still high enough to be offered as a suggestion rather than discarded.
    assert score >= SUGGEST_THRESHOLD - 20


def test_a_qualifier_scores_below_the_threshold():
    assert score_similarity(
        "deluxe double occupancy super", "deluxe double occupancy"
    ) < AUTO_MATCH_THRESHOLD


def test_word_order_alone_still_matches():
    """Room names are descriptive, not grammatical: order carries no meaning."""
    assert score_similarity("deluxe double", "double deluxe") == 100.0


def test_empty_strings_score_zero():
    assert score_similarity("", "deluxe") == 0.0


class TestAFieldLabelMistakenForARoom:
    """The guard that stops a spec-table label becoming a room.

    A room-name selector does not fail by finding nothing. It fails by finding
    the wrong element and returning it with total confidence, and nothing
    downstream can catch that: "Beds:" normalises to "beds", collides with no
    existing room, and is duly created as a room the hotel has just added.

    Ananthyam is the real case -- Booking.com prints each card's bed
    configuration as a definition list, the selector reached into it, and the
    hotel acquired rooms called "Bed:", "Bedroom:" and "Beds:", each with its
    own price series.
    """

    @pytest.mark.parametrize("raw", ["Bed:", "Bedroom:", "Beds:", "Sleeps:", "Room size:"])
    def test_a_trailing_colon_makes_it_a_label(self, raw):
        assert room_matching.looks_like_a_field_label(raw)

    def test_a_colon_inside_the_name_is_left_alone(self):
        """A room described in two halves is still a room. The rule is about a
        name that ENDS in a colon, which is a label with its value missing."""
        assert not room_matching.looks_like_a_field_label("Deluxe Room: Garden View")

    @pytest.mark.parametrize("raw", [
        "Deluxe Room",
        "Cottage - 2 King Bed - Pool view - Sitout",
        "Twin Bed Room",
        "Villa 3 Bedroom with Living Room",
        "Suíte Máster",
    ])
    def test_a_real_room_name_survives(self, raw):
        """Including the ones whose words overlap the labels. "Bed" and
        "Bedroom" are spec labels AND parts of real names, which is exactly
        why the rule reads the punctuation and not the vocabulary."""
        assert not room_matching.looks_like_a_field_label(raw)

    @pytest.mark.parametrize("raw", ["---", "2", "", None, "   "])
    def test_it_does_not_reach_for_the_cases_the_queue_already_owns(self, raw):
        """THE REGRESSION THIS PINS. An earlier draft also rejected any name
        with no letter in it, which reads as an improvement and is not:
        "---" normalises to nothing, so ingest ALREADY queues it as unmatched
        where a person can see it. Rejecting it here dropped it one step
        earlier and one step quieter, and two integration tests said so."""
        assert not room_matching.looks_like_a_field_label(raw)
        if raw:
            assert not normalize_room_name(raw) or raw == "2"
