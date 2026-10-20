"""Unit checks that need no Qdrant: luccy ids (C3), pins, caches, the scan guard, the image."""
from __future__ import annotations

import re
import types
from pathlib import Path

import numpy as np
import pytest

ANALYZER = Path(__file__).resolve().parents[2]


def row(external_id, source_type, url="", document_id="0b6c2a7e-6f4f-4b55-9d0e-2b1f3f6c9a11", **kw):
    return {"external_id": external_id, "document_id": document_id, "version_id": "v", "source_type": source_type,
            "source_url": url, "collection_name": kw.get("collection_name", "Docs"), "group": "G"}


def test_luccy_id_for_follows_luccys_rules():
    import retrieve

    from luccy_qdrant.ids import luccy_id_for, luccy_ref_for, valid
    wiki = "https://x.atlassian.net/wiki/spaces/COM/pages/6857523203"
    cases = [
        (row("confluence:6857523203", "confluence", wiki), "6857523203", "confluence"),
        (row("confluence:6857523203:part:2", "confluence", wiki), "6857523203-part-2", "confluence"),
        (row("jira:AP-520", "jira", "https://x.atlassian.net/browse/AP-520"), "AP-520", "jira"),
        (row("upload:a.pdf", "upload"), "gd-dg-0b6c2a7e-6f4f-4b55-9d0e-2b1f3f6c9a11", "drive"),
        (row("github:pr:1", "github", "https://github.com/w/r/pull/1"), "dg-0b6c2a7e-6f4f-4b55-9d0e-2b1f3f6c9a11", "github"),
        (row("dbt:m.yml", "dbt", ""), "gd-dg-0b6c2a7e-6f4f-4b55-9d0e-2b1f3f6c9a11", "drive"),
        (row("powerbi:a:b", "powerbi"), "dg-0b6c2a7e-6f4f-4b55-9d0e-2b1f3f6c9a11", "powerbi"),
        (row("confluence:6857523203:part:2", "confluence", ""), "gd-dg-0b6c2a7e-6f4f-4b55-9d0e-2b1f3f6c9a11", "drive"),
        (row("jira:ap-1", "jira", ""), "dg-0b6c2a7e-6f4f-4b55-9d0e-2b1f3f6c9a11", "jira"),
        # 1-3 digit ids read as evidence ranks in luccy's citation remap: never a page id
        (row("confluence:123", "confluence", wiki), "dg-0b6c2a7e-6f4f-4b55-9d0e-2b1f3f6c9a11", "confluence"),
    ]
    for doc, want_id, want_source in cases:
        ref = luccy_ref_for(doc)
        assert (luccy_id_for(doc), ref["source"]) == (want_id, want_source), doc
        assert valid(ref["id"]) and retrieve.source_of(ref["id"], ref["space"], ref["url"]) == ref["source"]
    assert luccy_ref_for(row("powerbi:a:b", "powerbi"))["space"] == "PBI"
    assert luccy_ref_for(row("jira:AP-520", "jira"))["space"] == "AP"
    assert luccy_id_for(row("JIRA:PHOENIX-9428", "jira")) == "PHOENIX-9428"          # any prefix case
    assert luccy_id_for(row("Confluence:6836781135:Part:3", "confluence", wiki)) == "6836781135-part-3"
    # luccy's citation scanners read the whole id: no ":" (C3's `<id>:part:<n>` would be cut)
    from serving import claim_ground
    m = claim_ground._DOC_REF.search("as stated (doc 6857523203-part-2).")
    assert m.group(1) == "6857523203-part-2"
    assert claim_ground._DOC_REF.search("(doc 6857523203:part:2)").group(1) == "6857523203"


def test_assign_resolves_collisions_deterministically():
    from luccy_qdrant.ids import assign
    wiki = "https://x.atlassian.net/wiki/x"
    a = row("confluence:6857523203", "confluence", wiki, document_id="11111111-0000-4000-8000-000000000000")
    b = row("confluence:6857523203", "confluence", wiki, document_id="22222222-0000-4000-8000-000000000000")
    refs, report = assign([b, a])
    assert refs[a["document_id"]]["id"] == "6857523203"
    assert refs[b["document_id"]]["id"] == f"dg-{b['document_id']}" and report["collisions"] == 1


def test_assign_agrees_with_luccy_id_for_in_any_order():
    """The stable API: what the sync stores (assign) is what luccy_id_for gives one row."""
    from luccy_qdrant.ids import assign, luccy_id_for
    wiki = "https://x.atlassian.net/wiki/spaces/COM/pages/6857523203"
    rows = [row("confluence:6857523203", "confluence", wiki, document_id="11111111-0000-4000-8000-000000000000"),
            row("confluence:6857523203:part:2", "confluence", wiki, document_id="22222222-0000-4000-8000-000000000000"),
            row("jira:AP-520", "jira", "https://x.atlassian.net/browse/AP-520",
                document_id="33333333-0000-4000-8000-000000000000"),
            row("upload:a.pdf", "upload", document_id="44444444-0000-4000-8000-000000000000"),
            row("github:pr:1", "github", "https://github.com/w/r/pull/1",
                document_id="55555555-0000-4000-8000-000000000000"),
            row("powerbi:a:b", "powerbi", document_id="66666666-0000-4000-8000-000000000000")]
    refs, report = assign(rows)
    assert {d: r["id"] for d, r in refs.items()} == {r["document_id"]: luccy_id_for(r) for r in rows}
    assert assign(list(reversed(rows)))[0] == refs and report["collisions"] == 0
    linked = assign(rows, "https://datagov.example/documents/{document_id}")[0]
    upload = "44444444-0000-4000-8000-000000000000"
    assert linked[upload]["id"] == refs[upload]["id"] and linked[upload]["url"].endswith(upload)


def test_sync_key_falls_back_to_the_serving_key(monkeypatch):
    from luccy_qdrant.settings import Settings
    monkeypatch.setenv("LUCCY_QDRANT_API_KEY", "read-only-key")
    monkeypatch.delenv("LUCCY_QDRANT_SYNC_API_KEY", raising=False)
    s = Settings.from_env()
    assert (s.qdrant_api_key, s.sync_key) == ("read-only-key", "read-only-key")
    monkeypatch.setenv("LUCCY_QDRANT_SYNC_API_KEY", "read-write-key")
    s = Settings.from_env()
    assert (s.qdrant_api_key, s.sync_key) == ("read-only-key", "read-write-key")


def test_serving_code_never_calls_a_qdrant_write():
    """Static half of the read-only audit (the dynamic half is the write spy in the e2e and
    equality runs, and test_keys.py): no module the web process loads names a Qdrant write;
    every write is in sync.py."""
    from .fakes import QDRANT_WRITES
    pkg = ANALYZER / "luccy_qdrant"
    calls = re.compile(r"\.(" + "|".join(QDRANT_WRITES) + r")\(")
    for name in ("app.py", "serve.py", "hooks.py", "live.py", "settings.py", "kwscore.py", "ids.py", "pins.py"):
        assert not calls.findall((pkg / name).read_text(encoding="utf-8")), name
    assert calls.findall((pkg / "sync.py").read_text(encoding="utf-8"))


def test_pins_match_and_detect_a_change(monkeypatch, capsys):
    import retrieve

    from luccy_qdrant import hooks, pins
    hooks.verify()
    monkeypatch.setitem(pins.PINS, "retrieve:KwStem.scores", "0" * 16)
    with pytest.raises(RuntimeError, match="retrieve:KwStem.scores"):
        hooks.verify()                                   # the image build
    monkeypatch.setitem(hooks._STATE, "warnings", [])
    hooks.verify(strict=False)                           # at run time: a loud warning only
    assert "WARNING" in capsys.readouterr().err and "retrieve:KwStem.scores" in hooks._STATE["warnings"][0]
    # The container's own settings still refuse to start, strict or not.
    monkeypatch.setenv("LUCCY_KW_STEM", "0")
    with pytest.raises(RuntimeError, match="LUCCY_KW_STEM"):
        hooks.verify(routes=False, strict=False)
    monkeypatch.setenv("LUCCY_KW_STEM", "1")
    monkeypatch.setattr(retrieve, "VECTOR_STORE", "npy")
    with pytest.raises(RuntimeError, match="LUCCY_VECTOR_STORE"):
        hooks.verify(routes=False, strict=False)


def test_kwscore_is_luccys_arithmetic(tmp_path):
    import retrieve

    from luccy_qdrant import kwscore
    rng = np.random.default_rng(3)
    words = [f"word{i}" for i in range(60)]
    texts = [" ".join(rng.choice(words, size=rng.integers(5, 40))) for _ in range(300)]
    retrieve.write_kw_stem(texts, tmp_path)
    luccy = retrieve.KwStem(tmp_path / retrieve.KW_STEM)
    enc = retrieve.stem_encoder()
    embs = list(enc.embed(texts, batch_size=256))
    df = {}
    for e in embs:
        for t in np.asarray(e.indices).tolist():
            df[t] = df.get(t, 0) + 1
    for q in ("word1 word2 word3", "word7", "word10 word11 word12 word13 word14 word59", "nothing"):
        want = luccy.scores(q)
        terms = [int(t) for t in next(iter(enc.query_embed(q))).indices.tolist()]
        idf = {t: kwscore.idf32(len(texts), df[t]) for t in terms if t in df}
        for i, e in enumerate(embs):
            vals = dict(zip(np.asarray(e.indices).tolist(), np.asarray(e.values, np.float32)))
            assert kwscore.score(terms, vals, idf) == want[i]


def test_lru_bounds():
    from luccy_qdrant.live import LRU, MISSING
    c = LRU(3, max_weight=10)
    for i in range(5):
        c.put(i, i, weight=2)
    assert len(c) == 3 and c.get(0) is MISSING and c.get(4) == 4
    c.put("big", "x", weight=9)
    assert len(c) == 1 and c.get("big") == "x"


def test_chunk_list_refuses_a_full_scan():
    from luccy_qdrant.live import LiveChunks
    live = types.SimpleNamespace(bundle=types.SimpleNamespace(n=5), local=types.SimpleNamespace())
    chunks = LiveChunks(live)
    assert len(chunks) == 5
    with pytest.raises(RuntimeError, match="whole chunk list"):
        list(chunks)
    with pytest.raises(IndexError):
        chunks[5]
    with pytest.raises(TypeError):
        chunks[1:3]


def test_image_contract():
    """What the image needs, in any order and with any comments."""
    text = (ANALYZER / "Dockerfile").read_text()
    code = " ".join(ln.strip().rstrip("\\") for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))
    for required in ("FROM ${LUCCY_IMAGE}", "COPY luccy_qdrant /opt/datagov-analyzer/luccy_qdrant",
                     "PYTHONPATH=/opt/datagov-analyzer:/app", "LUCCY_KW_STEM=1", "LUCCY_VECTOR_STORE=qdrant",
                     "RUN python -m luccy_qdrant.hooks --app", 'CMD ["python", "-m", "luccy_qdrant.serve"]'):
        assert required in code, required
    assert "ENTRYPOINT" not in code
    assert code.index("COPY luccy_qdrant") < code.index("RUN python -m luccy_qdrant.hooks")
    assert (ANALYZER / "luccy_qdrant" / "STORAGE_MAP.md").is_file()


def test_serve_runs_luccys_gunicorn_on_the_hooked_app():
    """The flags luccy's serving relies on: one uvicorn worker (the job table and the loaded
    corpus live in one process), its long timeouts, and the port."""
    from luccy_qdrant import serve
    args = serve.argv("8787")
    assert args[:2] == ["gunicorn", "luccy_qdrant.app:app"]
    flags = dict(zip(args[2::2], args[3::2]))
    assert flags["--worker-class"] == "uvicorn_worker.UvicornWorker" and flags["--workers"] == "1"
    assert int(flags["--timeout"]) >= 900 and int(flags["--graceful-timeout"]) >= 300
    assert flags["--bind"] == "0.0.0.0:8787"
