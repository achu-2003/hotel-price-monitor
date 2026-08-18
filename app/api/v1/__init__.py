"""The v1 API router.

Versioned in the path from the first commit. Adding ``/api/v2`` later costs
nothing; retrofitting a version onto URLs that scripts and bookmarks already
depend on costs a coordinated migration.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, hotels, notifications, ops, prices, sources, targets

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(hotels.router)
api_router.include_router(sources.router)
api_router.include_router(targets.router)
api_router.include_router(prices.router)
api_router.include_router(notifications.router)
api_router.include_router(ops.router)

__all__ = ["api_router"]
