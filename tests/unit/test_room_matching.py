"""Tests for room-name resolution.

The behaviour that matters most is the NEGATIVE one: when the system is not
confident, it must refuse to match. A gap is visible and gets fixed; a wrong
mapping is invisible and corrupts a price series forever.
"""
from __future__ import annotations

import pytest

from app.services.room_matching import (
    AUTO_MATCH_THRESHOLD,
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


# ── the rename case this exists to survive ───────────────────────────
def test_a_site_renaming_a_room_still_resolves():
    """"Deluxe Room" becoming "Deluxe Double Room with Balcony" must not split
    the price history in two."""
    result = resolve(
        "Deluxe Double Room with Balcony", aliases={}, candidates=CANDIDATES
    )
    assert result.matched
    assert result.room_type_id == 2
    assert result.score >= AUTO_MATCH_THRESHOLD


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


def test_extra_descriptive_words_do_not_destroy_the_score():
    assert score_similarity("balcony deluxe double", "deluxe") >= AUTO_MATCH_THRESHOLD


def test_empty_strings_score_zero():
    assert score_similarity("", "deluxe") == 0.0
