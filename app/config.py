"""Application settings.

Values come from (highest priority first):
  1. real environment variables
  2. Docker secrets mounted at /run/secrets/<name>   ← production
  3. a git-ignored .env file                          ← local development

Nothing here has a real credential as a default. Secrets that must exist in
production have no default at all, so the app refuses to boot rather than
silently running with a placeholder.
"""
from __future__ import annotations

import os
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SECRETS_DIR = "/run/secrets"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir=_SECRETS_DIR if os.path.isdir(_SECRETS_DIR) else None,
        extra="ignore",
        case_sensitive=False,
    )

    # ── app ──────────────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production"] = "development"
    app_name: str = "hotel-price-monitor"
    log_level: str = "INFO"
    timezone: str = "Asia/Kolkata"

    # Where this deployment answers from on the public internet, e.g.
    # "https://monitor.example.com". The one place a link inside an alert can
    # come from: a message is read on a phone that has never been on this
    # network, so a request's own Host header is not an answer -- the worker
    # that builds the message has no request.
    #
    # EMPTY MEANS NO LINK IS ADDED, and that is the safe default rather than a
    # guess. A message carrying http://127.0.0.1:8000 is worse than one
    # carrying nothing: it is a dead tap for every reader, and it looks like
    # the feature is working. See docs/DEPLOY-WINDOWS-NATIVE.md step 12.
    public_base_url: str = ""

    # How long a link inside an alert keeps working. Thirty days: long enough
    # that somebody scrolling back a fortnight still lands on the page the
    # message described, short enough that a message forwarded out of the
    # business stops being a window into it.
    comparison_link_days: int = 30

    # ── database ─────────────────────────────────────────────────────
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "hotelmonitor"
    postgres_user: str = "hotelmonitor_app"
    postgres_password: SecretStr

    # ── redis ────────────────────────────────────────────────────────
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db_broker: int = 0
    redis_db_result: int = 1
    redis_db_cache: int = 2

    # ── security ─────────────────────────────────────────────────────
    secret_key: SecretStr
    credential_kek: SecretStr
    access_token_expire_minutes: int = 15
    session_max_age_seconds: int = 43_200

    # The bootstrap account scripts/create_account.py creates. A username,
    # not an address — nothing is ever sent to it.
    admin_username: str = "admin"
    admin_password: SecretStr | None = None

    # ── monitoring defaults ──────────────────────────────────────────
    default_interval_minutes: int = 30
    default_min_delta_abs: float = 50.0
    default_min_delta_pct: float = 2.0
    default_confirm_checks: int = 2
    default_currency: str = "INR"
    dispatch_jitter_seconds: int = 180

    # Which of the stored price components is the one we show and compare on.
    #
    # "exclusive" is the default because it is the number the booking engines
    # print in large type -- a page quoting "₹999 + ₹49.95 taxes & fees" is
    # read by everyone, guest and revenue manager alike, as a ₹999 room. A
    # dashboard showing ₹1,048.95 for it looks wrong even when it is arguably
    # more honest, and a price nobody can find on the source page cannot be
    # checked.
    #
    # Sites that publish only an all-in figure are unaffected: the offer falls
    # back to whichever component exists (NormalizedOffer.price_on), so they
    # keep showing their own headline number.
    price_basis: Literal["inclusive", "exclusive"] = "exclusive"

    # ── sold-out rollover ────────────────────────────────────────────
    # A last-minute window that comes back "no available rooms" is re-checked
    # one night later, and the prices found there are recorded under THAT
    # night. The sold-out is still recorded for the night that was asked for;
    # the roll adds a reading, it never substitutes one.
    #
    # 1 = tonight and tomorrow roll, nothing else. 0 switches it off. Anything
    # higher starts moving windows that were chosen for their date rather than
    # their nearness -- see services/dates.rollover_window.
    sold_out_rollover_days: int = 1

    # ── playwright ───────────────────────────────────────────────────
    browser_headless: bool = True
    browser_locale: str = "en-IN"
    browser_timezone: str = "Asia/Kolkata"
    browser_nav_timeout_ms: int = 45_000
    browser_max_concurrency: int = 3
    browser_user_agent_suffix: str = "HotelPriceMonitor/1.0"
    artifact_dir: Path = Path("/data/artifacts")
    artifact_retention_days: int = 7

    # ── how much history to keep ─────────────────────────────────────
    # Months of price changes, check runs, recorded errors and sent messages
    # the clean on Settings keeps. Nothing discards them on a schedule --
    # this only says where the line falls when somebody presses the button.
    # See services/retention.py for the full list of what is deleted and,
    # more importantly, what is not.
    #
    # One by default. Twenty price changes a day on one hotel is seven thousand
    # rows a year, and none of them is read: the dashboard asks about this
    # week, the alerts about this hour. The cost is real and worth stating --
    # "what did this competitor charge last Diwali" stops having an answer.
    # Raise it if that question matters more than the disk.
    history_retention_months: int = 1

    # ── self-repair ──────────────────────────────────────────────────
    # A site redesign breaks stored selectors, and until now the only cure was
    # a person noticing and editing adapter_config by hand. Discovery already
    # knows how to derive that config; this lets it run again on an EXISTING
    # source when a fetch shows the stored one has stopped describing the page.
    #
    # On by default because the alternative is a hotel quietly monitored wrong
    # until somebody happens to look. It is still bounded: a repair is written
    # only when discovery verifies it, at most `auto_rediscovery_max_attempts`
    # times per source, no more often than the cooldown, and every attempt is
    # recorded whether it succeeded or not.
    auto_rediscovery_enabled: bool = True
    auto_rediscovery_cooldown_minutes: int = 360
    auto_rediscovery_max_attempts: int = 3

    # ── politeness ───────────────────────────────────────────────────
    respect_robots_txt: bool = True
    default_rate_limit_per_min: int = 6
    egress_allowed_domains: str = ""

    # ── email ────────────────────────────────────────────────────────
    email_provider: Literal["smtp", "resend"] = "smtp"
    email_from: str = "alerts@example.com"
    email_from_name: str = "Hotel Price Monitor"
    smtp_host: str = "mailhog"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = False
    resend_api_key: SecretStr | None = None

    # ── whatsapp ─────────────────────────────────────────────────────
    whatsapp_enabled: bool = False
    # Which side of the WhatsApp API we talk to. "meta_cloud" is Meta's own
    # Graph endpoint; "mydreams" is the My Dreams Technology reseller the
    # client's number is licensed through. They share the template name and
    # nothing else -- different transport, different auth, different errors,
    # and only meta_cloud reports delivery.
    whatsapp_provider: Literal["meta_cloud", "mydreams"] = "meta_cloud"
    whatsapp_graph_version: str = "v21.0"
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr | None = None
    whatsapp_template_name: str = "price_change_alert"
    whatsapp_template_lang: str = "en"

    # The SECOND approved template, carrying the market summary that goes out
    # every ``alert_defaults.summary_interval_hours``. Empty until it has been
    # approved, and empty means summaries do not go out over WhatsApp at all --
    # see ``notifications.base.whatsapp_template_for``.
    #
    # A separate template because it is a different message with a different
    # shape, and Meta approves a shape rather than a name: the price-change
    # template's seven variables describe ONE room moving, and no arrangement
    # of them says "four rooms moved in two hours, and here are ten properties
    # across seven categories". Sending the summary through it would be
    # rejected with 132000, which is permanent -- paid for, never delivered,
    # never retried.
    whatsapp_comparison_template_name: str = ""
    # How many body variables that template has. Configurable because the
    # approval is somebody else's decision, and because a bigger template
    # carries more of the window: two variables are fixed and every one above
    # them holds more of the moves.
    # See ``notifications.base.WHATSAPP_COMPARISON_MIN_PARAMS``.
    #
    # Not a number to maximise. Meta refuses a template that has too many
    # variables for the length of its own text (error 2388293), and this
    # message is short. Five -- three slots for the moves -- carries about
    # twenty changed rooms and leaves the body comfortably within the ratio.
    whatsapp_comparison_template_params: int = 5
    whatsapp_webhook_verify_token: SecretStr | None = None
    # Signs Meta's status callbacks. Without it the POST webhook is an open
    # endpoint that anyone who can guess a provider_message_id may use to mark
    # a notification delivered -- or failed, which is worse, because a failure
    # nobody sent looks exactly like one that happened.
    whatsapp_app_secret: SecretStr | None = None

    # Accept status callbacks that carry no valid signature.
    #
    # Off by default, and it should stay off. An unsigned callback endpoint is
    # open to anyone who can reach it: guess a provider_message_id and a
    # notification can be marked delivered, read, or FAILED -- and failure is
    # terminal, so a genuine callback afterwards cannot undo it.
    #
    # The one honest use is the few minutes between subscribing the webhook at
    # Meta and having the app secret to hand. Turn it on, finish the setup,
    # turn it off.
    whatsapp_webhook_allow_unsigned: bool = False

    # ── whatsapp via my dreams technology ────────────────────────────
    # The reseller authenticates on the QUERY STRING, so both of these end up
    # in any access log, proxy log or error breadcrumb that records a URL.
    # The provider scrubs them before logging; nothing else may log the URL.
    mydreams_base_url: str = "https://wa.mydreamstechnology.in/api"
    mydreams_license_number: str = ""
    mydreams_api_key: SecretStr | None = None

    # ── notification throttling ──────────────────────────────────────
    digest_window_seconds: int = 60
    recipient_max_msgs_per_hour: int = 10
    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(7, 0)

    # ── observability ────────────────────────────────────────────────
    sentry_dsn: str = ""
    metrics_enabled: bool = True

    # Dead-man's switch. Beat pings this URL every few minutes; the service at
    # the other end alerts when the pings STOP.
    #
    # Every other alarm in this system is raised by the system itself, which
    # means none of them can fire when the system is the thing that is down --
    # a stopped beat cannot notice that it stopped. This is the only check that
    # works from outside, and it costs one HTTP GET.
    #
    # Point it at a healthchecks.io / Better Uptime / Cronitor ping URL. Empty
    # disables it.
    heartbeat_url: str = ""
    heartbeat_timeout_seconds: float = 10.0

    # ── validators ───────────────────────────────────────────────────
    @field_validator("secret_key", "credential_kek")
    @classmethod
    def _reject_placeholders(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if raw.startswith("CHANGE_ME") or len(raw) < 32:
            raise ValueError(
                "Refusing to start with a placeholder or short secret. "
                "Generate a real value — see .env.example for the command."
            )
        return v

    @field_validator("default_currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()

    # ── derived ──────────────────────────────────────────────────────
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_async(self) -> str:
        """For FastAPI (asyncpg)."""
        return self._dsn("postgresql+asyncpg")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> str:
        """For Celery workers and Alembic (psycopg3, sync).

        Workers are sync on purpose: the sync Playwright API cannot run
        inside an event loop, and Celery prefork does not give us one.
        """
        return self._dsn("postgresql+psycopg")

    def _dsn(self, driver: str) -> str:
        pwd = self.postgres_password.get_secret_value()
        return (
            f"{driver}://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def redis_url(self, db: int) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{db}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_broker_url(self) -> str:
        return self.redis_url(self.redis_db_broker)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_result_backend(self) -> str:
        return self.redis_url(self.redis_db_result)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_url(self) -> str:
        return self.redis_url(self.redis_db_cache)

    @property
    def egress_allowlist(self) -> set[str]:
        return {d.strip().lower() for d in self.egress_allowed_domains.split(",") if d.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
