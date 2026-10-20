"""Every knob of the adapter, read from the environment when asked (tests change them).

Shared with luccy: LUCCY_QDRANT_URL and LUCCY_QDRANT_API_KEY name the Qdrant server (the one
DataGov's engine writes), so luccy's own settings and the adapter's agree.

Two Qdrant keys. The long-running web process (serve, live, hooks) only ever reads Qdrant, so
LUCCY_QDRANT_API_KEY may be Qdrant's READ-ONLY key (QDRANT__SERVICE__READ_ONLY_API_KEY). Every
write (payload blocks, payload indexes, the keyword collections, collection metadata, the
alias) is the sync job's, and it uses LUCCY_QDRANT_SYNC_API_KEY, a read-write key, falling
back to LUCCY_QDRANT_API_KEY when that is unset (one key for both).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _secret(name: str) -> str:
    """luccy's own secret lookup (env, NAME_FILE, /run/secrets/<name>), so a deployment
    hands both the same way. Imported lazily: serving.config is stdlib-only."""
    from serving.config import secret
    return secret(name)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number") from None


@dataclass(frozen=True)
class Settings:
    qdrant_url: str
    qdrant_api_key: str          # the serving key (may be read-only)
    qdrant_sync_api_key: str     # the sync job's read-write key; "" = qdrant_api_key
    qdrant_grpc: bool
    qdrant_timeout: int
    chunks: str                  # DataGov engine collection (DOCUMENT_EXPLORER_COLLECTION)
    kw_alias: str                # alias of the live keyword collection
    datagov_url: str
    sync_token: str
    upload_url_template: str     # optional link for uploads without a source URL
    chunk_cache: int             # LRU sizes (entries / characters)
    row_cache: int
    df_cache: int
    body_cache_chars: int
    page_cache: int

    @property
    def sync_key(self) -> str:
        """The key the sync job writes with."""
        return self.qdrant_sync_api_key or self.qdrant_api_key

    @property
    def kw_prefix(self) -> str:
        return self.kw_alias + "_"

    @property
    def df_prefix(self) -> str:
        return self.kw_alias + "df_"

    def df_name(self, kw_name: str) -> str:
        """The term-statistics collection that belongs to keyword collection `kw_name`."""
        if not kw_name.startswith(self.kw_prefix):
            raise ValueError(f"{kw_name} is not a {self.kw_prefix}* collection")
        return self.df_prefix + kw_name[len(self.kw_prefix):]

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            qdrant_url=os.environ.get("LUCCY_QDRANT_URL", "http://qdrant:6333"),
            qdrant_api_key=_secret("LUCCY_QDRANT_API_KEY"),
            qdrant_sync_api_key=_secret("LUCCY_QDRANT_SYNC_API_KEY"),
            qdrant_grpc=os.environ.get("LUCCY_LIVE_QDRANT_GRPC", "0") == "1",
            qdrant_timeout=_int("LUCCY_LIVE_QDRANT_TIMEOUT_S", 20),
            chunks=os.environ.get("LUCCY_LIVE_CHUNKS_COLLECTION",
                                  os.environ.get("DOCUMENT_EXPLORER_COLLECTION", "document_explorer_v1")),
            kw_alias=os.environ.get("LUCCY_LIVE_KW_ALIAS", "luccy_kw"),
            datagov_url=os.environ.get("DATAGOV_URL", "http://backend:8000").rstrip("/"),
            sync_token=_secret("LUCCY_SYNC_TOKEN"),
            upload_url_template=os.environ.get("LUCCY_LIVE_UPLOAD_URL_TEMPLATE", ""),
            chunk_cache=_int("LUCCY_LIVE_CHUNK_CACHE", 16384),
            row_cache=_int("LUCCY_LIVE_ROW_CACHE", 131072),
            df_cache=_int("LUCCY_LIVE_DF_CACHE", 65536),
            body_cache_chars=_int("LUCCY_LIVE_BODY_CACHE_CHARS", 8_000_000),
            page_cache=_int("LUCCY_LIVE_PAGE_CACHE", 8192),
        )


def client(s: Settings, *, sync: bool = False):
    """A thread-safe Qdrant client, made after gunicorn forks (never at import). The serving
    key by default; `sync=True` (the sync job only) the read-write sync key."""
    from qdrant_client import QdrantClient
    key = s.sync_key if sync else s.qdrant_api_key
    return QdrantClient(url=s.qdrant_url, api_key=key or None,
                        prefer_grpc=s.qdrant_grpc, timeout=s.qdrant_timeout)
