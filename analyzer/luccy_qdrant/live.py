"""The read side: what luccy reads from its bundle and page files, answered live from Qdrant.

A process serves ONE bundle: the keyword collection the alias `luccy_kw` pointed at when the
process loaded (luccy's own rule; a moved alias restarts the process, see hooks.py). The
bundle fixes everything luccy derives from its chunk list: the row order, the row count N,
the signature, the recency range, and which chunk points belong to the corpus. Point ids are
shared between the keyword collection and DataGov's chunk collection, so a meaning-search
hit maps to its row through the keyword collection, and a point that is not in the bundle is
not part of this process's corpus.

Memory holds only bounded LRU caches (chunks, row ids, term statistics, page bodies).
Nothing is written to disk, and nothing is written to Qdrant: every call here is a read
(aliases, collection info, count, scroll, retrieve, query), so the web process runs with
Qdrant's read-only key (settings.py). Every write is the sync job's.
"""
from __future__ import annotations

import operator
import sys
import threading
import types
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

import numpy as np

from . import kwscore
from .settings import Settings, client

META_FORMAT = 1
SPARSE = "bm25"
ALL_SOURCES = ("confluence", "jira", "drive", "github", "powerbi")
# The chunk point fields a chunk dict, a page body and a catalog row are built from.
CHUNK_FIELDS = ["text", "index_text", "title", "section", "source_url", "page", "ordinal",
                "version_id", "document_id"]
KW_FIELDS = ["row", "luccy", "version_id", "document_id"]
MISSING = object()
# What a scoped iteration (hooks.py) yields for a row outside the scope: no page id, so
# luccy's own `c.get("page_id") in want` / `== page["id"]` tests skip it. Read-only.
OUT_OF_SCOPE = types.MappingProxyType({"page_id": None, "heading": None})


class BundleMissing(RuntimeError):
    """No usable keyword collection behind the alias (sync never ran, or it was deleted)."""


class LRU:
    """A bounded, thread-safe LRU map; `max_weight` > 0 also bounds the summed weights."""

    def __init__(self, max_entries: int, max_weight: int = 0):
        self.max_entries, self.max_weight = max(1, max_entries), max_weight
        self._d: OrderedDict = OrderedDict()
        self._w: dict = {}
        self._total = 0
        self._lock = threading.Lock()

    def get(self, key, default=MISSING):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
            return default

    def put(self, key, value, weight: int = 0) -> None:
        with self._lock:
            if key in self._d:
                self._total -= self._w.pop(key, 0)
                del self._d[key]
            self._d[key] = value
            self._w[key] = weight
            self._total += weight
            while len(self._d) > self.max_entries or (
                    self.max_weight and self._total > self.max_weight and len(self._d) > 1):
                old, _ = self._d.popitem(last=False)
                self._total -= self._w.pop(old, 0)

    def __len__(self) -> int:
        return len(self._d)


@dataclass(frozen=True)
class Bundle:
    """The keyword collection behind the alias, as its sync left it (collection metadata)."""
    name: str
    df_name: str
    sig: str
    n: int
    pages: int
    organization_id: str
    chunks: str
    min_mod: str
    max_mod: str
    built_at: str
    refreshed_at: str
    digest: str
    missing_versions: tuple = ()     # exported versions that had no chunk points at the build

    @property
    def identity(self) -> tuple:
        """What luccy's `retrieve.bundle_identity()` returns: (folder, sig.txt) on disk; here
        (the keyword collection, its signature). A moved alias changes it."""
        return (PurePosixPath(self.name), self.sig)


def alias_target(qc, alias: str) -> str | None:
    return next((a.collection_name for a in qc.get_aliases().aliases if a.alias_name == alias), None)


def read_bundle(qc, s: Settings) -> Bundle:
    target = alias_target(qc, s.kw_alias)
    if target is None:
        raise BundleMissing(f"{s.kw_alias} alias missing: run analyzer-sync (python -m luccy_qdrant.sync)")
    meta = qc.get_collection(target).config.metadata or {}
    if meta.get("luccy_live") != META_FORMAT:
        raise BundleMissing(f"collection {target} (alias {s.kw_alias}) was not written by luccy_qdrant.sync "
                            f"format {META_FORMAT}")
    return Bundle(name=target, df_name=meta["df_collection"], sig=meta["sig"], n=int(meta["n"]),
                  pages=int(meta["pages"]), organization_id=str(meta["organization_id"]),
                  chunks=meta["chunks_collection"], min_mod=meta.get("min_mod") or "",
                  max_mod=meta.get("max_mod") or "", built_at=meta.get("built_at") or "",
                  refreshed_at=meta.get("refreshed_at") or meta.get("built_at") or "",
                  digest=meta.get("digest") or "", missing_versions=tuple(meta.get("missing_versions") or ()))


@contextmanager
def server_call(what: str):
    """luccy's own failure type for an unreachable Qdrant, so a failed question tells the
    reader "document search isn't answering" exactly as luccy does (serving/jobs.py)."""
    from ingest.qdrant_index import QdrantUnavailable
    try:
        yield
    except (BundleMissing, QdrantUnavailable):
        raise
    except Exception as e:
        raise QdrantUnavailable(f"the Qdrant server did not answer ({what}: {type(e).__name__}: "
                                f"{str(e)[:200]})") from e


def chunk_dict(kwp: dict, cp: dict) -> dict:
    """One chunk in the shape luccy's `retrieve.chunk_body` gives it, from the keyword point's
    `luccy` block (page identity) and the DataGov chunk point (text). DataGov keeps no tier,
    breadcrumb or supersession metadata, so those are what luccy's `temporal_fields` gives a
    page without them."""
    lu = kwp["luccy"]
    return {"page_id": lu["id"], "title": cp.get("title") or "", "url": lu.get("url") or "",
            "space": lu.get("space") or "?", "tier": "", "modified": lu.get("modified") or "",
            "breadcrumb": "", "source_version": "", "as_of": "", "supersedes": [],
            "superseded_by": [], "stale_hint": False,
            "heading": cp.get("section") or "", "text": cp.get("text") or "",
            "index_text": cp.get("index_text") or ""}


def _models():
    from qdrant_client import models
    return models


class Live:
    """One bundle's corpus, read from Qdrant."""

    def __init__(self, s: Settings | None = None, qc=None, bundle: Bundle | None = None):
        self.s = s or Settings.from_env()
        self.qc = qc or client(self.s)
        with server_call("reading the keyword alias"):
            self.bundle = bundle or read_bundle(self.qc, self.s)
        s = self.s
        self.chunk_lru = LRU(s.chunk_cache)
        self.row_lru = LRU(s.row_cache)          # row -> (point id, keyword payload)
        self.id_lru = LRU(s.row_cache)           # point id -> row, -1 = not in this bundle
        self.df_lru = LRU(s.df_cache)            # term -> chunks holding it, 0 = none
        self.page_lru = LRU(s.page_cache)        # luccy page id -> page record or None
        self.body_lru = LRU(s.page_cache, s.body_cache_chars)
        self.local = threading.local()
        self.stats = {"qdrant_calls": 0}

    # ---------------------------------------------------------------- filters
    def _org(self):
        m = _models()
        return m.FieldCondition(key="organization_id", match=m.MatchValue(value=self.bundle.organization_id))

    def _allowed(self, off) -> list[str]:
        return [x for x in ALL_SOURCES if x not in set(off or ())]

    def chunk_filter(self, off=None):
        """DataGov chunk points of this organization that the sync gave a `luccy` block, minus
        the sources the reader switched off."""
        m = _models()
        return m.Filter(must=[self._org(), m.FieldCondition(
            key="luccy.source", match=m.MatchAny(any=self._allowed(off)))])

    def kw_filter(self, off=None):
        m = _models()
        if not off:
            return None
        return m.Filter(must=[m.FieldCondition(key="luccy.source", match=m.MatchAny(any=self._allowed(off)))])

    # ---------------------------------------------------------------- per-thread context
    @contextmanager
    def toggles(self, off):
        """The reader's switched-off sources for the keyword leg of the current search
        (luccy hands them to the meaning leg itself; see hooks.retrieve)."""
        prev = getattr(self.local, "off", None)
        self.local.off = list(off) if off else None
        try:
            yield
        finally:
            self.local.off = prev

    @contextmanager
    def scope(self, page_ids):
        """Pages a luccy loop over the whole chunk list may see (hooks.py)."""
        prev = getattr(self.local, "scope", None)
        self.local.scope = frozenset(str(p) for p in page_ids)
        try:
            yield
        finally:
            self.local.scope = prev

    def _pending(self) -> set:
        p = getattr(self.local, "pending", None)
        if p is None:
            p = self.local.pending = set()
        return p

    # ---------------------------------------------------------------- keyword leg
    def df(self, terms: list[int]) -> dict[int, int]:
        out, want = {}, []
        for t in dict.fromkeys(terms):
            n = self.df_lru.get(t)
            if n is MISSING:
                want.append(t)
            else:
                out[t] = n
        if want:
            with server_call("term statistics"):
                self.stats["qdrant_calls"] += 1
                got = {int(p.id): int(p.payload["n"]) for p in
                       self.qc.retrieve(self.bundle.df_name, ids=want, with_payload=["n"], with_vectors=False)}
            for t in want:
                out[t] = got.get(t, 0)
                self.df_lru.put(t, out[t])
        return out

    def _remember(self, pid: str, payload: dict) -> int:
        row = int(payload["row"])
        self.row_lru.put(row, (pid, payload))
        self.id_lru.put(pid, row)
        return row

    def kw_scores(self, terms: list[int], topn: int) -> list[tuple[int, np.float32]]:
        """(row, luccy keyword score) for enough chunks that luccy's top `topn` among them is
        its top `topn` over the whole corpus (the sources switched off for this thread
        excluded, as luccy's mask excludes them)."""
        m = _models()
        n_chunks = self.bundle.n
        df = self.df(terms)
        present = [t for t in dict.fromkeys(terms) if df.get(t)]
        if not present:
            return []
        idf = {t: kwscore.idf32(n_chunks, df[t]) for t in present}
        query = m.SparseVector(indices=present, values=[float(idf[t]) for t in present])
        flt = self.kw_filter(getattr(self.local, "off", None))
        limit = max(64, 4 * topn)
        while True:
            with server_call("keyword search"):
                self.stats["qdrant_calls"] += 1
                points = self.qc.query_points(self.bundle.name, query=query, using=SPARSE, limit=limit,
                                              query_filter=flt, with_payload=KW_FIELDS,
                                              with_vectors=[SPARSE]).points
            scored = []
            for p in points:
                vec = p.vector[SPARSE]
                values = dict(zip((int(i) for i in vec.indices), np.asarray(vec.values, np.float32)))
                row = self._remember(str(p.id), p.payload)
                scored.append((row, kwscore.score(terms, values, idf), float(np.float32(p.score))))
            if len(points) < limit or limit >= n_chunks:
                break
            ranked = sorted(scored, key=lambda x: (-x[1], x[0]))
            if len(ranked) >= topn and kwscore.certain(min(x[2] for x in scored), float(ranked[topn - 1][1])):
                break
            limit *= 4
        top = sorted(scored, key=lambda x: (-x[1], x[0]))[:topn]
        self._pending().update(r for r, _, _ in top)
        return [(r, sc) for r, sc, _ in scored]

    # ---------------------------------------------------------------- meaning leg
    def rows_for_ids(self, ids: list[str]) -> dict[str, int]:
        out, want = {}, []
        for pid in ids:
            r = self.id_lru.get(pid)
            if r is MISSING:
                want.append(pid)
            elif r >= 0:
                out[pid] = r
        if want:
            with server_call("mapping hits to rows"):
                self.stats["qdrant_calls"] += 1
                points = self.qc.retrieve(self.bundle.name, ids=want, with_payload=KW_FIELDS, with_vectors=False)
            found = {str(p.id): p for p in points}
            for pid in want:
                p = found.get(pid)
                if p is None:
                    self.id_lru.put(pid, -1)      # not in this bundle (a later sync's point)
                else:
                    out[pid] = self._remember(pid, p.payload)
        return out

    def vector_search(self, qv, k: int, off=None, exact: bool = False) -> list[int]:
        """Rows of the `k` chunks nearest `qv`, best first: luccy's QdrantVectors.search, over
        DataGov's chunk collection (cosine; luccy's dot product on its unit vectors ranks the
        same) restricted to this organization's synced points and the sources left on."""
        m = _models()
        with server_call("search"):
            self.stats["qdrant_calls"] += 1
            points = self.qc.query_points(
                self.bundle.chunks, query=np.asarray(qv, dtype=np.float32).tolist(), limit=k,
                query_filter=self.chunk_filter(off),
                search_params=m.SearchParams(exact=True) if exact else None,
                with_payload=CHUNK_FIELDS).points
        rows = self.rows_for_ids([str(p.id) for p in points])
        out = []
        for p in points:
            row = rows.get(str(p.id))
            if row is None:
                continue
            kwp = self.row_lru.get(row)
            if self.chunk_lru.get(row) is MISSING and kwp is not MISSING:
                self.chunk_lru.put(row, chunk_dict(kwp[1], p.payload))
            out.append(row)
        return out

    # ---------------------------------------------------------------- chunks by row
    def _rows_known(self, rows: list[int]) -> None:
        m = _models()
        want = [r for r in rows if self.row_lru.get(r) is MISSING]
        for i in range(0, len(want), 256):
            batch = want[i:i + 256]
            with server_call("rows"):
                self.stats["qdrant_calls"] += 1
                points, _ = self.qc.scroll(self.bundle.name, scroll_filter=m.Filter(must=[
                    m.FieldCondition(key="row", match=m.MatchAny(any=batch))]),
                    limit=len(batch), with_payload=KW_FIELDS, with_vectors=False)
            for p in points:
                self._remember(str(p.id), p.payload)

    def prefetch(self, rows) -> None:
        rows = [int(r) for r in rows if self.chunk_lru.get(int(r)) is MISSING]
        if not rows:
            return
        self._rows_known(rows)
        ids = {}
        for r in rows:
            got = self.row_lru.get(r)
            if got is not MISSING:
                ids[got[0]] = (r, got[1])
        keys = list(ids)
        for i in range(0, len(keys), 256):
            batch = keys[i:i + 256]
            with server_call("chunk text"):
                self.stats["qdrant_calls"] += 1
                points = self.qc.retrieve(self.bundle.chunks, ids=batch, with_payload=CHUNK_FIELDS,
                                          with_vectors=False)
            for p in points:
                r, kwp = ids[str(p.id)]
                self.chunk_lru.put(r, chunk_dict(kwp, p.payload))

    def chunk(self, row: int) -> dict:
        got = self.chunk_lru.get(row)
        if got is not MISSING:
            return got
        pending = self._pending()
        self.prefetch(sorted(pending | {row}))
        pending.clear()
        got = self.chunk_lru.get(row)
        if got is MISSING:
            raise RuntimeError(f"row {row} of bundle {self.bundle.name} has no chunk point in "
                               f"{self.bundle.chunks}: the corpus changed under this process; it "
                               "restarts onto the new bundle at the next bundle check")
        return got

    # ---------------------------------------------------------------- pages
    def page(self, luccy_id: str) -> dict | None:
        """{luccy, version_id, rows: [(row, point id)] in row order} for one page, or None."""
        rec = self.page_lru.get(luccy_id)
        if rec is not MISSING:
            return rec
        m = _models()
        flt = m.Filter(must=[m.FieldCondition(key="luccy.id", match=m.MatchValue(value=luccy_id))])
        rows, offset, first = [], None, None
        while True:
            with server_call("page lookup"):
                self.stats["qdrant_calls"] += 1
                points, offset = self.qc.scroll(self.bundle.name, scroll_filter=flt, limit=1024,
                                                offset=offset, with_payload=KW_FIELDS, with_vectors=False)
            for p in points:
                rows.append((self._remember(str(p.id), p.payload), str(p.id)))
                first = first or p.payload
            if offset is None:
                break
        rec = None if not rows else {"luccy": first["luccy"], "version_id": first["version_id"],
                                     "document_id": first["document_id"], "rows": sorted(rows)}
        self.page_lru.put(luccy_id, rec)
        return rec

    def page_rows(self, page_ids) -> list[int]:
        """Rows of every chunk of these pages, in row order (a scoped loop, hooks.py)."""
        rows = []
        for pid in page_ids:
            rec = self.page(str(pid))
            if rec:
                rows += [r for r, _ in rec["rows"]]
        rows.sort()
        self.prefetch(rows)
        return rows

    def _chunk_payloads(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 256):
            with server_call("page text"):
                self.stats["qdrant_calls"] += 1
                out += [p.payload for p in self.qc.retrieve(self.bundle.chunks, ids=ids[i:i + 256],
                                                             with_payload=CHUNK_FIELDS, with_vectors=False)]
        return out

    def page_body(self, luccy_id: str) -> str:
        """The page's whole text: every chunk of its DataGov version, ordered by (page,
        ordinal) and rebuilt by the engine's shared `document_explorer.stitch.stitch_document`.
        "" for an unknown page."""
        body = self.body_lru.get(luccy_id)
        if body is not MISSING:
            return body
        rec = self.page(luccy_id)
        body = ""
        if rec:
            from document_explorer.stitch import stitch_document
            chunks = self._chunk_payloads([pid for _, pid in rec["rows"]])
            if len(chunks) != len(rec["rows"]) or any(type(c.get("ordinal")) is not int for c in chunks):
                # A chunk gone, or one without its position (written before ordinals and not
                # yet backfilled): the page cannot be rebuilt in order. luccy's own answer for a
                # page it cannot read is an empty body: fill_whole_pages marks it "empty" and
                # keeps its passages, the viewer shows no body, open_doc finds nothing.
                print(f"[luccy_qdrant] page {luccy_id}: {len(chunks)}/{len(rec['rows'])} chunks, some "
                      "without an ordinal or missing; served as passages only", file=sys.stderr)
            else:
                body = (stitch_document(chunks) or "").strip()    # parse_page strips the body
        self.body_lru.put(luccy_id, body, weight=len(body) + 64)
        return body

    def page_meta(self, luccy_id: str) -> dict | None:
        """What luccy's `rqb.page_meta` returns: the page's frontmatter fields plus `body`; None
        for an unknown page or an empty body (parse_page's None)."""
        rec = self.page(luccy_id)
        if rec is None:
            return None
        body = self.page_body(luccy_id)
        if not body:
            return None
        lu = rec["luccy"]
        return {"id": lu["id"], "title": lu.get("title") or "", "url": lu.get("url") or "",
                "space": lu.get("space") or "?", "modified": lu.get("modified") or "",
                "source": lu.get("source") or "", "body": body}

    def catalog_row(self, luccy_id: str) -> dict | None:
        """The display fields luccy's document viewer keeps per catalog.jsonl row."""
        rec = self.page(luccy_id)
        if rec is None:
            return None
        lu = rec["luccy"]
        return {"title": lu.get("title"), "space": lu.get("space"), "url": lu.get("url") or None,
                "modified": lu.get("modified") or ""}

    # ---------------------------------------------------------------- health
    def check(self) -> tuple[dict, list[str]]:
        """(qdrant info, reasons) for /health: the alias still names this process's bundle or
        a readable one, and the statistics and chunk collections exist. No point count: the
        sync checks every bundle's counts before it moves the alias."""
        reasons, info = [], {"collection": self.s.chunks, "alias": None, "keyword": self.bundle.name}
        try:
            with server_call("health check"):
                current = read_bundle(self.qc, self.s)
                info["alias"] = current.name
                if not self.qc.collection_exists(current.df_name):
                    reasons.append(f"term statistics collection {current.df_name} is missing")
                if not self.qc.collection_exists(current.chunks):
                    reasons.append(f"chunk collection {current.chunks} is missing")
        except Exception as e:  # noqa: BLE001 - a reason, never a crash
            reasons.append(str(e)[:300])
        return info, reasons


def hours_since(iso: str) -> float | None:
    try:
        return round((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


class LiveChunks:
    """luccy's `Retriever.chunks`: indexable by row, sized N, never scanned whole.

    A loop over it is allowed only inside a scope (hooks.py wraps the two luccy functions
    that loop): rows of the scoped pages come back as chunks, every other row as
    OUT_OF_SCOPE, so `enumerate()` still yields true row numbers. Any other full scan is a
    missing hook and fails loudly instead of reading the whole corpus."""

    def __init__(self, live: Live):
        self.live = live

    def __len__(self) -> int:
        return self.live.bundle.n

    def __getitem__(self, i):
        if isinstance(i, slice):
            raise TypeError("luccy_qdrant: the live chunk list cannot be sliced")
        i = operator.index(i)
        n = self.live.bundle.n
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError(i)
        return self.live.chunk(i)

    def __iter__(self):
        scope = getattr(self.live.local, "scope", None)
        if scope is None:
            raise RuntimeError("luccy_qdrant: a loop over the whole chunk list outside a hooked "
                               "scope; luccy changed or a hook is missing (see STORAGE_MAP.md)")
        rows = set(self.live.page_rows(scope))
        for i in range(self.live.bundle.n):
            yield self.live.chunk(i) if i in rows else OUT_OF_SCOPE


class LiveKwStem:
    """luccy's `retrieve.KwStem` (the stemmed keyword scorer over `kw_stem.npz`), live."""

    def __init__(self, live: Live, encoder=None):
        import retrieve
        self.live = live
        self.n = live.bundle.n
        self.encoder = encoder or retrieve.stem_encoder()

    def scores(self, query: str) -> np.ndarray:
        """The float32 score array luccy's KwStem.scores returns, exact on every row luccy's
        top BM25_TOPN can come from and 0 elsewhere (luccy keeps only rows above 0)."""
        import retrieve
        e = next(iter(self.encoder.query_embed(query)))
        terms = [int(t) for t in np.asarray(e.indices).tolist()]
        sc = np.zeros(self.n, np.float32)
        for row, s in self.live.kw_scores(terms, retrieve.BM25_TOPN):
            sc[row] = s
        return sc


class LiveVectors:
    """luccy's `ingest.qdrant_index.QdrantVectors`, over DataGov's chunk collection."""

    def __init__(self, live: Live, exact: bool):
        self.live, self.exact = live, exact
        self.qc, self.sig, self.n_chunks = live.qc, live.bundle.sig, live.bundle.n
        self.collection = live.bundle.chunks

    def search(self, qv, k: int, off: list[str] | None = None) -> list[int]:
        return self.live.vector_search(qv, k, off, self.exact)
