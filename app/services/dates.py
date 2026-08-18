"""Resolving a monitoring configuration into concrete stay dates.

THE ROLLING-DATE TRAP
=====================
A target configured as "7 days out, 1 night" means 21 Aug when it runs today
and 22 Aug when it runs tomorrow. Those are DIFFERENT NIGHTS. If the rolling
offset were part of the price identity, tomorrow's check would compare a
22 Aug price against a 21 Aug price and report a change that never happened —
and weekend nights against weekday nights would produce huge phantom swings.

So the rule is: **a rolling strategy only GENERATES dates. The offer_key always
contains absolute dates.** A stay window that rolls out of range simply ends
that price series, which is correct: that night is in the past and can no
longer change.

This module is pure and takes ``today`` as an argument so tests do not need to
freeze the clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.models.enums import DateStrategy


@dataclass(frozen=True, slots=True)
class StayWindow:
    """A concrete, absolute stay. Never relative."""

    check_in: date
    check_out: date

    def __post_init__(self) -> None:
        if self.check_out <= self.check_in:
            raise ValueError(
                f"check_out ({self.check_out}) must be after check_in ({self.check_in})"
            )

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    def __str__(self) -> str:
        return f"{self.check_in.isoformat()}->{self.check_out.isoformat()}"


def local_today(tz_name: str = "Asia/Kolkata") -> date:
    """Today in the hotel's timezone.

    The server runs on UTC, but "7 days out" means seven days from the local
    date. Between 18:30 and 24:00 UTC those differ, and using the UTC date
    would silently shift every rolling window by a day for a third of the day.
    """
    return datetime.now(ZoneInfo(tz_name)).date()


def resolve_stay_window(
    *,
    strategy: DateStrategy,
    today: date,
    fixed_check_in: date | None = None,
    fixed_check_out: date | None = None,
    lead_time_days: int | None = None,
    length_of_stay_nights: int | None = None,
) -> StayWindow | None:
    """Turn a target's configuration into an absolute stay window.

    Returns ``None`` when the window is in the past — the caller should stop
    checking rather than treat it as an error. A fixed window for 20 Aug is
    simply finished once 20 Aug has passed.
    """
    if strategy == DateStrategy.FIXED:
        if fixed_check_in is None or fixed_check_out is None:
            raise ValueError("fixed strategy requires both fixed_check_in and fixed_check_out")
        if fixed_check_in < today:
            return None  # the stay has started or passed; nothing left to monitor
        return StayWindow(fixed_check_in, fixed_check_out)

    if strategy == DateStrategy.ROLLING:
        if lead_time_days is None or length_of_stay_nights is None:
            raise ValueError(
                "rolling strategy requires both lead_time_days and length_of_stay_nights"
            )
        if lead_time_days < 0:
            raise ValueError("lead_time_days must not be negative")
        if length_of_stay_nights < 1:
            raise ValueError("length_of_stay_nights must be at least 1")
        check_in = today + timedelta(days=lead_time_days)
        return StayWindow(check_in, check_in + timedelta(days=length_of_stay_nights))

    raise ValueError(f"Unknown date strategy: {strategy}")


def next_weekend(today: date, nights: int = 1) -> StayWindow:
    """The upcoming Friday night.

    Weekend rates behave differently from weekdays, so this is usually one of
    the more informative windows to watch. If today IS Friday, this returns
    next Friday rather than tonight — tonight's price is nearly fixed already.
    """
    days_until_friday = (4 - today.weekday()) % 7 or 7
    check_in = today + timedelta(days=days_until_friday)
    return StayWindow(check_in, check_in + timedelta(days=nights))


def default_windows(today: date) -> list[StayWindow]:
    """The three windows a new hotel starts with.

    Short lead time, medium lead time, and a weekend. Together these show
    both last-minute discounting and forward-looking rate strategy.
    """
    return [
        StayWindow(today + timedelta(days=7), today + timedelta(days=8)),
        StayWindow(today + timedelta(days=14), today + timedelta(days=15)),
        next_weekend(today),
    ]
