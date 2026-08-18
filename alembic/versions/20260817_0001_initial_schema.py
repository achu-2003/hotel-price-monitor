"""Initial schema.

Creates every table, plus the two things Alembic autogenerate cannot express:

* ``price_observations`` as a RANGE-partitioned table with its first partitions
* the ``pg_trgm`` extension used for fuzzy room-name matching

Revision ID: 0001_initial
Revises:
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Enum types are created once, up front, and then referenced with
# create_type=False so no table creation tries to create them a second time.
ENUMS: dict[str, tuple[str, ...]] = {
    "date_strategy": ("fixed", "rolling"),
    "circuit_state": ("closed", "open", "half_open"),
    "price_basis": ("inclusive", "exclusive"),
    "change_direction": (
        "increase", "decrease", "became_unavailable", "became_available",
    ),
    "match_method": ("exact", "fuzzy", "manual"),
    "notification_status": ("queued", "sent", "delivered", "read", "failed"),
    "check_run_status": ("running", "success", "failed", "skipped"),
    "user_role": ("admin", "viewer"),
    "error_class": (
        "network", "timeout", "http_status", "auth", "rate_limited", "blocked",
        "robots_disallowed", "parse_schema_drift", "no_availability",
        "browser_crash", "adapter_config", "unknown",
    ),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    # Trigram index support for fuzzy room-name matching in SQL. Without it,
    # similarity() falls back to a sequential scan.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    # ── users & audit ────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("full_name", sa.String(120)),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", _enum("user_role"), nullable=False, server_default="viewer"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("failed_login_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("must_change_password", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        *_timestamps(),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("entity", sa.String(60), nullable=False),
        sa.Column("entity_id", sa.String(60)),
        sa.Column("before", postgresql.JSONB),
        sa.Column("after", postgresql.JSONB),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("user_agent", sa.Text),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_entity", "audit_log", ["entity", "entity_id", "at"])
    op.create_index("ix_audit_user_time", "audit_log", ["user_id", "at"])

    # ── hotels & sources ─────────────────────────────────────────────
    op.create_table(
        "hotels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False, unique=True),
        sa.Column("location", sa.String(200)),
        sa.Column("latitude", sa.Numeric(9, 6)),
        sa.Column("longitude", sa.Numeric(9, 6)),
        sa.Column("is_own_property", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text),
        *_timestamps(),
    )
    op.create_index("ix_hotels_slug", "hotels", ["slug"])
    op.create_index("ix_hotels_is_active", "hotels", ["is_active"])

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(60), nullable=False, unique=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("adapter_key", sa.String(60), nullable=False),
        sa.Column("base_domain", sa.String(200)),
        sa.Column("requires_auth", sa.Boolean, nullable=False, server_default=sa.false()),
        # Defaults to DISABLED: a source becomes fetchable only after a human
        # has reviewed its Terms of Service and enabled it explicitly.
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("rate_limit_per_min", sa.Integer, nullable=False, server_default="6"),
        sa.Column("robots_checked_at", sa.DateTime(timezone=True)),
        sa.Column("robots_allows", sa.Boolean),
        sa.Column("tos_reviewed_at", sa.Date),
        sa.Column("tos_reviewed_by", sa.String(120)),
        sa.Column("tos_notes", sa.Text),
        *_timestamps(),
    )

    op.create_table(
        "hotel_sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("hotel_id", sa.Integer,
                  sa.ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("external_id", sa.String(200)),
        sa.Column("url", sa.Text),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("adapter_config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("hotel_id", "source_id", name="uq_hotel_sources_hotel_id"),
    )
    op.create_index("ix_hotel_sources_hotel_id", "hotel_sources", ["hotel_id"])
    op.create_index("ix_hotel_sources_source_id", "hotel_sources", ["source_id"])

    op.create_table(
        "room_types",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("hotel_id", sa.Integer,
                  sa.ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("canonical_name", sa.String(200), nullable=False),
        sa.Column("capacity", sa.Integer),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("hotel_id", "canonical_name", name="uq_room_types_hotel_id"),
    )
    op.create_index("ix_room_types_hotel_id", "room_types", ["hotel_id"])
    # Trigram index: powers "find the closest existing room name" in SQL when
    # the in-process fuzzy match needs a candidate shortlist.
    op.execute(
        "CREATE INDEX ix_room_types_canonical_trgm ON room_types "
        "USING gin (canonical_name gin_trgm_ops)"
    )

    op.create_table(
        "room_type_aliases",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("room_type_id", sa.Integer,
                  sa.ForeignKey("room_types.id", ondelete="CASCADE"), nullable=False),
        # Denormalised from room_types so the uniqueness below can include it:
        # a unique constraint cannot reach through a foreign key.
        sa.Column("hotel_id", sa.Integer,
                  sa.ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_name", sa.String(300), nullable=False),
        sa.Column("normalized_name", sa.String(300), nullable=False),
        sa.Column("match_method", _enum("match_method"), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3)),
        *_timestamps(),
        # One raw name, on one source, FOR ONE HOTEL means one room. The hotel
        # must be in the key: "Deluxe Room" appears on the same OTA for a dozen
        # properties, and scoping to the source alone would let whichever hotel
        # was mapped first own that name for all the others.
        sa.UniqueConstraint("source_id", "hotel_id", "normalized_name",
                            name="uq_room_type_aliases_source_id"),
    )
    op.create_index("ix_room_type_aliases_room_type_id", "room_type_aliases",
                    ["room_type_id"])
    op.create_index("ix_room_type_aliases_hotel_id", "room_type_aliases", ["hotel_id"])
    op.create_index("ix_room_type_aliases_lookup", "room_type_aliases",
                    ["source_id", "hotel_id", "normalized_name"])

    op.create_table(
        "source_credentials",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        # An envelope from app.core.crypto: useless without the KEK, which
        # lives only in the environment.
        sa.Column("encrypted_value", sa.Text, nullable=False),
        sa.Column("is_session_state", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("reauth_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reauth_window_start", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_source_credentials_source_id", "source_credentials", ["source_id"])

    # ── monitoring ───────────────────────────────────────────────────
    op.create_table(
        "monitor_targets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("hotel_source_id", sa.Integer,
                  sa.ForeignKey("hotel_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("adults", sa.Integer, nullable=False, server_default="2"),
        sa.Column("children", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rooms", sa.Integer, nullable=False, server_default="1"),
        sa.Column("meal_plan_filter", sa.String(60)),
        sa.Column("date_strategy", _enum("date_strategy"), nullable=False),
        sa.Column("fixed_check_in", sa.Date),
        sa.Column("fixed_check_out", sa.Date),
        sa.Column("lead_time_days", sa.Integer),
        sa.Column("length_of_stay_nights", sa.Integer),
        sa.Column("interval_minutes", sa.Integer, nullable=False, server_default="30"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("min_delta_abs", sa.Numeric(12, 2)),
        sa.Column("min_delta_pct", sa.Numeric(6, 2)),
        sa.Column("confirm_checks", sa.Integer),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("circuit_state", _enum("circuit_state"), nullable=False,
                  server_default="closed"),
        sa.Column("circuit_opened_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        # A 5-minute floor is a politeness guarantee: no dashboard edit can
        # accidentally turn this into a hammering loop against a small hotel.
        sa.CheckConstraint("interval_minutes >= 5",
                           name="interval_at_least_5_min"),
        sa.CheckConstraint("adults >= 1", name="at_least_one_adult"),
        sa.CheckConstraint(
            "(date_strategy = 'fixed' AND fixed_check_in IS NOT NULL"
            " AND fixed_check_out IS NOT NULL)"
            " OR (date_strategy = 'rolling' AND lead_time_days IS NOT NULL"
            " AND length_of_stay_nights IS NOT NULL)",
            name="date_strategy_fields_present",
        ),
    )
    op.create_index("ix_monitor_targets_hotel_source_id", "monitor_targets",
                    ["hotel_source_id"])
    # The dispatcher's hot path, run every 60 seconds.
    op.create_index("ix_targets_due", "monitor_targets", ["is_enabled", "next_run_at"])
    op.create_index("ix_targets_circuit", "monitor_targets", ["circuit_state"])

    op.create_table(
        "check_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("monitor_target_id", sa.Integer,
                  sa.ForeignKey("monitor_targets.id", ondelete="SET NULL")),
        sa.Column("triggered_by", sa.String(40), nullable=False, server_default="scheduler"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", _enum("check_run_status"), nullable=False),
        sa.Column("check_in", sa.Date),
        sa.Column("check_out", sa.Date),
        sa.Column("offers_found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("offers_unmatched", sa.Integer, nullable=False, server_default="0"),
        sa.Column("changes_detected", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("error_summary", sa.Text),
    )
    op.create_index("ix_check_runs_target_time", "check_runs",
                    ["monitor_target_id", "started_at"])

    op.create_table(
        "monitoring_errors",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("monitor_target_id", sa.Integer,
                  sa.ForeignKey("monitor_targets.id", ondelete="SET NULL")),
        sa.Column("hotel_id", sa.Integer, sa.ForeignKey("hotels.id", ondelete="CASCADE")),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="SET NULL")),
        sa.Column("check_run_id", postgresql.UUID(as_uuid=False)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_class", _enum("error_class"), nullable=False),
        sa.Column("is_transient", sa.Boolean, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("context", postgresql.JSONB),
        sa.Column("screenshot_path", sa.Text),
        sa.Column("html_path", sa.Text),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_monitoring_errors_monitor_target_id", "monitoring_errors",
                    ["monitor_target_id"])
    op.create_index("ix_errors_unresolved", "monitoring_errors",
                    ["resolved_at", "occurred_at"])
    op.create_index("ix_errors_hotel_time", "monitoring_errors", ["hotel_id", "occurred_at"])
    op.create_index("ix_errors_class", "monitoring_errors", ["error_class"])

    # ── notification ─────────────────────────────────────────────────
    op.create_table(
        "recipients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("phone_e164", sa.String(20)),
        sa.Column("timezone", sa.String(60), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("quiet_hours_start", sa.Time),
        sa.Column("quiet_hours_end", sa.Time),
        *_timestamps(),
    )
    op.create_index("ix_recipients_email", "recipients", ["email"])

    op.create_table(
        "hotel_recipients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("hotel_id", sa.Integer,
                  sa.ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_id", sa.Integer,
                  sa.ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channels", postgresql.ARRAY(sa.String(20)), nullable=False,
                  server_default="{email}"),
        sa.Column("min_delta_abs", sa.Numeric(12, 2)),
        sa.Column("min_delta_pct", sa.Numeric(6, 2)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("hotel_id", "recipient_id",
                            name="uq_hotel_recipients_hotel_id"),
    )
    op.create_index("ix_hotel_recipients_hotel_id", "hotel_recipients", ["hotel_id"])
    op.create_index("ix_hotel_recipients_recipient_id", "hotel_recipients", ["recipient_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("recipient_id", sa.Integer,
                  sa.ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("hotel_id", sa.Integer, sa.ForeignKey("hotels.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        # Unique: a Celery retry must never send the same digest twice.
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("price_change_ids", postgresql.ARRAY(sa.BigInteger), nullable=False),
        sa.Column("subject", sa.String(300)),
        sa.Column("body_rendered", sa.Text),
        sa.Column("status", _enum("notification_status"), nullable=False,
                  server_default="queued"),
        sa.Column("provider_message_id", sa.String(200)),
        sa.Column("error_code", sa.String(60)),
        sa.Column("error_detail", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("dedupe_key", name="uq_notifications_dedupe"),
    )
    op.create_index("ix_notifications_recipient_time", "notifications",
                    ["recipient_id", "created_at"])
    op.create_index("ix_notifications_status", "notifications", ["status", "created_at"])
    op.create_index("ix_notifications_provider_msg", "notifications",
                    ["provider_message_id"])
    op.create_index("ix_notifications_scheduled_for", "notifications", ["scheduled_for"])

    # ── prices ───────────────────────────────────────────────────────
    op.create_table(
        "price_series",
        sa.Column("offer_key", sa.String(64), primary_key=True),
        sa.Column("hotel_id", sa.Integer,
                  sa.ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("room_type_id", sa.Integer,
                  sa.ForeignKey("room_types.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("check_in", sa.Date, nullable=False),
        sa.Column("check_out", sa.Date, nullable=False),
        sa.Column("adults", sa.Integer, nullable=False),
        sa.Column("children", sa.Integer, nullable=False, server_default="0"),
        sa.Column("meal_plan", sa.String(60)),
        sa.Column("refundable", sa.Boolean),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("last_price", sa.Numeric(12, 2)),
        sa.Column("last_price_basis", _enum("price_basis"), nullable=False,
                  server_default="inclusive"),
        sa.Column("is_available", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True)),
        sa.Column("pending_price", sa.Numeric(12, 2)),
        sa.Column("pending_since", sa.DateTime(timezone=True)),
        sa.Column("pending_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_price_series_hotel_id", "price_series", ["hotel_id"])
    op.create_index("ix_price_series_room_type_id", "price_series", ["room_type_id"])
    op.create_index("ix_price_series_hotel_dates", "price_series",
                    ["hotel_id", "check_in", "check_out"])
    op.create_index("ix_price_series_last_changed", "price_series", ["last_changed_at"])
    # Powers the "no successful check in 3 intervals" staleness alarm.
    op.create_index("ix_price_series_stale", "price_series", ["last_checked_at"])

    # Partitioned, so 90-day retention is a DROP PARTITION rather than a DELETE
    # of millions of rows. Written as raw SQL: op.create_table cannot express
    # PARTITION BY.
    op.execute(
        """
        CREATE TABLE price_observations (
            id              BIGSERIAL        NOT NULL,
            checked_at      TIMESTAMPTZ      NOT NULL,
            offer_key       VARCHAR(64)      NOT NULL,
            price_exclusive NUMERIC(12,2),
            taxes_fees      NUMERIC(12,2),
            price_inclusive NUMERIC(12,2),
            currency        VARCHAR(3)       NOT NULL,
            is_available    BOOLEAN          NOT NULL DEFAULT TRUE,
            rooms_left      INTEGER,
            raw_room_name   VARCHAR(300),
            raw_payload     JSONB,
            check_run_id    UUID,
            CONSTRAINT pk_price_observations PRIMARY KEY (id, checked_at),
            CONSTRAINT uq_observation_offer_time UNIQUE (offer_key, checked_at),
            CONSTRAINT ck_price_observations_price_inclusive_non_negative
                CHECK (price_inclusive IS NULL OR price_inclusive >= 0)
        ) PARTITION BY RANGE (checked_at)
        """
    )
    op.execute(
        "CREATE INDEX ix_observations_offer_time "
        "ON price_observations (offer_key, checked_at DESC)"
    )
    op.execute("CREATE INDEX ix_observations_run ON price_observations (check_run_id)")

    # Helper used by the monthly maintenance task, so partition creation is not
    # a deploy-time concern. IF NOT EXISTS makes it safe to call every day.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION create_price_observation_partition(target_month DATE)
        RETURNS void AS $$
        DECLARE
            start_date DATE := date_trunc('month', target_month)::DATE;
            end_date   DATE := (date_trunc('month', target_month) + INTERVAL '1 month')::DATE;
            part_name  TEXT := 'price_observations_' || to_char(start_date, 'YYYY_MM');
        BEGIN
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF price_observations '
                'FOR VALUES FROM (%L) TO (%L)',
                part_name, start_date, end_date
            );
        END;
        $$ LANGUAGE plpgsql
        """
    )

    # Current month plus three ahead: a missing partition makes INSERTs fail,
    # so there is always a buffer even if the maintenance task stops running.
    op.execute(
        """
        DO $$
        DECLARE i INTEGER;
        BEGIN
            FOR i IN -1..3 LOOP
                PERFORM create_price_observation_partition(
                    (CURRENT_DATE + (i || ' month')::INTERVAL)::DATE
                );
            END LOOP;
        END $$
        """
    )

    op.create_table(
        "price_changes",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("offer_key", sa.String(64), nullable=False),
        sa.Column("hotel_id", sa.Integer,
                  sa.ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("old_price", sa.Numeric(12, 2)),
        sa.Column("new_price", sa.Numeric(12, 2)),
        sa.Column("delta", sa.Numeric(12, 2)),
        sa.Column("delta_pct", sa.Numeric(7, 2)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("direction", _enum("change_direction"), nullable=False),
        sa.Column("observation_id_old", sa.BigInteger),
        sa.Column("observation_id_new", sa.BigInteger),
        sa.Column("notified", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_price_changes_hotel_time", "price_changes",
                    ["hotel_id", "changed_at"])
    op.create_index("ix_price_changes_unnotified", "price_changes",
                    ["notified", "changed_at"])
    op.create_index("ix_price_changes_offer", "price_changes", ["offer_key", "changed_at"])

    op.create_table(
        "unmatched_offers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("hotel_source_id", sa.Integer,
                  sa.ForeignKey("hotel_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_room_name", sa.String(300), nullable=False),
        sa.Column("normalized_name", sa.String(300), nullable=False),
        sa.Column("sample_payload", postgresql.JSONB),
        sa.Column("suggested_room_type_id", sa.Integer,
                  sa.ForeignKey("room_types.id", ondelete="SET NULL")),
        sa.Column("suggested_confidence", sa.Numeric(4, 3)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text),
        *_timestamps(),
        sa.UniqueConstraint("hotel_source_id", "normalized_name",
                            name="uq_unmatched_offers_hotel_source_id"),
    )
    op.create_index("ix_unmatched_offers_hotel_source_id", "unmatched_offers",
                    ["hotel_source_id"])
    op.create_index("ix_unmatched_open", "unmatched_offers", ["resolved_at"])


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS create_price_observation_partition(DATE)")
    # Dropping the parent drops every partition with it.
    op.execute("DROP TABLE IF EXISTS price_observations CASCADE")

    for table in (
        "unmatched_offers", "price_changes", "price_series", "notifications",
        "hotel_recipients", "recipients", "monitoring_errors", "check_runs",
        "monitor_targets", "source_credentials", "room_type_aliases",
        "room_types", "hotel_sources", "sources", "hotels", "audit_log", "users",
    ):
        op.drop_table(table)

    bind = op.get_bind()
    for name in ENUMS:
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)

    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
