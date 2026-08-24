"""A scanner fix has to be able to reach the hotels it was written for.

THE DEAD END THIS CLOSES
========================
Automatic re-discovery gets three attempts and a six-hour cooldown. Both encode
the same judgement: "we have already asked this question and we know the
answer, so stop asking and fetch a person."

That judgement is sound while the scanner is the same code. It stops being true
the instant the scanner is improved -- and at that moment every exhausted
source is holding a verdict reached by a scanner that no longer exists. The
budget, which exists to prevent a pointless retry loop, instead locks those
sources out of the very fix written for them.

This is not hypothetical, and it has already happened twice. Two hotels sat on
Attention repeating the same alert every thirty minutes. The scanner fault
behind it was found and fixed. The fix could not reach either hotel, because
both had spent their attempts proving the OLD scanner could not do it. The only
way through was a person clicking Resolve on each affected source, having first
worked out that this was what the button was for.

Resolve still exists and still does that -- see
test_resolving_an_error_unblocks_repair.py. It is the manual override. It is
not a plan for thirty hotels.

So a config now records the generation of scanner that produced it, and a
source whose stamp is behind gets a fresh attempt. Not an extra attempt: the
counter is not a resource being topped up. The refusal simply no longer
applies, because the question has not been put to the scanner now asking it.

THE TRAP ON THE OTHER SIDE
==========================
A stamp that is written only on success turns this into the runaway it was
meant to prevent: a source that comes back "no_change" stays unstamped, reads
as never-tried forever, and drives a browser at somebody else's site every
half hour. The last test in this file is that trap, pinned.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.rediscovery import (
    DISCOVERY_VERSION,
    STATE_KEY,
    VERSION_KEY,
    RepairState,
    may_attempt,
    merge_config,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

#: A source that has spent every attempt and is still inside its cooldown --
#: locked out twice over. Anything that gets past this is getting past both.
EXHAUSTED = {
    "attempts": 3,
    "last_attempt_at": (NOW - timedelta(minutes=5)).isoformat(),
    "last_outcome": "no_change",
}


def gate(config: dict) -> object:
    return may_attempt(
        RepairState.from_config(config),
        now=NOW,
        enabled=True,
        cooldown_minutes=360,
        max_attempts=3,
    )


class TestAnOutdatedStampReopensTheGate:
    def test_an_exhausted_source_from_an_older_scanner_may_try_again(self):
        """The whole point. Both refusals are void, not merely one."""
        verdict = gate({STATE_KEY: EXHAUSTED, VERSION_KEY: DISCOVERY_VERSION - 1})
        assert verdict.allowed
        assert "has not tried this page yet" in verdict.reason

    def test_a_config_predating_versioning_counts_as_outdated(self):
        """No stamp means written before any of this existed.

        That population is exactly the one that predates the fixes, so a
        missing stamp has to read as "older", never as "current". Defaulting
        the other way would leave every already-broken hotel locked out --
        the precise failure this file exists to prevent.
        """
        assert gate({STATE_KEY: EXHAUSTED}).allowed

    def test_a_first_attach_config_is_allowed_on_its_own_merits(self):
        """Never repaired, so it carries no repair state at all.

        It is eligible either way -- nothing has been spent. What matters is
        WHICH rule let it through: the ordinary budget, not the version
        escape. The stamp is read from the config rather than from the repair
        block for exactly this reason, because a source discovered once and
        never repaired has no repair block to read, and would otherwise look
        permanently out of date and be re-scanned on every fetch.
        """
        verdict = gate({"room_card": ".room", VERSION_KEY: DISCOVERY_VERSION})
        assert verdict.allowed
        assert verdict.reason == "eligible", "the version rule should not be involved"

    def test_a_garbled_stamp_is_treated_as_outdated_not_fatal(self):
        """A bad value must not take the fetch down, and must not be trusted."""
        assert gate({STATE_KEY: EXHAUSTED, VERSION_KEY: "two"}).allowed


class TestTheBudgetStillMeansSomething:
    """The version escape must not quietly become "always allowed"."""

    def test_a_current_scanner_still_honours_the_spent_budget(self):
        verdict = gate({STATE_KEY: EXHAUSTED, VERSION_KEY: DISCOVERY_VERSION})
        assert not verdict.allowed
        assert "needs a person" in verdict.reason

    def test_a_current_scanner_still_honours_the_cooldown(self):
        recent = {
            "attempts": 1,
            "last_attempt_at": (NOW - timedelta(minutes=5)).isoformat(),
            "last_outcome": "no_change",
        }
        verdict = gate({STATE_KEY: recent, VERSION_KEY: DISCOVERY_VERSION})
        assert not verdict.allowed
        assert "cooling off" in verdict.reason

    def test_disabled_still_wins_over_everything(self):
        """The off switch has to be the off switch."""
        verdict = may_attempt(
            RepairState.from_config({STATE_KEY: EXHAUSTED}),
            now=NOW,
            enabled=False,
            cooldown_minutes=360,
            max_attempts=3,
        )
        assert not verdict.allowed


class TestEveryWrittenConfigCarriesTheStamp:
    """Without this the escape hatch is a runaway browser loop."""

    def test_merge_config_stamps_the_current_generation(self):
        merged = merge_config({"standing_rate": True}, {"room_card": ".room"})
        assert merged[VERSION_KEY] == DISCOVERY_VERSION

    def test_the_stamp_survives_a_repair_and_closes_the_gate(self):
        """A repaired source must not be eligible again on the next fetch."""
        merged = merge_config({STATE_KEY: EXHAUSTED}, {"room_card": ".room"})
        merged[STATE_KEY] = {"attempts": 3, "last_attempt_at": None,
                             "last_outcome": "repaired"}
        assert not gate(merged).allowed

    def test_a_stale_stamp_on_the_old_config_is_overwritten(self):
        """discovery_version is discovery-owned: a repair replaces it.

        Carrying the old value through would leave a freshly repaired source
        still reading as out of date, and it would be re-scanned forever.
        """
        merged = merge_config(
            {VERSION_KEY: DISCOVERY_VERSION - 1, "room_card": ".old"},
            {"room_card": ".new"},
        )
        assert merged[VERSION_KEY] == DISCOVERY_VERSION

    def test_a_person_s_settings_still_survive_the_stamp(self):
        """The stamp must not become a reason to drop hand-set keys."""
        merged = merge_config(
            {"standing_rate": True, "meal_plan_filter": "RO"},
            {"room_card": ".room"},
        )
        assert merged["standing_rate"] is True
        assert merged["meal_plan_filter"] == "RO"
