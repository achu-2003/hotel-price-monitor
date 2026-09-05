"""Hotels, sources, room types, and aliases.

Note what a hotel payload does NOT accept: room types are not supplied when a
competitor is created. The site decides what its rooms are called, and they
are discovered by the first successful fetch. Pre-declaring them would invite
an operator to invent names that never match anything.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import Field, field_validator

from app.db.models.enums import MatchMethod
from app.schemas.common import ORMModel


# -- hotels ----------------------------------------------------------
class HotelBase(ORMModel):
    name: str = Field(min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    is_own_property: bool = False
    notes: str | None = None


class HotelCreate(HotelBase):
    slug: str | None = Field(
        default=None,
        description="URL-safe identifier. Derived from the name when omitted.",
        max_length=200,
    )

    @field_validator("slug")
    @classmethod
    def _clean_slug(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = "-".join(v.lower().strip().split())
        return "".join(c for c in cleaned if c.isalnum() or c == "-")


class HotelUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    location: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    is_active: bool | None = None
    #: Correctable after the fact, which it was not before.
    #:
    #: The flag could only be set on the create form, and it is the easiest
    #: thing on that form to miss -- one tick among the name, the location and
    #: the booking URL. Missing it makes the matrix wrong in the specific way
    #: that matters: the property whose prices the operator actually sets is
    #: shown as one more competitor, unhighlighted, and every comparison read
    #: off that screen is a comparison against the wrong baseline.
    #:
    #: Listing it here is what makes the control on the hotel page real.
    #: ORMModel does not forbid extra fields, so a PATCH carrying a name this
    #: model does not declare is accepted, ignored, and answered 200 -- the
    #: checkbox would have saved nothing and said it had.
    is_own_property: bool | None = None


class HotelOut(HotelBase):
    id: int
    slug: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class HotelHealth(ORMModel):
    """The at-a-glance state of one hotel's monitoring."""

    targets_total: int
    targets_enabled: int
    circuits_open: int
    last_success_at: datetime | None
    unresolved_errors: int
    unmatched_rooms: int
    is_stale: bool


class HotelDetail(HotelOut):
    sources: list[HotelSourceOut] = []
    room_types: list[RoomTypeOut] = []
    recipients: list[dict[str, Any]] = []
    health: HotelHealth | None = None


# -- sources ---------------------------------------------------------
class SourceBase(ORMModel):
    code: str = Field(min_length=1, max_length=60)
    display_name: str = Field(min_length=1, max_length=120)
    adapter_key: str = Field(max_length=60)
    base_domain: str | None = Field(default=None, max_length=200)
    requires_auth: bool = False
    rate_limit_per_min: int = Field(default=6, ge=1, le=120)


class SourceCreate(SourceBase):
    pass


class SourceUpdate(ORMModel):
    display_name: str | None = None
    adapter_key: str | None = None
    base_domain: str | None = None
    rate_limit_per_min: int | None = Field(default=None, ge=1, le=120)
    is_enabled: bool | None = None


class SourceToSReview(ORMModel):
    """Recording a Terms of Service review.

    ``reviewed_by`` is required and is a person's name, not a user id: the
    point of the record is that a named human took responsibility, and it must
    stay readable after that person's account is gone.
    """

    reviewed_by: str = Field(min_length=2, max_length=120)
    reviewed_at: date | None = None
    notes: str | None = None
    approve: bool = Field(
        default=True,
        description="False records a review that came back negative, which keeps "
                    "the source disabled and documents why.",
    )


class SourceOut(SourceBase):
    id: int
    is_enabled: bool
    robots_checked_at: datetime | None
    robots_allows: bool | None
    tos_reviewed_at: date | None
    tos_reviewed_by: str | None
    tos_notes: str | None
    is_usable: bool


# -- hotel <-> source ------------------------------------------------
class HotelSourceCreate(ORMModel):
    source_id: int
    url: str | None = None
    external_id: str | None = Field(default=None, max_length=200)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    adapter_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-hotel selectors and endpoint shape. Fixing a broken "
                    "adapter is an edit here, not a deploy.",
    )


class AttachFromUrl(ORMModel):
    """Attach a hotel by pasting the URL from the address bar.

    The engine, adapter, field mapping, property code and date placeholders are
    all derived from it. Nothing else is asked for, because nothing else has to
    be: every one of those is a fact about the URL rather than a decision.
    """

    url: str = Field(
        min_length=8,
        max_length=2000,
        description=(
            "The booking page showing rates for specific dates. Its dates and "
            "guest counts are replaced with placeholders so the target follows "
            "a rolling window instead of pinning one night forever."
        ),
    )
    currency: str = Field(default="INR", min_length=3, max_length=3)

    @field_validator("url")
    @classmethod
    def _must_be_http(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned.lower().startswith(("http://", "https://")):
            raise ValueError("Paste the full URL, including https://")
        return cleaned


class HotelSourceUpdate(ORMModel):
    url: str | None = None
    external_id: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    adapter_config: dict[str, Any] | None = None
    is_active: bool | None = None


class HotelSourceOut(ORMModel):
    id: int
    hotel_id: int
    source_id: int
    source_code: str | None = None
    adapter_key: str | None = None
    url: str | None
    external_id: str | None
    currency: str
    adapter_config: dict[str, Any]
    is_active: bool
    last_verified_at: datetime | None


class ReplaceUrl(ORMModel):
    """Correct the link a hotel was attached with.

    The usual reason is the honest one: the wrong page was pasted. The URL is
    re-detected exactly as it was on attach, because a hand-edited URL that
    lost its date placeholders would pin one night forever and the prices would
    go stale while still looking current.
    """

    url: str = Field(
        min_length=8,
        max_length=2000,
        description="The corrected booking page, showing rates for specific dates.",
    )
    discard_history: bool = Field(
        default=False,
        description=(
            "Required when the new URL points at a DIFFERENT property. Prices "
            "already collected belong to the old one, and comparing the two "
            "would report the difference between two hotels as a price change."
        ),
    )

    @field_validator("url")
    @classmethod
    def _must_be_http(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned.lower().startswith(("http://", "https://")):
            raise ValueError("Paste the full URL, including https://")
        return cleaned


class HotelPurge(ORMModel):
    """Confirmation for erasing a hotel outright.

    The name is typed rather than clicked. This destroys data the application
    cannot give back, so the confirmation is deliberately not something a
    mis-click can satisfy.
    """

    confirm_name: str = Field(min_length=1, max_length=200)


class HotelPurgeResult(ORMModel):
    """What was destroyed, so it can be reported once and then is gone."""

    hotel_id: int
    name: str
    series_deleted: int
    observations_deleted: int
    changes_deleted: int


class ReplaceUrlResult(ORMModel):
    """What the replacement actually did, so the dashboard can say so."""

    hotel_source: HotelSourceOut
    property_changed: bool
    series_reset: int


# -- room types ------------------------------------------------------
class RoomTypeCreate(ORMModel):
    name: str = Field(min_length=1, max_length=200)
    capacity: int | None = Field(default=None, ge=1, le=30)
    sort_order: int = 0


class RoomTypeUpdate(ORMModel):
    name: str | None = None
    capacity: int | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class RoomTypeOut(ORMModel):
    id: int
    hotel_id: int
    name: str
    canonical_name: str
    capacity: int | None
    sort_order: int
    is_active: bool


class AliasCreate(ORMModel):
    """Mapping a raw room name to a room type by hand.

    Always recorded as ``manual``, and a manual mapping outranks anything the
    fuzzy matcher would prefer: a human decision is the highest-trust signal
    this system has.
    """

    source_id: int
    raw_name: str = Field(min_length=1, max_length=300)


class AliasOut(ORMModel):
    id: int
    room_type_id: int
    hotel_id: int
    source_id: int
    raw_name: str
    normalized_name: str
    match_method: MatchMethod
    confidence: float | None


HotelDetail.model_rebuild()
