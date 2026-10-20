"""The luccy id space: the adapter's ids, part ids, citation mapping, missing documents, selection."""
import httpx
import pytest
from conftest import DATAGOV, MISSING_PAGE, SPLIT_PAGE, World, document_id, listing_row

from luccy_qdrant import ids as adapter_ids
from luccy_qdrant.compare import ids, runner
from luccy_qdrant.compare.clients import (
    DataGovClient,
    Http,
    next_cursor,
    page_documents,
)
from luccy_qdrant.compare.ids import Corpus


def test_the_compare_package_keeps_no_id_mapping_of_its_own():
    assert not hasattr(ids, "luccy_id")                                     # ids.py is the one source
    docs = [listing_row(e) for e in ("confluence:4435476587", f"confluence:{SPLIT_PAGE}:part:3", "jira:AP-320",
                                     "JIRA:PHOENIX-9428", "upload:a.pdf")]
    refs, _ = adapter_ids.assign(docs)
    corpus = Corpus(docs)
    assert corpus.by_document == {d["document_id"]: refs[d["document_id"]]["id"] for d in docs}
    assert corpus.by_document[docs[1]["document_id"]] == f"{SPLIT_PAGE}-part-3"
    assert corpus.by_document[docs[3]["document_id"]] == "PHOENIX-9428"
    assert corpus.by_document[docs[4]["document_id"]] == "gd-dg-" + docs[4]["document_id"]
    assert corpus.report["natural"] == 4 and corpus.report["gd_dg"] == 1


def test_two_documents_claiming_one_page_resolve_as_the_sync_resolves_them():
    a = dict(listing_row("confluence:6857523203"), document_id="11111111-0000-4000-8000-000000000000")
    b = dict(listing_row("confluence:6857523203"), document_id="22222222-0000-4000-8000-000000000000")
    corpus = Corpus([b, a])
    assert corpus.by_document[a["document_id"]] == "6857523203"
    assert corpus.by_document[b["document_id"]] == adapter_ids.assign([a, b])[0][b["document_id"]]["id"]
    assert corpus.report["collisions"] == 1


def test_part_ids_count_as_their_page():
    assert ids.page_of("6836781135-part-2") == "6836781135"                 # the analyzer's form
    assert ids.page_of("6836781135:part:2") == "6836781135"                 # C3's form, still accepted
    assert ids.page_of("AP-320") == "AP-320" and ids.page_of("dg-x") == "dg-x"
    assert ids.page_of("PART-5") == "PART-5" and ids.page_of("gd-dg-1") == "gd-dg-1"
    assert ids.groups_hit(["6836781135|6836781087"], ["6836781135-part-2"])
    assert ids.groups_hit(["6836781135"], ["6836781135:part:1"])
    assert ids.groups_hit(["1|2", "3"], ["2", "3"]) and not ids.groups_hit(["1|2", "3"], ["2"])
    assert ids.groups_hit([], [])
    assert ids.expected_groups({"expected_doc_ids": ["registry:nps", "4"]}) == ["4"]


def test_citations_keep_their_luccy_id_else_map_the_document_id_else_gd_dg():
    docs = [listing_row("confluence:1111"), listing_row("confluence:2222:part:2"), listing_row("jira:AP-1"),
            listing_row("upload:x")]
    corpus = Corpus(docs + [{"external_id": "confluence:9999"}, "not a document"])   # no id: skipped
    d2, d4 = docs[1]["document_id"], docs[3]["document_id"]
    assert len(corpus) == 4 and corpus.skipped == 2
    assert corpus.ids == {"1111", "2222-part-2", "AP-1", "gd-dg-" + d4}
    assert corpus.pages == {"1111", "2222", "AP-1", "gd-dg-" + d4}
    assert corpus.cited_id({"document_id": d2, "luccy_id": None}) == "2222-part-2"
    assert corpus.cited_id({"document_id": d2, "luccy_id": "2222-part-2"}) == "2222-part-2"
    assert corpus.cited_id({"document_id": "elsewhere", "luccy_id": "3333"}) == "3333"
    assert corpus.cited_id({"luccy_id": "4444"}) == "4444"
    assert corpus.cited_id({"document_id": "elsewhere"}) == "gd-dg-elsewhere"
    assert corpus.cited_id({"title": "nothing"}) is None and corpus.cited_id("5555") == "5555"
    assert Corpus([]).report == {} and len(Corpus([])) == 0


def test_missing_names_the_groups_the_corpus_does_not_hold():
    corpus = Corpus([listing_row("confluence:1111"), listing_row("confluence:2222:part:1")])
    assert corpus.missing({"expected_doc_ids": ["1111"]}) == []
    assert corpus.missing({"expected_doc_ids": ["2222"]}) == []                 # a part holds its page
    assert corpus.missing({"expected_doc_ids": ["9999|1111", "2222"]}) == []
    assert corpus.missing({"expected_doc_ids": ["1111", "9999", "8888|7777"]}) == ["9999", "8888|7777"]
    assert corpus.missing({"expected_doc_ids": []}) == []
    assert corpus.missing({"expected_doc_ids": ["registry:nps"]}) == []        # a warehouse key, not a document


def test_every_gold_row_is_asked_warehouse_and_missing_documents_included(gold, corpus):
    """The golden set runs exactly: no kind and no row is left out for DataGov's sake."""
    args = runner.parse_args([])
    assert runner.choose(gold, args) == gold and len(gold) == 121
    assert sum(r["kind"] == "warehouse" for r in gold) == 10
    missing = {r["id"]: corpus.missing(r) for r in gold if corpus.missing(r)}
    assert missing["sh-001"] == [MISSING_PAGE]                                  # asked anyway, flagged
    assert all(all(MISSING_PAGE in g or "gd" in g for g in groups) for groups in missing.values())
    assert not any(SPLIT_PAGE in g for groups in missing.values() for g in groups)


def test_smoke_selections_and_unknown_ids(gold):
    pick = runner.choose(gold, runner.parse_args(["--ids", "mh-002,sh-001,sh-001"]))
    assert [r["id"] for r in pick] == ["sh-001", "mh-002"]                     # gold order, once each
    assert {r["kind"] for r in runner.choose(gold, runner.parse_args(["--kinds", "warehouse"]))} == {"warehouse"}
    assert len(runner.choose(gold, runner.parse_args(["--limit", "8"]))) == 8
    assert len(runner.choose(gold, runner.parse_args(["--kinds", "stale,abstain", "--limit", "3"]))) == 3
    with pytest.raises(runner.ConfigError, match="does not have: nope-1"):
        runner.choose(gold, runner.parse_args(["--ids", "sh-001,nope-1"]))


def test_selection_stratifies_and_spreads():
    rows = [{"id": f"{k}-{i}", "kind": k} for k in ("a", "b", "c") for i in range(4)]
    assert [r["id"] for r in runner.select(rows, limit=4)] == ["a-0", "a-1", "b-0", "c-0"]
    picked = runner.select(rows, stratified=2)
    assert len(picked) == 6 and {r["kind"] for r in picked} == {"a", "b", "c"}
    assert picked == runner.select(rows, stratified=2)                          # seeded
    assert len(runner.select(rows, stratified=2, limit=3)) == 3
    assert runner.select(rows) == rows


def test_arm_order_rotates_as_a_latin_square():
    arms = ["A", "A2", "C"]
    orders = [runner.arm_order(arms, i) for i in range(len(arms))]
    assert all(sorted(o) == sorted(arms) for o in orders)
    for place in range(len(arms)):                                              # every arm in every place once
        assert sorted(o[place] for o in orders) == sorted(arms)
    assert orders == [runner.arm_order(arms, i) for i in range(len(arms))]      # deterministic from the seed
    assert runner.arm_order(arms, 7) == orders[1]
    assert runner.arm_order(["A"], 3) == ["A"] and runner.arm_order([], 0) == []


def test_documents_listing_follows_cursors(documents):
    world = World(documents, page_size=7)
    with httpx.Client(transport=httpx.MockTransport(world)) as client:
        status, docs = DataGovClient(Http(client), DATAGOV, "eval-token", "sync-token", 5).documents()
    assert status == 200 and docs == documents
    assert len(world.calls) == -(-len(documents) // 7)
    assert document_id("upload:1AbCdEf") in {d["document_id"] for d in docs}


def test_next_cursor_shapes():
    assert next_cursor({"next_cursor": "abc"}) == "abc"
    assert next_cursor({"next": "https://dg/api/x/?cursor=c2&limit=500"}) == "c2"
    assert next_cursor({"next": "opaque"}) == "opaque"
    assert next_cursor({"next": None}) is None and next_cursor({}) is None
    assert page_documents([{"a": 1}]) == [{"a": 1}] and page_documents({"documents": [1]}) == [1]
