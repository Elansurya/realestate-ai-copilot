"""Reusable, in-process async caching layer for system configuration.

Every domain module (Customer, Lead, Property, Booking, Payment,
Dashboard, Report, AI, Notification, Audit Log, etc.) should read
runtime-configurable values (e.g. ``EMAIL.SMTP_HOST``,
``SECURITY.SESSION_TIMEOUT_MINUTES``) through this cache rather than
querying :class:`~app.repositories.settings_repository.SettingsRepository`
directly on every access. This keeps hot-path configuration reads fast
while staying eventually consistent with the ``settings`` table via
explicit reload/update/remove calls made by the Settings API layer
whenever an entry is created, updated, or deleted.

Design Notes:
    - The cache is a single process-local, module-level store guarded
      by an :class:`asyncio.Lock` so concurrent request handlers never
      observe a partially-rebuilt snapshot.
    - Cache keys follow the ``"{category}.{setting_key}"`` convention,
      identical to
      :meth:`~app.services.settings_service.SettingsService.get_cache_snapshot`,
      so the cache can be rehydrated directly from that method without
      any key translation.
    - Encrypted settings are never cached in cleartext: the snapshot
      already redacts them (``"***REDACTED***"``), and callers needing
      an encrypted value must resolve it explicitly via the service
      layer rather than through this cache.
    - This module intentionally has no FastAPI dependency-injection
      wiring of its own; it is a plain, importable utility that any
      layer (service, repository consumer, background worker) can call
      with an already-open :class:`AsyncSession`.

Example:
```python
    from app.utils.settings_cache import get_setting, reload_cache

    smtp_host = await get_setting(db, SettingCategory.EMAIL, "SMTP_HOST")

    # After an admin edits settings via the API:
    await reload_cache(db)
```
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import SettingCategory, SettingDataType
from app.repositories.settings_repository import SettingsRepository
from app.services.settings_service import SettingsService

__all__ = [
    "load_cache",
    "reload_cache",
    "clear_cache",
    "get_setting",
    "get_category",
    "update_cache_entry",
    "remove_cache_entry",
    "get_cache_statistics",
    "is_cache_loaded",
]


# --------------------------------------------------------------------------
# Module-level cache state
# --------------------------------------------------------------------------
# `_store` mirrors the shape returned by `SettingsService.get_cache_snapshot`:
#   "{category}.{setting_key}" -> {"value": ..., "data_type": ..., "is_encrypted": ...}
_store: dict[str, dict[str, Any]] = {}

# Guards every read/write against the store to keep concurrent async
# request handlers from observing a half-populated cache during a reload.
_lock = asyncio.Lock()

# Bookkeeping used by `get_cache_statistics()`.
_last_loaded_at: Optional[datetime] = None
_reload_count: int = 0
_hit_count: int = 0
_miss_count: int = 0


def _build_service(db: AsyncSession) -> SettingsService:
    """Constructs a :class:`SettingsService` bound to the given session.

    Args:
        db: The active asynchronous SQLAlchemy session.

    Returns:
        SettingsService: A service instance ready to read setting entries.
    """
    repository = SettingsRepository(db)
    return SettingsService(repository)


def _cache_key(category: SettingCategory, setting_key: str) -> str:
    """Builds the canonical cache key for a (category, setting_key) pair.

    Args:
        category: The functional category the setting belongs to.
        setting_key: The configuration key within that category.

    Returns:
        str: The canonical ``"{category}.{setting_key}"`` cache key.
    """
    category_value = (
        category.value if isinstance(category, SettingCategory) else str(category)
    )
    return f"{category_value}.{setting_key.strip().upper()}"


# --------------------------------------------------------------------------
# Load / reload / clear
# --------------------------------------------------------------------------


async def load_cache(
    db: AsyncSession, *, category: Optional[SettingCategory] = None
) -> dict[str, dict[str, Any]]:
    """Populates (without clearing) the cache from persisted settings.

    Existing entries outside the requested scope are left untouched;
    entries within the requested scope are overwritten with fresh
    values. Use :func:`reload_cache` instead when a clean rebuild is
    required.

    Args:
        db: The active asynchronous SQLAlchemy session.
        category: Optional category to scope the load to. If omitted,
            every category is loaded.

    Returns:
        dict[str, dict[str, Any]]: A shallow copy of the resulting
        in-memory cache store.
    """
    global _last_loaded_at

    service = _build_service(db)
    snapshot = await service.get_cache_snapshot(category=category)

    async with _lock:
        _store.update(snapshot)
        _last_loaded_at = datetime.now(timezone.utc)
        return dict(_store)


async def reload_cache(
    db: AsyncSession, *, category: Optional[SettingCategory] = None
) -> dict[str, dict[str, Any]]:
    """Rebuilds the cache from persisted settings, discarding stale entries.

    Intended to be invoked by the Settings API's
    ``POST /settings/cache/reload`` endpoint, and by any create/update/
    delete flow that wants a guaranteed-consistent view afterward.

    Args:
        db: The active asynchronous SQLAlchemy session.
        category: Optional category to scope the reload to. If
            provided, only that category's entries are cleared and
            reloaded; other cached categories are left as-is. If
            omitted, the entire cache is cleared and reloaded.

    Returns:
        dict[str, dict[str, Any]]: A shallow copy of the resulting
        in-memory cache store.
    """
    global _last_loaded_at, _reload_count

    service = _build_service(db)
    snapshot = await service.get_cache_snapshot(category=category)

    async with _lock:
        if category is None:
            _store.clear()
        else:
            prefix = f"{category.value}."
            for key in [k for k in _store if k.startswith(prefix)]:
                del _store[key]

        _store.update(snapshot)
        _last_loaded_at = datetime.now(timezone.utc)
        _reload_count += 1
        return dict(_store)


async def clear_cache(*, category: Optional[SettingCategory] = None) -> None:
    """Empties the cache without repopulating it from the database.

    Args:
        category: Optional category to clear. If omitted, the entire
            cache is emptied.
    """
    async with _lock:
        if category is None:
            _store.clear()
            return
        prefix = f"{category.value}."
        for key in [k for k in _store if k.startswith(prefix)]:
            del _store[key]


# --------------------------------------------------------------------------
# Read access
# --------------------------------------------------------------------------


async def get_setting(
    db: AsyncSession,
    category: SettingCategory,
    setting_key: str,
    *,
    default: Optional[Any] = None,
    auto_load: bool = True,
) -> Optional[Any]:
    """Retrieves a single cached setting value, loading on demand if needed.

    Args:
        db: The active asynchronous SQLAlchemy session, used only if
            the cache must be (re)populated.
        category: The functional category the setting belongs to.
        setting_key: The configuration key within that category.
        default: Value returned if the setting is not found.
        auto_load: If ``True`` (the default) and the entry is missing,
            the category is loaded from the database once before
            falling back to ``default``.

    Returns:
        Optional[Any]: The cached setting's value, or ``default`` if
        not found. Encrypted settings resolve to a redacted placeholder
        rather than their real value.
    """
    global _hit_count, _miss_count

    key = _cache_key(category, setting_key)

    async with _lock:
        entry = _store.get(key)

    if entry is None and auto_load:
        await load_cache(db, category=category)
        async with _lock:
            entry = _store.get(key)

    async with _lock:
        if entry is None:
            _miss_count += 1
            return default
        _hit_count += 1
        return entry.get("value", default)


async def get_category(
    db: AsyncSession, category: SettingCategory, *, auto_load: bool = True
) -> dict[str, Any]:
    """Retrieves every cached setting value within a given category.

    Args:
        db: The active asynchronous SQLAlchemy session, used only if
            the category must be (re)loaded.
        category: The functional category to retrieve.
        auto_load: If ``True`` (the default) and the category has no
            cached entries yet, it is loaded from the database first.

    Returns:
        dict[str, Any]: Mapping of bare ``setting_key`` (without the
        category prefix) to its cached value for every entry in the
        category.
    """
    prefix = f"{category.value}."

    async with _lock:
        matches = {k: v for k, v in _store.items() if k.startswith(prefix)}

    if not matches and auto_load:
        await load_cache(db, category=category)
        async with _lock:
            matches = {k: v for k, v in _store.items() if k.startswith(prefix)}

    return {key[len(prefix):]: entry.get("value") for key, entry in matches.items()}


# --------------------------------------------------------------------------
# Incremental write access (called after create/update/delete)
# --------------------------------------------------------------------------


async def update_cache_entry(
    category: SettingCategory,
    setting_key: str,
    value: Optional[Any],
    *,
    data_type: SettingDataType = SettingDataType.STRING,
    is_encrypted: bool = False,
) -> None:
    """Upserts a single entry directly into the cache without a DB round-trip.

    Intended for use immediately after a setting is created or updated,
    so the cache stays warm without requiring a full :func:`reload_cache`.

    Args:
        category: The functional category the setting belongs to.
        setting_key: The configuration key within that category.
        value: The new value to cache. Ignored (redacted) when
            ``is_encrypted`` is ``True``.
        data_type: The logical data type of ``value``.
        is_encrypted: Whether the underlying setting is encrypted; if
            ``True``, a redacted placeholder is cached instead of the
            real value.
    """
    key = _cache_key(category, setting_key)
    cached_value = "***REDACTED***" if is_encrypted else value

    async with _lock:
        _store[key] = {
            "value": cached_value,
            "data_type": (
                data_type.value if isinstance(data_type, SettingDataType) else data_type
            ),
            "is_encrypted": is_encrypted,
        }


async def remove_cache_entry(category: SettingCategory, setting_key: str) -> None:
    """Removes a single entry from the cache, e.g. after a delete.

    Args:
        category: The functional category the setting belongs to.
        setting_key: The configuration key within that category.
    """
    key = _cache_key(category, setting_key)
    async with _lock:
        _store.pop(key, None)


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------


async def is_cache_loaded() -> bool:
    """Reports whether the cache currently holds at least one entry.

    Returns:
        bool: ``True`` if the cache is non-empty.
    """
    async with _lock:
        return bool(_store)


async def get_cache_statistics() -> dict[str, Any]:
    """Reports aggregate statistics describing the cache's current state.

    Returns:
        dict[str, Any]: A snapshot containing:
            - ``total_entries``: Number of cached setting entries.
            - ``categories_cached``: Sorted list of categories with at
              least one cached entry.
            - ``hit_count`` / ``miss_count``: Cumulative lookup counts
              since process start.
            - ``hit_rate``: ``hit_count / (hit_count + miss_count)``,
              or ``0.0`` if there have been no lookups yet.
            - ``reload_count``: Number of times :func:`reload_cache`
              has been invoked since process start.
            - ``last_loaded_at``: UTC timestamp of the most recent
              load/reload, or ``None`` if the cache has never been
              populated.
            - ``is_loaded``: Whether the cache currently holds any
              entries.
    """
    async with _lock:
        categories_cached = sorted(
            {key.split(".", 1)[0] for key in _store if "." in key}
        )
        total_lookups = _hit_count + _miss_count
        hit_rate = (_hit_count / total_lookups) if total_lookups else 0.0

        return {
            "total_entries": len(_store),
            "categories_cached": categories_cached,
            "hit_count": _hit_count,
            "miss_count": _miss_count,
            "hit_rate": round(hit_rate, 4),
            "reload_count": _reload_count,
            "last_loaded_at": _last_loaded_at,
            "is_loaded": bool(_store),
        }