"""Hotels, the sources they can be priced from, and their room types."""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pg_enum
from app.db.models.enums import MatchMethod

if TYPE_CHECKING:
    from app.db.models.monitoring import MonitorTarget
    from app.db.models.notification import HotelRecipient


class Hotel(Base, TimestampMixin):
    """A property we track: yours or a competitor's."""

    __tablename__ = "hotels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    location: Mapped[str | None] = mapped_column(String(200))
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))

    # Your own property is sourced from your PMS, never scraped.
    is_own_property: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    room_types: Mapped[list[RoomType]] = relationship(
        back_populates="hotel", cascade="all, delete-orphan"
    )
    hotel_sources: Mapped[list[HotelSource]] = relationship(
        back_populates="hotel", cascade="all, delete-orphan"
    )
    recipients: Mapped[list[HotelRecipient]] = relationship(
        back_populates="hotel", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Hotel {self.id} {self.name!r}>"


class Source(Base, TimestampMixin):
    """A place prices can be read from: a booking engine, an OTA, or manual entry.

    The ToS / robots columns are an audit trail. A source is not fetchable
    until a named human has reviewed it (plan section 6 vetting checklist).
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(60), nullable=False)
    base_domain: Mapped[str | None] = mapped_column(String(200))

    requires_auth: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Politeness budget, enforced by a Redis token bucket before every fetch.
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=6, nullable=False)

    robots_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    robots_allows: Mapped[bool | None] = mapped_column(Boolean)

    # Compliance audit trail: who reviewed the Terms of Service, and when.
    tos_reviewed_at: Mapped[date | None] = mapped_column(Date)
    tos_reviewed_by: Mapped[str | None] = mapped_column(String(120))
    tos_notes: Mapped[str | None] = mapped_column(Text)

    hotel_sources: Mapped[list[HotelSource]] = relationship(back_populates="source")

    @property
    def is_usable(self) -> bool:
        """A source may only be fetched once it is enabled AND vetted."""
        return self.is_enabled and self.tos_reviewed_at is not None

    def __repr__(self) -> str:
        return f"<Source {self.code}>"


class HotelSource(Base, TimestampMixin):
    """One hotel as it appears on one source: the thing an adapter fetches."""

    __tablename__ = "hotel_sources"
    __table_args__ = (UniqueConstraint("hotel_id", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(
        ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    external_id: Mapped[str | None] = mapped_column(String(200))  # property token / hotel code
    url: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    # Per-hotel selector/flow overrides layered on top of the adapter YAML.
    # JSONB so that fixing a broken adapter is a config edit, not a deploy.
    adapter_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    hotel: Mapped[Hotel] = relationship(back_populates="hotel_sources")
    source: Mapped[Source] = relationship(back_populates="hotel_sources")
    monitor_targets: Mapped[list[MonitorTarget]] = relationship(
        back_populates="hotel_source", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<HotelSource hotel={self.hotel_id} source={self.source_id}>"


class RoomType(Base, TimestampMixin):
    """A canonical room for a hotel.

    ``canonical_name`` is the normalised form used for matching; ``name`` is
    what a human sees in the dashboard.
    """

    __tablename__ = "room_types"
    __table_args__ = (UniqueConstraint("hotel_id", "canonical_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(
        ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    hotel: Mapped[Hotel] = relationship(back_populates="room_types")
    aliases: Mapped[list[RoomTypeAlias]] = relationship(
        back_populates="room_type", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<RoomType {self.id} {self.name!r}>"


class RoomTypeAlias(Base, TimestampMixin):
    """Maps a raw room name seen on a source to a canonical room type.

    This table is what keeps a price series intact when a site renames
    "Deluxe Room" to "Deluxe Double Room with Balcony". Without it the rename
    silently starts a new series and the price history breaks in two.

    Unique on (source_id, hotel_id, normalized_name): one raw name, on one
    source, FOR ONE HOTEL can only ever mean one room. The hotel must be part
    of the key — "Deluxe Room" appears on the same OTA for a dozen different
    properties, and scoping the mapping to the source alone would let the
    first hotel to be mapped own that name for every other hotel.

    ``hotel_id`` is denormalised from ``room_type.hotel_id`` because a unique
    constraint cannot reach through a foreign key, and because the alias
    lookup at the start of every ingest is keyed on it.
    """

    __tablename__ = "room_type_aliases"
    __table_args__ = (
        UniqueConstraint("source_id", "hotel_id", "normalized_name"),
        Index("ix_room_type_aliases_lookup", "source_id", "hotel_id", "normalized_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    room_type_id: Mapped[int] = mapped_column(
        ForeignKey("room_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hotel_id: Mapped[int] = mapped_column(
        ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )

    raw_name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)
    match_method: Mapped[MatchMethod] = mapped_column(
        pg_enum(MatchMethod, "match_method"), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))

    room_type: Mapped[RoomType] = relationship(back_populates="aliases")

    def __repr__(self) -> str:
        return f"<RoomTypeAlias {self.normalized_name!r} -> room {self.room_type_id}>"
