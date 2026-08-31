"""A check is about a moment. Once the moment has gone, so is the check.

THE INCIDENT THIS FILE IS ABOUT
===============================
The stack died on 29 Aug and came back on 31 Aug. Redis persists its queues, so
94 browser jobs queued before the outage were still there when the worker
started, each carrying ``check_in: 2026-08-29`` frozen into its arguments at
enqueue time. The worker did what it was told: it drove a browser to Agoda,
Booking.com and MGM asking for a night that was two days in the past.

Those sites answer that question with an empty result. No room card matches, so
every adapter reported the only thing it could see --

    parse_schema_drift - No elements matched room_card selector
    'tr.js-rt-block-row...', and no sold-out phrase appears anywhere in the
    7,934 characters of text the page rendered. This is almost certainly a
    redesign

-- about pages that had not been redesigned at all. Five in a row opened
A R Thanga Kottai's circuit, and each failure dispatched a repair that
re-derived selectors from the same empty page. Meanwhile the worker runs
``--pool=solo`` on Windows, one task at a time, so the backlog of stale jobs
starved the checks for the night that was actually on sale. The operator got
"Price monitoring has gone quiet on 9 sources" while the worker was flat out.

THE GUARD THAT WAS SUPPOSED TO CATCH THIS
=========================================
``dispatch_due_checks`` has always passed an ``expires`` to ``apply_async``, and
the intent in the comment beside it was right. The value was not::

    expires=now.timestamp() + 1800     # 1,788,152,000

Celery reads a bare number there as *seconds from now* (``AMQP.as_task_v2``),
not as a POSIX deadline, so this asked for an expiry about 56 years out. The
guard was present in the source, reviewed, and had never once fired.

So there are two tests here, and the second is the load-bearing one: expiry is a
broker convenience that a retry or a replayed message can outlive, whereas the
task refusing a window that has already started cannot be got around.
"""
from __future__ import annotations

import numbers
from datetime import UTC, date, datetime, timedelta

from app.services.dates import StayWindow
from app.workers import tasks_fetch, tasks_repair

TODAY = date(2026, 8, 31)
THE_NIGHT_THAT_HAS_GONE = StayWindow(date(2026, 8, 29), date(2026, 8, 30))
TONIGHT = StayWindow(TODAY, date(2026, 9, 1))


def payload(stay: StayWindow) -> dict:
    return {
        "hotel_id": 49,
        "hotel_name": "A R Thanga Kottai",
        "hotel_source_id": 43,
        "source_id": 4,
        "adapter_key": "ota",
        "url": "https://www.agoda.com/a-r-thangakottai/hotel/yelagiri-in.html",
        "external_id": None,
        "currency": "INR",
        "adapter_config": {},
        "check_in": stay.check_in.isoformat(),
        "check_out": stay.check_out.isoformat(),
        "adults": 2,
        "children": 0,
        "rooms": 1,
        "meal_plan_filter": None,
        "target_ids": [47],
        "rate_limit_per_min": 6,
    }


class TestTheExpiryIsADeadlineNotAClockReading:
    """The unit bug itself, asserted the way Celery reads the value."""

    def test_the_expiry_celery_receives_is_within_the_hour(self, monkeypatch):
        sent: dict = {}

        def capture(*_args, **kwargs):
            sent.update(kwargs)

        monkeypatch.setattr(tasks_fetch.fetch_prices, "apply_async", capture)
        monkeypatch.setattr(
            tasks_fetch.monitoring, "find_due_groups", lambda *_a, **_k: []
        )

        # Reproduce the call the sweep makes, then read it as Celery would.
        now = datetime.now(UTC)
        tasks_fetch.fetch_prices.apply_async(
            args=[payload(TONIGHT)],
            queue="browser",
            expires=now + timedelta(seconds=tasks_fetch._STALE_AFTER_SECONDS),
        )

        expires = sent["expires"]
        # The regression: a float here means "seconds from now", so a POSIX
        # timestamp silently becomes an expiry decades away.
        assert not isinstance(expires, numbers.Real), (
            "expires must be a datetime deadline; Celery reads a bare number "
            "as seconds-from-now, which is how a timestamp became ~56 years"
        )
        assert expires - now <= timedelta(minutes=30)


class TestAWindowThatHasStartedIsRefusedOutright:
    """The guard that holds when expiry does not."""

    def test_a_stale_fetch_is_skipped_before_the_browser_starts(self, monkeypatch):
        written: dict = {}
        monkeypatch.setattr(tasks_fetch, "local_today", lambda _tz: TODAY)
        monkeypatch.setattr(
            tasks_fetch,
            "_write_check_run",
            lambda *a, **kw: written.update(
                {"status": a[4], "summary": kw.get("error_summary", "")}
            ),
        )

        def must_not_run(*_a, **_k):
            raise AssertionError("a past night must never reach the browser")

        monkeypatch.setattr(tasks_fetch, "dispatch_lock", must_not_run)

        result = tasks_fetch.fetch_prices(payload(THE_NIGHT_THAT_HAS_GONE))

        assert result["status"] == "skipped"
        assert result["why"] == "stale"

    def test_it_is_recorded_as_skipped_not_failed(self, monkeypatch):
        """A skipped run must not count toward the circuit breaker.

        Filing it as a failure is what took A R Thanga Kottai off monitoring:
        five stale messages, five 'failures', circuit open -- for a site that
        was answering normally the whole time.
        """
        written: dict = {}
        monkeypatch.setattr(tasks_fetch, "local_today", lambda _tz: TODAY)
        monkeypatch.setattr(
            tasks_fetch,
            "_write_check_run",
            lambda *a, **kw: written.update(
                {"status": a[4], "summary": kw.get("error_summary", "")}
            ),
        )
        monkeypatch.setattr(tasks_fetch, "dispatch_lock", _unreachable)

        tasks_fetch.fetch_prices(payload(THE_NIGHT_THAT_HAS_GONE))

        assert written["status"] is tasks_fetch.CheckRunStatus.SKIPPED
        assert "already started" in written["summary"]

    def test_tonight_is_not_refused(self, monkeypatch):
        """The guard has to be a date comparison, not a blanket stop."""
        monkeypatch.setattr(tasks_fetch, "local_today", lambda _tz: TODAY)
        reached: list = []

        def granted(*_a, **_k):
            reached.append(True)
            raise tasks_fetch.LockNotAcquired  # stop before any real work

        monkeypatch.setattr(tasks_fetch, "dispatch_lock", granted)
        monkeypatch.setattr(tasks_fetch, "_write_check_run", lambda *a, **kw: None)

        tasks_fetch.fetch_prices(payload(TONIGHT))

        assert reached, "tonight's check must still run"


class TestARepairForAPastNightIsNotAttempted:
    """Discovery reads a page; a page for a past night has nothing to learn from."""

    def test_a_stale_repair_is_skipped(self, monkeypatch):
        monkeypatch.setattr(tasks_repair, "local_today", lambda _tz: TODAY)
        monkeypatch.setattr(tasks_repair, "sync_session", _unreachable)

        result = tasks_repair.rediscover_source(
            43,
            check_in="2026-08-29",
            check_out="2026-08-30",
            reason="adapter_config",
        )

        assert result["status"] == "skipped"
        assert result["why"] == "stale window"

    def test_it_does_not_spend_an_attempt_from_the_budget(self, monkeypatch):
        """Checked before the claim, so a backlog cannot exhaust the budget.

        auto_rediscovery_max_attempts is small on purpose. Ninety-four stale
        repairs arriving at once would burn through it and leave every source
        marked 'needs a person' without a single live page having been read.
        """
        monkeypatch.setattr(tasks_repair, "local_today", lambda _tz: TODAY)
        monkeypatch.setattr(tasks_repair, "sync_session", _unreachable)

        # _unreachable raises if the claim transaction is opened at all.
        tasks_repair.rediscover_source(43, check_in="2026-08-29")


def _unreachable(*_a, **_k):
    raise AssertionError("must not be reached for a stale window")
