"""Celery worker package.

Importing the app here is load-bearing, not tidiness.

Every task in this package is declared with ``@shared_task``, which binds to
whatever Celery considers the *current app* at import time. If a task module is
imported before the app exists — as ``app/api/v1/targets.py`` does when it
reaches for ``fetch_prices`` to queue a manual run — Celery falls back to a
default app whose broker is ``amqp://localhost:5672``. Nothing is listening
there, so ``apply_async`` fails with a bare "connection refused" while every
diagnostic insists Redis is healthy, because the failing connection was never
aimed at Redis.

Importing the app from the package ``__init__`` means any ``app.workers.*``
import initialises it first, so the current app is always ours regardless of
who imports what in which order. This is the pattern Celery's own docs use.
"""
from app.workers.celery_app import celery_app

__all__ = ["celery_app"]
