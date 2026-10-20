"""The runs body: row shape, answer cut, and the 2 MB cap (oldest detail goes first)."""
import json

from luccy_qdrant.compare import metrics, payload

CODES = ["A", "A2", "C"]
SUMMARY = {"questions": 121, "arms": {}, "by_kind": {}, "agreement": [],
           "noise_floor": {"flips": 3, "threshold": 6, "disagree_rate": 0.0248},
           "verdicts": [], "excluded": {"infra_error": []}, "spent_usd": 12.5}


def gold_row(i: int) -> dict:
    return {"id": f"q-{i:03d}", "kind": "single_hop", "question": f"question {i}?", "expected_behaviour": "answer",
            "expected_doc_ids": ["1"], "expected_facts": ["a", "b"]}


def legs(row: dict, answer: str) -> dict:
    out = {}
    for code in CODES:
        leg = {"status": "ok", "error": None, "infra": False, "answer": answer, "cited": ["1", "2-part-1"],
               "abstained": False, "gaps": [], "latency_s": 12.34}
        out[code] = metrics.make_outcome(row, leg, {"pass": True, "facts_supported": 2, "facts_total": 2,
                                                    "behaviour_ok": True, "mechanical_leak": False}, None, 0.01, 0.0)
    return out


def rows_of(n: int, answer: str) -> list[dict]:
    return [payload.public_row(gold_row(i), legs(gold_row(i), answer), CODES) for i in range(n)]


def test_public_row_shape_and_cut():
    r = gold_row(1)
    o = legs(r, "x" * 5000)
    o["C"] = metrics.make_outcome(r, {"status": "error", "error": "HTTP 503: busy", "infra": True, "answer": "",
                                      "cited": [], "abstained": False, "gaps": [], "latency_s": 0.5},
                                  None, None, 0.01, 0.0)
    pub = payload.public_row(r, o, ["A", "A2", "C"], ["C", "A", "A2"], ["9999"], True)
    assert set(pub) == {"id", "kind", "question", "order", "arms", "expected_docs_missing", "recorded"}
    assert list(pub["arms"]) == ["A", "A2", "C"]
    assert pub["order"] == ["C", "A", "A2"]                                     # the order they were asked in
    assert pub["expected_docs_missing"] == ["9999"] and pub["recorded"] == "pass"
    plain = payload.public_row(r, o, ["A"])
    assert plain["expected_docs_missing"] == [] and plain["recorded"] is None    # nothing missing, no recorded verdict
    assert payload.public_row(r, o, ["A"], recorded=False)["recorded"] == "fail"
    a = pub["arms"]["A"]
    assert set(a) == {"verdict", "infra_error", "cited", "answer", "latency_s", "fact_ratio"}
    assert len(a["answer"]) == payload.ANSWER_CHARS and a["answer"].endswith(payload.ELLIPSIS)
    assert a["cited"] == ["1", "2-part-1"] and a["fact_ratio"] == 1.0 and a["latency_s"] == 12.34
    assert a["infra_error"] is False
    assert pub["arms"]["C"] == {"verdict": "error", "infra_error": True, "cited": [],
                                "answer": "[error] HTTP 503: busy", "latency_s": 0.5, "fact_ratio": None}
    assert payload.public_row(r, {"A": o["A"]}, ["A", "C"])["order"] == ["A"]    # no order given: the arms present


def test_a_small_body_is_sent_whole():
    rows = rows_of(3, "short answer")
    data = payload.build("run-1", "running", {"arms": CODES}, SUMMARY, rows)
    body = json.loads(data)
    assert set(body) == {"run_id", "status", "params", "summary", "rows"}
    assert body["rows"][0]["arms"]["A"]["answer"] == "short answer" and body["summary"] == SUMMARY


def test_a_long_run_of_long_answers_fits_by_shortening_the_oldest_answers():
    rows = rows_of(200, "y" * 4500)
    assert len(payload.encode(rows)) > payload.MAX_BODY_BYTES
    data = payload.build("run-1", "done", {}, SUMMARY, rows)
    assert len(data) <= payload.MAX_BODY_BYTES
    body = json.loads(data)
    assert len(body["rows"]) == 200 and body["summary"] == SUMMARY
    assert len(body["rows"][-1]["arms"]["C"]["answer"]) == payload.ANSWER_CHARS         # newest keep theirs
    assert len(body["rows"][0]["arms"]["A"]["answer"]) <= 1000                         # oldest go first
    assert all(r["arms"]["A"]["verdict"] == "pass" and r["arms"]["A"]["cited"] for r in body["rows"])


def test_rows_are_dropped_oldest_first_only_when_answers_are_gone():
    rows = rows_of(121, "z" * 4000)
    cap = 40_000
    data = payload.build("run-1", "done", {}, SUMMARY, rows, cap=cap)
    assert len(data) <= cap
    body = json.loads(data)
    assert 0 < len(body["rows"]) < 121 and body["rows"][-1]["id"] == "q-120"
    assert all(leg["answer"] == "" for r in body["rows"] for leg in r["arms"].values())
    assert body["summary"] == SUMMARY


def test_the_cap_counts_bytes_not_characters():
    rows = rows_of(121, "é" * 4000)                     # two bytes a character in UTF-8
    data = payload.build("run-1", "done", {}, SUMMARY, rows)
    assert len(data) <= payload.MAX_BODY_BYTES
    assert len(data.decode("utf-8")) < len(data)
