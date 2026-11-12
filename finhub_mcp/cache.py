# Copyright (c) 2026 bachbnt. All rights reserved.

import functools
import threading
import time
from collections.abc import Callable
from typing import Any

from finhub_mcp.config import get_ttl

MAX_ENTRIES = 512

_MISSING = object()


def ttl_cache(kind: str, ttl: Callable[..., float] | None = None):
    """Cache a function's successful results for a TTL taken from config.

    The TTL comes from ``cache_ttl_seconds[kind]`` unless ``ttl`` is given: a callable
    receiving the call's arguments and returning seconds (0 disables caching for that call).
    Exceptions are never cached. Cached values are shared between callers, so treat
    them as read-only. Arguments must be hashable.
    """

    def decorator(fn):
        store: dict[Any, tuple[float, Any]] = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            seconds = ttl(*args, **kwargs) if ttl else get_ttl(kind)
            if seconds <= 0:
                return fn(*args, **kwargs)
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with lock:
                hit = store.get(key, _MISSING)
                if hit is not _MISSING and hit[0] > now:
                    return hit[1]
            value = fn(*args, **kwargs)
            with lock:
                if len(store) >= MAX_ENTRIES:
                    _evict(store, time.monotonic())
                store[key] = (time.monotonic() + seconds, value)
            return value

        def clear() -> None:
            with lock:
                store.clear()

        wrapper.cache_clear = clear
        return wrapper

    return decorator


def _evict(store: dict[Any, tuple[float, Any]], now: float) -> None:
    """Drop expired entries; if still full, drop the entries closest to expiry."""
    for key in [k for k, (expires, _) in store.items() if expires <= now]:
        del store[key]
    if len(store) >= MAX_ENTRIES:
        for key, _ in sorted(store.items(), key=lambda item: item[1][0])[: MAX_ENTRIES // 4]:
            del store[key]
