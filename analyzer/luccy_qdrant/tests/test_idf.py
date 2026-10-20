"""Does Qdrant's sparse IDF modifier reproduce luccy's kw-stem score?

luccy: sum over query terms of float32(ln((N - n + 0.5)/(n + 0.5) + 1)) * value, N and n
counted over exactly luccy's chunk set (retrieve.KwStem over kw_stem.npz, written by luccy's
own write_kw_stem). Qdrant: the same sparse values in a collection with Modifier.IDF, the
query terms at weight 1. Both over the corpus' chunks as DataGov's engine makes them.

Result (asserted below): close but NOT bit-exact (Qdrant computes the idf in float32), so the
adapter does not use the modifier: it stores raw values, sends luccy's idf as the query
weights and rescores the returned candidates exactly (kwscore.py). The adapter path is then
checked bit for bit against luccy's KwStem here, and over the whole pipeline in
test_equality.py.
"""
from __future__ import annotations

import json
import types
import uuid

import numpy as np

from .conftest import RUN
from .corpus import ORG, QUERIES, corpus


def chunks_of(docs):
    from document_explorer.config import Settings as EngineSettings
    from document_explorer.processing import prepare_pages
    es = EngineSettings(api_key="t", data_root="/tmp", qdrant_url="unused")
    out = []
    for d in docs:
        doc = types.SimpleNamespace(id=d["document_id"], version_id=d["version_id"], title=d["title"],
                                    source_type=d["source_type"], source_url=d["source_url"])
        out += prepare_pages(ORG, doc, [{"page": 1, "text": d["pages"][0]}], es, d["content_hash"])["chunks"]
    return out


def test_qdrant_idf_modifier_vs_luccy(qc, tmp_path, capsys):
    import retrieve
    from qdrant_client import models

    from luccy_qdrant import kwscore
    texts = [c["index_text"] for c in chunks_of(corpus())]
    retrieve.write_kw_stem(texts, tmp_path)                       # luccy's own keyword file
    luccy = retrieve.KwStem(tmp_path / retrieve.KW_STEM)
    enc = retrieve.stem_encoder()
    embs = list(enc.embed(texts, batch_size=256))
    name = f"{RUN}_idf_{uuid.uuid4().hex[:6]}"
    raw = name + "_raw"
    qc.create_collection(name, vectors_config={}, sparse_vectors_config={
        "bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)})
    qc.create_collection(raw, vectors_config={}, sparse_vectors_config={"bm25": models.SparseVectorParams()})
    for coll in (name, raw):
        for i in range(0, len(embs), 256):
            qc.upsert(coll, points=[models.PointStruct(
                id=j, vector={"bm25": models.SparseVector(indices=np.asarray(e.indices, np.int64).tolist(),
                                                          values=np.asarray(e.values, np.float32).tolist())},
                payload={"half": j % 2}) for j, e in enumerate(embs[i:i + 256], start=i)], wait=True)
    df = {}
    for e in embs:
        for t in np.asarray(e.indices).tolist():
            df[t] = df.get(t, 0) + 1
    n = len(texts)
    stats = {"queries": 0, "scored": 0, "bit_exact": 0, "max_abs": 0.0, "max_rel": 0.0, "max_ulps": 0,
             "top20_same": 0, "filter_changes_idf": False, "adapter_bit_exact": 0}
    values = [{int(t): np.float32(v) for t, v in zip(np.asarray(e.indices).tolist(), np.asarray(e.values, np.float32))}
              for e in embs]
    for q, _ in QUERIES:
        qe = next(iter(enc.query_embed(q)))
        terms = [int(t) for t in np.asarray(qe.indices).tolist()]
        present = [t for t in dict.fromkeys(terms) if t in df]
        want = luccy.scores(q)
        top = [int(i) for i in np.argsort(-want, kind="stable")[:20] if want[i] > 0]
        if not present:
            assert not top
            continue
        stats["queries"] += 1
        got = qc.query_points(name, query=models.SparseVector(indices=present, values=[1.0] * len(present)),
                              using="bm25", limit=n).points
        q_scores = {int(p.id): np.float32(p.score) for p in got}
        for i, s in q_scores.items():
            stats["scored"] += 1
            a, b = float(want[i]), float(s)
            stats["bit_exact"] += a == b
            stats["max_abs"] = max(stats["max_abs"], abs(a - b))
            stats["max_rel"] = max(stats["max_rel"], abs(a - b) / max(abs(a), 1e-12))
            stats["max_ulps"] = max(stats["max_ulps"], abs(int(np.float32(a).view(np.int32)) - int(np.float32(b).view(np.int32))))
        q_top = [i for i, _ in sorted(q_scores.items(), key=lambda x: (-x[1], x[0]))[:20]]
        stats["top20_same"] += q_top == top
        # IDF over the whole collection, not the filtered subset?
        half = qc.query_points(name, query=models.SparseVector(indices=present, values=[1.0] * len(present)),
                               using="bm25", limit=n, query_filter=models.Filter(must=[
                                   models.FieldCondition(key="half", match=models.MatchValue(value=0))])).points
        stats["filter_changes_idf"] |= any(np.float32(p.score) != q_scores[int(p.id)] for p in half)
        # the adapter's path: raw values, luccy's idf as weights, exact rescoring
        idf = {t: kwscore.idf32(n, df[t]) for t in present}
        cand = qc.query_points(raw, query=models.SparseVector(indices=present, values=[float(idf[t]) for t in present]),
                               using="bm25", limit=n, with_vectors=["bm25"]).points
        for p in cand:
            vec = p.vector["bm25"]
            mine = kwscore.score(terms, dict(zip(vec.indices, np.asarray(vec.values, np.float32))), idf)
            assert mine == want[int(p.id)], (q, p.id)
            assert dict(zip(vec.indices, np.asarray(vec.values, np.float32))) == values[int(p.id)]
            stats["adapter_bit_exact"] += 1
        assert sum(1 for x in want if x > 0) == len(cand)          # every scoring chunk returned
    qc.delete_collection(name)
    qc.delete_collection(raw)
    with capsys.disabled():
        print("\nIDF-REPORT " + json.dumps(stats))
    assert stats["queries"] >= 20
    assert stats["max_rel"] < 1e-5                       # the same formula ...
    assert stats["bit_exact"] < stats["scored"]          # ... but not bit for bit: not used
    assert stats["adapter_bit_exact"] > 1000             # filter_changes_idf: reported only (unused)
