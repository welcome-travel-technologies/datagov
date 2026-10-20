"""Test doubles: a deterministic embedder (the same vectors in both modes), stub LLMs that
record the prompts they get, and a fake DataGov corpus export (contract C2). No network
beyond the throwaway Qdrant, no paid call."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

DIMS = 768
TOKEN = re.compile(r"[a-z0-9]+")


@lru_cache(maxsize=65536)
def _token_vec(token: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "little")
    return np.random.default_rng(seed).standard_normal(DIMS)


def fake_vector(text: str) -> np.ndarray:
    """A unit float32 vector: a hashed bag of words (texts sharing words are near) plus a
    small component unique to the text (no exact ties)."""
    v = np.zeros(DIMS)
    for tok in TOKEN.findall(text.lower()):
        v += _token_vec(tok)
    v += 0.3 * _token_vec("\0" + hashlib.sha256(text.encode()).hexdigest())
    v = v.astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


class FakeGeminiEmbedder:
    """Stands in for ingest.gemini_embed.GeminiEmbedder in luccy's own index build."""

    def __init__(self, client=None, dims=DIMS, per_minute=0):
        self.client, self.dims = client, dims

    def _embed(self, texts):
        return np.stack([fake_vector(t) for t in texts]), 0

    def call(self, texts):
        return self._embed(texts)[0]


class FakeQueryEmbedder:
    """Stands in for ingest.gemini_embed.QueryEmbedder: the query form luccy embeds."""

    def __init__(self, timeout_s=5.0, log_dir=None, dims=DIMS, client=None):
        pass

    def vectors(self, queries):
        from ingest.gemini_embed import query_text
        return {q: fake_vector(query_text(q)) for q in queries}


def install_fake_embedders() -> None:
    from ingest import gemini_embed
    gemini_embed.GeminiEmbedder = FakeGeminiEmbedder
    gemini_embed.QueryEmbedder = FakeQueryEmbedder
    gemini_embed.client_from_env = lambda timeout_s=None: None


def engine_vectors(chunks) -> np.ndarray:
    """The vectors DataGov's engine stores: the embedder over luccy's doc_text form."""
    from ingest.gemini_embed import doc_text
    return np.stack([fake_vector(doc_text(c["title"], c["index_text"])) for c in chunks])


DOC_ID = re.compile(r"doc_id=(\S+)")


class StubPlanner:
    """The planner (decompose) on the no-key path: a fixed plan per question."""
    model, api_key = "stub-planner", ""

    def __init__(self, plans: dict[str, list[str]]):
        self.plans, self.prompts = plans, []

    def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        question = prompt.rsplit("## Question\n\n", 1)[1].strip()
        queries = self.plans.get(question) or [question]
        return {"text": json.dumps({"queries": queries, "warehouse": [], "warehouse_gap": False}),
                "usage": {"input_tokens": 10, "output_tokens": 10}, "finish_reason": "STOP",
                "model": self.model, "latency_s": 0.0}


class StubWriter:
    """The writer on the no-key path: records each prompt, cites the first evidence page."""
    model, api_key = "stub-writer", ""

    def __init__(self):
        self.prompts = []
        self.lock = threading.Lock()

    def complete(self, prompt, **kw):
        with self.lock:
            self.prompts.append(prompt)
        ids = DOC_ID.findall(prompt.split("## Evidence", 1)[-1])     # not the prompt's examples
        cite = ids[:1]
        answer = (f"The documents describe this in detail (doc {cite[0]})." if cite
                  else "The documents do not cover this.")
        return {"text": json.dumps({"answer": answer, "citations": cite, "unanswerable_parts": []}),
                "usage": {"input_tokens": len(prompt) // 4, "output_tokens": 20}, "finish_reason": "STOP",
                "model": self.model, "latency_s": 0.0}


class C2Server:
    """DataGov's corpus export (contract C2) over a mutable document list."""

    def __init__(self, organization_id: str, documents: list[dict], token: str = "sync-token",
                 host: str = "127.0.0.1", port: int = 0):
        self.organization_id, self.documents, self.token = organization_id, list(documents), token
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, {"detail": "Invalid service credentials."})
                url = urlparse(self.path)
                base = "/api/document-analyzer/internal/documents/"
                docs = sorted(outer.documents, key=lambda d: d["document_id"])
                if url.path == base + "digest/":
                    versions = sorted(d["version_id"] for d in docs)
                    return self._send(200, {"organization_id": outer.organization_id, "count": len(versions),
                                            "digest": hashlib.sha256("\n".join(versions).encode()).hexdigest()})
                if url.path == base:
                    q = parse_qs(url.query)
                    limit = int((q.get("limit") or ["200"])[0])
                    cursor = (q.get("cursor") or [""])[0]
                    rest = [d for d in docs if d["document_id"] > cursor] if cursor else docs
                    page, more = rest[:limit], len(rest) > limit
                    return self._send(200, {"organization_id": outer.organization_id,
                                            "generated_at": "2026-10-16T00:00:00+00:00", "total": len(docs),
                                            "next_cursor": page[-1]["document_id"] if more else None,
                                            "documents": [export_row(d) for d in page]})
                return self._send(404, {"detail": "Not found."})

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def export_row(d: dict) -> dict:
    """One C2 `documents` row (identities only, never text)."""
    return {"document_id": d["document_id"], "version_id": d["version_id"],
            "content_hash": d["content_hash"], "pipeline_version": "",
            "external_id": d["external_id"], "title": d["title"], "source_type": d["source_type"],
            "source_url": d["source_url"] or None, "collection_id": d["collection_id"],
            "collection_name": d["collection_name"], "group": d["group"], "modified": d["modified"]}


# Every Qdrant client method that changes the server (collections, points, payload, payload
# indexes, aliases, snapshots, shard keys).
QDRANT_WRITES = ("upsert", "update_vectors", "delete_vectors", "delete", "set_payload", "overwrite_payload",
                 "delete_payload", "clear_payload", "batch_update_points", "update_collection_aliases",
                 "create_collection", "recreate_collection", "update_collection", "delete_collection",
                 "create_payload_index", "delete_payload_index", "upload_points", "upload_collection",
                 "upload_records", "create_snapshot", "delete_snapshot", "create_full_snapshot",
                 "delete_full_snapshot", "recover_snapshot", "create_shard_key", "delete_shard_key",
                 "create_shard_snapshot", "delete_shard_snapshot", "recover_shard_snapshot")


def install_write_spy() -> list[str]:
    """Record every Qdrant write this process makes from anywhere but the sync job
    (luccy_qdrant/sync.py on the stack), through either client class (the adapter's
    QdrantClient, luccy's AsyncQdrantClient wrapper). Returns the live list."""
    import inspect
    import traceback

    from qdrant_client import AsyncQdrantClient, QdrantClient
    calls: list[str] = []

    def outside_sync(name):
        stack = traceback.extract_stack()[:-2]
        if any(f.filename.replace("\\", "/").endswith("luccy_qdrant/sync.py") for f in stack):
            return
        where = next((f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in reversed(stack)
                      if "qdrant_client" not in f.filename), "?")
        calls.append(f"{name} from {where}")

    def wrap(cls, name):
        orig = getattr(cls, name, None)
        if orig is None:
            return
        if inspect.iscoroutinefunction(orig):
            async def spy(self, *a, **k):
                outside_sync(name)
                return await orig(self, *a, **k)
        else:
            def spy(self, *a, **k):
                outside_sync(name)
                return orig(self, *a, **k)
        setattr(cls, name, spy)

    for cls in (QdrantClient, AsyncQdrantClient):
        for name in QDRANT_WRITES:
            wrap(cls, name)
    return calls
