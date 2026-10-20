"""The runner end to end against fake DataGov and fake luccy servers (httpx.MockTransport):
the full golden set against luccy's recorded run, a smoke run, a --dry-run, the paid path with
fake answers, the spend cap, every configuration stop (a judge other than the recorded one
included), the fatal stops, infrastructure failures and interrupts."""
import io
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import httpx
import pytest
from conftest import ENV, World

from luccy_qdrant.compare import arms, clients, golden, payload, runner

SUMMARY_KEYS = {"questions", "arms", "by_kind", "agreement", "noise_floor", "verdicts", "excluded", "spent_usd",
                "gold", "complete", "luccy_recorded", "config_drift", "expected_docs_missing"}
GOLD_KEYS = {"rows", "selected", "digest", "baseline_digest", "same_as_recorded", "changed_ids"}
RECORDED_KEYS = {"ts", "n", "n_pass", "pass_rate", "fact_ratio", "judge_model", "judge_votes", "gold_digest",
                 "latency_p50_s", "latency_p90_s", "usd", "pass_by_id", "on_this_run"}
ARM_KEYS = {"label", "n", "answered", "pass", "pass_rate", "expected_doc_hit_rate", "fact_recall",
            "correct_abstentions", "abstain_rows", "privacy_leaks", "errors", "infra_errors", "p50_latency_s",
            "p90_latency_s", "est_cost_usd", "cost_source"}
VERDICT_KEYS = {"arm", "vs", "outcome", "text", "n", "delta", "threshold", "diff_rate", "ci_low", "ci_high", "margin",
                "disagree_rate", "luccy_disagree_rate", "within_noise", "disagrees_more_than_luccy_itself"}
ROW_ARM_KEYS = {"verdict", "infra_error", "cited", "answer", "latency_s", "fact_ratio"}
DEFAULT = list(arms.DEFAULT_ARMS)


def dry_judge():
    from eval.gold.judge import Judge
    return Judge(dry_run=True)


def run(world, *argv, env=ENV, judge=None):
    out = io.StringIO()
    code = runner.main(list(argv), env=env, out=out, transport=httpx.MockTransport(world), judge=judge)
    return code, out.getvalue()


def luccy_answers(gold, corpus, fail_ids=(), status=503):
    """luccy's /ask over the fake corpus, with a run record (cost $0.05) per answer."""
    by_question = {r["question"]: r for r in gold}
    records, counter = {}, itertools.count(1)

    def ask(question):
        row = by_question[question]
        if row["id"] in fail_ids:
            return status, {"error": "busy"}
        log = f"{next(counter):08x}"
        records[log] = {"id": log, "cost": {"usd": 0.05}, "usage": {}}
        return 200, dict(arms.fake_luccy_body("A", row, corpus), log=log)
    return ask, records


def datagov_answers(gold, corpus, fail=(), cost=None, change=None):
    """DataGov's eval answers over the fake corpus. `fail`: {(arm code, row id)} answered 502.
    `cost`: {arm code: usd measured}; arms left out reply without a cost (an older server).
    `change(code, row, body) -> body` edits a reply."""
    by_question = {r["question"]: r for r in gold}
    code_of = {a.eval_arm: code for code, a in arms.ARMS.items() if a.eval_arm}

    def answer(arm_name, question):
        code, row = code_of[arm_name], by_question[question]
        if (code, row["id"]) in fail:
            return 502, {"detail": "bad gateway"}
        body = arms.fake_datagov_body(code, row, corpus)
        body.pop("cost_usd"), body.pop("cost_source")
        if cost and code in cost:
            body.update(cost_usd=cost[code], cost_source="measured")
        return 200, change(code, row, body) if change else body
    return answer


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_shape(body):
    assert set(body) == {"run_id", "status", "params", "summary", "rows"}
    summary = body["summary"]
    assert set(summary) == SUMMARY_KEYS and is_int(summary["questions"])
    for a in summary["arms"].values():
        assert set(a) == ARM_KEYS and a["cost_source"] in {"measured", "estimate", "mixed", None}
        for key in ("pass_rate", "expected_doc_hit_rate", "fact_recall"):      # fractions 0..1, or null
            assert a[key] is None or (isinstance(a[key], float) and 0 <= a[key] <= 1) or a[key] in (0, 1)
        for key in ("n", "answered", "pass", "correct_abstentions", "abstain_rows", "privacy_leaks", "errors",
                    "infra_errors"):
            assert is_int(a[key])
    floor = summary["noise_floor"]
    assert floor is None or (set(floor) == {"flips", "threshold", "disagree_rate"} and is_int(floor["flips"])
                             and is_int(floor["threshold"]))
    for v in summary["verdicts"]:
        assert set(v) == VERDICT_KEYS and v["outcome"] in {"better", "worse", "same", "inconclusive"}
        assert is_int(v["delta"]) and is_int(v["threshold"]) and is_int(v["n"])
        assert v["within_noise"] in (True, False, None) and isinstance(v["text"], str) and v["text"]
    for p in summary["agreement"]:
        assert all(is_int(p[k]) for k in ("n", "both_pass", "only_a", "only_b", "both_fail"))
        assert 0 <= p["mcnemar_p"] <= 1
    for kind in summary["by_kind"].values():
        assert all(is_int(cell["n"]) and is_int(cell["pass"]) for cell in kind.values())
    assert set(summary["excluded"]) == {"infra_error"}                          # nothing else is left out
    assert all(set(e) == {"id", "arm", "message"} for e in summary["excluded"]["infra_error"])
    assert set(summary["gold"]) == GOLD_KEYS and isinstance(summary["complete"], bool)
    assert set(summary["luccy_recorded"]) == RECORDED_KEYS
    drift = summary["config_drift"]
    assert set(drift) == {"same", "differences", "documents"} and drift["same"] is (not drift["differences"])
    assert all(set(d) == {"field", "recorded", "current", "text"} and d["text"] for d in drift["differences"])
    assert not any(d["field"] in golden.DOCUMENT_FIELDS for d in drift["differences"])   # expected, not drift
    assert all(set(m) == {"id", "question", "missing"} and m["missing"] for m in summary["expected_docs_missing"])
    for row in body["rows"]:
        assert set(row) == {"id", "kind", "question", "order", "arms", "expected_docs_missing", "recorded"}
        assert row["recorded"] in ("pass", "fail", None)
        assert sorted(row["order"]) == sorted(row["arms"])
        for leg in row["arms"].values():
            assert set(leg) == ROW_ARM_KEYS and leg["verdict"] in {"pass", "fail", "abstain", "error"}


def first_datagov(order):
    return next(i for i, code in enumerate(order) if arms.ARMS[code].system == "datagov")


# --------------------------------------------------------------------------- dry run
def test_post_dry_run_end_to_end(gold, documents, corpus):
    world = World(documents)
    code, out = run(world, "--post-dry-run", "--stratified", "2", "--run-id", "dry-1")
    assert code == 0, out
    assert world.paid_calls() == []                                    # no /ask, no eval answer
    assert "GET /health" in world.calls and "GET /records/00000000" in world.calls
    assert world.calls.count("GET " + clients.DOCUMENTS_PATH) == -(-len(documents) // 150)
    assert "GET " + clients.DIGEST_PATH in world.calls
    statuses = [p["status"] for p in world.posts]
    assert statuses == ["running", "running", "done"]                  # start, at 10 questions, end
    sizes = [len(p["rows"]) for p in world.posts]
    assert sizes[0] == 0 and 10 <= sizes[1] <= 14 and sizes[2] == 16   # 2 per kind; the 11th may finish first
    final = world.posts[-1]
    check_shape(final)
    s, params = final["summary"], final["params"]
    assert final["run_id"] == "dry-1" and params["dry_run"] is True
    assert params["judge_model"] == "dry-run" and params["selected"] == 16 and params["gold_rows"] == 121
    assert params["luccy"] == {"version": "0.13.1", "git": "abc123", "index_sig": "sig-1",
                               "corpus_pages": len(documents)}
    assert params["corpus_digest"] == {"count": len(documents), "digest": "f" * 64}
    assert params["corpus_ids"]["natural"] == len(documents) - 1 and params["corpus_ids"]["gd_dg"] == 1
    assert params["concurrency"] == 2 and params["arm_order"].startswith("rotated")
    assert s["questions"] == 16 and list(s["arms"]) == DEFAULT
    assert DEFAULT == ["A", "A2", "C"]
    assert {a: s["arms"][a]["label"] for a in s["arms"]} == {
        "A": "Luccy", "A2": "Luccy (2nd run)", "C": "DataGov chat · Luccy only"}
    assert [(p["a"], p["b"]) for p in s["agreement"]] == [("A", "A2"), ("C", "A"), ("A", "recorded"),
                                                         ("C", "recorded")]
    assert [v["arm"] for v in s["verdicts"]] == ["C"]
    assert all(v["text"] == "Too few questions to judge (n=16)" for v in s["verdicts"])
    assert s["excluded"] == {"infra_error": []}
    assert s["spent_usd"] == 0 and all(a["est_cost_usd"] == 0 for a in s["arms"].values())
    assert all(s["by_kind"]["abstain"][a]["n"] == 2 for a in DEFAULT)
    assert all(s["by_kind"]["warehouse"][a]["n"] == 2 for a in DEFAULT)         # warehouse rows are scored
    assert s["complete"] is False and s["gold"]["selected"] == 16 and s["gold"]["rows"] == 121
    selected_rows = runner.select(gold, stratified=2)
    assert [m["id"] for m in s["expected_docs_missing"]] == [r["id"] for r in selected_rows if corpus.missing(r)]
    for row in final["rows"]:
        gold_row = next(r for r in gold if r["id"] == row["id"])
        assert row["expected_docs_missing"] == corpus.missing(gold_row)
    selected = [r["id"] for r in selected_rows]
    for row in final["rows"]:                                          # every citation in luccy ids
        assert row["order"] == runner.arm_order(DEFAULT, selected.index(row["id"]))
        for leg in row["arms"].values():
            assert set(leg["cited"]) <= corpus.ids
    assert len({row["order"][0] for row in final["rows"]}) == len(DEFAULT)      # no arm always goes first
    verbatim = [r["arms"]["C"]["answer"] for r in final["rows"] if r["arms"]["C"]["verdict"] != "error"]
    assert all(a.rstrip().split("\n")[-1].startswith("Answered by the Document Analyzer") for a in verbatim)
    assert "SUMMARY_JSON " in out
    printed = json.loads(out.split("SUMMARY_JSON ", 1)[1])
    assert printed == s
    assert "run dry-1: done, 16 questions" in out
    assert "golden set: 16 of 121 questions" in out and "PARTIAL: not the full golden set" in out


def test_a_plain_dry_run_posts_nothing(documents):
    world = World(documents)
    code, out = run(world, "--dry-run", "--limit", "3")
    assert code == 0 and world.posts == [] and world.paid_calls() == []
    assert "[not posted]" in out and "SUMMARY_JSON " in out
    assert runner.parse_args(["--post-dry-run"]).dry_run is True


def test_the_client_never_uses_proxy_settings(documents, monkeypatch):
    """DataGov answers 404 to an internal call that came through a proxy (X-Forwarded-For,
    X-Real-IP, Forwarded): the runner's client ignores HTTP(S)_PROXY."""
    made = []

    class Spy(httpx.Client):
        def __init__(self, *a, **kw):
            made.append(kw)
            super().__init__(*a, **kw)
    monkeypatch.setattr(runner.httpx, "Client", Spy)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    world = World(documents)
    code, _ = run(world, "--dry-run", "--limit", "1")
    assert code == 0 and made and all(kw.get("trust_env") is False for kw in made)


# --------------------------------------------------------------------------- the golden set, exactly
def test_a_full_run_is_luccys_golden_set_against_its_recorded_score(gold, documents, corpus):
    """No flag: every one of the 121 rows, warehouse and missing documents included, scored and
    set against luccy's recorded run (its score, per-question verdicts and configuration)."""
    from eval.gold import run_quality_gate as gate
    baseline = golden.load_baseline()
    world = World(documents)
    code, out = run(world, "--post-dry-run", "--run-id", "gold-1")
    assert code == 0, out
    final = world.posts[-1]
    check_shape(final)
    s, rows = final["summary"], final["rows"]
    assert s["questions"] == len(rows) == 121 and s["complete"] is True
    assert sorted(r["id"] for r in rows) == sorted(r["id"] for r in gold)        # every row, once
    assert s["gold"] == {"rows": 121, "selected": 121, "digest": gate.gold_digest(gold),
                         "baseline_digest": baseline["gold_digest"], "same_as_recorded": True, "changed_ids": []}
    assert s["excluded"] == {"infra_error": []}
    assert {a: s["by_kind"]["warehouse"][a]["n"] for a in DEFAULT} == {a: 10 for a in DEFAULT}
    assert sum(cell["n"] for cell in (s["by_kind"][k]["A"] for k in s["by_kind"])) == 121
    flagged = {m["id"]: m["missing"] for m in s["expected_docs_missing"]}
    assert flagged == {r["id"]: corpus.missing(r) for r in gold if corpus.missing(r)} and "sh-001" in flagged
    assert next(r for r in rows if r["id"] == "sh-001")["arms"]["C"]["verdict"] in {"pass", "fail", "abstain"}

    rec = s["luccy_recorded"]
    scores = {x["id"]: x for x in baseline["scores"]}
    assert (rec["ts"], rec["n"], rec["n_pass"], rec["pass_rate"], rec["fact_ratio"]) == (
        baseline["ts"], baseline["n"], baseline["n_pass"], baseline["pass_rate"], baseline["fact_ratio"])
    assert (rec["judge_model"], rec["judge_votes"], rec["gold_digest"]) == (
        baseline["judge_model"], baseline["manifest"]["judge_votes"], baseline["gold_digest"])
    assert rec["pass_by_id"] == {i: x["pass"] for i, x in scores.items()}
    on = rec["on_this_run"]
    assert (on["n"], on["pass"], on["pass_rate"]) == (121, baseline["n_pass"], round(baseline["n_pass"] / 121, 4))
    assert all(r["recorded"] == ("pass" if scores[r["id"]]["pass"] else "fail") for r in rows)
    pair = next(p for p in s["agreement"] if (p["a"], p["b"]) == ("A", "recorded"))
    assert pair["n"] == 121 and pair["both_pass"] + pair["only_b"] == baseline["n_pass"]

    drift = s["config_drift"]
    assert drift["documents"]["recorded"]["index_dir"] == baseline["manifest"]["env"]["LUCCY_INDEX_DIR"]
    assert drift["documents"]["current"] == {"documents": len(documents), "digest": "f" * 64, "index_sig": "sig-1"}
    expected = golden.config_drift(baseline, golden.current_manifest(baseline["judge_model"]))
    assert drift["differences"] == expected["differences"]                     # luccy's own drift, as reported
    assert f"golden set: 121 of 121 questions, digest {gate.gold_digest(gold)}, the same set" in out
    assert "PARTIAL" not in out and f"luccy recorded {baseline['ts']}: " in out


def test_a_smoke_run_says_it_is_not_the_full_golden_set(documents):
    world = World(documents)
    code, out = run(world, "--post-dry-run", "--ids", "wh-001,sh-001", "--run-id", "smoke-1")
    assert code == 0, out
    s = world.posts[-1]["summary"]
    assert s["complete"] is False and s["gold"]["selected"] == 2 and s["gold"]["rows"] == 121
    assert s["gold"]["same_as_recorded"] is True                               # the set itself is luccy's
    assert world.posts[-1]["params"]["ids"] == ["wh-001", "sh-001"]
    assert sorted(r["id"] for r in world.posts[-1]["rows"]) == ["sh-001", "wh-001"]
    assert s["luccy_recorded"]["on_this_run"]["n"] == 2
    assert "[PARTIAL: not the full golden set]" in out


@pytest.mark.parametrize("change, says", [
    ({"model": "gemini-2.0-pro"}, "model gemini-2.0-pro, the recorded run used gemini-3.5-flash-lite "
                                  "(set SERVING_JUDGE_MODEL=gemini-3.5-flash-lite)"),
    ({"votes": 3}, "3 vote(s) per answer, the recorded run used 1"),
])
def test_a_judge_other_than_the_recorded_runs_is_refused_before_any_call(documents, change, says, capsys):
    from eval.gold.judge import Judge
    world = World(documents)
    code, _ = run(world, "--limit", "3", judge=Judge(dry_run=True, **change))
    assert code == 2 and world.calls == [] and world.posts == []              # not even a free call
    err = capsys.readouterr().err
    assert "the judge is not the one luccy's recorded score used" in err and says in err


def test_without_luccys_recorded_run_nothing_is_asked(documents, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(golden, "baseline_path", lambda: tmp_path / "baseline.summary.json")
    world = World(documents)
    code, _ = run(world, "--dry-run", judge=dry_judge())
    assert code == 2 and world.calls == [] and world.posts == []
    assert "luccy's recorded run" in capsys.readouterr().err


# --------------------------------------------------------------------------- paid path, faked
def test_paid_path_maps_judges_prices_and_never_retries(gold, documents, corpus):
    selected = runner.select(gold, limit=6)
    failing = selected[2]["id"]
    ask, records = luccy_answers(gold, corpus)
    world = World(documents, luccy_ask=ask, records=records,
                  datagov_answer=datagov_answers(gold, corpus, fail={("C", failing)}))
    code, out = run(world, "--limit", "6", "--run-id", "live-1", judge=dry_judge())
    assert code == 0, out
    assert world.calls.count("POST /ask") == 12                        # A and A2, once each
    assert len(world.answers) == 6                                     # C, the failure included
    probes = world.calls.count("POST " + clients.EVAL_ANSWER_PATH) - len(world.answers)
    assert probes == 1 + 1                                             # the unknown arm, then arm C
    assert len([c for c in world.calls if c.startswith("GET /records/") and not c.endswith("00000000")]) == 12
    final = world.posts[-1]
    check_shape(final)
    s, rows = final["summary"], {r["id"]: r for r in final["rows"]}
    assert final["status"] == "done" and s["questions"] == 6
    assert final["params"]["readiness"] == {"C": "ready"}
    assert set(rows) == {r["id"] for r in selected}
    assert s["arms"]["A"]["est_cost_usd"] == pytest.approx(0.30)       # 6 x luccy's record cost
    assert (s["arms"]["A"]["cost_source"], s["arms"]["C"]["cost_source"]) == ("measured", "estimate")
    assert s["arms"]["C"]["est_cost_usd"] > 0 and s["spent_usd"] > 0.6  # an older server: no cost, estimated
    assert rows[failing]["arms"]["C"] == {"verdict": "error", "infra_error": True, "cited": [],
                                          "answer": "[error] HTTP 502: bad gateway",
                                          "latency_s": rows[failing]["arms"]["C"]["latency_s"], "fact_ratio": None}
    assert (s["arms"]["C"]["errors"], s["arms"]["C"]["infra_errors"], s["arms"]["C"]["n"]) == (1, 1, 5)
    assert s["excluded"]["infra_error"] == [{"id": failing, "arm": "C", "message": "HTTP 502: bad gateway"}]
    c = next(v for v in s["verdicts"] if v["arm"] == "C")
    assert c["n"] == 5                                                 # the failed question left the pair
    for row in selected:                                               # same answer, footer or not
        rid = row["id"]
        if rid != failing and arms.fake_good("A", rid) and arms.fake_good("C", rid):
            assert rows[rid]["arms"]["C"]["verdict"] == rows[rid]["arms"]["A"]["verdict"]
            assert rows[rid]["arms"]["C"]["fact_ratio"] == rows[rid]["arms"]["A"]["fact_ratio"]
    answered = [r["arms"]["C"]["answer"] for r in final["rows"] if r["arms"]["C"]["verdict"] != "error"]
    assert answered and all(a.rstrip().split("\n")[-1].startswith("Answered by the Document Analyzer")
                            for a in answered), "answers are posted as the arm wrote them"
    for index, row in enumerate(selected):
        assert rows[row["id"]]["order"] == runner.arm_order(DEFAULT, index)


def test_luccy_busy_is_retried_free_then_an_infra_error(gold, documents, corpus, monkeypatch):
    waits = []
    monkeypatch.setattr(runner.Run, "pause", lambda self, seconds: waits.append(seconds) or False)
    selected = runner.select(gold, limit=2)
    ask, records = luccy_answers(gold, corpus, fail_ids={selected[0]["id"]})
    world = World(documents, luccy_ask=ask, records=records, datagov_answer=datagov_answers(gold, corpus))
    code, _ = run(world, "--limit", "2", "--arms", "A,C", judge=dry_judge())
    assert code == 0 and world.calls.count("POST /ask") == 2 + 3       # three free retries of the busy one
    assert waits == [arms.BUSY_WAIT_S] * 3
    final = world.posts[-1]
    leg = next(r for r in final["rows"] if r["id"] == selected[0]["id"])["arms"]["A"]
    assert leg["verdict"] == "error" and leg["infra_error"] and "503" in leg["answer"]
    s = final["summary"]
    assert s["noise_floor"] is None                                    # no A2: luccy's noise unmeasured
    assert s["excluded"]["infra_error"] == [{"id": selected[0]["id"], "arm": "A", "message": "HTTP 503: busy"}]
    assert s["arms"]["A"]["n"] == 1 and s["verdicts"][0]["n"] == 1 and s["verdicts"][0]["within_noise"] is None


def test_spend_cap_stops_cleanly(gold, documents, corpus):
    ask, records = luccy_answers(gold, corpus)
    world = World(documents, luccy_ask=ask, records=records, datagov_answer=datagov_answers(gold, corpus))
    code, out = run(world, "--limit", "6", "--max-usd", "0.45", "--concurrency", "1", judge=dry_judge())
    assert code == 1
    assert world.calls.count("POST /ask") == 4                          # two questions, then the cap
    final = world.posts[-1]
    assert final["status"] == "failed" and final["summary"]["questions"] == 2
    assert final["params"]["stopped_reason"].startswith("spend cap")
    assert final["summary"]["spent_usd"] <= 0.45 and "stopping: spend cap" in out
    assert final["summary"]["complete"] is False


def test_the_spend_cap_counts_measured_cost(gold, documents, corpus):
    """DataGov's measured costs replace the priors: cheaper measured answers leave room for more."""
    ask, records = luccy_answers(gold, corpus)
    world = World(documents, luccy_ask=ask, records=records,
                  datagov_answer=datagov_answers(gold, corpus, cost={"C": 0.01}))
    run(world, "--limit", "6", "--max-usd", "0.45", "--concurrency", "1", judge=dry_judge())
    final = world.posts[-1]
    spent = final["summary"]["spent_usd"]
    assert spent == pytest.approx(final["summary"]["questions"] * (0.05 + 0.05 + 0.01))
    assert final["summary"]["questions"] > 2 and spent <= 0.45
    assert all(a["cost_source"] == "measured" for a in final["summary"]["arms"].values())


def test_luccys_own_spend_cap_stops_the_run_before_any_other_arm_is_paid(gold, documents, corpus):
    def capped(question):
        return 429, {"error": "spend cap reached"}
    world = World(documents, luccy_ask=capped, datagov_answer=datagov_answers(gold, corpus))
    code, _ = run(world, "--limit", "6", "--concurrency", "1", judge=dry_judge())
    order = runner.arm_order(DEFAULT, 0)
    first_luccy = next(code_ for code_ in order if arms.ARMS[code_].system == "luccy")
    assert code == 1 and world.calls.count("POST /ask") == 1           # nothing after the 429
    assert len(world.answers) == order.index(first_luccy)              # only the arms asked before it
    final = world.posts[-1]
    assert final["status"] == "failed" and final["summary"]["questions"] == 1
    assert final["params"]["stopped_reason"].startswith(f"{first_luccy} reached its spend cap (HTTP 429")
    assert final["summary"]["arms"][first_luccy]["infra_errors"] == 1
    assert final["rows"][0]["order"] == order[:order.index(first_luccy) + 1]


def test_a_failed_final_post_exits_1_with_the_summary_printed(gold, documents):
    world = World(documents, runs_status=[200, 500])
    code, out = run(world, "--post-dry-run", "--limit", "3")
    assert code == 1 and "SUMMARY_JSON " in out
    assert [p["status"] for p in world.posts] == ["running", "done"]
    run_id = world.posts[0]["run_id"]                                  # generated: run-<UTC stamp>-<6 hex>
    assert re.fullmatch(r"run-\d{8}T\d{6}Z-[0-9a-f]{6}", run_id) and runner.valid_run_id(run_id)
    assert {p["run_id"] for p in world.posts} == {run_id}


def test_body_stays_under_the_cap(gold, documents, corpus):
    long = "word " * 2000
    ask, records = luccy_answers(gold, corpus)

    def long_ask(question):
        status, body = ask(question)
        return status, dict(body, answer=body["answer"] + long)
    world = World(documents, luccy_ask=long_ask, records=records, datagov_answer=datagov_answers(gold, corpus))
    code, _ = run(world, "--limit", "3", "--arms", "A", judge=dry_judge())
    assert code == 0 and max(world.post_sizes) <= payload.MAX_BODY_BYTES
    assert all(len(r["arms"]["A"]["answer"]) == payload.ANSWER_CHARS for r in world.posts[-1]["rows"])


# --------------------------------------------------------------------------- infrastructure, fatal stops
def test_a_failed_analyzer_tool_is_an_infra_error_not_a_wrong_answer(gold, documents, corpus):
    selected = runner.select(gold, limit=6)
    broken = {selected[1]["id"], selected[4]["id"]}

    def change(code, row, body):
        if code == "C" and row["id"] in broken:
            return dict(body, answer="Sorry, the Document Analyzer could not answer.", judged_answer="",
                        tool_error=True, verbatim=False, citations=[])
        return body
    ask, records = luccy_answers(gold, corpus)
    world = World(documents, luccy_ask=ask, records=records,
                  datagov_answer=datagov_answers(gold, corpus, change=change))
    code, _ = run(world, "--limit", "6", "--arms", "A,A2,C", judge=dry_judge())
    assert code == 0
    final = world.posts[-1]
    s, rows = final["summary"], {r["id"]: r for r in final["rows"]}
    for rid in broken:
        leg = rows[rid]["arms"]["C"]
        assert leg["verdict"] == "error" and leg["infra_error"]
        assert leg["answer"] == "Sorry, the Document Analyzer could not answer."
    assert {(e["id"], e["arm"]) for e in s["excluded"]["infra_error"]} == {(rid, "C") for rid in broken}
    assert all(e["message"].startswith("tool error") for e in s["excluded"]["infra_error"])
    assert s["arms"]["C"]["infra_errors"] == 2 and s["arms"]["C"]["n"] == 4
    c = next(v for v in s["verdicts"] if v["arm"] == "C")
    assert c["n"] == 4 and c["delta"] == next(p for p in s["agreement"] if p["a"] == "C")["only_a"] - \
        next(p for p in s["agreement"] if p["a"] == "C")["only_b"]


def test_a_misconfigured_eval_user_stops_the_run_at_the_first_503(gold, documents, corpus):
    """Review probe: every DataGov arm answers 503 (LUCCY_EVAL_USER_ID not set; the server checks
    it after the free probes). The run stops at the first one instead of paying luccy for every
    question and reporting DataGov as worse."""
    def datagov_503(arm, question):
        return 503, {"detail": "LUCCY_EVAL_USER_ID must be the id of an active member of organization 1."}
    ask, records = luccy_answers(gold, corpus)
    world = World(documents, luccy_ask=ask, datagov_answer=datagov_503, records=records)
    code, out = run(world, "--limit", "12", "--concurrency", "1", "--run-id", "probe-misconfig", judge=dry_judge())
    order = runner.arm_order(DEFAULT, 0)
    assert code == 1
    assert len(world.answers) == 1                                     # the first DataGov arm asked, then stop
    assert world.calls.count("POST /ask") == first_datagov(order)      # only luccy arms asked before it
    final = world.posts[-1]
    assert final["status"] == "failed"
    assert "DataGov's eval endpoint is not set up" in final["params"]["stopped_reason"]
    s = final["summary"]
    assert s["questions"] == 1 and len(s["excluded"]["infra_error"]) == 1
    assert all(v["outcome"] == "inconclusive" and v["text"].startswith("Too few") for v in s["verdicts"])
    assert not any("Worse" in v["text"] for v in s["verdicts"])
    assert "stopping: " in out


def test_a_misconfigured_eval_user_caught_by_the_free_probe_exits_2(documents, capsys):
    world = World(documents, eval_status=503)                          # a server that checks it first
    code, _ = run(world, "--limit", "3", judge=dry_judge())
    assert code == 2 and world.paid_calls() == [] and world.posts == []
    assert "arm C (chat_analyzer_only) is not set up on DataGov (HTTP 503" in capsys.readouterr().err


def test_five_errors_in_a_row_from_one_arm_stop_the_run(gold, documents, corpus):
    def broken(code, row, body):
        return dict(body, error="the chat model timed out") if code == "C" else body
    ask, records = luccy_answers(gold, corpus)
    world = World(documents, luccy_ask=ask, records=records,
                  datagov_answer=datagov_answers(gold, corpus, change=broken))
    code, _ = run(world, "--limit", "10", "--arms", "A,C", "--concurrency", "1", judge=dry_judge())
    assert code == 1
    final = world.posts[-1]
    reason = final["params"]["stopped_reason"]
    assert reason.startswith("C failed 5 questions in a row (last: the chat model timed out)")
    s = final["summary"]
    assert s["questions"] == 5 and s["arms"]["C"]["errors"] == 5
    assert s["arms"]["C"]["infra_errors"] == 0 and s["arms"]["C"]["pass"] == 0     # its own errors: failures
    assert len([a for a, _ in world.answers]) == 5


# --------------------------------------------------------------------------- interrupts
def blocking_luccy(gold, corpus):
    """luccy's /ask that blocks until `release` is set; `asked` is set at the first call."""
    by_question = {r["question"]: r for r in gold}
    asked, release = threading.Event(), threading.Event()

    def ask(question):
        asked.set()
        release.wait(30)
        body = arms.fake_luccy_body("A", by_question[question], corpus)
        return 200, dict(body, log=None)
    return ask, asked, release


def kick(asked, signum, times, then=None):
    def go():
        asked.wait(10)
        time.sleep(0.3)
        for _ in range(times):
            os.kill(os.getpid(), signum)
            time.sleep(0.3)
        if then:
            then()
    threading.Thread(target=go, daemon=True).start()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_a_second_interrupt_abandons_the_questions_in_flight(gold, documents, corpus, signum):
    ask, asked, release = blocking_luccy(gold, corpus)
    world = World(documents, luccy_ask=ask, datagov_answer=datagov_answers(gold, corpus))
    before = signal.getsignal(signum)
    kick(asked, signum, 2)
    t0 = time.monotonic()
    try:
        code, out = run(world, "--limit", "6", "--arms", "A,C", judge=dry_judge())
    finally:
        release.set()
    assert time.monotonic() - t0 < 10                                 # never waited for the blocked answers
    assert code == 1
    final = world.posts[-1]
    assert final["status"] == "failed" and final["params"]["stopped_reason"] == "interrupted"
    assert final["summary"]["questions"] == 0 and final["rows"] == []
    assert "interrupted again: abandoning 2 question(s) in flight" in out
    assert signal.getsignal(signum) is before                          # the handlers are restored


def test_a_first_interrupt_finishes_the_questions_in_flight(gold, documents, corpus):
    ask, asked, release = blocking_luccy(gold, corpus)
    world = World(documents, luccy_ask=ask, datagov_answer=datagov_answers(gold, corpus))
    kick(asked, signal.SIGINT, 1, then=release.set)
    try:
        code, out = run(world, "--limit", "6", "--arms", "A,C", judge=dry_judge())
    finally:
        release.set()
    assert code == 1
    final = world.posts[-1]
    assert final["status"] == "failed" and final["params"]["stopped_reason"] == "interrupted"
    assert final["summary"]["questions"] == 2                          # the two in flight, no new one
    assert all(set(r["arms"]) == {"A", "C"} for r in final["rows"])
    assert "interrupt again to abandon them" in out


def test_the_loop_abandons_on_a_second_keyboard_interrupt_without_a_handler():
    """Review probe: a bare Run (no signal handler installed) on Python's own SIGINT."""
    class Args:
        concurrency, dry_run, max_usd, arms, post_dry_run = 2, True, 1.0, ["A"], False

    r = runner.Run(Args(), "probe", [{"id": f"q{i}", "question": "?"} for i in range(10)], None, None, None,
                   None, None, {}, io.StringIO())
    hold = threading.Event()
    r.ask = lambda row, index, reserved: hold.wait(30) and None
    r.post = lambda status: (True, "ok")

    def kicker():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)
    threading.Thread(target=kicker, daemon=True).start()
    t0 = time.monotonic()
    try:
        r.loop()
    finally:
        hold.set()
    assert time.monotonic() - t0 < 5 and r.stopped == "interrupted" and r.interrupts.cancel.is_set()


# --------------------------------------------------------------------------- configuration stops
@pytest.mark.parametrize("change", [
    {"env": {k: v for k, v in ENV.items() if k != "DATAGOV_URL"}},
    {"env": dict(ENV, LUCCY_URL="luccy.test")},
    {"env": dict(ENV, LUCCY_API_KEY="wrong")},
    {"env": dict(ENV, LUCCY_EVAL_TOKEN="wrong")},
    {"world": {"sync_token": "other"}},
    {"world": {"digest_status": 404}},
    {"world": {"health_status": 503}},
    {"world": {"runs_status": 403}},
    {"world": {"eval_status": 503}},
    {"world": {"eval_arms": ()}},
    {"argv": ["--ids", "sh-001,nope-1"]},
])
def test_configuration_problems_exit_2_before_any_paid_call(documents, change, capsys):
    world = World(documents, **change.get("world", {}))
    code, _ = run(world, *change.get("argv", []), env=change.get("env", ENV), judge=dry_judge())
    assert code == 2
    assert world.paid_calls() == []
    assert all(p["status"] == "running" for p in world.posts)
    assert "config error" in capsys.readouterr().err


def test_an_unknown_arm_names_the_arm(documents, capsys):
    world = World(documents, eval_arms=())                             # a DataGov that knows no eval arm
    assert run(world, "--arms", "A,C", judge=dry_judge())[0] == 2
    assert "DataGov does not know arm C (chat_analyzer_only)" in capsys.readouterr().err


def test_the_real_judge_needs_its_key(documents, capsys):
    from serving import config
    if config.GEMINI_API_KEY:
        pytest.skip("a judge key is set in this environment")
    world = World(documents)
    code, _ = run(world, "--limit", "1")
    assert code == 2 and world.paid_calls() == [] and world.posts == []
    assert "SERVING_GEMINI_API_KEY" in capsys.readouterr().err


def test_an_empty_corpus_is_a_configuration_error(capsys):
    world = World([])
    assert run(world, "--dry-run")[0] == 2 and world.posts == []
    assert "empty" in capsys.readouterr().err


def test_bad_flags_exit_2():
    for argv in (["--arms", "A,Z"], ["--arms", "B1"], ["--arms", "A,B2"], ["--arms", "C,N1"], ["--arms", "N2"],
                 ["--arms", ""], ["--kinds", "nope"], ["--ids", ""], ["--ids", " , "], ["--include-warehouse"],
                 ["--concurrency", "3"], ["--concurrency", "0"],
                 ["--max-usd", "0"], ["--limit", "0"], ["--run-id", "../x"], ["--run-id", "a..b"],
                 ["--run-id", "-x"], ["--run-id", "a/b"], ["--run-id", "a" * 129], ["--run-id", ""]):
        with pytest.raises(SystemExit) as e:
            runner.parse_args(argv)
        assert e.value.code == 2, argv
    for good in ("run-20261016T142233Z-3f9a1c", "A", "x:y.z_w-1", "a" * 128):
        assert runner.parse_args(["--run-id", good]).run_id == good
    assert runner.parse_args([]).arms == ["A", "A2", "C"]
    args = runner.parse_args(["--arms", "C,A2,A"])
    assert args.arms == ["A", "A2", "C"] and args.max_usd == 40 and args.concurrency == 2
    assert args.ids is None and args.kinds is None and args.limit is None and args.stratified is None
    assert not args.dry_run and not args.post_dry_run


def test_module_entry_point():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for key in ENV:
        env.pop(key, None)
    done = subprocess.run([sys.executable, "-m", "luccy_qdrant.compare", "--dry-run"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 2 and "config error: set DATAGOV_URL" in done.stderr
