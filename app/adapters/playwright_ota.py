"""OTA listing pages — the fallback, and only after a recorded ToS review.

Mechanically this is the direct-site adapter: same browser, same selectors,
same refusal to evade. It exists as a separate adapter_key for two reasons
that are about governance rather than code:

* **Consent is per-source.** ``sources.tos_reviewed_at`` gates fetching, and a
  hotel's own site being cleared says nothing about an OTA's terms. A distinct
  key means the two can never share one review record.
* **Data quality differs.** OTA room names are marketing copy, meal plans are
  often absent, and the displayed rate can include an OTA-specific discount.
  Anything sourced here is second-class, and having its own key makes that
  visible in every query rather than buried in a config field.

Prefer the hotel's own booking engine. Use this only for hotels that have no
direct site, and only once someone has read the terms and put their name in
``tos_reviewed_by``.
"""
from __future__ import annotations

from app.adapters.base import FetchContext, FetchResult
from app.adapters.playwright_direct_site import PlaywrightDirectSiteAdapter
from app.core.logging import get_logger

log = get_logger("adapter.ota")


class PlaywrightOtaAdapter(PlaywrightDirectSiteAdapter):
    """A ToS-vetted OTA listing page."""

    adapter_key = "playwright_ota"
    queue = "browser"

    def fetch(self, context: FetchContext) -> FetchResult:
        # Logged at every fetch so the audit trail shows exactly how often we
        # relied on a third-party listing rather than a hotel's own rates.
        log.info(
            "ota_fetch",
            hotel=context.hotel_name,
            hotel_source_id=context.hotel_source_id,
            check_in=context.check_in.isoformat(),
        )
        return super().fetch(context)
