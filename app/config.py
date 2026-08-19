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

from pydantic import Field, PostgresDsn, SecretStr, computed_field, field_validator
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

    admin_email: str = "admin@example.com"
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

    # ── playwright ───────────────────────────────────────────────────
    browser_headless: bool = True
    browser_locale: str = "en-IN"
    browser_timezone: str = "Asia/Kolkata"
    browser_nav_timeout_ms: int = 45_000
    browser_max_concurrency: int = 3
    browser_user_agent_suffix: str = "HotelPriceMonitor/1.0"
    artifact_dir: Path = Path("/data/artifacts")
    artifact_retention_days: int = 7

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
    whatsapp_graph_version: str = "v21.0"
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr | None = None
    whatsapp_template_name: str = "price_change_alert"
    whatsapp_template_lang: str = "en"
    whatsapp_webhook_verify_token: SecretStr | None = None

    # ── notification throttling ──────────────────────────────────────
    digest_window_seconds: int = 60
    recipient_max_msgs_per_hour: int = 10
    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(7, 0)

    # ── observability ────────────────────────────────────────────────
    sentry_dsn: str = ""
    metrics_enabled: bool = True

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
