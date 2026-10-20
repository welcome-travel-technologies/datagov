"""End to end, in its own process: luccy's web app (luccy_qdrant.app = hooks + luccy's app)
over the live corpus, stub LLMs, the fake export. Writes what it saw as JSON for test_e2e.py.

    python -m luccy_qdrant.tests.e2e_app --out result.json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import time
from pathlib import Path

KEY = "e2e-bearer-key-0123456789abcdef0123456789"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    from .corpus import ORG, PLANS, corpus, multi_page_doc
    from .fakes import C2Server, StubPlanner, StubWriter, install_fake_embedders, install_write_spy
    docs = corpus() + [multi_page_doc()]
    c2 = C2Server(ORG, docs)
    out: dict = {}

    serve_writes = install_write_spy()               # every Qdrant write not made by the sync
    install_fake_embedders()
    from luccy_qdrant.app import app                 # hooks first, then luccy's app
    from luccy_qdrant import hooks, sync
    from luccy_qdrant.settings import Settings
    from serving import driver, jobs
    jobs.WARM_RETRY_FIRST_S = 1
    jobs._build_svc = lambda: driver.AnswerService(llm=StubWriter(), plan_llm=StubPlanner(PLANS),
                                                   run_warehouse=False)
    s = dataclasses.replace(Settings.from_env(), datagov_url=c2.url, sync_token="sync-token")
    from fastapi.testclient import TestClient
    auth = {"Authorization": f"Bearer {KEY}"}
    with TestClient(app) as client:
        h = client.get("/health")
        out["health_before_sync"] = {"status": h.status_code, "body": h.json()}
        t0 = time.monotonic()
        out["sync"] = sync.run(s)
        out["sync_s"] = round(time.monotonic() - t0, 2)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:             # luccy's own load retry picks the bundle up
            h = client.get("/health")
            if h.status_code == 200:
                break
            time.sleep(0.5)
        out["ready_after_sync_s"] = round(time.monotonic() - t0, 2)
        out["health"] = {"status": h.status_code, "body": h.json()}

        t0 = time.monotonic()
        r = client.get("/ask", params={"question": "Why was the refund not issued after the cancellation, "
                                                   "and what does the policy say?"}, headers=auth)
        out["ask_s"] = round(time.monotonic() - t0, 2)
        out["ask"] = {"status": r.status_code, "body": r.json()}
        out["ask_unauthorized"] = client.get("/ask", params={"question": "x"}).status_code
        events = []
        with client.stream("POST", "/ask/stream", headers=auth,
                           json={"question": "Walk me through the partner contract renewal checklist.",
                                 "sources": ["drive", "confluence"]}) as resp:
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
                    if events[-1] in ("done", "error"):
                        break
        out["stream_events"] = events
        log = out["ask"]["body"].get("log")
        cited = (out["ask"]["body"].get("citations") or [{}])[0].get("id")
        d = client.get(f"/documents/{cited}", params={"answer": log}, headers=auth)
        out["document"] = {"status": d.status_code, "body": d.json()}
        pdf = next(x for x in docs if x["key"] == "pdf3")
        d2 = client.get(f"/documents/gd-dg-{pdf['document_id']}", headers=auth)
        out["pdf"] = {"status": d2.status_code, "body": d2.json(), "pages": pdf["pages"]}
        out["unknown_document"] = client.get("/documents/1234", headers=auth).status_code
        out["feedback"] = client.post("/feedback", headers=auth,
                                      json={"answer_id": log, "verdict": "right", "note": "good"}).json()
        out["record"] = client.get(f"/records/{log}", headers=auth).status_code

        # a sync that moves the alias: luccy's own bundle pickup sees it
        import retrieve
        r0 = jobs._svc._retriever
        out["identity_before"] = [str(x) for x in r0.bundle]
        c2.documents = [x for x in c2.documents if x["key"] != "conf3"]
        out["sync2"] = sync.run(s)
        jobs._bundle_checked = 0.0
        out["identity_after"] = [str(x) for x in retrieve.bundle_identity()]
        out["bundle_moved"] = jobs._bundle_moved()
        out["health_after_move"] = client.get("/health").status_code
        out["serve_writes"] = list(serve_writes)
        out["serving_key"] = "read-only" if hooks.settings().qdrant_api_key == os.environ.get(
            "QDRANT_TEST_READ_ONLY_API_KEY", "\0") else "other"
        if out["serving_key"] == "read-only":
            # The web process's own client really holds a read-only key: Qdrant refuses it a write.
            from qdrant_client.http.exceptions import UnexpectedResponse
            try:
                hooks.current_live().qc.create_collection("e2e_write_probe", vectors_config={})
                out["serving_write_refused"] = None
            except UnexpectedResponse as e:
                out["serving_write_refused"] = e.status_code
    log_dir = Path(os.environ["SERVING_LOG_DIR"])
    out["log_files"] = sorted(str(p.relative_to(log_dir)) for p in log_dir.rglob("*") if p.is_file())
    out["pages_dir_exists"] = Path(retrieve.PAGES).exists()
    out["index_dir_exists"] = Path(retrieve.INDEX).exists()
    out["hooks"] = [t for t, _, _ in hooks.HOOKS]
    c2.close()
    a.out.write_text(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
