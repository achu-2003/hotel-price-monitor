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
    "discovery_version",
})

#: The parts of a config that go into the OFFER KEY rather than merely into how
#: a page is read. ``room_name`` is deliberately absent: a renamed room still
#: resolves to the same ``room_type`` through the matcher and its aliases, so
#: the key survives. These two do not go through anything -- their text is
#: hashed into the key verbatim.
IDENTITY_SELECTORS = ("meal_plan", "refundable")


#: Where the repair history is kept. Inside ``adapter_config`` rather than in
#: new columns because ``discovery_note`` already establishes that this column
#: carries the provenance of the configuration beside the configuration.
STATE_KEY = "auto_repair"

#: The scanner's generation. BUMP THIS whenever discovery or the DOM scan is
#: changed in a way that could produce a different config for the same page --
#: a new heuristic, a widened ancestor walk, a changed ranking, a fixed guard.
#:
#: WHY A VERSION EXISTS AT ALL
#: ===========================
#: The attempt budget rests on one premise: "a source that has defeated
#: discovery three times will not yield on the fourth, so stop and fetch a
#: person". That premise holds only while the scanner is the same code. The
#: moment it is improved, every exhausted source is holding a verdict reached
#: by a scanner that no longer exists -- and the budget, which exists to
#: prevent a pointless retry loop, instead locks those sources out of the very
#: fix that was written for them.
#:
#: That is not hypothetical. Two hotels sat on Attention repeating the same
#: alert every thirty minutes; the scanner fault behind it was found and fixed,
#: and the fix could not reach either of them, because both had spent their
#: attempts proving the OLD scanner could not do it. The only way through was a
#: person clicking Resolve on each affected source, one at a time, having first
#: worked out that this was what the button was for.
#:
#: So a config records the generation that produced it, and a source whose
#: stamp is behind gets a fresh, unbudgeted attempt. Not an extra attempt --
#: the counter is not a resource being topped up. The refusal simply no longer
#: applies, because the question "can discovery read this page?" has not
#: actually been asked of the scanner now doing the asking.
#:
#: Generation 3 covers three scanner changes that all bear on the same alert:
#: a group is asked for a node that yields both a name and a price rather than
#: told which one speaks for it; a candidate whose cards link away to other
#: properties sorts last; and the scan now derives a ``meal_plan`` selector for
#: a page whose cards collide. Every source stamped 2 was read by a scanner
#: that could do none of that, so its stored verdict says nothing about this
#: one -- which is the entire reason this constant exists.
#:
#: Generation 4 is one change with a wide reach: a name candidate is no
#: longer discarded for containing the words "guests" or "adults". Occupancy
#: is part of what a room is CALLED, and the blanket match was deleting
#: Booking.com's "Deluxe Double Room (2 Adults + 1 Child)" from
#: consideration -- leaving the amenity badges beside it, so a property was
#: monitored as rooms named "Room", "Room" and "Private suite", three of
#: them collapsing onto one identity at three different prices. Alongside
#: it: a label ending in a colon can no longer be a room name (the same
#: property reported "Bed:", "Bedroom:" and "Beds:" for weeks), and a class
#: that says both "roomtype" and "icon" is read as a name rather than as
#: chrome. Every source stamped 3 was read by a scanner with all three
#: faults, so its stored verdict says nothing about this one.
DISCOVERY_VERSION = 4

#: Where that stamp lives. Discovery-owned, so a repair overwrites it with the
#: current generation rather than carrying an old one forward.
VERSION_KEY = "discovery_version"

#: Error classes worth re-deriving a config for. Both mean "the page no longer
#: matches what we stored". A timeout or a block means the opposite -- the page
#: was never read -- and re-running discovery against it would only add load to
#: a site that is already refusing us.
#:
#: Here rather than in the task, because the task and the operator-facing
#: resolve endpoint have to agree on what "this is a selector problem" means.
REPAIRABLE = frozenset({"parse_schema_drift", "adapter_config"})


@dataclass(frozen=True, slots=True)
class RepairState:
    """What has already been tried for one source."""

    attempts: int = 0
    last_attempt_at: datetime | None = None
    last_outcome: str | None = None
    #: The scanner generation that produced the config this state belongs to.
    #: Absent on every row written before versioning existed, which is exactly
    #: the population that predates the fixes -- so a missing stamp reads as
    #: "older than the current scanner", not as "current".
    discovery_version: int = 0

    @property
    def scanner_moved_on(self) -> bool:
        """Has discovery changed since this config was derived?

        When it has, the stored verdict was reached by code that no longer
        exists and says nothing about what the scanner would do now.
        """
        return self.discovery_version < DISCOVERY_VERSION

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> RepairState:
        config = config or {}
        raw = config.get(STATE_KEY) or {}
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
        # Read from the config, not from the repair state: a config written by
        # first-attach discovery has never had a repair, so it carries no
        # STATE_KEY at all and would otherwise always look out of date.
        try:
            stamped = int(config.get(VERSION_KEY) or 0)
        except (TypeError, ValueError):
            stamped = 0
        return cls(
            attempts=int(raw.get("attempts") or 0),
            last_attempt_at=parsed,
            last_outcome=raw.get("last_outcome"),
            discovery_version=stamped,
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
        self,
        now: datetime,
        *,
        outcome: str,
        reset: bool = False,
        refund: bool = False,
    ) -> dict[str, Any]:
        """Record how the claimed attempt ended, without spending another.

        ``reset`` on a successful repair, so a source that breaks again years
        later gets a fresh budget rather than inheriting an exhausted one.

        ``refund`` gives the attempt back. The budget is small because it is
        rationing something specific -- a browser driven against someone
        else's site, in the belief that the site can be read and we are
        failing to read it. An attempt that ended because the PAGE had nothing
        on it to read spent none of that: no selector was tried and found
        wanting, and the same page tomorrow, with the hotel's rooms back on
        sale, is a different page.

        Charging those attempts is how a hotel that sells out for three nights
        running exhausts a budget meant for three failed repairs, and arrives
        at "this one needs a person" having never once been read. The cooldown
        still applies, so a refund cannot become a retry loop.
        """
        if refund:
            attempts = max(0, self.attempts - 1)
        elif reset:
            attempts = 0
        else:
            attempts = self.attempts
        return self._fragment(now, outcome=outcome, attempts=attempts)

    def release(self) -> dict[str, Any]:
        """Hand the budget back because a PERSON has looked at this.

        The attempt budget rations a browser driven against someone else's
        site, and running out is a deliberate signal: "this one needs a
        person". What was missing was the other half of that sentence -- a way
        for the person, having arrived, to say they are done and it may try
        again.

        Without it the only human action on the Health tab, Resolve, closed the
        alert row and left the source locked out of repair for good. The next
        fetch collapsed the same offers, raised the same alert, and refused the
        same repair, forever. A fault fixed in the scanner itself could not
        reach the hotels that needed it.

        ``last_attempt_at`` is cleared as well as the counter. Keeping it would
        start a fresh six-hour cooldown on top of the reset, which reads as
        "try again later" when what was asked for was "try again".
        """
        return {
            STATE_KEY: {
                "attempts": 0,
                "last_attempt_at": None,
                "last_outcome": "budget_restored",
            }
        }

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

    # Checked BEFORE the budget and the cooldown, because it is the reason both
    # of them stop meaning anything. Neither is a punishment; both encode "we
    # already know the answer". A scanner change is precisely the event that
    # makes that false, and the stored refusal is then an answer to a question
    # nobody is asking any more.
    #
    # This is what stops a fixed scanner from being unable to reach the hotels
    # it was written for. Without it the only route was a person clicking
    # Resolve on every affected source, which does not scale past a handful and
    # depends on somebody knowing that is what the button does.
    if state.scanner_moved_on:
        return Verdict(
            True,
            f"config was derived by scanner generation {state.discovery_version}; "
            f"generation {DISCOVERY_VERSION} has not tried this page yet",
        )

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

    The result is stamped with the scanner generation that produced it. Stamped
    HERE, in the one place every written config passes through, rather than at
    each call site -- a repair that forgot to stamp would be re-attempted on
    every fetch forever, which is the failure this whole mechanism exists to
    prevent, arriving from the other direction.
    """
    kept = {
        key: value
        for key, value in (current or {}).items()
        if key not in DISCOVERY_OWNED_KEYS
    }
    merged = {**kept, **discovered, VERSION_KEY: DISCOVERY_VERSION}

    # ── the two selectors wholesale replacement must not silently drop ──
    #
    # ``meal_plan`` and ``refundable`` are not instructions for reading a page.
    # They are hashed into the offer key, so removing one re-keys every offer
    # on the page: the stored series become unreachable, the fetch writes new
    # ones beside them, and the old rows are reported sold out a check later.
    #
    # Discovery derives a ``meal_plan`` only where the cards are colliding, so
    # on a page that has stopped colliding -- because a person put the selector
    # there and it works -- it produces none, and the wholesale replacement
    # would take the working one with it. The repair would then re-break the
    # hotel it was called out to fix, and charge a history restart for it.
    #
    # Carried only between configs of the SAME route. A source moving from CSS
    # selectors to a JSON contract must not take a CSS selector along in its
    # ``fields``, where it would match nothing and mean nothing.
    for container in ("selectors", "fields"):
        inherited = (current or {}).get(container) or {}
        derived = discovered.get(container)
        if not isinstance(derived, dict) or not isinstance(inherited, dict):
            continue
        carried = {
            key: inherited[key]
            for key in IDENTITY_SELECTORS
            if inherited.get(key) and not derived.get(key)
        }
        if carried:
            merged[container] = {**derived, **carried}

    return merged


def names_to_retire(
    collapsed_names: list[str] | None,
    discovered_names: list[str] | None,
    cross_sold_names: list[str] | None = None,
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

    TWO WAYS A ROOM GETS INVENTED, AND THE SAME PROOF FOR BOTH
    ==========================================================
    ``collapsed_names`` is the first: the ingest layer watched these labels
    share one identity, so a selector was reading something several cards
    shared.

    ``cross_sold_names`` is the second, and it never collapses anything. A
    hotel page carries a "similar properties" carousel, and to every measure
    the ranking has it is the better room list -- repeated cards, one price
    each, four distinct names against the room's one. A config built from it
    monitors four COMPETITORS under this hotel's name and looks perfect from
    everywhere downstream: those prices really are on the page, corroboration
    passes, and no alert is ever raised. Nothing in a fetch can notice it,
    which is exactly why the evidence has to come from the scan -- the names
    belong to a candidate demoted because each of its cards leads to a
    different property page.

    Both are only ever a nomination. The second condition is what makes this
    safe, and it is unchanged: a name the repaired config READS BACK is a real
    room and is kept, whatever the broken config did with it.

    Matching is on the raw strings as the ingest layer reported them; the caller
    normalises, because normalisation lives in ``room_matching`` and this module
    stays free of that dependency.
    """
    suspect = {n.strip() for n in (collapsed_names or []) if n and n.strip()}
    suspect |= {n.strip() for n in (cross_sold_names or []) if n and n.strip()}
    if not suspect:
        return set()
    kept = {n.strip() for n in (discovered_names or []) if n and n.strip()}
    return suspect - kept


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


def identity_selectors_changed(
    current: dict[str, Any] | None, discovered: dict[str, Any]
) -> bool:
    """Will the repaired config file the same offers under different keys?

    WHY THIS HAS TO BE ASKED
    ========================
    An offer key is a hash of the booking conditions, and the meal plan is one
    of them. So the moment a repair adds a ``meal_plan`` selector — which is
    exactly what a collapsing page needs — every offer on that page starts
    hashing to a key that has never been seen before.

    Nothing downstream reads that as "the config changed". It reads as every
    room on the page vanishing at once: the new keys are written as new series,
    and the old ones, still marked available and still selected by the
    disappearance sweep, are absent from the fetch. One check later each is
    confirmed gone, recorded as BECAME_UNAVAILABLE and sent to whoever is on
    the recipient list. The repair that fixed a silent wrong price would have
    announced a sell-out of the entire hotel.

    Removing a selector does the same in reverse, so the test is inequality
    rather than "was one added".
    """
    def selectors(config: dict[str, Any] | None) -> dict[str, Any]:
        config = config or {}
        # A DOM config carries them under "selectors" and a JSON one under
        # "fields". Both are read, because a repair is allowed to move a
        # source from one route to the other.
        merged = {**(config.get("fields") or {}), **(config.get("selectors") or {})}
        return {key: merged.get(key) for key in IDENTITY_SELECTORS}

    return selectors(current) != selectors(discovered)


def needs_rescan(
    config: dict[str, Any] | None,
    *,
    now: datetime,
    cooldown_minutes: int,
) -> bool:
    """Is this config worth offering to the current scanner, unprompted?

    THE QUESTION A FETCH NEVER ASKS
    ===============================
    Every other repair in this system is triggered by a fetch that noticed
    something. That covers the faults which announce themselves and misses the
    ones that do not — a config reading a "similar properties" carousel
    monitors the hotels next door at prices that are genuinely on the page, so
    corroboration passes, no offers collapse, and every check reports success.
    Nothing will ever ask. :func:`may_attempt` would allow the repair; it is
    simply never consulted.

    So a sweep asks on its own, and this is what it asks. Two conditions:

    * the config was written by DISCOVERY. ``discovery_note`` is the marker.
      An engine profile — Agoda's JSON paths, aiosell's field map — was written
      by a person against a documented payload, and running a DOM scan over one
      would replace knowledge with a guess.

    * a generation older than the scanner now running wrote it. That is the
      whole claim being made: not "this is broken", but "the code that decided
      this no longer exists, and the current code has never been asked".

    WHY THE COOLDOWN IS APPLIED HERE AND NOT IN ``may_attempt``
    ==========================================================
    ``may_attempt`` waives both the budget and the cooldown for a
    behind-generation config, on purpose: a fetch asking for a repair is
    holding a live alert, and making it wait six hours for a scanner that has
    already been fixed is exactly the lockout the version stamp exists to end.

    A sweep is holding nothing. It found a config that MIGHT be improvable and
    has all day to find out — so it waits, and that wait is what stops a hotel
    with nothing on its page from being re-read every hour forever. Those
    attempts are refunded and the generation stamp withheld, correctly, because
    a page with no rooms on it never asked this generation anything; without a
    cooldown of its own the sweep would offer the same empty page back
    endlessly.
    """
    config = config or {}
    if not config.get("discovery_note"):
        return False

    state = RepairState.from_config(config)
    if not state.scanner_moved_on:
        return False

    if state.last_attempt_at is not None and cooldown_minutes > 0:
        if now < state.last_attempt_at + timedelta(minutes=cooldown_minutes):
            return False

    return True
