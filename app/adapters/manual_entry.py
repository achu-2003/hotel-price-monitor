"""The fallback for hotels no automation can cover.

Some of the thirty will have no booking engine, a robots.txt that says no, or
a ToS review that comes back negative. Those hotels are not dropped: an
operator types the price into the dashboard, it flows through the identical
``offer_key`` / comparison / notification path, and the resulting history is
indistinguishable from a scraped one except for its source.

This adapter therefore performs no fetch at all. It exists so that "manual" is
a first-class source rather than a special case threaded through the
scheduler: the dispatcher sees ``queue == "manual"`` and never enqueues these
targets, and the API's manual-entry endpoint writes offers directly.
"""
from __future__ import annotations

from app.adapters.base import FetchContext, FetchResult
from app.core.errors import AdapterConfigError


class ManualEntryAdapter:
    """A source whose prices arrive by hand."""

    adapter_key = "manual_entry"
    #: Not a real Celery queue. The dispatcher treats it as "never schedule".
    queue = "manual"

    def fetch(self, context: FetchContext) -> FetchResult:
        # Reached only if the dispatcher's skip is ever removed. Raising a
        # permanent, non-retried error makes that a loud configuration bug
        # rather than a silent stream of empty check runs.
        raise AdapterConfigError(
            f"{context.hotel_name} is a manual-entry source: prices are entered "
            f"in the dashboard, not fetched. This target should not have been "
            f"scheduled."
        )
