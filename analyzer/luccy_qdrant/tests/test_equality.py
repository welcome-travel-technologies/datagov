"""EQUALITY PROOF: luccy on its own FILE index vs luccy LIVE on DataGov's Qdrant, same corpus.

One synthetic corpus (corpus.py, >= 30 single-page documents) goes two ways:
  file  luccy's own build path (`Retriever(rebuild=True)` over pages/*.md, the fake embedder
        standing in for Gemini) and luccy's own Qdrant load (`ingest.qdrant_index.load`), then
        luccy served read-only from that bundle (LUCCY_VECTOR_SEARCH=exact).
  live  DataGov's engine path (`prepare_pages` + `VectorIndex.upsert`, same fake vectors, the
        `ordinal` it adds), `luccy_qdrant.sync` over a fake corpus export, then luccy with the
        hooks: no page, index or catalog file anywhere.
Both runs execute the same battery (battery.py) in their own process. Everything must be
IDENTICAL, except whole-document texts, which must be equal under the shared stitcher's
normalize() (and whose whole-vs-passages decisions must be identical).
"""
from __future__ import annotations

import json
import re

import pytest

from .conftest import (QDRANT_KEY, engine_upsert, live_env, luccy_blocks, run_module, settings_for,
                       write_file_corpus)
from .corpus import ORG, PLANS, QUERIES, corpus

HEADER_LEN = re.compile(r"-- full document \([\d,]+ chars;")


@pytest.fixture(scope="module")
def eq(qc, tmp_path_factory):
    from luccy_qdrant import sync

    from .fakes import C2Server
    docs = corpus()
    work = tmp_path_factory.mktemp("equality")
    c2 = C2Server(ORG, docs)
    s = settings_for("eq", c2.url)
    try:
        n_engine = engine_upsert(qc, s.chunks, docs, work, ORG)
        report = sync.run(s)
        blocks = luccy_blocks(qc, s)
        ids = write_file_corpus(work, docs, blocks)
        # luccy's own file mode loads its bundle into Qdrant itself: the read-write key
        env_file = {"LUCCY_INDEX_DIR": str(work / "index"), "LUCCY_QDRANT_URL": s.qdrant_url,
                    "LUCCY_QDRANT_API_KEY": QDRANT_KEY, "SERVING_LOG_DIR": str(work / "logs_file")}
        run_module("luccy_qdrant.tests.battery", ["--mode", "build", "--work", str(work)],
                   {**env_file, "LUCCY_INDEX_BUILD_CONTEXT": "1", "LUCCY_VECTOR_STORE": "flat"})
        run_module("luccy_qdrant.tests.battery", ["--mode", "file", "--work", str(work),
                                                  "--out", str(work / "file.json")],
                   {**env_file, "LUCCY_VECTOR_STORE": "qdrant"})
        run_module("luccy_qdrant.tests.battery", ["--mode", "live", "--work", str(work),
                                                  "--out", str(work / "live.json")],
                   {**live_env(s), "SERVING_LOG_DIR": str(work / "logs_live"), "LUCCY_VECTOR_STORE": "qdrant"})
        yield {"file": json.loads((work / "file.json").read_text()),
               "live": json.loads((work / "live.json").read_text()),
               "build": json.loads((work / "build.json").read_text()),
               "report": report, "docs": docs, "ids": ids, "blocks": blocks, "n_engine": n_engine}
    finally:
        c2.close()


def normalize(text: str) -> str:
    from document_explorer.stitch import normalize as norm
    return norm(text)


HEADING = re.compile(r"^(#{1,6})\s+(.*)")
LIMITS: dict[str, set] = {}


def same_document(pid: str, original: str, stitched: str) -> bool:
    """normalize(original) == normalize(stitched), or exactly the engine's documented stitching
    limit "short text before the first heading moves after it" (engine tests,
    test_limit_short_text_before_the_first_heading_moves_after_it): the chunker packs text
    under 200 characters ahead of the first heading into that heading's chunk, which carries
    only the heading's path. Accepted only when moving that text after the first heading
    makes the two equal under normalize(); counted in LIMITS for the report."""
    if normalize(original) == normalize(stitched):
        return True
    lines = original.split("\n")
    first = next((i for i, ln in enumerate(lines) if HEADING.match(ln)), None)
    if first:
        pre = "\n".join(lines[:first]).strip()
        if pre and len(pre) < 200:
            moved = "\n".join([lines[first], pre] + lines[first + 1:])
            if normalize(moved) == normalize(stitched):
                LIMITS.setdefault("short text before the first heading", set()).add(pid)
                return True
    return False


def mask(text: str, bodies: dict[str, str], ids) -> str:
    """`text` with each whole-document block (and the char count in its header) masked."""
    for pid in ids:
        block = "\n".join("       " + ln for ln in bodies[pid].splitlines())
        assert block in text, pid
        text = text.replace(block, f"<<WHOLE DOCUMENT {pid}>>")
    return HEADER_LEN.sub("-- full document (<<N>> chars;", text)


def bodies(side):
    return {pid: v["body"] for pid, v in side["pages"].items()}


# ------------------------------------------------------------------ corpus and bundle
def test_corpus_size(eq):
    assert len(eq["docs"]) >= 30
    assert len(QUERIES) >= 25
    assert sum(len(d["pages"][0]) > 20000 for d in eq["docs"]) >= 3


def test_sync_counts(eq):
    r = eq["report"]
    assert r["status"] == "built"
    assert r["documents_in_scope"] == r["documents_synced"] == len(eq["docs"])
    assert r["chunks"] == eq["n_engine"] == eq["build"]["n"]
    assert r["pages"] == len(eq["docs"])
    assert r["payload_set_versions"] == len(eq["docs"])
    assert r["points"] == r["chunks"] and r["skipped_versions"] == {}


def test_live_battery_made_no_qdrant_write(eq):
    """The whole live battery (load, both legs, rqb, the driver, page reads, deep tools)."""
    assert eq["live"]["serve_writes"] == []


def test_signature_rows_and_recency(eq):
    f, live = eq["file"], eq["live"]
    assert (live["n"], live["sig"]) == (f["n"], f["sig"]) == (eq["build"]["n"], eq["build"]["sig"])
    assert live["recency"] == f["recency"]


def test_luccy_ids_follow_luccys_own_rules(eq):
    import retrieve

    from luccy_qdrant import ids
    by_key = {d["document_id"]: d for d in eq["docs"]}
    for doc_id, lu in eq["blocks"].items():
        assert ids.valid(lu["id"])
        assert retrieve.source_of(lu["id"], lu["space"], lu["url"] or "") == lu["source"]
        d = by_key[doc_id]
        want = ids.WANT[d["source_type"]]
        if d["key"] == "dbt2":                 # a dbt file without a GitHub link reads as drive
            assert (lu["source"], lu["id"]) == ("drive", f"gd-dg-{doc_id}")
        else:
            assert lu["source"] == want
    got = {by_key[k]["external_id"]: v["id"] for k, v in eq["blocks"].items()}
    assert got["confluence:6800000000"] == "6800000000"
    assert got["confluence:6999000200:part:2"] == "6999000200-part-2"
    assert got["jira:AP-520"] == "AP-520"
    upload = next(d for d in eq["docs"] if d["key"] == "up0")["document_id"]
    assert eq["blocks"][upload]["id"] == f"gd-dg-{upload}" and eq["blocks"][upload]["source"] == "drive"


# ------------------------------------------------------------------ the two search legs
def test_chunks_identical(eq):
    f, live = eq["file"]["chunks"], eq["live"]["chunks"]
    assert f.keys() == live.keys()
    assert f == live


def test_keyword_candidates_and_scores(eq):
    f, live = eq["file"]["kw"], eq["live"]["kw"]
    assert f.keys() == live.keys() and len(f) >= 25
    exact = 0
    for k in f:
        assert [r for r, _ in f[k]] == [r for r, _ in live[k]], k
        for (_, a), (_, b) in zip(f[k], live[k]):
            assert abs(a - b) <= 1e-6, (k, a, b)
            exact += a == b
    assert exact == sum(len(v) for v in f.values())      # bit-identical float32 scores
    assert sum(len(v) for v in f.values()) > 300


def test_dense_candidates(eq):
    f, live = eq["file"]["dense"], eq["live"]["dense"]
    assert f == live
    assert all(len(v) == 20 for k, v in f.items() if k.endswith("|*"))


def test_reranked_bundles(eq):
    f, live = eq["file"]["retrieve"], eq["live"]["retrieve"]
    assert f.keys() == live.keys()
    for k in f:
        assert f[k] == live[k], k


# ------------------------------------------------------------------ rqb, whole pages, writer
def test_rqb_merged_pages_and_passages(eq):
    f, live = eq["file"]["rqb"], eq["live"]["rqb"]
    for q in PLANS:
        for part in ("merged", "coverage", "cut", "dropped", "with_text"):
            assert f[q][part] == live[q][part], (q, part)


def test_whole_page_decisions_and_texts(eq):
    f, live = eq["file"]["rqb"], eq["live"]["rqb"]
    reasons = set()
    for q in PLANS:
        assert f[q]["fed"] == live[q]["fed"], q
        assert f[q]["skip"] == live[q]["skip"], q
        reasons |= set(f[q]["skip"].values())
        for pid in f[q]["fed"]:
            assert same_document(pid, f[q]["full_text"][pid], live[q]["full_text"][pid]), (q, pid)
        assert mask(f[q]["evidence"], f[q]["full_text"], f[q]["fed"]) == \
            mask(live[q]["evidence"], live[q]["full_text"], live[q]["fed"]), q
    assert {"too_long", "total_full"} <= reasons      # both caps exercised


def test_writer_prompts(eq):
    f, live = eq["file"]["prompts"], eq["live"]["prompts"]
    fb, lb = bodies(eq["file"]), bodies(eq["live"])
    assert f.keys() == live.keys()
    for k in f:
        a, b = f[k], live[k]
        assert a["planner"] == b["planner"], k
        for field in ("whole_pages", "passages_only", "cut_at_cap", "coverage", "pages", "answer",
                      "citations", "checks_citations", "corpus_sig"):
            assert a[field] == b[field], (k, field)
        assert len(a["writer"]) == len(b["writer"]) >= 1
        for pa, pb in zip(a["writer"], b["writer"]):
            assert mask(pa, fb, a["whole_pages"]) == mask(pb, lb, b["whole_pages"]), k
        for pid in a["whole_pages"]:
            assert same_document(pid, fb[pid], lb[pid]), (k, pid)


def test_elaborate_and_section_read(eq):
    f, live = eq["file"], eq["live"]
    assert f["elaborate"] == live["elaborate"]
    assert f["section_read"]["sec"] == live["section_read"]["sec"]
    assert f["section_read"]["sec"]["fired"] is True
    assert f["section_read"]["evidence"] == live["section_read"]["evidence"]


# ------------------------------------------------------------------ page reads and deep tools
def test_page_reads(eq):
    f, live = eq["file"]["pages"], eq["live"]["pages"]
    assert f.keys() == live.keys() == set(eq["ids"])
    for pid in f:
        assert same_document(pid, f[pid]["body"], live[pid]["body"]), pid
        # luccy's parse_page drops an empty LAST frontmatter field (it strips the block), so a
        # missing value reads None there and "" here: both mean "no value" to every reader.
        assert {k: v or "" for k, v in f[pid]["meta"].items()} ==             {k: v or "" for k, v in live[pid]["meta"].items()}, pid
        for part in ("catalog", "ticket", "owners"):
            assert f[pid][part] == live[pid][part], (pid, part)
        assert f[pid]["ticket"] == "" and f[pid]["owners"] == []
    assert eq["file"]["jira_tickets"] == eq["live"]["jira_tickets"] == []


def test_deep_tools(eq):
    f, live = eq["file"]["deep"], eq["live"]["deep"]
    assert f["search"] == live["search"]
    assert f["list_tickets"] == live["list_tickets"]
    lb = bodies(eq["live"])
    for a, b in zip(f["open_doc"], live["open_doc"]):
        head_a, _, text_a = a.partition("\n\n")
        head_b, _, text_b = b.partition("\n\n")
        pid = head_a.split("(", 1)[1].split(")", 1)[0]
        assert head_a.split("->")[0] == head_b.split("->")[0]
        assert normalize(text_b) in normalize(lb[pid])                   # served from that page
        assert set(normalize(text_a).split()[:300]) <= set(normalize(lb[pid]).split())
    for a, b in zip(f["find_in_doc"], live["find_in_doc"]):
        assert a.split("\n", 1)[0] == b.split("\n", 1)[0]                # same count of lines
        # the tool indents each line by 4; a restored heading may differ in level (normalize)
        hits = [{normalize(ln.strip()) for ln in x.split("matching lines:\n", 1)[-1].split("\n")}
                for x in (a, b)]
        assert hits[0] == hits[1]


def test_report_numbers(eq, capsys):
    """Not an assertion of speed: the numbers for the report."""
    import statistics
    f, live = eq["file"], eq["live"]
    same_bytes = sum(f["pages"][p]["body"] == live["pages"][p]["body"] for p in f["pages"])
    lens = [(len(f["pages"][p]["body"]), len(live["pages"][p]["body"])) for p in f["pages"]]
    summary = {
        "queries": len(f["retrieve"]), "pages": len(f["pages"]), "chunks": f["n"],
        "kw_candidates": sum(len(v) for v in f["kw"].values()),
        "dense_candidates": sum(len(v) for v in f["dense"].values()),
        "writer_prompts": sum(len(v["writer"]) for v in f["prompts"].values()),
        "whole_texts_byte_identical": f"{same_bytes}/{len(f['pages'])}",
        "whole_texts_normalize_equal": sum(normalize(f["pages"][p]["body"]) == normalize(live["pages"][p]["body"])
                                           for p in f["pages"]),
        "documented_stitch_limit_pages": {k: sorted(v) for k, v in LIMITS.items()},
        "max_len_diff_whole_text": max(abs(a - b) for a, b in lens),
        "init_s": {"file": f["init_s"], "live": live["init_s"]},
        "retrieve_ms_median": {m: round(1000 * statistics.median(x["retrieve_s"]), 1)
                               for m, x in (("file", f), ("live", live))},
        "retrieve_ms_max": {m: round(1000 * max(x["retrieve_s"]), 1) for m, x in (("file", f), ("live", live))},
        "page_body_ms_median": {m: round(1000 * statistics.median(x["body_s"]), 2)
                                for m, x in (("file", f), ("live", live))},
        "answer_s_median": {m: statistics.median(x["answer_s"]) for m, x in (("file", f), ("live", live))},
        "qdrant_calls_live": live.get("qdrant_calls"),
        "sync_s": eq["report"]["seconds"], "build_s": eq["build"]["build_s"],
    }
    with capsys.disabled():
        print("\nEQUALITY-REPORT " + json.dumps(summary))
