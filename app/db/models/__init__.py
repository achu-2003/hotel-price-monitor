"""All ORM models.

Imported as one module so Alembic autogenerate and SQLAlchemy relationship
resolution see the complete metadata. Import order matters only in that every
model must be registered before ``Base.metadata`` is read.
"""
from app.db.models.enums import (
    ChangeDirection,
    CheckRunStatus,
    CircuitState,
    DateStrategy,
    MatchMethod,
    NotificationStatus,
    PriceBasis,
    UserRole,
)
from app.db.models.hotel import Hotel, HotelSource, RoomType, RoomTypeAlias, Source
from app.db.models.monitoring import CheckRun, MonitoringError, MonitorTarget
from app.db.models.notification import HotelRecipient, Notification, Recipient
from app.db.models.price import (
    OFFER_KEY_LEN,
    PriceChange,
    PriceObservation,
    PriceSeries,
    UnmatchedOffer,
)
from app.db.models.user import AuditLog, SourceCredential, User

__all__ = [
    "OFFER_KEY_LEN",
    "AuditLog",
    "ChangeDirection",
    "CheckRun",
    "CheckRunStatus",
    "CircuitState",
    "DateStrategy",
    "Hotel",
    "HotelRecipient",
    "HotelSource",
    "MatchMethod",
    "MonitorTarget",
    "MonitoringError",
    "Notification",
    "NotificationStatus",
    "PriceBasis",
    "PriceChange",
    "PriceObservation",
    "PriceSeries",
    "Recipient",
    "RoomType",
    "RoomTypeAlias",
    "Source",
    "SourceCredential",
    "UnmatchedOffer",
    "User",
    "UserRole",
]
