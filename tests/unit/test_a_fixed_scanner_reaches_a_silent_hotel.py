"""A scanner fix that no fetch will ever ask for.

THE FAILURE THIS EXISTS TO STOP
===============================
TREEBO MIDVALLEY RESIDENCY was monitored as four rooms::

    Treebo Premium Emerald Dove with Swimming Pool          Rs 2,642
    Itsy Hotels Kurinji Stay Inn with Swimming Pool         Rs 1,632
    Treebo SNS Grand Inn with Swimming Pool                 Rs 2,210
    Treebo Laa Gardenia Resort Yelagiri with Swimming Pool  Rs 1,888

Four other hotels. The page shows ONE room card and hides the rest behind
"View All Rooms", so the "similar properties" carousel further down out-counts
the real room list four to one and wins every measure the ranking has. The scan
now demotes it by where a click GOES -- every card leading off the page, each
somewhere different -- and that fix could not reach this hotel.

Every repair in this system is triggered by a fetch that noticed something.
This fault is invisible to a fetch: those prices really are on the page,
corroboration passes, no offers collapse, and every check reports success. So
nothing ever asked for a repair, and the config stayed wrong.

Two things were needed, and neither is about the scan itself, which was already
right:

1. Something has to ASK. ``DISCOVERY_VERSION`` already knew this config
   predated the fix and ``may_attempt`` already waives the budget on those
   grounds -- but only when consulted, and only a fetch consults it. A sweep
   asks now, hourly, a few at a time.

2. The repair has to take the neighbours out with it. ``_seed_room_types``
   created those four room types from the first fetch that succeeded; a new
   config does not remove them, and the next fetch reports each one sold out to
   whoever is on the recipient list. So the scan carries out the names of the
   candidate it demoted, which is the only place that evidence exists.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.discovery import cross_sold_names
from app.adapters.dom_discovery import find_room_cards
from app.services.rediscovery import (
    DISCOVERY_VERSION,
    STATE_KEY,
    VERSION_KEY,
    names_to_retire,
    needs_rescan,
)

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)

# Absolute hrefs: set_content serves from about:blank, against which a relative
# URL cannot be resolved, and the rule would never fire. A real page has a real
# origin, so relative links there resolve and the rule applies as written.
HERE = "https://example.test/hotel/treebo-midvalley-residency/"
NEIGHBOURS = [
    ("treebo-premium-emerald-dove", "Treebo Premium Emerald Dove with Swimming Pool", "2,642"),
    ("itsy-hotels-kurinji-stay-inn", "Itsy Hotels Kurinji Stay Inn with Swimming Pool", "1,632"),
    ("treebo-sns-grand-inn", "Treebo SNS Grand Inn with Swimming Pool", "2,210"),
    ("treebo-laa-gardenia-resort", "Treebo Laa Gardenia Resort Yelagiri with Swimming Pool", "1,888"),
]

PAGE = """<html><body>
  <div class="rooms">
    <div class="inMrU room">
      <h4 class="rname">Deluxe Room (Maple)</h4>
      <span class="rprice">&#8377; 1,632</span>
      <a href="%sbook">Book this room</a>
    </div>
    <button>View All Rooms</button>
  </div>
  <div class="similar">
""" % HERE + "".join(f"""
    <a class="gjOMp" href="https://example.test/hotel/{slug}/">
      <h4 class="rname">{name}</h4>
      <span class="rprice">&#8377; {price}</span>
    </a>""" for slug, name, price in NEIGHBOURS) + """
  </div>
</body></html>"""

THE_ROOM = "Deluxe Room (Maple)"
THE_NEIGHBOURS = [name for _, name, _ in NEIGHBOURS]


@pytest.fixture(scope="module")
def scanned():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment without playwright
        pytest.skip("playwright is not installed")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content(PAGE)
                return find_room_cards(page)
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


class TestTheEvidenceOnlyTheScanHas:
    def test_the_hotels_room_wins_and_the_carousel_is_last(self, scanned):
        """One card against four, and the one card is right."""
        assert scanned[0]["names"] == [THE_ROOM], scanned[0]["names"]
        assert scanned[-1]["linksAway"] == 1, scanned[-1]["card"]
        assert sorted(scanned[-1]["names"]) == sorted(THE_NEIGHBOURS)

    def test_the_neighbours_are_carried_out_of_the_scan(self, scanned):
        """Nothing downstream can supply this. A carousel price is a real price
        on a real page: no offer collapses, no selector fails, every check
        succeeds. The scan is the only place the fault is visible."""
        assert sorted(cross_sold_names(scanned, [THE_ROOM])) == sorted(THE_NEIGHBOURS)

    def test_the_hotels_own_room_is_never_among_them(self, scanned):
        assert THE_ROOM not in cross_sold_names(scanned, [THE_ROOM])

    def test_a_page_with_no_carousel_nominates_nobody(self, scanned):
        """`linksAway` is what nominates, so a room list nominates nothing."""
        rooms_only = [c for c in scanned if not c["linksAway"]]
        assert cross_sold_names(rooms_only, [THE_ROOM]) == []


class TestRetiringTheHotelsNextDoor:
    """``names_to_retire`` keeps its second condition, which is the safe one."""

    def test_the_four_neighbours_are_retired(self):
        assert names_to_retire(
            None, [THE_ROOM], THE_NEIGHBOURS
        ) == set(THE_NEIGHBOURS)

    def test_a_name_the_repaired_config_reads_is_kept(self):
        """A carousel may legitimately mention a room this hotel really has --
        a chain listing its own other properties' room types, say. Whatever the
        broken config did, a name the new one reads back is a real room."""
        survivor = THE_NEIGHBOURS[0]
        assert survivor not in names_to_retire(
            None, [THE_ROOM, survivor], THE_NEIGHBOURS
        )

    def test_a_collapse_and_a_carousel_are_retired_together(self):
        """One repair can fix both faults; both sets of evidence apply."""
        assert names_to_retire(
            ["King Size Bed"], [THE_ROOM], THE_NEIGHBOURS
        ) == {"King Size Bed", *THE_NEIGHBOURS}

    def test_nothing_is_retired_without_evidence(self):
        assert names_to_retire(None, [THE_ROOM], None) == set()
        assert names_to_retire(None, [THE_ROOM], []) == set()


class TestWhoAsksWhenNoFetchWill:
    """``needs_rescan`` is the sweep's whole decision."""

    def _config(self, **overrides):
        config = {
            "room_card": "a.gjOMp",
            "selectors": {"room_name": "h4.rname", "price": "span.rprice"},
            "discovery_note": "Auto-discovered: 4 rooms, 4/4 prices confirmed.",
            VERSION_KEY: DISCOVERY_VERSION - 1,
        }
        config.update(overrides)
        return config

    def test_a_config_an_older_scanner_wrote_is_offered_to_this_one(self):
        assert needs_rescan(self._config(), now=NOW, cooldown_minutes=360) is True

    def test_a_config_this_scanner_wrote_is_left_alone(self):
        """Or the sweep re-reads every page it has already answered for."""
        assert needs_rescan(
            self._config(**{VERSION_KEY: DISCOVERY_VERSION}),
            now=NOW, cooldown_minutes=360,
        ) is False

    def test_a_config_written_before_stamping_existed_is_offered(self):
        """A missing stamp reads as older than the scanner, not as current --
        and that population is exactly the one that predates the fixes."""
        config = self._config()
        del config[VERSION_KEY]
        assert needs_rescan(config, now=NOW, cooldown_minutes=360) is True

    def test_an_engine_profile_is_never_touched(self):
        """Agoda's JSON paths and aiosell's field map were written by a person
        against a documented payload. Running a DOM scan over one would replace
        knowledge with a guess, and no version stamp says otherwise."""
        profile = {
            "rooms_path": "roomGridData.masterRooms[*].rooms",
            "fields": {"room_name": "name"},
        }
        assert needs_rescan(profile, now=NOW, cooldown_minutes=360) is False

    def test_the_sweep_waits_where_a_fetch_does_not(self):
        """A hotel with nothing on its page refunds the attempt and withholds
        the stamp -- correctly, since this generation never got to look. With
        no cooldown of its own the sweep would offer the same empty page back
        every hour, forever."""
        recent = self._config(**{
            STATE_KEY: {
                "attempts": 1,
                "last_attempt_at": (NOW - timedelta(hours=1)).isoformat(),
                "last_outcome": "unlearnable",
            }
        })
        assert needs_rescan(recent, now=NOW, cooldown_minutes=360) is False
        assert needs_rescan(
            recent, now=NOW + timedelta(hours=6), cooldown_minutes=360
        ) is True

    def test_an_empty_config_is_not_a_candidate(self):
        assert needs_rescan(None, now=NOW, cooldown_minutes=360) is False
        assert needs_rescan({}, now=NOW, cooldown_minutes=360) is False


class TestTheSweepIsBounded:
    def test_it_is_registered_and_runs_hourly(self):
        from app.workers.celery_app import celery_app

        assert "maintenance.sweep_stale_configs" in celery_app.tasks
        entry = celery_app.conf.beat_schedule["sweep-stale-configs"]
        assert entry["schedule"] == 3_600.0
        # Beat can fall behind after a restart, and replaying a backlog of
        # these would drive several browsers at one site in the same minute.
        assert entry["options"]["expires"] < 3_600.0

    def test_it_hands_out_only_a_few_at_a_time(self):
        """Each one drives a real browser at somebody else's site, and nothing
        here is urgent -- these hotels have been wrong for as long as it took
        to notice."""
        from app.workers.tasks_maintenance import STALE_CONFIG_BATCH

        assert 1 <= STALE_CONFIG_BATCH <= 10

    def test_it_does_nothing_when_self_repair_is_switched_off(self, monkeypatch):
        from app.config import get_settings
        from app.workers import tasks_maintenance

        settings = get_settings().model_copy(
            update={"auto_rediscovery_enabled": False}
        )
        monkeypatch.setattr(tasks_maintenance, "get_settings", lambda: settings)
        # Returns without opening a session at all, so no database is needed
        # for this to be a real assertion.
        assert tasks_maintenance.sweep_stale_configs() == {"stale": 0, "requested": 0}
