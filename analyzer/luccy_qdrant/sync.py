"""`python -m luccy_qdrant.sync`: DataGov's corpus export (contract C2) -> luccy's live corpus.

    python -m luccy_qdrant.sync [--force] [--dry-run]

1. Digest (`GET {DATAGOV_URL}/api/document-analyzer/internal/documents/digest/`, bearer
   LUCCY_SYNC_TOKEN). Equal to the digest the live bundle was built from: one filtered count
   says whether the versions that had no chunk points at that build (`missing_versions`) have
   some now (then the run rebuilds), and one count checks that every chunk point of the
   bundle still carries its `luccy` block. All there: only `refreshed_at` is stamped
   ("unchanged"). Some missing (DataGov's engine re-upserts a version's points whole, payload
   included, when one is missing): the blocks are set again from the bundle ("repaired"),
   or, when the bundle's points no longer all exist, the run goes on to a full rebuild. The
   digest covers the SET of current versions, so a renamed title without a new version
   waits for `--force`. An unchanged run is a handful of requests.
2. The export, page by page (cursor, limit 500): current, ready versions of one
   organization's collections every member may read, identities only. Every page must name
   the same organization.
3. Every DataGov chunk point of those versions (Qdrant `document_explorer_v1`, filtered by
   organization and version_id). A version without points is reported, left out and kept in
   the bundle's `missing_versions`, so a later run picks it up once it has points. A point
   without `ordinal` (written before ordinals, not yet backfilled) is kept, ordered after its
   page's ordered chunks, and reported; its page is served as passages only (see
   live.page_body). A version the export lists twice, whose chunk points share one (page
   id, page, ordinal), or whose points vanish while the keyword collection is written is
   left out of this run (`skipped_versions` in the report) instead of failing it; vanished
   ones also count as missing.
4. luccy page ids (ids.py, contract C3) and the `luccy` block {id, source, modified, title,
   space, url}.
5. luccy's row order: page files sorted by name (`<id>.md`), chunks in page order, i.e.
   (page id + ".md", page, ordinal). A new keyword collection `luccy_kw_<UTC stamp>` gets one
   point per chunk with the SAME point id, the chunk's Qdrant/bm25 sparse vector encoded from
   its `index_text` exactly as luccy's `write_kw_stem` does (fastembed, no IDF modifier: see
   kwscore.py), and payload {row, version_id, document_id, luccy}; `luccy_kwdf_<stamp>` holds
   each term's chunk count (luccy's n_t). luccy's signature, chunk count, page count and
   recency range, the export digest and the missing versions go into the collection's
   metadata. Nothing is embedded: dense vectors stay where the engine put them.
6. The `luccy` block is set on every chunk point of each exported version, the alias
   `luccy_kw` moves to the new collection in one call, the block is removed from points of
   versions no longer exported, and older keyword collections are deleted except the one the
   alias pointed at before (rollback: move the alias back).

Qdrant keys: every call here uses LUCCY_QDRANT_SYNC_API_KEY (read-write), else
LUCCY_QDRANT_API_KEY. The web process needs only read access (settings.py). A read-only key
here is refused at the run's first write, before anything changed, with a message naming
the variable to set.

No file is written. A failure before the alias moves deletes the new collections and leaves
the live corpus as it was. Run one sync at a time (one scheduled job).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

import numpy as np

from . import ids, kwscore
from .live import ALL_SOURCES, META_FORMAT, SPARSE, BundleMissing, alias_target, read_bundle
from .settings import Settings, client

C2_PATH = "/api/document-analyzer/internal/documents/"
PAGE_LIMIT = 500
BATCH = 256
# Builds of one run whose chunk points keep vanishing before it gives up (the next run tries again).
BUILD_ATTEMPTS = 3
# DataGov's internal endpoints answer 404 to any request that came through a proxy (one
# carrying X-Forwarded-For, X-Real-IP or Forwarded), so HTTP(S)_PROXY is never used here:
# the sync reaches DATAGOV_URL (http://web:8000) directly.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SyncError(RuntimeError):
    pass


class Vanished(SyncError):
    """Chunk points of `versions` disappeared while the keyword collection was written."""

    def __init__(self, versions: set):
        super().__init__(f"chunk points of {len(versions)} versions vanished during the sync")
        self.versions = versions


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get(s: Settings, path: str, params: dict | None = None) -> dict:
    """GET a DataGov machine endpoint with the sync token."""
    what = "export"
    url = s.datagov_url + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    headers = {"Authorization": f"Bearer {s.sync_token}", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _DIRECT.open(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = str(json.loads(e.read().decode("utf-8")).get("detail") or "")[:200]
        except Exception:  # noqa: BLE001 - the status code is the message
            pass
        raise SyncError(f"DataGov {what} {path}: HTTP {e.code}" + (f" ({detail})" if detail else "")) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SyncError(f"DataGov {what} {path}: {type(e).__name__}: {e}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise SyncError(f"DataGov {what} {path}: not JSON ({e})") from None


def c2_digest(s: Settings) -> dict:
    return _get(s, C2_PATH + "digest/")


def c2_documents(s: Settings) -> tuple[str, list[dict]]:
    docs, cursor, org = [], "", None
    while True:
        page = _get(s, C2_PATH, {"cursor": cursor, "limit": PAGE_LIMIT})
        if org is not None and str(page["organization_id"]) != org:
            raise SyncError("the export changed organization between pages")
        org = str(page["organization_id"])
        docs += page["documents"]
        cursor = page.get("next_cursor")
        if not cursor:
            return org, docs


def qdrant_refusal(e: BaseException) -> str | None:
    """A clear message when Qdrant refused the sync's key (401) or one of its writes (403),
    else None."""
    code = None
    try:
        from qdrant_client.http.exceptions import UnexpectedResponse
        if isinstance(e, UnexpectedResponse):
            code = e.status_code
    except ImportError:  # pragma: no cover
        pass
    if code is None:
        try:
            import grpc
            if isinstance(e, grpc.RpcError):
                code = {grpc.StatusCode.UNAUTHENTICATED: 401,
                        grpc.StatusCode.PERMISSION_DENIED: 403}.get(e.code())
        except ImportError:  # pragma: no cover
            pass
    if code == 403:
        return ("Qdrant refused a write (403 Forbidden): the sync's Qdrant key is read-only. Set "
                "LUCCY_QDRANT_SYNC_API_KEY to Qdrant's read-write API key (LUCCY_QDRANT_API_KEY, "
                "the web process's key, may stay read-only). The sync stopped at its first write; "
                "the live corpus and the luccy_kw alias are as they were")
    if code == 401:
        return ("Qdrant rejected the sync's API key (401 Unauthorized): check LUCCY_QDRANT_SYNC_API_KEY "
                "(or LUCCY_QDRANT_API_KEY when it is unset)")
    return None


def version_digest(docs: list[dict]) -> str:
    """DataGov's digest of an export: sha256 of the sorted version ids, newline-joined."""
    return hashlib.sha256("\n".join(sorted(str(d["version_id"]) for d in docs)).encode()).hexdigest()


def _m():
    from qdrant_client import models
    return models


def _org(org: str):
    m = _m()
    return m.FieldCondition(key="organization_id", match=m.MatchValue(value=org))


# The `luccy` fields with a payload index on the chunk collection.
CHUNK_INDEXED = ("luccy.source", "luccy.id")


def ensure_chunk_indexes(qc, s: Settings) -> None:
    """The `luccy` fields the live searches filter on (a non-destructive payload index)."""
    m = _m()
    if not qc.collection_exists(s.chunks):
        raise SyncError(f"chunk collection {s.chunks} does not exist")
    for field in CHUNK_INDEXED:
        qc.create_payload_index(s.chunks, field_name=field, field_schema=m.PayloadSchemaType.KEYWORD, wait=True)


# A point the engine wrote before ordinals existed (not yet backfilled by
# `python -m document_explorer.ordinals backfill`): its rows go after its page's ordered
# chunks (by point id), and its page is never fed whole (live.page_body returns "", luccy's
# "empty": passages only).
NO_ORDINAL = 10 ** 9


def scan_chunks(qc, s: Settings, org: str, versions: list[str]) -> tuple[list, Counter, set]:
    """(point id, version, page, ordinal) of every chunk point of `versions`, the chunk count
    per version, and the versions with a point that has no `ordinal`."""
    m = _m()
    points, per, unordered = [], Counter(), set()
    for i in range(0, len(versions), 64):
        flt = m.Filter(must=[_org(org), m.FieldCondition(key="version_id",
                                                          match=m.MatchAny(any=versions[i:i + 64]))])
        offset = None
        while True:
            got, offset = qc.scroll(s.chunks, scroll_filter=flt, limit=1024, offset=offset,
                                    with_payload=["version_id", "page", "ordinal"],
                                    with_vectors=False)
            for p in got:
                pl = p.payload
                ordinal = pl.get("ordinal")
                if type(ordinal) is not int:
                    unordered.add(pl["version_id"])
                    ordinal = NO_ORDINAL
                points.append((str(p.id), pl["version_id"], int(pl.get("page") or 0), ordinal))
                per[pl["version_id"]] += 1
            if offset is None:
                break
    return points, per, unordered


def has_luccy(org: str | None = None):
    m = _m()
    must = [m.FieldCondition(key="luccy.source", match=m.MatchAny(any=list(ALL_SOURCES)))]
    return m.Filter(must=([_org(org)] if org else []) + must)


def count_points(qc, s: Settings, org: str, versions) -> int:
    """Chunk points of `versions` in the chunk collection (one filtered count)."""
    m = _m()
    return qc.count(s.chunks, count_filter=m.Filter(must=[_org(org), m.FieldCondition(
        key="version_id", match=m.MatchAny(any=sorted(versions)))]), exact=True).count


def repair(qc, s: Settings, b) -> dict | None:
    """The export did not change, but chunk points may have lost their `luccy` block: the
    engine re-upserts a version's points whole (payload included) when one of them is
    missing. Re-set the block from the bundle, version by version. None when the bundle's
    point set no longer exists in the chunk collection (the caller rebuilds)."""
    blocks, ids, offset = {}, [], None
    while True:
        pts, offset = qc.scroll(b.name, limit=2048, offset=offset, with_payload=["version_id", "luccy"],
                                with_vectors=False)
        for p in pts:
            ids.append(str(p.id))
            blocks[p.payload["version_id"]] = p.payload["luccy"]
        if offset is None:
            break
    found = sum(len(qc.retrieve(s.chunks, ids=ids[i:i + 1000], with_payload=False, with_vectors=False))
                for i in range(0, len(ids), 1000))
    if found != len(ids):
        return None
    removed = remove_stale_payload(qc, s, set(blocks))
    versions = set_luccy_payload(qc, s, b.organization_id, blocks)
    return {"versions_reset": versions, "payload_removed_versions": removed}


def _unique_name(qc, base: str) -> str:
    existing = {c.name for c in qc.get_collections().collections}
    name, n = base, 1
    while name in existing:
        n += 1
        name = f"{base}_{n}"
    return name


def build_keyword(qc, s: Settings, name: str, df_name: str, points: list, luccy_of: dict,
                  doc_of: dict, encoder=None, log=None) -> dict:
    """Write the keyword and term-statistics collections; return their metadata numbers."""
    import retrieve
    m = _m()
    qc.create_collection(name, vectors_config={}, sparse_vectors_config={SPARSE: m.SparseVectorParams()})
    for field, schema in (("row", m.PayloadSchemaType.INTEGER), ("luccy.id", m.PayloadSchemaType.KEYWORD),
                          ("luccy.source", m.PayloadSchemaType.KEYWORD),
                          ("version_id", m.PayloadSchemaType.KEYWORD)):
        qc.create_payload_index(name, field_name=field, field_schema=schema, wait=True)
    qc.create_collection(df_name, vectors_config={})
    enc = encoder or retrieve.stem_encoder()
    sig, df = kwscore.Signature(), Counter()
    for i in range(0, len(points), BATCH):
        batch = points[i:i + BATCH]
        got = {str(p.id): p.payload for p in qc.retrieve(s.chunks, ids=[b[0] for b in batch],
                                                         with_payload=["index_text"], with_vectors=False)}
        vanished = {b[1] for b in batch if b[0] not in got}
        if vanished:
            raise Vanished(vanished)
        texts = [got[b[0]]["index_text"] for b in batch]
        structs = []
        for j, (b, e, text) in enumerate(zip(batch, enc.embed(texts, batch_size=256), texts)):
            lu = luccy_of[b[1]]
            sig.add(lu["id"], text)
            idx = np.asarray(e.indices, np.int64)
            val = np.asarray(e.values, np.float32)        # luccy's write_kw_stem keeps float32
            df.update(idx.tolist())
            vec = {SPARSE: m.SparseVector(indices=idx.tolist(), values=val.tolist())} if len(idx) else {}
            structs.append(m.PointStruct(id=b[0], vector=vec, payload={
                "row": i + j, "version_id": b[1], "document_id": doc_of[b[1]], "luccy": lu}))
        qc.upsert(name, points=structs, wait=True)
        if log and (i // BATCH) % 20 == 0:
            log(f"[sync] keyword rows {i + len(batch)}/{len(points)}")
    terms = sorted(df)
    for i in range(0, len(terms), 2048):
        qc.upsert(df_name, points=[m.PointStruct(id=int(t), vector={}, payload={"n": int(df[t])})
                                   for t in terms[i:i + 2048]], wait=True)
    got_n, got_t = qc.count(name, exact=True).count, qc.count(df_name, exact=True).count
    if got_n != len(points) or got_t != len(terms):
        raise SyncError(f"{name} holds {got_n}/{len(points)} chunks, {df_name} {got_t}/{len(terms)} terms")
    return {"sig": sig.hexdigest(), "n": len(points), "vocabulary": len(terms)}


def set_luccy_payload(qc, s: Settings, org: str, luccy_of: dict) -> int:
    m = _m()
    ops = [m.SetPayloadOperation(set_payload=m.SetPayload(
        payload={"luccy": lu},
        filter=m.Filter(must=[_org(org), m.FieldCondition(key="version_id", match=m.MatchValue(value=v))])))
        for v, lu in luccy_of.items()]
    for i in range(0, len(ops), 100):
        qc.batch_update_points(s.chunks, update_operations=ops[i:i + 100], wait=True)
    return len(ops)


def remove_stale_payload(qc, s: Settings, keep_versions: set) -> int:
    """Remove the `luccy` block from points of every version no longer in scope (any
    organization: a re-bound analyzer leaves nothing of the old one searchable).

    The indexed nested keys go first, then the block: Qdrant (1.19.1, tested) drops a
    point's `luccy` payload on `delete_payload(keys=["luccy"])` but keeps its `luccy.source` /
    `luccy.id` index entries, so filtered counts, scrolls and searches would still match it.
    Points left that way by an earlier run match the scroll below and are healed too."""
    m = _m()
    has = m.Filter(must=[m.FieldCondition(key="luccy.source", match=m.MatchAny(any=list(ALL_SOURCES)))])
    stale, offset = set(), None
    while True:
        got, offset = qc.scroll(s.chunks, scroll_filter=has, limit=2048, offset=offset,
                                with_payload=["version_id"], with_vectors=False)
        stale.update(p.payload["version_id"] for p in got if p.payload["version_id"] not in keep_versions)
        if offset is None:
            break
    stale = sorted(stale)
    for i in range(0, len(stale), 256):
        qc.delete_payload(s.chunks, keys=[*CHUNK_INDEXED, "luccy"], points=m.Filter(must=[
            m.FieldCondition(key="version_id", match=m.MatchAny(any=stale[i:i + 256]))]), wait=True)
    return len(stale)


def move_alias(qc, s: Settings, name: str) -> str | None:
    m = _m()
    previous = alias_target(qc, s.kw_alias)
    ops = [m.CreateAliasOperation(create_alias=m.CreateAlias(collection_name=name, alias_name=s.kw_alias))]
    if previous is not None:
        ops.insert(0, m.DeleteAliasOperation(delete_alias=m.DeleteAlias(alias_name=s.kw_alias)))
    qc.update_collection_aliases(change_aliases_operations=ops)
    return previous


def prune(qc, s: Settings, keep: set) -> list[str]:
    """Delete keyword collections (and their statistics) except `keep`."""
    names = {c.name for c in qc.get_collections().collections}
    gone = []
    for n in sorted(names):
        if n.startswith(s.kw_prefix) and n not in keep:
            qc.delete_collection(n)
            gone.append(n)
    kept_kw = {n for n in names if n.startswith(s.kw_prefix)} - set(gone)
    for n in sorted(names):
        if n.startswith(s.df_prefix) and s.kw_prefix + n[len(s.df_prefix):] not in kept_kw:
            qc.delete_collection(n)
            gone.append(n)
    return gone


def run(s: Settings | None = None, *, force: bool = False, dry_run: bool = False, qc=None,
        encoder=None, log=None) -> dict:
    """One sync. Returns the run's report.

    Raises SyncError when the export or Qdrant refuses (Qdrant's 401/403 with a message
    naming the key to set; nothing is published)."""
    log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    s = s or Settings.from_env()
    if not s.sync_token:
        raise SyncError("LUCCY_SYNC_TOKEN is not set")
    t0 = time.monotonic()
    qc = qc or client(s, sync=True)
    try:
        report = _sync(s, qc, force=force, dry_run=dry_run, encoder=encoder, log=log)
    except Exception as e:
        refused = qdrant_refusal(e)
        if refused is None:
            raise
        raise SyncError(refused) from None
    report["seconds"] = round(time.monotonic() - t0, 2)
    return report


def _layout(s: Settings, docs: list[dict], points: list, skipped: dict):
    """luccy's view of the versions with chunk points: ({version: luccy block}, {version:
    document id}, the points in luccy's row order, their (page id, page, ordinal) keys, the
    id report). A version whose points share a key is added to `skipped` and left out."""
    from ingest.redact import redact_text
    while True:
        synced = [d for d in docs if str(d["version_id"]) not in skipped]
        if not synced:
            raise SyncError("no exported document has chunk points: refusing to publish an empty corpus")
        refs, id_report = ids.assign(synced, s.upload_url_template)
        luccy_of, doc_of = {}, {}
        for d in synced:
            r = refs[d["document_id"]]
            v = str(d["version_id"])
            luccy_of[v] = {"id": r["id"], "source": r["source"], "modified": d.get("modified") or None,
                           "title": redact_text(d.get("title") or "", [])[0], "space": r["space"], "url": r["url"]}
            doc_of[v] = str(d["document_id"])
        rows = sorted((p for p in points if p[1] in luccy_of),
                      key=lambda p: (luccy_of[p[1]]["id"] + ".md", p[2], p[3], p[0]))
        keys = [(luccy_of[p[1]]["id"], p[2], p[3], p[0] if p[3] == NO_ORDINAL else "") for p in rows]
        shared = {k for k, n in Counter(keys).items() if n > 1}
        if not shared:
            return luccy_of, doc_of, rows, keys, id_report
        for p, k in zip(rows, keys):
            if k in shared:
                skipped[p[1]] = "two chunk points share one (page id, page, ordinal)"


def _sync(s: Settings, qc, *, force: bool, dry_run: bool, encoder, log) -> dict:
    """The Qdrant work of one run."""
    dg = c2_digest(s)
    org = str(dg["organization_id"])
    try:
        current = read_bundle(qc, s)
    except BundleMissing:
        current = None
    stamp = _now()
    rebuild_reason = "forced" if force else ("no bundle" if current is None else "export changed")
    if (current is not None and not force and current.digest == dg["digest"]
            and current.organization_id == org and current.chunks == s.chunks
            and qc.collection_exists(current.df_name)
            and qc.count(current.name, exact=True).count == current.n):
        arrived = count_points(qc, s, org, current.missing_versions) if current.missing_versions else 0
        if arrived:
            rebuild_reason = "versions that had no chunk points have some now"
        else:
            # The export is the same. One count says whether every chunk point of the bundle
            # still carries its `luccy` block (an engine re-upsert wipes it).
            with_block = qc.count(s.chunks, count_filter=has_luccy(org), exact=True).count
            report = {"bundle": current.name, "documents": dg.get("count"), "chunks": current.n,
                      "pages": current.pages, "digest": dg["digest"], "with_luccy_before": with_block,
                      "documents_without_chunks": len(current.missing_versions)}
            stamp_meta = {"refreshed_at": stamp.isoformat()}
            if with_block == current.n:
                if not dry_run:
                    qc.update_collection(current.name, metadata=stamp_meta)
                return {"status": "unchanged", **report, "points": with_block}
            if dry_run:
                return {"status": "dry-run", "needs": "repair", **report}
            fixed = repair(qc, s, current)
            if fixed is not None:
                points = qc.count(s.chunks, count_filter=has_luccy(org), exact=True).count
                if points == current.n:
                    qc.update_collection(current.name, metadata=stamp_meta)
                    return {"status": "repaired", **report, **fixed, "points": points}
            rebuild_reason = "chunk points changed under an unchanged export"

    org2, docs = c2_documents(s)
    if org2 != org:
        raise SyncError(f"the digest names organization {org}, the export {org2}")
    listed = Counter(str(d["version_id"]) for d in docs)
    skipped = {v: "listed twice by the export" for v, n in listed.items() if n > 1}
    ensure_chunk_indexes(qc, s)
    points, per, unordered = scan_chunks(qc, s, org, sorted(listed))
    missing = {v for v in listed if not per[v]}
    digest = version_digest(docs)
    for attempt in range(1, BUILD_ATTEMPTS + 1):
        with_points = [d for d in docs if str(d["version_id"]) not in missing]
        luccy_of, doc_of, rows, keys, id_report = _layout(s, with_points, points, skipped)
        mods = [lu["modified"] for lu in luccy_of.values() if lu["modified"]]
        report = {"status": "dry-run" if dry_run else "built", "reason": rebuild_reason,
                  "organization_id": org, "documents_in_scope": len(docs), "documents_synced": len(luccy_of),
                  "documents_without_chunks": len(missing), "missing_versions": sorted(missing)[:20],
                  "skipped_versions": dict(sorted(skipped.items())),
                  "chunks": len(rows), "pages": len({k[0] for k in keys}), "ids": id_report,
                  "versions_without_ordinal": len(unordered & set(luccy_of))}
        if skipped:
            log(f"[sync] left out {len(skipped)} versions: {sorted(set(skipped.values()))}")
        if dry_run:
            return report
        name = _unique_name(qc, s.kw_prefix + stamp.strftime("%Y%m%dT%H%M%S"))
        df_name = s.df_name(name)
        try:
            built = build_keyword(qc, s, name, df_name, rows, luccy_of, doc_of, encoder, log)
            qc.update_collection(name, metadata={
                "luccy_live": META_FORMAT, "sig": built["sig"], "n": built["n"], "pages": report["pages"],
                "vocabulary": built["vocabulary"], "organization_id": org, "chunks_collection": s.chunks,
                "df_collection": df_name, "min_mod": min(mods) if mods else "", "max_mod": max(mods) if mods else "",
                "built_at": stamp.isoformat(), "refreshed_at": stamp.isoformat(),
                "digest": digest, "missing_versions": sorted(missing),
                "documents": len(luccy_of), "kw_model": "Qdrant/bm25", "row_order": "page_id.md, page, ordinal"})
            report["payload_set_versions"] = set_luccy_payload(qc, s, org, luccy_of)
            break
        except BaseException as e:
            for n in (name, df_name):
                try:
                    if qc.collection_exists(n):
                        qc.delete_collection(n)
                except Exception as e2:  # noqa: BLE001 - the first failure is the one to report
                    log(f"[sync] could not delete {n} after the failure ({type(e2).__name__}: {str(e2)[:160]})")
            if not isinstance(e, Vanished) or attempt == BUILD_ATTEMPTS:
                raise
            # Left out of this run; the next one finds them again once they have points.
            for v in e.versions:
                skipped[v] = "chunk points vanished during the sync"
                missing.add(v)
            log(f"[sync] {e}; building again without them")
    previous = move_alias(qc, s, name)
    report["payload_removed_versions"] = remove_stale_payload(qc, s, set(luccy_of))
    report["pruned"] = prune(qc, s, {name} | ({previous} if previous else set()))
    report.update(bundle=name, previous=previous, sig=built["sig"], vocabulary=built["vocabulary"],
                  digest=digest, points=qc.count(s.chunks, count_filter=has_luccy(org), exact=True).count)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="rebuild even when the export's digest is unchanged")
    ap.add_argument("--dry-run", action="store_true", help="read and count, write nothing")
    a = ap.parse_args(argv)
    try:
        report = run(force=a.force, dry_run=a.dry_run)
    except SyncError as e:
        print(f"[sync] FAILED: {e}", file=sys.stderr, flush=True)
        print(json.dumps({"status": "failed", "error": str(e)}), flush=True)
        return 1
    print(json.dumps(report, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
