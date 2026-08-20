"""Repairing a source's selectors after the site underneath them changed.

WHY THIS EXISTS
===============
Discovery derives ``adapter_config`` from a live page, and until now it ran
exactly once: when a source was first attached. Every later redesign was a
manual repair, which meant a hotel stayed wrong for as long as it took someone
to notice — and the whole problem with this failure is that it does not look
like a failure. A collapsed room list still fetches, still succeeds, still
shows a price. It is simply the wrong price for a fraction of the rooms.

The machinery to fix it already existed and was only being denied a second
chance. This module is that second chance, with the guard rails that make
re-running it on live configuration safe.

WHAT MAKES THIS SAFE
====================
Rewriting the configuration a monitor runs on is not a small thing to automate,
so four rules bound it:

1. **Nothing is written unless discovery verifies it.** ``Candidate.is_verified``
   already refuses a candidate whose prices are not on the page, or whose rooms
   all share one name from an untrusted element. A repair that cannot clear
   that bar is not applied — the source keeps the config it had and the human
   alert stands.

2. **A repair must actually differ.** Re-deriving the identical config is not a
   fix; writing it would clear the alert and change nothing, which is worse
   than leaving both alone.

3. **Only discovery's own keys are touched.** Anything a person set by hand —
   ``standing_rate``, meal-plan filters, timeout overrides — survives, because
   an automatic repair that silently discards a human decision would be a
   second bug wearing the first one's clothes.

4. **Attempts are claimed before the work, not after.** The browser run is slow
   and can crash; recording the attempt first means a cooldown still holds and
   a crash loop cannot form.

The decisions here are pure so they can be tested without a browser or a
network. The task that drives them lives in ``app/workers/tasks_repair.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

#: Keys in ``adapter_config`` that discovery owns and may overwrite. Everything
#: else on the row was put there by a person or by the engine profile and is
#: carried through a repair untouched.
DISCOVERY_OWNED_KEYS = frozenset({
    "room_card",
    "wait_for",
    "selectors",
    "rooms_path",
    "fields",
    "json_url_contains",
    "wait_timeout_ms",
    "discovery_note",
})

#: Where the repair history is kept. Inside ``adapter_config`` rather than in
#: new columns because ``discovery_note`` already establishes that this column
#: carries the provenance of the configuration beside the configuration.
STATE_KEY = "auto_repair"


@dataclass(frozen=True, slots=True)
class RepairState:
    """What has already been tried for one source."""

    attempts: int = 0
    last_attempt_at: datetime | None = None
    last_outcome: str | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> RepairState:
        raw = (config or {}).get(STATE_KEY) or {}
        stamp = raw.get("last_attempt_at")
        parsed: datetime | None = None
        if isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                parsed = None
            else:
                # A naive timestamp from an older row would blow up the
                # cooldown comparison. Treat it as UTC, which is what it was.
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
        return cls(
            attempts=int(raw.get("attempts") or 0),
            last_attempt_at=parsed,
            last_outcome=raw.get("last_outcome"),
        )

    def claim(self, now: datetime) -> dict[str, Any]:
        """Spend one attempt, before the work that might not come back.

        This is the ONLY place the counter goes up. An attempt is spent when it
        starts, not when it finishes: a browser run that is killed halfway has
        still cost the site a page load, and a counter that only advanced on
        clean completion would let a crashing repair retry forever.
        """
        return self._fragment(now, outcome="started", attempts=self.attempts + 1)

    def settle(
        self, now: datetime, *, outcome: str, reset: bool = False
    ) -> dict[str, Any]:
        """Record how the claimed attempt ended, without spending another.

        ``reset`` on a successful repair, so a source that breaks again years
        later gets a fresh budget rather than inheriting an exhausted one.
        """
        return self._fragment(now, outcome=outcome, attempts=0 if reset else self.attempts)

    def _fragment(self, now: datetime, *, outcome: str, attempts: int) -> dict[str, Any]:
        return {
            STATE_KEY: {
                "attempts": attempts,
                "last_attempt_at": now.isoformat(),
                "last_outcome": outcome,
            }
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether to attempt a repair, and why not when the answer is no."""

    allowed: bool
    reason: str


def may_attempt(
    state: RepairState,
    *,
    now: datetime,
    enabled: bool,
    cooldown_minutes: int,
    max_attempts: int,
) -> Verdict:
    """Decide whether this source may be re-discovered right now.

    The attempt budget is deliberately small. Discovery drives a real browser
    against someone else's site, and a source that has defeated it three times
    is not going to yield on the fourth — it needs a person. Burning the budget
    is therefore a signal, not a failure: it is what stops an automatic repair
    from turning into an automatic retry loop.
    """
    if not enabled:
        return Verdict(False, "auto-rediscovery is disabled by configuration")

    if max_attempts >= 0 and state.attempts >= max_attempts:
        return Verdict(
            False,
            f"already attempted {state.attempts} time(s) without a verified "
            f"result; this one needs a person",
        )

    if state.last_attempt_at is not None and cooldown_minutes > 0:
        earliest = state.last_attempt_at + timedelta(minutes=cooldown_minutes)
        if now < earliest:
            return Verdict(
                False,
                f"last attempt was at {state.last_attempt_at.isoformat()}; "
                f"cooling off until {earliest.isoformat()}",
            )

    return Verdict(True, "eligible")


def merge_config(
    current: dict[str, Any] | None, discovered: dict[str, Any]
) -> dict[str, Any]:
    """Lay a freshly discovered config over the stored one.

    Discovery's keys are REPLACED rather than merged key-by-key, and the ones
    it did not produce are dropped. A site that moved from DOM scraping to a
    JSON endpoint would otherwise keep its stale ``selectors`` alongside the new
    ``fields``, and the adapter, finding both, would go on trying the dead one
    first.

    Everything outside :data:`DISCOVERY_OWNED_KEYS` is carried through: those
    are the settings a person chose, and no automatic repair has any business
    reconsidering them.
    """
    kept = {
        key: value
        for key, value in (current or {}).items()
        if key not in DISCOVERY_OWNED_KEYS
    }
    return {**kept, **discovered}


def names_to_retire(
    collapsed_names: list[str] | None, discovered_names: list[str] | None
) -> set[str]:
    """Which room types the broken config invented, and the repair must remove.

    A collapsed fetch does not only produce wrong prices — it produces wrong
    ROOMS. ``_seed_room_types`` creates a hotel's room types from the first
    fetch that succeeds, so a hotel onboarded through a broken selector ends up
    with a room genuinely called "King Size Bed" holding a price series built
    from a tax line. Repairing the selector does not touch any of that, and the
    correctly-named offers that arrive afterwards match nothing and go to the
    unmatched queue, waiting for a person. The repair would be automatic right
    up to the point where it stopped being useful.

    Worse, leaving the invented room in place makes the next fetch report it as
    having disappeared, which the pipeline correctly reads as "sold out" and
    correctly sends to whoever is on the recipient list. A silent fault would
    have become a false alarm.

    So the invented rooms are retired — but only those provably invented:

    * the name must be one the ingest layer actually saw collapse, and
    * it must NOT appear among the names the repaired config now reads.

    The second condition is what makes this safe. If "King Size Bed" turns out
    to be a real room on the repaired page, it is kept, and a hotel's genuine
    rooms can never be retired by this no matter what the broken config did.

    Matching is on the raw strings as the ingest layer reported them; the caller
    normalises, because normalisation lives in ``room_matching`` and this module
    stays free of that dependency.
    """
    collapsed = {n.strip() for n in (collapsed_names or []) if n and n.strip()}
    if not collapsed:
        return set()
    kept = {n.strip() for n in (discovered_names or []) if n and n.strip()}
    return collapsed - kept


def is_a_real_change(
    current: dict[str, Any] | None, discovered: dict[str, Any]
) -> bool:
    """Did discovery actually come back with something different?

    Re-deriving the config that is already stored means the page still matches
    it and the fault lies elsewhere. Writing it anyway would look like a repair,
    resolve the alert, and fix nothing — the worst of the three outcomes,
    because it also destroys the evidence that anything was wrong.

    ``discovery_note`` is ignored in the comparison: it carries a room count and
    a corroboration tally that move between runs without the selectors changing
    at all.
    """
    def comparable(config: dict[str, Any] | None) -> dict[str, Any]:
        return {
            key: value
            for key, value in (config or {}).items()
            if key in DISCOVERY_OWNED_KEYS and key != "discovery_note"
        }

    return comparable(current) != comparable(discovered)
