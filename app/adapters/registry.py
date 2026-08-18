"""Adapter lookup by key.

``sources.adapter_key`` is a string in the database, so adding a source is a
row insert rather than a deploy. This module is the only place that turns that
string into code.

Imports are LAZY, deliberately. The API container and the notify worker never
touch Playwright; importing it at module load would make a broken browser
install break the dashboard, and would slow every process start for nothing.
An unknown key raises :class:`AdapterConfigError`, which is classified as
permanent — a typo in configuration is not something a retry fixes.
"""
from __future__ import annotations

import importlib
from functools import lru_cache

from app.adapters.base import SourceAdapter
from app.core.errors import AdapterConfigError

#: adapter_key -> "module path:class name".
#: The queue an adapter runs on is declared on the adapter class itself, so a
#: new adapter cannot be added without stating how heavy it is.
_ADAPTERS: dict[str, str] = {
    "playwright_direct_site": "app.adapters.playwright_direct_site:PlaywrightDirectSiteAdapter",
    "playwright_ota": "app.adapters.playwright_ota:PlaywrightOtaAdapter",
    "http_json": "app.adapters.http_json:HttpJsonAdapter",
    "aiosell": "app.adapters.aiosell:AiosellAdapter",
    "manual_entry": "app.adapters.manual_entry:ManualEntryAdapter",
}

#: Queues declared without importing the adapter, so the dispatcher can route a
#: task to the right worker without loading Playwright in the beat process.
_QUEUES: dict[str, str] = {
    "playwright_direct_site": "browser",
    "playwright_ota": "browser",
    "http_json": "http",
    "aiosell": "http",
    "manual_entry": "manual",
}


def available_keys() -> list[str]:
    return sorted(_ADAPTERS)


def queue_for(adapter_key: str) -> str:
    """Which Celery queue this adapter belongs on.

    ``manual`` is not a real queue: it is the marker that says this source is
    never fetched at all. The dispatcher checks for it and skips.
    """
    try:
        return _QUEUES[adapter_key]
    except KeyError as exc:
        raise AdapterConfigError(
            f"Unknown adapter_key {adapter_key!r}. Known keys: {available_keys()}"
        ) from exc


@lru_cache(maxsize=None)
def get_adapter(adapter_key: str) -> SourceAdapter:
    """Import and instantiate the adapter for ``adapter_key``.

    Cached: adapters are stateless and one instance per worker process is
    enough. State that varies per fetch travels in ``FetchContext``.
    """
    try:
        path = _ADAPTERS[adapter_key]
    except KeyError as exc:
        raise AdapterConfigError(
            f"Unknown adapter_key {adapter_key!r}. Known keys: {available_keys()}"
        ) from exc

    module_name, _, class_name = path.partition(":")
    try:
        module = importlib.import_module(module_name)
        adapter_cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise AdapterConfigError(
            f"Adapter {adapter_key!r} is registered as {path!r} but could not be "
            f"loaded: {exc}"
        ) from exc

    return adapter_cls()


def register(adapter_key: str, import_path: str, queue: str) -> None:
    """Add an adapter at runtime.

    Exists for tests and for out-of-tree adapters; the built-ins are declared
    in the table above so the set is greppable.
    """
    _ADAPTERS[adapter_key] = import_path
    _QUEUES[adapter_key] = queue
    get_adapter.cache_clear()
