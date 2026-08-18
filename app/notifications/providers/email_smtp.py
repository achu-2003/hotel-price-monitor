"""Email over SMTP.

The default provider, and the one used in development: Mailhog catches
everything on port 1025 so notification templates can be exercised without
sending anything to a real address.

In production this works fine against any relay. Resend is the alternative
when delivery reporting matters — see ``email_resend``.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from app.config import get_settings
from app.core.logging import get_logger
from app.notifications.base import Destination, RenderedMessage, SendResult

log = get_logger("notify.smtp")

_TIMEOUT = 20


class SmtpEmailProvider:
    channel = "email"
    provider_name = "smtp"

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.smtp_host and settings.email_from)

    def send(self, destination: Destination, message: RenderedMessage) -> SendResult:
        settings = get_settings()
        if not destination.email:
            return SendResult(
                ok=False,
                error_code="no_address",
                error_detail="Recipient has no email address",
                retryable=False,
            )

        msg = EmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = f"{settings.email_from_name} <{settings.email_from}>"
        msg["To"] = f"{destination.name} <{destination.email}>"
        # Our own id, kept so a bounce or a delivery log can be traced back to
        # the notification row that produced it.
        message_id = make_msgid(domain=settings.email_from.split("@")[-1])
        msg["Message-ID"] = message_id
        msg["Auto-Submitted"] = "auto-generated"

        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")

        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT) as client:
                if settings.smtp_use_tls:
                    client.starttls()
                if settings.smtp_username and settings.smtp_password:
                    client.login(
                        settings.smtp_username, settings.smtp_password.get_secret_value()
                    )
                client.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            # A refused address will be refused again. Retrying it three times
            # is how a sender reputation gets damaged.
            return SendResult(
                ok=False, error_code="recipient_refused",
                error_detail=str(exc)[:500], retryable=False,
            )
        except smtplib.SMTPAuthenticationError as exc:
            return SendResult(
                ok=False, error_code="auth_failed",
                error_detail=str(exc)[:500], retryable=False,
            )
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(
                ok=False, error_code="smtp_error",
                error_detail=str(exc)[:500], retryable=True,
            )

        log.info("email_sent", to_domain=destination.email.split("@")[-1])
        return SendResult(ok=True, provider_message_id=message_id.strip("<>"))
