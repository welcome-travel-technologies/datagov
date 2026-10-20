"""Per-arm measures, agreement, McNemar, the noise floor, the paired verdicts and the spend guard."""
import math

import pytest

from luccy_qdrant.compare import arms, budget, metrics

MINUS = "−"


def row(qid, kind="single_hop", behaviour="answer", docs=("1",), facts=2):
    return {"id": qid, "kind": kind, "question": f"q {qid}", "expected_behaviour": behaviour,
            "expected_doc_ids": list(docs), "expected_facts": [f"fact {i}" for i in range(facts)]}


def outcome(r, *, passed=False, status="ok", infra=None, abstained=False, cited=("1",), supported=1, leak=False,
            behaviour_ok=True, latency=10.0, answer_usd=0.01, judge_usd=0.0, judge_error=None, cost_source=None):
    infra = status != "ok" if infra is None else infra
    leg = {"status": status, "error": None if status == "ok" else "HTTP 502: failed", "infra": infra,
           "answer": "text", "cited": list(cited), "abstained": abstained, "gaps": [], "latency_s": latency}
    score = None if status != "ok" or judge_error else {
        "pass": passed, "facts_supported": supported, "facts_total": len(r["expected_facts"]),
        "behaviour_ok": behaviour_ok, "mechanical_leak": leak}
    return metrics.make_outcome(r, leg, score, judge_error, answer_usd, judge_usd, cost_source)


def summarize(patterns: dict[str, list], arms_=None):
    """patterns: {arm: [True / False / "infra"] per question}."""
    codes = arms_ or list(patterns)
    n = len(next(iter(patterns.values())))
    rows = [row(f"q{i}") for i in range(n)]
    outcomes = {r["id"]: {a: (outcome(r, status="error") if patterns[a][i] == "infra"
                              else outcome(r, passed=patterns[a][i])) for a in codes}
                for i, r in enumerate(rows)}
    return metrics.summarize(codes, rows, outcomes, 0.0)


def verdict_of(s, arm="C"):
    return next(v for v in s["verdicts"] if v["arm"] == arm)


# --------------------------------------------------------------------------- math
def test_mcnemar_exact():
    assert metrics.mcnemar_exact(0, 0) == 1.0
    assert metrics.mcnemar_exact(5, 0) == pytest.approx(2 / 32)
    assert metrics.mcnemar_exact(0, 5) == pytest.approx(2 / 32)
    assert metrics.mcnemar_exact(10, 2) == pytest.approx(2 * (1 + 12 + 66) / 4096)
    assert metrics.mcnemar_exact(3, 3) == 1.0
    assert metrics.mcnemar_exact(1, 0) == 1.0


def test_percentile():
    assert metrics.percentile([1, 2, 3, 4], 50) == 2.5
    assert metrics.percentile([10, 20, 30, 40, 50], 90) == pytest.approx(46.0)
    assert metrics.percentile([], 50) is None and metrics.percentile([7, None], 90) == 7


def test_paired_difference_and_its_interval():
    d, lo, hi = metrics.paired_diff(1, 10, 100)
    var = (11 - 81 / 100) / 100 ** 2
    assert d == pytest.approx(-0.09)
    assert (lo, hi) == pytest.approx((-0.09 - 1.96 * math.sqrt(var), -0.09 + 1.96 * math.sqrt(var)))
    assert metrics.paired_diff(0, 0, 50) == (0.0, 0.0, 0.0)
    assert metrics.paired_diff(1, 0, 1) == (1.0, 1.0, 1.0)                     # clipped to [-1, 1]
    assert metrics.paired_diff(0, 0, 0) is None
    assert metrics.points(-0.0321) == MINUS + "3.2" and metrics.points(0.041) == "+4.1"


# --------------------------------------------------------------------------- one leg, one arm
def test_row_verdicts():
    r = row("q")
    assert outcome(r, passed=True)["verdict"] == "pass"
    assert outcome(r)["verdict"] == "fail"
    assert outcome(r, abstained=True)["verdict"] == "abstain"
    infra = outcome(r, status="error")
    assert infra["verdict"] == "error" and infra["infra_error"]
    own = outcome(r, status="error", infra=False)
    assert own["verdict"] == "error" and not own["infra_error"]                # the arm's own failure
    judged_out = outcome(r, judge_error="judge failed: RuntimeError")
    assert judged_out["verdict"] == "error" and judged_out["replied"] and judged_out["error"].startswith("judge")
    assert judged_out["infra_error"]                                           # not the arm's answer
    assert outcome(r, supported=1)["fact_ratio"] == 0.5 and outcome(r, status="error")["fact_ratio"] is None
    ab = row("ab", kind="abstain", behaviour="abstain", docs=(), facts=0)
    assert outcome(ab, passed=True, abstained=True)["verdict"] == "pass"       # a correct abstention passes
    assert outcome(r, cost_source="measured")["cost_source"] == "measured" and outcome(r)["cost_source"] == "estimate"


def test_arm_summary():
    rows = [row("a"), row("b", docs=("1|2", "3")), row("c", kind="abstain", behaviour="abstain", docs=(), facts=0),
            row("d", docs=()), row("e"), row("f")]
    items = [
        (rows[0], outcome(rows[0], passed=True, cited=["1"], supported=2, latency=2, answer_usd=0.01,
                          cost_source="measured")),
        (rows[1], outcome(rows[1], cited=["2-part-1"], supported=0, latency=4, answer_usd=0.02,
                          cost_source="measured")),
        (rows[2], outcome(rows[2], passed=True, abstained=True, cited=[], supported=0, latency=6,
                          judge_usd=0.001, cost_source="measured")),
        (rows[3], outcome(rows[3], abstained=True, cited=[], leak=True, latency=8, cost_source="measured")),
        (rows[4], outcome(rows[4], status="error", infra=False, answer_usd=0.005, cost_source="estimate")),
        (rows[5], outcome(rows[5], status="error", answer_usd=0.0, cost_source="measured")),       # infra
    ]
    s = metrics.arm_summary("C", items)
    assert s["label"] == "DataGov chat · Luccy only"
    assert (s["n"], s["answered"], s["pass"], s["errors"], s["infra_errors"]) == (5, 2, 2, 2, 1)
    assert s["pass_rate"] == 0.4                                                # the arm's own error is a failure
    assert s["expected_doc_hit_rate"] == pytest.approx(round(1 / 3, 4))         # a, b (2 of 1|2 but not 3), e
    assert s["fact_recall"] == pytest.approx(round(3 / 8, 4))                   # e's 2 gold facts unsupported
    assert (s["correct_abstentions"], s["abstain_rows"], s["privacy_leaks"]) == (1, 1, 1)
    assert s["p50_latency_s"] == 5.0 and s["p90_latency_s"] == pytest.approx(7.4)
    assert s["est_cost_usd"] == pytest.approx(0.01 + 0.02 + 0.011 + 0.01 + 0.005)
    assert s["cost_source"] == "mixed"
    assert metrics.arm_summary("A", items[:4])["cost_source"] == "measured"
    assert metrics.arm_summary("A", items[4:5])["cost_source"] == "estimate"
    assert metrics.arm_summary("A", [])["cost_source"] is None


def test_agreement_and_by_kind_leave_infrastructure_failures_out():
    rows = [row(f"q{i}", kind="multi_hop" if i % 2 else "single_hop") for i in range(7)]
    plan = [(True, True), (True, True), (True, False), (False, True), (False, False), (True, "own error"),
            (True, "infra")]
    outcomes = {}
    for r, (a, b) in zip(rows, plan):
        c = (outcome(r, status="error", infra=False) if b == "own error" else
             outcome(r, status="error") if b == "infra" else outcome(r, passed=b))
        outcomes[r["id"]] = {"A": outcome(r, passed=a), "C": c}
    pair = metrics.agreement("C", "A", rows, outcomes)
    assert pair == {"a": "C", "b": "A", "n": 6, "both_pass": 2, "only_a": 1, "only_b": 2, "both_fail": 1,
                    "mcnemar_p": round(metrics.mcnemar_exact(1, 2), 4)}
    kinds = metrics.by_kind(rows, outcomes, ["A", "C"])
    assert kinds == {"multi_hop": {"A": {"n": 3, "pass": 2}, "C": {"n": 3, "pass": 2}},
                     "single_hop": {"A": {"n": 4, "pass": 3}, "C": {"n": 3, "pass": 1}}}


# --------------------------------------------------------------------------- verdicts (review probes)
N = 40
A = [i < 28 for i in range(N)]


def test_without_a2_there_is_no_noise_floor():
    """Probe 1: arms A and C only. The floor was never measured, so it is not reported, and the
    verdict uses the fixed 5-point margin and says luccy's variation was not measured."""
    c = list(A)
    c[0] = c[1] = False                                                         # C loses 2 of 40
    s = summarize({"A": A, "C": c})
    assert s["noise_floor"] is None
    v = verdict_of(s)
    assert v["margin"] == 0.05 and v["luccy_disagree_rate"] is None
    assert v["within_noise"] is None and v["disagrees_more_than_luccy_itself"] is None
    assert v["outcome"] == "inconclusive" and v["delta"] == -2 and v["n"] == 40
    same = summarize({"A": A, "C": list(A)})
    v = verdict_of(same)
    assert v["outcome"] == "same" and v["within_noise"] is None
    assert v["text"] == ("Same as Luccy (difference between +0.0 and +0.0 points; within ±5.0 points, "
                         "Luccy's own variation not measured)")


def test_equal_pass_counts_with_heavy_disagreement_are_not_called_the_same():
    """Probe 2: A/A2 flip on 4 questions; C disagrees with A on 20 (10 each way). Same pass
    count, so the old rule said "Same"; the interval is far wider than luccy's own variation."""
    a2 = list(A)
    a2[0] = a2[1] = False
    a2[30] = a2[31] = True
    c = list(A)
    for i in range(10):
        c[i] = False
    for i in range(28, 38):
        c[i] = True
    s = summarize({"A": A, "A2": a2, "C": c})
    assert s["noise_floor"] == {"flips": 4, "threshold": 4, "disagree_rate": 0.1}
    v = verdict_of(s)
    half = 1.96 * math.sqrt(20 / 40 ** 2)
    assert v["delta"] == 0 and v["diff_rate"] == 0.0
    assert (v["ci_low"], v["ci_high"]) == (round(-half, 4), round(half, 4))
    assert v["margin"] == 0.1 and v["threshold"] == 4
    assert v["outcome"] == "inconclusive" and v["within_noise"] is False
    assert v["text"] == f"No clear difference yet (95% CI {MINUS}21.9 to +21.9 points) — run more questions"
    assert (v["disagree_rate"], v["luccy_disagree_rate"]) == (0.5, 0.1)
    assert v["disagrees_more_than_luccy_itself"] is True


def test_zero_flips_still_leaves_a_five_point_margin():
    """Probe 3: luccy never flips; C loses 2 of 40. The old floor called that "Same"; 5 points of
    margin with this interval (−11.8 to +1.8) cannot."""
    c = list(A)
    c[0] = c[1] = False
    s = summarize({"A": A, "A2": list(A), "C": c})
    assert s["noise_floor"] == {"flips": 0, "threshold": 2, "disagree_rate": 0.0}
    v = verdict_of(s)
    assert v["margin"] == 0.05 and v["outcome"] == "inconclusive" and v["within_noise"] is False
    assert v["text"] == f"No clear difference yet (95% CI {MINUS}11.8 to +1.8 points) — run more questions"
    assert v["disagrees_more_than_luccy_itself"] is False                       # 5 points more: not more than 5


def test_a_small_run_is_too_few_to_judge():
    """Probe 4: 12 questions, A 75% and C 41.7%; the old rule said "Same" (within 4 passes)."""
    n = 12
    a = [i < 9 for i in range(n)]
    a2 = list(a)
    a2[0] = a2[1] = False
    a2[10] = a2[11] = True
    c = [i < 5 for i in range(n)]
    s = summarize({"A": a, "A2": a2, "C": c})
    assert s["arms"]["A"]["pass_rate"] == 0.75 and s["arms"]["C"]["pass_rate"] == 0.4167
    v = verdict_of(s)
    assert v["outcome"] == "inconclusive" and v["text"] == "Too few questions to judge (n=12)"
    assert v["within_noise"] is False and v["delta"] == -4 and v["n"] == 12
    assert v["ci_low"] is not None and v["ci_high"] < 0                        # still reported


def test_a_clear_loss_inside_luccys_noise_is_worse_not_same():
    """Probe 6: luccy flips 26 of 100; C loses 10 and gains 1. The old rule said "Same"
    (within ceil(1.96 sqrt 26) = 10); the paired interval is entirely below zero."""
    n = 100
    a = [i < 60 for i in range(n)]
    a2 = list(a)
    for i in range(13):
        a2[i] = False
    for i in range(60, 73):
        a2[i] = True
    c = list(a)
    for i in range(20, 30):
        c[i] = False
    c[90] = True
    s = summarize({"A": a, "A2": a2, "C": c})
    v = verdict_of(s)
    assert v["outcome"] == "worse" and v["delta"] == -9 and v["within_noise"] is False
    assert v["text"] == f"Worse than Luccy by 9 questions (95% CI {MINUS}15.3 to {MINUS}2.7 points)"
    assert v["margin"] == 0.26 and v["threshold"] == 26 and v["luccy_disagree_rate"] == 0.26
    assert v["disagrees_more_than_luccy_itself"] is False


def test_better_same_and_singular_wording():
    n = 60
    a = [i < 30 for i in range(n)]
    better = list(a)
    for i in range(30, 45):
        better[i] = True
    v = verdict_of(summarize({"A": a, "A2": list(a), "C": better}))
    assert v["outcome"] == "better" and v["delta"] == 15 and v["text"].startswith("Better than Luccy by 15 questions (95% CI +")
    a2 = list(a)
    a2[0] = False
    a2[59] = True
    v = verdict_of(summarize({"A": a, "A2": a2, "C": list(a)}))
    assert v["outcome"] == "same" and v["within_noise"] is True
    assert v["text"] == "Same as Luccy (difference between +0.0 and +0.0 points; Luccy itself varies ±5.0 points)"
    assert metrics.verdict("C", {"n": 1, "only_a": 1, "only_b": 0}, None)["text"] == "Too few questions to judge (n=1)"
    v = metrics.verdict("C", {"n": 400, "only_a": 30, "only_b": 0}, None)
    assert v["text"].startswith("Better than Luccy by 30 questions")
    v = metrics.verdict("C", {"n": 4000, "only_a": 0, "only_b": 1}, None)       # one question, many paired
    assert v["outcome"] == "same" and v["delta"] == -1
    assert metrics.questions(1) == "1 question" and metrics.questions(2) == "2 questions"


def test_infrastructure_failures_leave_the_pair_and_are_listed():
    n = 30
    a = [i < 20 for i in range(n)]
    c = list(a)
    c[0] = c[1] = c[2] = "infra"                                                 # C's tool failed
    a_ = list(a)
    a_[3] = "infra"                                                              # luccy busy
    s = summarize({"A": a_, "A2": list(a), "C": c})
    v = verdict_of(s)
    assert v["n"] == 26 and v["delta"] == 0
    assert s["arms"]["C"]["infra_errors"] == 3 and s["arms"]["C"]["n"] == 27
    assert s["arms"]["A"]["infra_errors"] == 1 and s["arms"]["A"]["pass_rate"] == round(19 / 29, 4)
    assert s["excluded"]["infra_error"] == [
        {"id": "q0", "arm": "C", "message": "HTTP 502: failed"}, {"id": "q1", "arm": "C", "message": "HTTP 502: failed"},
        {"id": "q2", "arm": "C", "message": "HTTP 502: failed"}, {"id": "q3", "arm": "A", "message": "HTTP 502: failed"}]
    every = summarize({"A": a, "A2": list(a), "C": ["infra"] * n})              # a misconfigured arm
    v = verdict_of(every)
    assert v["n"] == 0 and v["outcome"] == "inconclusive" and v["text"] == "Too few questions to judge (n=0)"
    assert v["ci_low"] is None and v["diff_rate"] is None and v["disagree_rate"] is None
    assert v["disagrees_more_than_luccy_itself"] is None
    assert every["arms"]["C"]["pass_rate"] is None and every["arms"]["C"]["pass"] == 0


VERDICT_KEYS = {"arm", "vs", "outcome", "text", "n", "delta", "threshold", "diff_rate", "ci_low", "ci_high", "margin",
                "disagree_rate", "luccy_disagree_rate", "within_noise", "disagrees_more_than_luccy_itself"}
ARM_KEYS = {"label", "n", "answered", "pass", "pass_rate", "expected_doc_hit_rate", "fact_recall",
            "correct_abstentions", "abstain_rows", "privacy_leaks", "errors", "infra_errors", "p50_latency_s",
            "p90_latency_s", "est_cost_usd", "cost_source"}


def test_summary_shape_is_exactly_what_the_frontend_reads():
    rows = [row(f"q{i}") for i in range(4)]
    codes = ["A", "A2", "C"]
    outcomes = {r["id"]: {c: outcome(r, passed=(i + j) % 3 != 0) for j, c in enumerate(codes)}
                for i, r in enumerate(rows)}
    s = metrics.summarize(codes, rows, outcomes, 1.23456)
    assert set(s) == {"questions", "arms", "by_kind", "agreement", "noise_floor", "verdicts", "recorded", "excluded",
                      "spent_usd"}
    assert s["questions"] == 4 and s["spent_usd"] == 1.2346
    assert list(s["arms"]) == codes
    for a in s["arms"].values():
        assert set(a) == ARM_KEYS
    assert {a["label"] for a in s["arms"].values()} == {arms.ARMS[c].label for c in codes}
    assert set(s["by_kind"]["single_hop"]) == set(codes) and set(s["by_kind"]["single_hop"]["A"]) == {"n", "pass"}
    assert [(p["a"], p["b"]) for p in s["agreement"]] == [("A", "A2"), ("C", "A")]
    for p in s["agreement"]:
        assert set(p) == {"a", "b", "n", "both_pass", "only_a", "only_b", "both_fail", "mcnemar_p"}
    assert set(s["noise_floor"]) == {"flips", "threshold", "disagree_rate"}
    assert [v["arm"] for v in s["verdicts"]] == ["C"]
    for v in s["verdicts"]:
        assert set(v) == VERDICT_KEYS and v["vs"] == "A"
        pair = next(p for p in s["agreement"] if (p["a"], p["b"]) == (v["arm"], "A"))
        assert v["delta"] == pair["only_a"] - pair["only_b"] and v["n"] == pair["n"]
        assert v["outcome"] == "inconclusive" and v["text"] == "Too few questions to judge (n=4)"
    flips = s["agreement"][0]["only_a"] + s["agreement"][0]["only_b"]
    assert s["noise_floor"]["flips"] == flips
    assert s["excluded"] == {"infra_error": []}                                 # nothing else is ever left out
    assert s["recorded"] is None and "recorded" not in s["by_kind"]["single_hop"]


def test_summary_without_luccy_has_no_verdicts():
    rows = [row("q0")]
    s = metrics.summarize(["C"], rows, {"q0": {"C": outcome(rows[0], passed=True)}}, 0.0)
    assert s["verdicts"] == [] and s["agreement"] == [] and s["noise_floor"] is None
    assert s["excluded"] == {"infra_error": []}
    empty = metrics.summarize(["A", "C"], [], {}, 0.0)
    assert empty["questions"] == 0 and empty["arms"]["A"]["pass_rate"] is None
    assert verdict_of(empty)["text"] == "Too few questions to judge (n=0)"


def test_a_partial_question_counts_only_for_the_arms_that_answered():
    rows = [row(f"q{i}") for i in range(3)]
    outcomes = {"q0": {"A": outcome(rows[0], passed=True), "C": outcome(rows[0], passed=True)},
                "q1": {"A": outcome(rows[1], passed=True), "C": outcome(rows[1])},
                "q2": {"A": outcome(rows[2], passed=True)}}                     # a fatal stop came before C
    s = metrics.summarize(["A", "C"], rows, outcomes, 0.0)
    assert s["arms"]["A"]["n"] == 3 and s["arms"]["C"]["n"] == 2
    assert verdict_of(s)["n"] == 2 and verdict_of(s)["delta"] == -1


# --------------------------------------------------------------------------- luccy's recorded run
def recorded(passed, facts=(1, 2), latency=20.0):
    return {"passed": passed, "infra_error": False, "facts_supported": facts[0], "facts_total": facts[1],
            "latency_s": latency}


def test_the_recorded_run_is_a_reference_column_never_an_arm():
    """luccy's recorded verdicts on the questions finished: their own figures, a per-kind column
    and the pairs A/recorded and C/recorded; the verdict and the noise floor stay A, A2 and C's."""
    rows = [row(f"q{i}", kind="multi_hop" if i % 2 else "single_hop") for i in range(6)]
    plan = [(True, True, True), (True, False, True), (False, True, False), (False, False, True),
            (True, "infra", False), (True, True, None)]                         # q5: no recorded verdict
    outcomes, rec = {}, {}
    for r, (a, c, was) in zip(rows, plan, strict=True):
        outcomes[r["id"]] = {"A": outcome(r, passed=a),
                             "C": outcome(r, status="error") if c == "infra" else outcome(r, passed=c)}
        if was is not None:
            rec[r["id"]] = recorded(was, facts=(2, 2) if was else (0, 2), latency=10.0 * (len(rec) + 1))
    s = metrics.summarize(["A", "C"], rows, outcomes, 0.0, rec)
    plain = metrics.summarize(["A", "C"], rows, outcomes, 0.0)
    assert s["verdicts"] == plain["verdicts"] and s["noise_floor"] == plain["noise_floor"]
    assert s["arms"] == plain["arms"] and "recorded" not in s["arms"]
    assert s["recorded"] == {"label": "Luccy, recorded", "n": 5, "pass": 3, "pass_rate": 0.6, "fact_recall": 0.6,
                             "p50_latency_s": 30.0, "p90_latency_s": 46.0}
    pairs = {(p["a"], p["b"]): p for p in s["agreement"]}
    assert list(pairs) == [("C", "A"), ("A", "recorded"), ("C", "recorded")]
    assert pairs[("A", "recorded")] == {"a": "A", "b": "recorded", "n": 5, "both_pass": 2, "only_a": 1, "only_b": 1,
                                        "both_fail": 1, "mcnemar_p": 1.0}
    assert pairs[("C", "recorded")]["n"] == 4                                   # C's infra failure leaves the pair
    assert s["by_kind"]["single_hop"]["recorded"] == {"n": 3, "pass": 1}       # q0, q2, q4
    assert s["by_kind"]["multi_hop"]["recorded"] == {"n": 2, "pass": 2}        # q1, q3 (q5 has none)
    assert s["excluded"] == {"infra_error": [{"id": "q4", "arm": "C", "message": "HTTP 502: failed"}]}
    assert metrics.summarize(["A"], rows, outcomes, 0.0, {})["recorded"] is None


# --------------------------------------------------------------------------- spend
def test_budget_refuses_past_the_cap_and_learns_measured_costs():
    b = budget.Budget(0.30)
    first = b.reserve(["A", "C"])
    expected = budget.PRIOR_USD["luccy"] + budget.PRIOR_USD["luccy"] + budget.CHAT_OVERHEAD_USD \
        + 2 * budget.PRIOR_USD["judge"]
    assert first == pytest.approx(expected)
    assert b.reserve(["A", "C"]) is None                                        # in flight + next > cap
    b.settle(first, [("luccy", 0.02, True), ("C", 0.05, False), ("judge", 0.001, True)])
    assert b.spent == pytest.approx(0.071) and b.inflight == 0
    for _ in range(2):
        b.settle(0, [("luccy", 0.02, True)])
    assert b.estimate_arm("A2") == pytest.approx(0.02)                          # three luccy samples
    assert b.estimate_arm("C") == pytest.approx(0.02 + budget.CHAT_OVERHEAD_USD)
    assert budget.Budget(None).reserve(["A"] * 100) is not None
