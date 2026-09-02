"""The rules that bound automatic self-repair.

Rewriting the configuration a monitor runs on is the most consequential thing
this system does without asking, so the interesting tests here are the ones
about REFUSING. A repair that fires when it should not is worse than the fault
it was trying to fix: it burns someone else's site, and — if it writes anything
— it resolves the alert that was the only evidence of a problem.

No browser and no database: these are the decisions, not the machinery.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.rediscovery import (
    DISCOVERY_VERSION,
    STATE_KEY,
    VERSION_KEY,
    RepairState,
    identity_selectors_changed,
    is_a_real_change,
    may_attempt,
    merge_config,
    names_to_retire,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

DOM_CONFIG = {
    "room_card": "div.hotel-room",
    "wait_for": "div.hotel-room",
    "wait_timeout_ms": 45000,
    "selectors": {"room_name": "div.mr-3.font-light", "price": "div.text-xs"},
    "standing_rate": True,
    "discovery_note": "Auto-discovered: 6 rooms, 6/6 prices confirmed.",
}

REPAIRED = {
    "room_card": "div.hotel-room",
    "wait_for": "div.hotel-room",
    "wait_timeout_ms": 45000,
    "selectors": {"room_name": "h5.text-lg.font-semibold", "price": "span.total-price"},
    "discovery_note": "Auto-repaired 2026-08-20: 6 rooms, 6/6 prices confirmed.",
}


class TestWhenARepairMayRun:
    def test_a_fresh_source_is_eligible(self):
        verdict = may_attempt(
            RepairState(), now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3
        )
        assert verdict.allowed

    def test_the_feature_switch_is_honoured(self):
        verdict = may_attempt(
            RepairState(), now=NOW, enabled=False, cooldown_minutes=360, max_attempts=3
        )
        assert not verdict.allowed
        assert "disabled" in verdict.reason

    def test_a_source_that_has_defeated_discovery_is_left_to_a_person(self):
        """Three failed attempts is not bad luck. Continuing would be an
        automatic retry loop against somebody else's website."""
        verdict = may_attempt(
            RepairState(attempts=3, discovery_version=DISCOVERY_VERSION), now=NOW,
            enabled=True, cooldown_minutes=360, max_attempts=3,
        )
        assert not verdict.allowed
        assert "needs a person" in verdict.reason

    def test_the_cooldown_holds(self):
        state = RepairState(attempts=1, last_attempt_at=NOW - timedelta(minutes=30),
                            discovery_version=DISCOVERY_VERSION)
        verdict = may_attempt(
            state, now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3
        )
        assert not verdict.allowed
        assert "cooling off" in verdict.reason

    def test_the_cooldown_expires(self):
        state = RepairState(attempts=1, last_attempt_at=NOW - timedelta(hours=7))
        verdict = may_attempt(
            state, now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3
        )
        assert verdict.allowed

    def test_a_naive_timestamp_does_not_crash_the_comparison(self):
        """An older row could hold a timestamp without a timezone. Comparing it
        to an aware `now` raises TypeError, which inside a worker would look
        like the repair machinery itself being broken."""
        state = RepairState.from_config(
            {STATE_KEY: {"attempts": 1, "last_attempt_at": "2026-08-20T11:30:00"},
             VERSION_KEY: DISCOVERY_VERSION}
        )
        verdict = may_attempt(
            state, now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3
        )
        assert not verdict.allowed

    def test_an_unparseable_timestamp_does_not_block_forever(self):
        state = RepairState.from_config(
            {STATE_KEY: {"attempts": 1, "last_attempt_at": "not a date"}}
        )
        assert may_attempt(
            state, now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3
        ).allowed


class TestTheAttemptCounter:
    def test_claiming_spends_exactly_one_attempt(self):
        state = RepairState(attempts=1)
        assert state.claim(NOW)[STATE_KEY]["attempts"] == 2

    def test_settling_spends_none(self):
        """The attempt was already spent when it was claimed. An outcome that
        also incremented would halve the real budget -- three configured
        attempts would allow one and a half."""
        claimed = RepairState.from_config({STATE_KEY: RepairState(attempts=1).claim(NOW)[STATE_KEY]})
        assert claimed.attempts == 2
        assert claimed.settle(NOW, outcome="unverified")[STATE_KEY]["attempts"] == 2

    def test_a_successful_repair_restores_the_budget(self):
        """This source is fixed. When it breaks again in a year it should get a
        full set of attempts, not inherit today's exhausted one."""
        state = RepairState(attempts=2)
        assert state.settle(NOW, outcome="repaired", reset=True)[STATE_KEY]["attempts"] == 0

    def test_a_claim_survives_the_round_trip_through_config(self):
        config = {**DOM_CONFIG, **RepairState().claim(NOW)}
        assert RepairState.from_config(config).attempts == 1
        assert RepairState.from_config(config).last_outcome == "started"


class TestWhatARepairIsAllowedToOverwrite:
    def test_hand_set_configuration_survives(self):
        """`standing_rate` is a human judgement about what the URL can express.
        An automatic repair that dropped it would silently start filing today's
        rate under a future date."""
        merged = merge_config(DOM_CONFIG, REPAIRED)
        assert merged["standing_rate"] is True

    def test_the_selectors_are_replaced(self):
        merged = merge_config(DOM_CONFIG, REPAIRED)
        assert merged["selectors"]["room_name"] == "h5.text-lg.font-semibold"
        assert merged["selectors"]["price"] == "span.total-price"

    def test_selectors_are_replaced_wholesale_not_key_by_key(self):
        """A site that moves from scraping to a JSON endpoint must not keep its
        dead `selectors` alongside the new `fields` -- the adapter tries the DOM
        path first and would go on using the stale one."""
        json_config = {
            "json_url_contains": ["/api/availability"],
            "rooms_path": "data.rooms",
            "fields": {"room_name": "name", "price_inclusive": "rate.total"},
            "wait_timeout_ms": 45000,
        }
        merged = merge_config(DOM_CONFIG, json_config)
        assert "selectors" not in merged
        assert "room_card" not in merged
        assert merged["fields"]["room_name"] == "name"
        # ...and the human setting still survives the switch.
        assert merged["standing_rate"] is True


class TestRefusingToRepairWhatIsNotBroken:
    def test_an_identical_config_is_not_a_change(self):
        """Discovery re-deriving what is already stored means the page still
        matches it, so the fault is elsewhere. Writing it anyway would resolve
        the alert and fix nothing -- destroying the only evidence."""
        assert not is_a_real_change(DOM_CONFIG, dict(DOM_CONFIG))

    def test_a_new_note_alone_is_not_a_change(self):
        """The note carries a room count and a corroboration tally that move
        between runs without any selector changing."""
        same_but_noted = {**DOM_CONFIG, "discovery_note": "Auto-repaired 2026-08-20: ..."}
        assert not is_a_real_change(DOM_CONFIG, same_but_noted)

    def test_different_selectors_are_a_change(self):
        assert is_a_real_change(DOM_CONFIG, REPAIRED)

    def test_human_keys_do_not_make_it_a_change(self):
        """`standing_rate` lives outside discovery's remit, so a difference
        there says nothing about whether the selectors need replacing."""
        assert not is_a_real_change(
            DOM_CONFIG, {**DOM_CONFIG, "standing_rate": False}
        )


class TestRetiringTheRoomsABrokenConfigInvented:
    """Repairing the selectors is only half of it.

    `_seed_room_types` builds a hotel's rooms from its first successful fetch,
    so a hotel onboarded through a broken selector owns a room genuinely called
    "King Size Bed" whose price series was scraped from a tax line. Fixing the
    config does not touch that: the correctly-named offers then match nothing
    and queue up for a human, and the invented room — no longer seen — gets
    reported as SOLD OUT to whoever is on the recipient list.
    """

    def test_the_invented_room_is_retired(self):
        assert names_to_retire(
            ["King Size Bed"],
            ["Suite", "Deluxe Suite", "One Bedroom Villa"],
        ) == {"King Size Bed"}

    def test_a_real_room_is_never_retired_for_having_collapsed(self):
        """The guard that makes this safe. If the label the ingest layer saw
        collapse turns out to be a real room on the repaired page, it stays —
        whatever the broken config did with it."""
        assert names_to_retire(
            ["Deluxe Room"],
            ["Deluxe Room", "Suite"],
        ) == set()

    def test_nothing_is_retired_without_evidence_of_a_collapse(self):
        """A repair triggered by schema drift carries no collapsed names, and
        must not start deleting rooms on the strength of a config change."""
        assert names_to_retire(None, ["Suite", "Villa"]) == set()
        assert names_to_retire([], ["Suite", "Villa"]) == set()

    def test_blank_and_whitespace_labels_are_ignored(self):
        assert names_to_retire(["  ", ""], ["Suite"]) == set()

    def test_surrounding_whitespace_does_not_defeat_the_match(self):
        assert names_to_retire([" Deluxe Room "], ["Deluxe Room"]) == set()


class TestHandingTheBudgetBack:
    """The counter has to be resettable by a person, or it is a dead end.

    Running out of attempts means "this one needs a person". Nothing in the
    system let that person say they had arrived: Resolve on the Health tab
    closed the alert row and left the source locked out, so the next check
    raised the identical alert and declined the identical repair. A source
    could be permanently unrepairable while the only visible action appeared
    to do something about it -- and a fault fixed in the SCANNER could never
    reach the hotels that needed it, because none of them would ever run
    discovery again.
    """

    SPENT = RepairState(
        attempts=3, last_attempt_at=NOW - timedelta(minutes=5),
        last_outcome="no_change", discovery_version=DISCOVERY_VERSION,
    )

    def test_a_spent_source_is_refused_before_the_reset(self):
        verdict = may_attempt(
            self.SPENT, now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3,
        )
        assert verdict.allowed is False
        assert "needs a person" in verdict.reason

    def test_and_eligible_after_it(self):
        released = RepairState.from_config(self.SPENT.release())
        verdict = may_attempt(
            released, now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3,
        )
        assert verdict.allowed is True

    def test_the_cooldown_does_not_survive_the_reset(self):
        """Otherwise "try again" quietly means "try again in six hours"."""
        released = RepairState.from_config(self.SPENT.release())
        assert released.attempts == 0
        assert released.last_attempt_at is None

    def test_it_says_why_the_counter_is_zero(self):
        """A reset that looks like a fresh source hides that someone acted."""
        assert self.SPENT.release()[STATE_KEY]["last_outcome"] == "budget_restored"

    def test_it_touches_nothing_but_the_repair_state(self):
        config = {**DOM_CONFIG, STATE_KEY: {"attempts": 3}}
        released = {**config, **RepairState.from_config(config).release()}
        assert released["selectors"] == DOM_CONFIG["selectors"]
        assert released["room_card"] == DOM_CONFIG["room_card"]
        assert released["discovery_note"] == DOM_CONFIG["discovery_note"]


class TestARepairThatChangesWhatAnOfferIS:
    """Some selectors decide how a page is READ. Two decide what it IS.

    ``meal_plan`` and ``refundable`` are hashed into the offer key, so adding
    either one re-keys every offer on the page. Nothing downstream reads that
    as a config change: it reads as every room disappearing at once, and one
    check later each is recorded as BECAME_UNAVAILABLE and sent to whoever is
    on the recipient list. A repair that fixed a silently wrong price would
    have announced that the hotel had sold out.

    So the repair has to be able to ASK, which is what this is for. What it
    does with the answer -- retire the series the new config can no longer
    address, before they can be reported as a sell-out -- lives in
    ``tasks_repair._retire_superseded_series``.
    """

    WITH_PLAN = {
        **REPAIRED,
        "selectors": {**REPAIRED["selectors"], "meal_plan": "div.board"},
    }

    def test_adding_a_meal_plan_selector_is_an_identity_change(self):
        assert identity_selectors_changed(REPAIRED, self.WITH_PLAN) is True

    def test_removing_one_is_too(self):
        """The same event in reverse, and just as loud."""
        assert identity_selectors_changed(self.WITH_PLAN, REPAIRED) is True

    def test_renaming_the_room_selector_is_not(self):
        """A renamed room still resolves to its room type through the matcher
        and its aliases, so the key survives and the history with it."""
        assert identity_selectors_changed(DOM_CONFIG, REPAIRED) is False

    def test_an_unchanged_plan_is_not(self):
        moved = {
            **self.WITH_PLAN,
            "selectors": {**self.WITH_PLAN["selectors"], "price": "span.new"},
        }
        assert identity_selectors_changed(self.WITH_PLAN, moved) is False

    def test_a_json_config_is_read_from_its_own_key(self):
        """DOM configs carry selectors under "selectors" and JSON ones under
        "fields". A repair may move a source from one route to the other."""
        json_config = {"rooms_path": "rooms", "fields": {"room_name": "name"}}
        with_plan = {
            "rooms_path": "rooms",
            "fields": {"room_name": "name", "meal_plan": "board"},
        }
        assert identity_selectors_changed(json_config, with_plan) is True
        assert identity_selectors_changed(json_config, json_config) is False

    def test_a_source_with_no_config_at_all_does_not_crash_it(self):
        assert identity_selectors_changed(None, REPAIRED) is False
        assert identity_selectors_changed(None, self.WITH_PLAN) is True


class TestTheScannerGenerationMovedOn:
    """The bump that lets a fixed scanner reach the configs it was written for.

    Three scanner changes shipped against one alert -- an unrepresentative
    first node no longer kills a group, a candidate whose cards link to other
    properties sorts last, and the scan now derives a rate plan. Every source
    stamped with the previous generation spent its attempts proving the OLD
    scanner could not read it, and without a bump each would stay locked out of
    the fix. That has happened before; it is why the stamp exists.
    """

    def test_a_config_from_the_previous_generation_is_tried_again(self):
        spent = RepairState.from_config({
            VERSION_KEY: DISCOVERY_VERSION - 1,
            STATE_KEY: {"attempts": 3, "last_attempt_at": NOW.isoformat()},
        })
        verdict = may_attempt(
            spent, now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3,
        )
        assert verdict.allowed is True, verdict.reason

    def test_a_config_from_this_generation_is_not(self):
        """The budget still means something, or the bump is a way around it."""
        spent = RepairState.from_config({
            VERSION_KEY: DISCOVERY_VERSION,
            STATE_KEY: {"attempts": 3, "last_attempt_at": NOW.isoformat()},
        })
        verdict = may_attempt(
            spent, now=NOW, enabled=True, cooldown_minutes=360, max_attempts=3,
        )
        assert verdict.allowed is False, verdict.reason


class TestAHandSetIdentitySelectorSurvivesARepair:
    """Wholesale replacement is right for everything except these two.

    ``merge_config`` replaces discovery's keys rather than merging them, so a
    site that moved from CSS to a JSON endpoint does not keep a dead
    ``selectors`` block beside its new ``fields``. But ``meal_plan`` and
    ``refundable`` are hashed into the offer key, and discovery derives a
    ``meal_plan`` only where the cards are COLLIDING -- so on a page that has
    stopped colliding because somebody put the selector there, the replacement
    would take the working one with it and re-break the hotel.
    """

    WITH_PLAN = {
        **DOM_CONFIG,
        "selectors": {**DOM_CONFIG["selectors"], "meal_plan": "div.board"},
    }

    def test_it_is_carried_through(self):
        merged = merge_config(self.WITH_PLAN, REPAIRED)
        assert merged["selectors"]["meal_plan"] == "div.board"

    def test_and_the_offers_keep_their_keys(self):
        """Which is the whole point: no re-key, so no series to retire."""
        merged = merge_config(self.WITH_PLAN, REPAIRED)
        assert identity_selectors_changed(self.WITH_PLAN, merged) is False

    def test_a_derived_one_wins_over_the_stored_one(self):
        """Discovery read the page; the stored selector is a guess about a
        page that has since changed."""
        derived = {
            **REPAIRED,
            "selectors": {**REPAIRED["selectors"], "meal_plan": "h4.plan"},
        }
        merged = merge_config(self.WITH_PLAN, derived)
        assert merged["selectors"]["meal_plan"] == "h4.plan"

    def test_everything_else_is_still_replaced_wholesale(self):
        merged = merge_config(self.WITH_PLAN, REPAIRED)
        assert merged["selectors"]["room_name"] == REPAIRED["selectors"]["room_name"]
        assert merged["selectors"]["price"] == REPAIRED["selectors"]["price"]

    def test_a_css_selector_does_not_follow_a_source_onto_a_json_route(self):
        """It would match nothing there, and mean nothing."""
        to_json = {"rooms_path": "rooms", "fields": {"room_name": "name"}}
        merged = merge_config(self.WITH_PLAN, to_json)
        assert "selectors" not in merged
        assert "meal_plan" not in merged["fields"]
