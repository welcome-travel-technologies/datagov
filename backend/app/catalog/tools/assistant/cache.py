"""
Per-org TTL caching for front-loaded assistant context.

Front-loaded catalog context is expensive to assemble (DB scans, and for
BigQuery live API calls) and changes rarely, so each provider's
``build_context`` result is cached per org + scope for a short TTL.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Callable, Iterable, Optional

from django.core.cache import cache

logger = logging.getLogger(__name__)

CONTEXT_TTL_SECONDS = 600  # 10 minutes


def scope_key(scope_ids: Optional[Iterable]) -> str:
    """Stable short token for a scope selection, for use in a cache key."""
    if not scope_ids:
        return 'all'
    joined = ','.join(sorted(str(s) for s in scope_ids))
    return hashlib.md5(joined.encode()).hexdigest()[:10]


def cached_context(key: str, builder: Callable[[], str],
                   ttl: int = CONTEXT_TTL_SECONDS) -> str:
    """Return ``builder()``'s result, cached under ``key`` for ``ttl`` seconds.

    Falls back to building uncached if the cache backend misbehaves so a
    cache outage never breaks the assistant.
    """
    try:
        hit = cache.get(key)
        if hit is not None:
            return hit
    except Exception:
        logger.exception('assistant cache read failed for %s', key)
        return builder()
    value = builder()
    try:
        cache.set(key, value, ttl)
    except Exception:
        logger.exception('assistant cache write failed for %s', key)
    return value
