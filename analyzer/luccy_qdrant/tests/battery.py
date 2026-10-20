"""Run the same measurements over luccy's FILE index or over the LIVE adapter (one process per
mode, so the hooks never touch the file run), and write them as JSON for test_equality.py.

    python -m luccy_qdrant.tests.battery --mode build --work DIR   # luccy's own index build
    python -m luccy_qdrant.tests.battery --mode file  --work DIR --out file.json
    python -m luccy_qdrant.tests.battery --mode live  --work DIR --out live.json

The parent sets the environment (index dir, Qdrant URL, collections, log dir) and writes
DIR/pages/*.md, DIR/catalog.jsonl and DIR/ids.json before running a mode.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import types
from pathlib import Path

import numpy as np


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (set, frozenset)):
        return sorted(_jsonable(v) for v in x)
    return x


def qkey(q, sources) -> str:
    return f"{q}|{','.join(sorted(sources)) if sources else '*'}"


def build(work: Path) -> None:
    import retrieve
    from serving import config

    from .fakes import install_fake_embedders
    retrieve.PAGES = work / "pages"
    config.LOG_DIR = work / "logs_build"
    install_fake_embedders()
    (work / "index").mkdir(exist_ok=True)
    t0 = time.monotonic()
    r = retrieve.Retriever(rebuild=True)
    built = time.monotonic() - t0
    from ingest import qdrant_index
    emb = np.load(work / "index" / "emb_gemini.npy")
    sources = [retrieve.source_of(c["page_id"], c.get("space") or "", c.get("url") or "") for c in r.chunks]
    name = qdrant_index.load(qdrant_index.client(), emb, sources, r.sig, pinned_as=work.name.replace("-", "_"))
    (work / "build.json").write_text(json.dumps({"sig": r.sig, "n": len(r.chunks), "collection": name,
                                                 "build_s": round(built, 2)}))


def load(mode: str, work: Path):
    """luccy's retriever for `mode`, and the seconds it took to load."""
    import retrieve
    from serving import config

    from .fakes import install_fake_embedders
    if mode == "file":
        from serving import rqb
        retrieve.PAGES = rqb.PAGES = work / "pages"
        config.CATALOG = work / "catalog.jsonl"
    else:
        from luccy_qdrant import hooks
        hooks.install()
        # Nothing document-related on disk: these point nowhere, and must not be needed.
        retrieve.PAGES = Path("/nonexistent/pages")
        retrieve.INDEX = Path("/nonexistent/index")
        config.CATALOG = Path("/nonexistent/catalog.jsonl")
        from serving import rqb
        rqb.PAGES = retrieve.PAGES
    install_fake_embedders()
    from serving import rqb
    t0 = time.monotonic()
    r = rqb.Retriever(rebuild=False)
    r.reranker()
    return r, time.monotonic() - t0


def kw_top(r, q, sources):
    import retrieve as R
    off = None if sources is None else [s for s in R.SOURCES if s not in sources]
    ctx = r.live.toggles(off) if hasattr(r, "live") else contextlib.nullcontext()
    with ctx:
        bm = r.kw_stem.scores(q)
    if sources is not None:
        bm = np.where(r._source_keep(sources), bm, 0.0)
    order = np.argsort(-bm, kind="stable")[:R.BM25_TOPN]
    return [[int(i), float(bm[i])] for i in order if bm[i] > 0]


def chunk_view(c) -> dict:
    return {k: c.get(k) for k in ("page_id", "title", "url", "space", "tier", "modified", "breadcrumb",
                                  "source_version", "as_of", "supersedes", "superseded_by", "stale_hint",
                                  "heading", "text", "index_text")}


def battery(mode: str, work: Path) -> dict:
    import retrieve as R
    from serving import deep, driver, rqb
    from serving.api import page_routes
    from serving.temporal import resolve

    from .corpus import PLANS, QUERIES
    from .fakes import StubPlanner, StubWriter, install_write_spy
    out: dict = {"mode": mode}
    writes = install_write_spy() if mode == "live" else []
    r, init_s = load(mode, work)
    out["init_s"] = round(init_s, 3)
    out["n"], out["sig"] = len(r.chunks), r.sig
    out["recency"] = [r._min_mod, r._max_mod]
    out["index_built"] = r.index_built

    # --- keyword and meaning legs, and luccy's retrieve -----------------------------
    out["kw"], out["dense"], out["retrieve"], times, rows = {}, {}, {}, [], set()
    for q, sources in QUERIES:
        k = qkey(q, sources)
        out["kw"][k] = kw_top(r, q, sources)
        off = None if sources is None else [s for s in R.SOURCES if s not in sources]
        qv = r.query_vectors([q])[q]
        out["dense"][k] = [int(i) for i in r.vector_store.search(qv, R.VEC_TOPN, off)]
        t0 = time.monotonic()
        res = r.retrieve(q, sources=sources)
        times.append(time.monotonic() - t0)
        out["retrieve"][k] = _jsonable(res)
        rows.update(i for i, _ in out["kw"][k])
        rows.update(out["dense"][k])
    out["chunks"] = {str(i): chunk_view(r.chunks[i]) for i in sorted(rows)}
    out["retrieve_s"] = [round(t, 4) for t in times]

    # --- rqb: merged pages, whole-page fill, evidence ----------------------------------
    out["rqb"] = {}
    for question, queries in PLANS.items():
        merged = rqb.batch_retrieve(r, queries, None)
        entry = {"merged": _jsonable([{**p, "passages": p["passages"]} for p in merged]),
                 "coverage": _jsonable(merged.coverage), "cut": _jsonable(merged.cut)}
        kept, dropped, conflicts = resolve(list(merged))
        fed = rqb.fill_whole_pages(kept)
        rqb.text_coverage(merged.coverage, kept)
        entry["dropped"] = [d["id"] for d in dropped]
        entry["fed"] = fed
        entry["skip"] = {p["id"]: p.get("whole_skip") for p in kept if p.get("whole_skip")}
        entry["full_text"] = {p["id"]: p["full_text"] for p in kept if p.get("full_text") is not None}
        entry["evidence"] = rqb.render(queries, kept)
        entry["with_text"] = [c.get("with_text") for c in merged.coverage]
        out["rqb"][question] = entry

    # --- the whole driver, to the writer -------------------------------------------------
    os.environ["LUCCY_DEEP_FALLBACK"] = "0"
    writer, planner = StubWriter(), StubPlanner(PLANS)
    svc = driver.AnswerService(llm=writer, plan_llm=planner, retriever=r,
                               log_dir=work / f"logs_{mode}", run_warehouse=False)
    out["prompts"], answer_s = {}, []
    asks = [(q, None) for q in PLANS] + [(next(iter(PLANS)), ["confluence", "jira"])]
    for question, sources in asks:
        before = len(writer.prompts)
        t0 = time.monotonic()
        rec = svc.answer(question, conversation={"sources": sources} if sources else None)
        answer_s.append(time.monotonic() - t0)
        out["prompts"][qkey(question, sources)] = {
            "planner": planner.prompts[-1], "writer": writer.prompts[before:],
            "whole_pages": rec["retrieval"]["whole_pages"],
            "passages_only": rec["retrieval"].get("passages_only"),
            "cut_at_cap": rec["retrieval"].get("cut_at_cap"),
            "coverage": rec["retrieval"]["coverage"], "pages": rec["retrieval"]["pages"],
            "answer": rec["answer"]["answer"], "citations": rec["answer"]["citations"],
            "checks_citations": rec["checks"]["citations"]["valid"],
            "evidence_chars": rec["retrieval"]["evidence_chars"],
            "corpus_sig": rec["corpus_sig"]}
    out["answer_s"] = [round(t, 3) for t in answer_s]

    # elaborate (pages named by id) and the bounded section read
    first = next(iter(PLANS))
    ids = [p["id"] for p in out["rqb"][first]["merged"]][:3] + ["no-such-page"]
    m, ev, *_ = svc._pages_from_ids(first, ids, None)
    out["elaborate"] = {"merged": _jsonable(m), "evidence": ev}
    os.environ.update(LUCCY_SECTION_READ="1", LUCCY_WHOLE_PAGES="0")
    walk = "Walk me through the partner contract renewal checklist."
    merged, evidence, _c, _d, _cov, sec, _b = svc._retrieve(PLANS[walk], walk)
    out["section_read"] = {"sec": sec, "evidence": evidence}
    os.environ.update(LUCCY_SECTION_READ="0", LUCCY_WHOLE_PAGES="1")

    # --- page-level reads: bodies, viewer metadata, catalog, ticket lines, owners ------
    page_ids = json.loads((work / "ids.json").read_text())
    out["pages"], body_s = {}, []
    for pid in page_ids:
        t0 = time.monotonic()
        body = rqb.page_body(pid)
        body_s.append(time.monotonic() - t0)
        meta = rqb.page_meta(pid) or {}
        cat = page_routes._catalog_row(pid)
        out["pages"][pid] = {"body": body,
                             "meta": {k: meta.get(k) for k in ("id", "title", "url", "space", "modified")},
                             "catalog": cat,
                             "ticket": rqb.ticket_line({"id": pid, "space": (cat or {}).get("space") or "",
                                                        "url": (cat or {}).get("url") or ""}),
                             "owners": list(driver.page_owners(pid))}
    out["body_s"] = [round(t, 4) for t in body_s]
    out["jira_tickets"] = deep.jira_tickets()

    # --- the deep loop's tools, called as the model would --------------------------------
    deps = deep.DeepDeps(search=lambda q: list(rqb.batch_retrieve(r, [q], None)),
                         open_doc=rqb.page_body,
                         find_in_doc=lambda d, n: deep.find_lines(rqb.page_body(d), n), user="maria_p")
    ctx = types.SimpleNamespace(deps=deps)
    tool = {"search": deep.search(ctx, "driver payout delayed weekend")}
    some = list(deps.pages)[:3]
    tool["open_doc"] = [deep.open_doc(ctx, pid, 0) for pid in some]
    tool["find_in_doc"] = [deep.find_in_doc(ctx, pid, "payout|driver") for pid in some]
    tool["list_tickets"] = deep.list_tickets(ctx, "Maria", "all")
    out["deep"] = tool
    if mode == "live":
        out["qdrant_calls"] = r.live.stats["qdrant_calls"]
        out["serve_writes"] = list(writes)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("build", "file", "live"), required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.mode == "build":
        build(a.work)
        return
    result = battery(a.mode, a.work)
    a.out.write_text(json.dumps(_jsonable(result)))
    print(f"[battery] {a.mode}: {len(result['retrieve'])} queries, init {result['init_s']} s", file=sys.stderr)


if __name__ == "__main__":
    main()
