"""Resolving a site's room name to a canonical room type.

The hardest correctness problem in this system. Sites rename rooms
("Deluxe Room" becomes "Deluxe Double Room with Balcony"), and no two sources
agree on wording. Get this wrong and a price series silently splits in two, or
worse, two different rooms get merged into one series and every comparison
after that is meaningless.

The resolution order is deliberate:

1. **exact** match on the normalised name (the common path once trained)
2. **fuzzy** match above a high threshold, recorded with its score for review
3. **give up** and record an ``UnmatchedOffer`` for a human to map

Step 3 is the important one. The system NEVER guesses below the threshold: a
missing price for one room is a visible gap that gets fixed, while a wrong
mapping is invisible and corrupts history indefinitely.

Everything here is pure — no database, no I/O — so it is cheap to test against
the real room names collected during the source spike.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

# Above this, accept a fuzzy match automatically (flagged for review).
AUTO_MATCH_THRESHOLD = 85.0
# Between this and AUTO_MATCH_THRESHOLD, record as a suggestion only.
SUGGEST_THRESHOLD = 60.0

# Words that carry no distinguishing information for a room type. "non" and
# "ac" are deliberately absent: "Non AC Deluxe" and "AC Deluxe" are different
# rooms at very different prices.
_STOP_WORDS = frozenset({
    "room", "rooms", "with", "the", "a", "an", "and", "our", "type",
    "category", "accommodation", "stay", "unit", "in", "of", "for", "per",
})

# Expanded before stop-word removal so "dbl" and "double" collapse together.
_ABBREVIATIONS = {
    "dbl": "double", "sgl": "single", "trpl": "triple", "quad": "quadruple",
    "dlx": "deluxe", "std": "standard", "exec": "executive", "sup": "superior",
    "ste": "suite", "apt": "apartment", "bhk": "bedroom",
    "ac": "ac", "a/c": "ac", "aircon": "ac", "airconditioned": "ac",
    "nonac": "non ac", "non-ac": "non ac",
    "w/": "with", "&": "and", "+": "and",
}

_PUNCT_RE = re.compile(r"[^\w\s/&+-]")
_WS_RE = re.compile(r"\s+")


def normalize_room_name(raw: str) -> str:
    """Canonical form used as the alias lookup key.

    Tokens are SORTED, so "Deluxe Double" and "Double Deluxe" resolve to the
    same room. Room names are descriptive rather than grammatical, so word
    order carries no meaning worth preserving.

    Returns an empty string for input that normalises away entirely; callers
    must treat that as unmatchable rather than as a valid key.
    """
    if not raw:
        return ""

    # Fold accents so "Suíte" and "Suite" agree.
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()

    # Expand abbreviations before punctuation is stripped, since some of them
    # contain punctuation ("a/c", "w/").
    for abbr, full in _ABBREVIATIONS.items():
        text = re.sub(rf"(?<![\w]){re.escape(abbr)}(?![\w])", f" {full} ", text)

    text = _PUNCT_RE.sub(" ", text).replace("/", " ").replace("-", " ")
    text = _WS_RE.sub(" ", text).strip()

    tokens = [t for t in text.split() if t and t not in _STOP_WORDS]
    if not tokens:
        return ""
    return " ".join(sorted(tokens))


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    room_type_id: int
    canonical_name: str
    score: float


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Outcome of resolving one raw name.

    ``room_type_id is None`` means unmatched; ``suggestion`` may still hold the
    best near-miss so the dashboard can offer a one-click mapping.
    """

    normalized: str
    room_type_id: int | None
    score: float | None
    is_exact: bool
    suggestion: MatchCandidate | None = None

    @property
    def matched(self) -> bool:
        return self.room_type_id is not None


def score_similarity(a: str, b: str) -> float:
    """Similarity of two already-normalised names, 0-100.

    ``token_set_ratio`` is the right choice here because it ignores extra
    words: "deluxe double balcony" still scores highly against "deluxe double",
    which is exactly the rename case this has to survive.
    """
    if not a or not b:
        return 0.0
    return float(fuzz.token_set_ratio(a, b))


def resolve(
    raw_name: str,
    *,
    aliases: dict[str, int],
    candidates: list[tuple[int, str]],
    auto_threshold: float = AUTO_MATCH_THRESHOLD,
    suggest_threshold: float = SUGGEST_THRESHOLD,
) -> MatchResult:
    """Resolve ``raw_name`` to a room type id.

    Args:
        aliases: ``{normalized_name: room_type_id}`` for this source, including
            every previously confirmed manual mapping.
        candidates: ``(room_type_id, canonical_name)`` for this hotel, used for
            fuzzy matching when no alias exists.

    The caller persists the outcome: a new alias row on a match, or an
    ``UnmatchedOffer`` row when unmatched.
    """
    normalized = normalize_room_name(raw_name)
    if not normalized:
        return MatchResult(normalized="", room_type_id=None, score=None, is_exact=False)

    # 1. Exact alias hit. Includes manual mappings, so a human decision always
    #    wins over anything fuzzy matching might prefer.
    if (room_type_id := aliases.get(normalized)) is not None:
        return MatchResult(
            normalized=normalized, room_type_id=room_type_id, score=100.0, is_exact=True
        )

    # 2. Fuzzy against this hotel's known rooms.
    best: MatchCandidate | None = None
    for room_type_id, canonical in candidates:
        score = score_similarity(normalized, canonical)
        if best is None or score > best.score:
            best = MatchCandidate(room_type_id, canonical, score)

    if best and best.score >= auto_threshold:
        return MatchResult(
            normalized=normalized,
            room_type_id=best.room_type_id,
            score=best.score,
            is_exact=False,
            suggestion=best,
        )

    # 3. Unmatched. Keep the near-miss as a suggestion, but do not act on it.
    suggestion = best if best and best.score >= suggest_threshold else None
    return MatchResult(
        normalized=normalized,
        room_type_id=None,
        score=best.score if best else None,
        is_exact=False,
        suggestion=suggestion,
    )
