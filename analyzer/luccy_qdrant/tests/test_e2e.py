"""/ask end to end over the live adapter: luccy's own web app, stub LLMs, no file corpus."""
from __future__ import annotations

import json

import pytest

from .conftest import QDRANT_RO_KEY, engine_upsert, live_env, run_module, settings_for
from .corpus import ORG, corpus, multi_page_doc


@pytest.fixture(scope="module")
def e2e(qc, tmp_path_factory):
    work = tmp_path_factory.mktemp("e2e")
    s = settings_for("e2e", "http://unused")
    engine_upsert(qc, s.chunks, corpus() + [multi_page_doc()], work, ORG)
    from .e2e_app import KEY
    env = {**live_env(s), "SERVING_LOG_DIR": str(work / "logs"), "LUCCY_API_KEY": KEY,
           "LUCCY_ROUTE_FIRST_TURN": "0", "LUCCY_DEEP_FALLBACK": "0", "LUCCY_VECTOR_STORE": "qdrant"}
    run_module("luccy_qdrant.tests.e2e_app", ["--out", str(work / "e2e.json")], env)
    return json.loads((work / "e2e.json").read_text())


def test_health_before_and_after_first_sync(e2e):
    before = e2e["health_before_sync"]
    assert before["status"] == 503
    assert any("alias missing: run analyzer-sync" in r for r in before["body"]["reasons"])
    h = e2e["health"]
    assert h["status"] == 200, h
    body = h["body"]
    assert body["status"] == "ok" and body["models_loaded"] is True
    assert body["index"]["sig"] == e2e["sync"]["sig"] and body["index"]["chunk_count"] == e2e["sync"]["chunks"]
    assert body["corpus_pages"] == e2e["sync"]["pages"] and body["qdrant"]["alias"] == e2e["sync"]["bundle"]
    from serving.api.schemas import HealthResponse
    HealthResponse.model_validate(body)
    HealthResponse.model_validate(before["body"])


def test_serving_never_writes_to_qdrant(e2e):
    """The whole web process (start-up, /health, /ask, the stream, the viewer, feedback, the
    bundle pickup) made no Qdrant write; only the sync did. Against a two-key Qdrant it ran on
    the read-only key, which Qdrant refuses any write."""
    assert e2e["serve_writes"] == []
    if QDRANT_RO_KEY:
        assert e2e["serving_key"] == "read-only" and e2e["serving_write_refused"] == 403


def test_ask_answers_from_the_live_corpus(e2e):
    ask = e2e["ask"]
    assert ask["status"] == 200, ask
    b = ask["body"]
    assert b["citations"] and b["citations"][0]["id"] and not b["abstained"]
    assert "](" in b["answer"]                                   # luccy's own link render
    assert "documents as of" in b["footer"]
    assert e2e["ask_unauthorized"] == 401
    assert e2e["stream_events"][0] == "stage" and e2e["stream_events"][-1] == "done"
    assert {"plan", "pages", "answer"} <= set(e2e["stream_events"])


def test_document_viewer_and_feedback(e2e):
    d = e2e["document"]
    assert d["status"] == 200 and d["body"]["body_html"] and d["body"]["passages"]
    assert d["body"]["source"] in ("Confluence", "Jira", "Google Drive", "GitHub", "Power BI")
    pdf = e2e["pdf"]
    assert pdf["status"] == 200 and pdf["body"]["source"] == "Google Drive"
    from document_explorer.stitch import normalize
    import re
    text = normalize(re.sub(r"<[^>]+>", " ", pdf["body"]["body_html"]))
    for page in pdf["pages"]:                                    # all three PDF pages, in order
        assert normalize(page.split("\n\n")[0])[:120] in text
    assert e2e["unknown_document"] == 404
    assert e2e["feedback"] == {"ok": True} and e2e["record"] == 200


def test_bundle_pickup_and_no_document_files(e2e):
    assert e2e["sync2"]["status"] == "built" and e2e["sync2"]["previous"] == e2e["sync"]["bundle"]
    assert e2e["identity_before"] != e2e["identity_after"] and e2e["bundle_moved"] is True
    assert e2e["health_after_move"] == 200                       # the previous bundle is kept
    assert not e2e["pages_dir_exists"] and not e2e["index_dir_exists"]
    # only luccy's operational logs were written: answer records, feedback, the spend ledger
    for f in e2e["log_files"]:
        assert f.startswith(("20", "feedback/", "conversations.json")), f


def test_report_numbers(e2e, capsys):
    with capsys.disabled():
        print("\nE2E-REPORT " + json.dumps({k: e2e[k] for k in ("sync_s", "ready_after_sync_s", "ask_s")}
                                           | {"chunks": e2e["sync"]["chunks"], "hooks": len(e2e["hooks"])}))
