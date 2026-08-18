"""Enumerations shared across models.

Stored as native PostgreSQL enums for referential safety. Adding a value later
needs a migration (``ALTER TYPE ... ADD VALUE``) — that friction is deliberate,
because a typo'd status silently breaking a query is worse.
"""
from __future__ import annotations

from enum import StrEnum


class DateStrategy(StrEnum):
    FIXED = "fixed"      # watch specific dates, e.g. 20-21 Aug 2026
    ROLLING = "rolling"  # watch "7 days out, 1 night" — resolved to absolute dates each run


class CircuitState(StrEnum):
    CLOSED = "closed"        # healthy, checking normally
    OPEN = "open"            # too many failures, paused
    HALF_OPEN = "half_open"  # sending a single probe


class PriceBasis(StrEnum):
    """Which number we compare on.

    A hotel quoting ₹2,500 + taxes and one quoting ₹2,950 inclusive are the
    same price. We store all three components and compare on ONE configured
    basis so the choice is explicit rather than accidental.
    """

    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"


class ChangeDirection(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    BECAME_UNAVAILABLE = "became_unavailable"  # sold out — NOT "price dropped to 0"
    BECAME_AVAILABLE = "became_available"


class MatchMethod(StrEnum):
    EXACT = "exact"    # normalized names matched outright
    FUZZY = "fuzzy"    # similarity above threshold — flagged for human review
    MANUAL = "manual"  # a human mapped it; highest trust


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class CheckRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # lock held — a previous run was still going


class UserRole(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"
