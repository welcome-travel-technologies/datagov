"""Shared fixtures: a throwaway Qdrant (QDRANT_TEST_URL), DataGov engine chunk points made with
the engine's own `prepare_pages` + `VectorIndex.upsert`, a fake corpus export and the sync.

Run inside the luccy image with the engine worktree mounted at /src, e.g.
    PYTHONPATH=/analyzer:/src:/app QDRANT_TEST_URL=http://luccy-live-qdrant:6333 \
        python -m pytest -q -p no:cacheprovider luccy_qdrant/tests

Two-key mode: against a Qdrant started with QDRANT__SERVICE__API_KEY and
QDRANT__SERVICE__READ_ONLY_API_KEY, also set QDRANT_TEST_API_KEY (read-write) and
QDRANT_TEST_READ_ONLY_API_KEY. Then every sync runs with the read-write key
(LUCCY_QDRANT_SYNC_API_KEY) and every live process (battery, e2e app) with the read-only one
(LUCCY_QDRANT_API_KEY), and test_keys.py runs.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest

QDRANT = os.environ.get("QDRANT_TEST_URL")
QDRANT_KEY = os.environ.get("QDRANT_TEST_API_KEY", "")                 # read-write
QDRANT_RO_KEY = os.environ.get("QDRANT_TEST_READ_ONLY_API_KEY", "")    # read-only
RUN = "t" + uuid.uuid4().hex[:6]
TOKEN = "sync-token"
KEYS = ("conf0", "conf2", "conf4", "big0", "qbr0", "part2", "jira0", "jira3", "pr812", "dbt2", "pbi1", "up0")


@pytest.fixture(scope="session")
def qc():
    if not QDRANT:
        pytest.skip("QDRANT_TEST_URL names no Qdrant server")
    from qdrant_client import QdrantClient
    client = QdrantClient(url=QDRANT, api_key=QDRANT_KEY or None, timeout=60)
    yield client
    for c in client.get_collections().collections:
        if c.name.startswith(RUN) or c.name.startswith("document_chunks_pinned_" + RUN):
            client.delete_collection(c.name)


def engine_upsert(qc, collection: str, docs: list[dict], work: Path, organization: str) -> int:
    """DataGov's engine path for each version: prepare_pages (luccy's chunk_body per page,
    redaction, chunk ids), the stored vectors, VectorIndex.upsert (adds `ordinal`)."""
    from document_explorer.config import Settings as EngineSettings
    from document_explorer.index import VectorIndex
    from document_explorer.processing import prepare_pages

    from .fakes import engine_vectors
    es = EngineSettings(api_key="test", data_root=work, qdrant_url=QDRANT, collection=collection)
    index = VectorIndex(es, client=qc)
    index.ensure()
    n = 0
    for d in docs:
        document = types.SimpleNamespace(id=d["document_id"], version_id=d["version_id"], title=d["title"],
                                         source_type=d["source_type"], source_url=d["source_url"])
        art = prepare_pages(organization, document, [{"page": i + 1, "text": t} for i, t in enumerate(d["pages"])],
                            es, d["content_hash"])
        index.upsert(art["chunks"], engine_vectors(art["chunks"]))
        n += len(art["chunks"])
    return n


def settings_for(name: str, c2_url: str):
    """Serving reads with the read-only key, the sync writes with the read-write key (both ""
    against a Qdrant without keys)."""
    from luccy_qdrant.settings import Settings
    return dataclasses.replace(Settings.from_env(), qdrant_url=QDRANT,
                               qdrant_api_key=QDRANT_RO_KEY or QDRANT_KEY, qdrant_sync_api_key=QDRANT_KEY,
                               chunks=f"{RUN}_{name}_chunks", kw_alias=f"{RUN}{name}",
                               datagov_url=c2_url, sync_token=TOKEN)


def live_env(s) -> dict:
    """The environment a live process needs for settings `s`."""
    return {"LUCCY_QDRANT_URL": s.qdrant_url, "LUCCY_QDRANT_API_KEY": s.qdrant_api_key,
            "LUCCY_QDRANT_SYNC_API_KEY": s.qdrant_sync_api_key, "LUCCY_LIVE_CHUNKS_COLLECTION": s.chunks,
            "LUCCY_LIVE_KW_ALIAS": s.kw_alias, "DATAGOV_URL": s.datagov_url, "LUCCY_SYNC_TOKEN": s.sync_token}


@pytest.fixture
def world(qc, tmp_path):
    """Its own chunk collection (the engine's path), alias and fake export, for one test."""
    from .corpus import ORG, corpus, multi_page_doc
    from .fakes import C2Server
    docs = [d for d in corpus() if d["key"] in KEYS] + [multi_page_doc()]
    c2 = C2Server(ORG, docs)
    name = "s" + uuid.uuid4().hex[:5]
    s = settings_for(name, c2.url)
    n = engine_upsert(qc, s.chunks, docs, tmp_path, ORG)
    yield types.SimpleNamespace(s=s, c2=c2, docs=docs, n=n, tmp=tmp_path)
    c2.close()


def run_module(module: str, args: list[str], env: dict, timeout: int = 2400) -> subprocess.CompletedProcess:
    full = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "LUCCY_QDRANT_PREFER_GRPC": "0",
            "LUCCY_VECTOR_SEARCH": "exact", **env}
    done = subprocess.run([sys.executable, "-m", module, *args], env=full, capture_output=True, text=True,
                          timeout=timeout)
    if done.returncode != 0:
        raise AssertionError(f"{module} {' '.join(args)} failed ({done.returncode}):\n"
                             f"{done.stderr[-6000:]}\n{done.stdout[-2000:]}")
    return done


def write_file_corpus(work: Path, docs: list[dict], luccy_by_doc: dict[str, dict]) -> list[str]:
    """luccy's page files and catalog for the same documents: frontmatter from the bundle's
    `luccy` block, body = the document's (single) page text, exactly what DataGov chunked."""
    pages = work / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    rows, ids = [], []
    for d in docs:
        lu = luccy_by_doc[d["document_id"]]
        assert len(d["pages"]) == 1
        fm = [f"id: {lu['id']}", f"title: {lu['title']}", f"url: {lu['url'] or ''}", f"space: {lu['space']}",
              f"modified: {lu['modified'] or ''}"]
        (pages / f"{lu['id']}.md").write_text("---\n" + "\n".join(fm) + "\n---\n" + d["pages"][0] + "\n",
                                             encoding="utf-8")
        rows.append({"id": lu["id"], "title": lu["title"], "space": lu["space"], "url": lu["url"] or None,
                     "modified": lu["modified"] or ""})
        ids.append(lu["id"])
    (work / "catalog.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (work / "ids.json").write_text(json.dumps(sorted(ids)))
    return ids


def luccy_blocks(qc, s) -> dict[str, dict]:
    """{document_id: luccy block} as the sync wrote it on the chunk points."""
    from qdrant_client import models
    out, offset = {}, None
    flt = models.Filter(must=[models.FieldCondition(key="luccy.source", match=models.MatchAny(
        any=["confluence", "jira", "drive", "github", "powerbi"]))])
    while True:
        pts, offset = qc.scroll(s.chunks, scroll_filter=flt, limit=1024, offset=offset,
                                with_payload=["document_id", "luccy"], with_vectors=False)
        for p in pts:
            out[p.payload["document_id"]] = p.payload["luccy"]
        if offset is None:
            return out
