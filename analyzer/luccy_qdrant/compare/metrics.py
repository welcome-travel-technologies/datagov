"""The numbers: per-arm measures, per-kind pass counts, pairwise agreement with an exact
McNemar test, luccy's noise floor and the verdict of DataGov's arm (C) against luccy.

Every question of the golden set is scored, a warehouse question or one whose documents
DataGov does not hold included. Infrastructure failures (an HTTP error, a timeout, a failed
analyzer tool call, a failed judge call: outcome["infra_error"]) are not answers, so they are
left out of every rate and of every pair they touch; summary.excluded.infra_error lists them,
and they are the only thing left out. An answer that came back with the arm's own error is a
failure and counts as one, as in luccy's own gate.

luccy's recorded score (`recorded`: its verdict per gold id, from golden.py) is a reference,
never an arm: on the questions this run finished it gets its own figures (summary.recorded),
a column in by_kind and the pairs RECORDED_PAIRS in agreement. It never enters a verdict.

A verdict compares arm X with luccy (A) on the PAIRED questions (both finished without an
infrastructure failure), from the discordant pairs of their pass/not-pass table:

    n = paired questions; only_x, only_a = questions only X / only A passed
    d = (only_x - only_a) / n                        X's pass rate minus A's
    var = (only_x + only_a - (only_x - only_a)^2 / n) / n^2
    95% CI = d -/+ 1.96 sqrt(var), clipped to [-1, 1]
    margin = max(0.05, luccy's own flip rate A vs A2)   (0.05 without A2)

    n < 20 -> inconclusive; CI above 0 -> better; CI below 0 -> worse;
    CI inside [-margin, +margin] -> same; else inconclusive (run more questions).
"""
from __future__ import annotations

import math

from .arms import ARMS, PAIRS, RECORDED, RECORDED_LABEL, RECORDED_PAIRS, VERDICT_ARMS
from .ids import expected_groups, groups_hit

Z = 1.96
MIN_PAIRED = 20            # fewer paired questions than this: too few to judge
MIN_MARGIN = 0.05          # the smallest difference (5 points) that is ever called a difference
EXTRA_DISAGREEMENT = 0.05  # warn when X disagrees with A this much more than A with itself
MESSAGE_CHARS = 300
MINUS = "−"


# --------------------------------------------------------------------------- one leg
def make_outcome(row: dict, leg: dict, score: dict | None, judge_error: str | None,
                 answer_usd: float, judge_usd: float, cost_source: str | None = None) -> dict:
    replied = leg["status"] == "ok"
    groups = expected_groups(row)
    infra = (not replied and bool(leg.get("infra"))) or bool(judge_error)
    if not replied or judge_error or score is None:
        verdict = "error"
    elif score.get("pass"):
        verdict = "pass"
    elif leg.get("abstained"):
        verdict = "abstain"
    else:
        verdict = "fail"
    facts_total = score.get("facts_total", 0) if score else len(row.get("expected_facts") or [])
    facts_supported = score.get("facts_supported", 0) if score else 0
    return {
        "verdict": verdict, "passed": verdict == "pass", "replied": replied,
        "abstained": bool(leg.get("abstained")) and replied,
        "error": leg.get("error") or judge_error, "infra_error": infra,
        "latency_s": leg.get("latency_s"), "usd": round(answer_usd + judge_usd, 6),
        "cost_source": cost_source or leg.get("cost_source") or "estimate",
        "facts_supported": facts_supported, "facts_total": facts_total,
        "fact_ratio": round(facts_supported / facts_total, 3) if score and facts_total else None,
        "behaviour_ok": bool(score and score.get("behaviour_ok")),
        "leak": bool(score and score.get("mechanical_leak")),
        "doc_hit": (replied and groups_hit(groups, leg.get("cited"))) if groups else None,
        "cited": list(leg.get("cited") or []), "answer": leg.get("answer") or "",
    }


# --------------------------------------------------------------------------- math
def percentile(values, q: float) -> float | None:
    """Linear interpolation between closest ranks (q in 0..100)."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pos = (len(values) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def ratio(num: float, den: float, digits: int = 4) -> float | None:
    return round(num / den, digits) if den else None


def rounded(value, digits: int):
    return None if value is None else round(value, digits)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar test on the discordant pairs: a binomial(b + c, 1/2) test."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_diff(only_x: int, only_a: int, n: int) -> tuple[float, float, float] | None:
    """(d, ci_low, ci_high) for X minus A over n paired questions; None for n = 0."""
    if n <= 0:
        return None
    d = (only_x - only_a) / n
    var = (only_x + only_a - (only_x - only_a) ** 2 / n) / n ** 2
    half = Z * math.sqrt(max(0.0, var))
    return d, max(-1.0, d - half), min(1.0, d + half)


def points(value: float) -> str:
    """A rate difference in percentage points, signed: '+4.1', '−3.2'."""
    return f"{MINUS if value < 0 else '+'}{abs(value) * 100:.1f}"


def questions(count: int) -> str:
    return f"{count} question{'' if count == 1 else 's'}"


def cost_source_of(outcomes: list[dict]) -> str | None:
    sources = {o.get("cost_source") or "estimate" for o in outcomes}
    if not sources:
        return None
    return sources.pop() if len(sources) == 1 else "mixed"


# --------------------------------------------------------------------------- per arm
def arm_summary(arm: str, items: list[tuple[dict, dict]]) -> dict:
    """`items`: (gold row, outcome) for every question this arm was asked. Rates are over the
    questions judged (`n`: no infrastructure failure); costs and latencies over every leg."""
    outcomes = [o for _, o in items]
    judged = [(r, o) for r, o in items if not o.get("infra_error")]
    judged_outcomes = [o for _, o in judged]
    doc_rows = [o for o in judged_outcomes if o["doc_hit"] is not None]
    abstain_rows = [o for r, o in judged if r.get("expected_behaviour") == "abstain"]
    passed = sum(o["passed"] for o in judged_outcomes)
    replied = [o["latency_s"] for o in outcomes if o["replied"]]
    return {
        "label": ARMS[arm].label,
        "n": len(judged_outcomes),
        "answered": sum(1 for o in judged_outcomes if o["replied"] and not o["abstained"]),
        "pass": passed,
        "pass_rate": ratio(passed, len(judged_outcomes)),
        "expected_doc_hit_rate": ratio(sum(1 for o in doc_rows if o["doc_hit"]), len(doc_rows)),
        "fact_recall": ratio(sum(o["facts_supported"] for o in judged_outcomes),
                             sum(o["facts_total"] for o in judged_outcomes)),
        "correct_abstentions": sum(1 for o in abstain_rows if o["behaviour_ok"]),
        "abstain_rows": len(abstain_rows),
        "privacy_leaks": sum(1 for o in judged_outcomes if o["leak"]),
        "errors": sum(1 for o in outcomes if o["verdict"] == "error"),
        "infra_errors": sum(1 for o in outcomes if o.get("infra_error")),
        "p50_latency_s": rounded(percentile(replied, 50), 2),
        "p90_latency_s": rounded(percentile(replied, 90), 2),
        "est_cost_usd": round(sum(o["usd"] for o in outcomes), 4),
        "cost_source": cost_source_of(outcomes),
    }


def by_kind(rows: list[dict], outcomes: dict[str, dict[str, dict]], arms: list[str]) -> dict:
    """Pass counts per gold kind over the judged questions (infrastructure failures left out)."""
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        for arm in arms:
            o = outcomes.get(row["id"], {}).get(arm)
            if o is None or o.get("infra_error"):
                continue
            cell = out.setdefault(row["kind"], {}).setdefault(arm, {"n": 0, "pass": 0})
            cell["n"] += 1
            cell["pass"] += int(o["passed"])
    return {kind: out[kind] for kind in sorted(out)}


def agreement(a: str, b: str, rows: list[dict], outcomes: dict[str, dict[str, dict]]) -> dict:
    """Pass/not-pass agreement over the PAIRED questions: both arms asked, neither failed for
    infrastructure (an answer that errored on its own is a not-pass)."""
    n = both = only_a = only_b = neither = 0
    for row in rows:
        legs = outcomes.get(row["id"], {})
        if a not in legs or b not in legs or legs[a].get("infra_error") or legs[b].get("infra_error"):
            continue
        pa, pb = legs[a]["passed"], legs[b]["passed"]
        n += 1
        both += pa and pb
        only_a += pa and not pb
        only_b += pb and not pa
        neither += not pa and not pb
    return {"a": a, "b": b, "n": n, "both_pass": both, "only_a": only_a, "only_b": only_b, "both_fail": neither,
            "mcnemar_p": round(mcnemar_exact(only_a, only_b), 4)}


def luccy_rate(luccy_pair: dict | None) -> float | None:
    """How often luccy disagreed with itself (A vs A2) on the paired questions."""
    if not luccy_pair or not luccy_pair["n"]:
        return None
    return (luccy_pair["only_a"] + luccy_pair["only_b"]) / luccy_pair["n"]


def margin_of(luccy_pair: dict | None) -> float:
    rate = luccy_rate(luccy_pair)
    return MIN_MARGIN if rate is None else max(MIN_MARGIN, rate)


def noise_floor(luccy_pair: dict | None) -> dict | None:
    """luccy against itself, or None when A2 did not run: how many paired questions two
    identical runs disagree on (flips), that as a rate, and the difference in questions the
    verdicts treat as normal (round(margin x n))."""
    if luccy_pair is None:
        return None
    flips = luccy_pair["only_a"] + luccy_pair["only_b"]
    rate = luccy_rate(luccy_pair)
    return {"flips": flips, "threshold": round(margin_of(luccy_pair) * luccy_pair["n"]),
            "disagree_rate": rounded(rate, 4)}


def verdict(arm: str, pair: dict, luccy_pair: dict | None) -> dict:
    """Arm X (`pair` = agreement(X, "A")) against luccy; `luccy_pair` = agreement("A", "A2"),
    or None when A2 did not run."""
    n, only_x, only_a = pair["n"], pair["only_a"], pair["only_b"]
    margin = margin_of(luccy_pair)
    own = luccy_rate(luccy_pair)
    diff = paired_diff(only_x, only_a, n)
    d, low, high = diff if diff else (None, None, None)
    disagree = (only_x + only_a) / n if n else None
    if n < MIN_PAIRED:
        outcome, text = "inconclusive", f"Too few questions to judge (n={n})"
    elif low > 0:
        outcome, text = "better", (f"Better than Luccy by {questions(only_x - only_a)} "
                                   f"(95% CI {points(low)} to {points(high)} points)")
    elif high < 0:
        outcome, text = "worse", (f"Worse than Luccy by {questions(only_a - only_x)} "
                                  f"(95% CI {points(low)} to {points(high)} points)")
    elif low >= -margin and high <= margin:
        outcome = "same"
        if luccy_pair is not None:
            text = (f"Same as Luccy (difference between {points(low)} and {points(high)} points; "
                    f"Luccy itself varies ±{margin * 100:.1f} points)")
        else:
            text = (f"Same as Luccy (difference between {points(low)} and {points(high)} points; "
                    f"within ±{margin * 100:.1f} points, Luccy's own variation not measured)")
    else:
        outcome, text = "inconclusive", (f"No clear difference yet (95% CI {points(low)} to {points(high)} points)"
                                         " — run more questions")
    return {
        "arm": arm, "vs": "A", "outcome": outcome, "text": text, "n": n,
        "delta": only_x - only_a, "threshold": round(margin * n),
        "diff_rate": rounded(d, 4), "ci_low": rounded(low, 4), "ci_high": rounded(high, 4),
        "margin": round(margin, 4), "disagree_rate": rounded(disagree, 4), "luccy_disagree_rate": rounded(own, 4),
        "within_noise": (outcome == "same") if luccy_pair is not None else None,
        "disagrees_more_than_luccy_itself": (disagree - own > EXTRA_DISAGREEMENT
                                             if disagree is not None and own is not None else None),
    }


def infra_errors(rows: list[dict], outcomes: dict[str, dict[str, dict]], arms: list[str]) -> list[dict]:
    return [{"id": row["id"], "arm": arm, "message": str(o.get("error") or "")[:MESSAGE_CHARS]}
            for row in rows for arm in arms
            if (o := outcomes.get(row["id"], {}).get(arm)) is not None and o.get("infra_error")]


def recorded_summary(rows: list[dict], recorded: dict[str, dict]) -> dict:
    """luccy's recorded figures on the questions this run finished that have a recorded verdict:
    the arm measures luccy's gate records (pass, facts, latency); the rest are not known."""
    items = [recorded[r["id"]] for r in rows if r["id"] in recorded]
    passed = sum(o["passed"] for o in items)
    latencies = [o.get("latency_s") for o in items]
    return {"label": RECORDED_LABEL, "n": len(items), "pass": passed, "pass_rate": ratio(passed, len(items)),
            "fact_recall": ratio(sum(o["facts_supported"] for o in items), sum(o["facts_total"] for o in items)),
            "p50_latency_s": rounded(percentile(latencies, 50), 2),
            "p90_latency_s": rounded(percentile(latencies, 90), 2)}


# --------------------------------------------------------------------------- everything
def summarize(arms: list[str], rows: list[dict], outcomes: dict[str, dict[str, dict]], spent_usd: float,
              recorded: dict[str, dict] | None = None) -> dict:
    """The summary DataGov's frontend renders. `rows` are the questions finished (a question cut
    short by a fatal stop holds only the arms that answered); `recorded` is luccy's recorded
    verdict per gold id (golden.recorded_outcomes), or None."""
    arm_rows = {arm: [(r, outcomes[r["id"]][arm]) for r in rows if arm in outcomes.get(r["id"], {})]
                for arm in arms}
    summary_arms = {arm: arm_summary(arm, arm_rows[arm]) for arm in arms}
    pairs = [agreement(a, b, rows, outcomes) for a, b in PAIRS if a in arms and b in arms]
    by_pair = {(p["a"], p["b"]): p for p in pairs}
    luccy_pair = by_pair.get(("A", "A2"))
    verdicts = [verdict(arm, by_pair[(arm, "A")], luccy_pair) for arm in VERDICT_ARMS
                if arm in arms and (arm, "A") in by_pair]
    columns, table = list(arms), outcomes
    if recorded:
        table = {rid: dict(legs, **({RECORDED: recorded[rid]} if rid in recorded else {}))
                 for rid, legs in outcomes.items()}
        columns.append(RECORDED)
        pairs += [agreement(a, b, rows, table) for a, b in RECORDED_PAIRS if a in arms]
    return {
        "questions": len(rows),
        "arms": summary_arms,
        "by_kind": by_kind(rows, table, columns),
        "agreement": pairs,
        "noise_floor": noise_floor(luccy_pair),
        "verdicts": verdicts,
        "recorded": recorded_summary(rows, recorded) if recorded else None,
        "excluded": {"infra_error": infra_errors(rows, outcomes, arms)},
        "spent_usd": round(spent_usd, 4),
    }
