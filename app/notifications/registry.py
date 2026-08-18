"""Channel -> provider resolution.

Which provider serves the ``email`` channel is a setting, so moving from SMTP
to Resend is an environment change and a restart, with no code touched and no
notification lost.

Instances are cached per process: providers are stateless and hold only an
HTTP client's worth of configuration.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.notifications.base import NotificationProvider

#: Every channel a hotel_recipients row may name.
KNOWN_CHANNELS = ("email", "whatsapp")


@lru_cache(maxsize=None)
def get_provider(channel: str) -> NotificationProvider:
    """The provider currently serving ``channel``.

    Raises ``LookupError`` for an unknown channel rather than falling back to
    email: silently redirecting a WhatsApp alert to an inbox would look like
    the system working when it is not.
    """
    settings = get_settings()

    if channel == "email":
        if settings.email_provider == "resend":
            from app.notifications.providers.email_resend import ResendEmailProvider

            return ResendEmailProvider()
        from app.notifications.providers.email_smtp import SmtpEmailProvider

        return SmtpEmailProvider()

    if channel == "whatsapp":
        from app.notifications.providers.whatsapp_cloud import WhatsAppCloudProvider

        return WhatsAppCloudProvider()

    raise LookupError(
        f"No provider for channel {channel!r}. Known channels: {list(KNOWN_CHANNELS)}"
    )


def available_channels() -> list[str]:
    """Channels that are actually configured right now.

    Used by the dashboard so an operator cannot assign someone to WhatsApp
    before the access token exists — the failure would otherwise only surface
    the first time a price moved.
    """
    ready = []
    for channel in KNOWN_CHANNELS:
        try:
            if get_provider(channel).is_configured():
                ready.append(channel)
        except LookupError:
            continue
    return ready


def reset_cache() -> None:
    """For tests, and for a settings reload."""
    get_provider.cache_clear()
