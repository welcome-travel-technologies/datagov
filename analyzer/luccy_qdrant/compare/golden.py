"""luccy's golden set, exactly as luccy measures it, and luccy's own recorded score on it.

Every comparison asks every row of luccy's gold set (eval/gold/gold_set.jsonl) and sets the
result next to luccy's recorded run: its quality-gate baseline, eval/gold/baseline.summary.json.
Both files ship in the luccy image the analyzer image is built FROM, next to judge.py; they are
read where they are and never changed.

Everything is measured with luccy's own functions (eval/gold/run_quality_gate.py):
  gold_digest, row_digests       which rows are graded. The set is "the same as recorded" when
                                 its digest is the baseline's gold_digest; a row whose digest
                                 differs from the baseline's row_digests (edited or added since)
                                 or that the baseline carried unmeasured has no recorded verdict
                                 to compare with.
  retrieval_knobs, env_manifest, stage_clients, prompt_shas
                                 the configuration now, built in this container from the same
                                 image and defaults the analyzer serves luccy with
  manifest_drift                 luccy's own drift rule (retrieval knobs, index, writer model),
                                 and compare_runs.manifest_diff over every prompt and every
                                 stage's model and door

The documents are the one difference by design: the recorded run read luccy's own pinned index
(env.LUCCY_INDEX_DIR and its signature), this run reads DataGov's copy. That is reported as
expected, never as drift. A judge other than the recorded run's (model or votes) is refused
before any paid call: its scores would not be comparable (judge_problem).

The functions raise ValueError, in plain words, for what the runner reports as a configuration
error (exit 2, nothing paid for).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

BASELINE_NAME = "baseline.summary.json"
# luccy's manifest field naming the index the recorded run read: DataGov's copy differs by design.
DOCUMENT_FIELDS = ("env.LUCCY_INDEX_DIR",)
# Compared beyond manifest_drift's own fields: a prompt or a stage's model changes what a run
# measures as surely as a retrieval knob does.
ALSO_COMPARED = ("prompts", "stages")
STAGE_NAMES = {"router": "router", "decompose": "planner", "synthesize": "writer",
               "arith_extract": "arithmetic extractor", "judge": "judge", "embed": "embedding"}
PROMPT_NAMES = {"route": "router", "decompose": "planner", "synthesize": "writer", "arith_extract": "arithmetic",
                "deep_research": "deep research", "style_plain": "plain style", "judge": "judge"}
DOCUMENTS_TEXT = ("Documents: luccy's recorded run searched its own pinned index; this run searches DataGov's "
                  "copy of the documents. This difference is expected: it is what the comparison measures.")


@dataclass
class Reference:
    """What one run is compared against, fixed before its first question."""
    gold: dict                                    # gold_identity, plus "selected"
    recorded: dict                                # recorded_reference: summary.luccy_recorded
    outcomes: dict                                # recorded_outcomes: the recorded verdicts, for metrics
    drift: dict                                   # config_drift: summary.config_drift
    missing: dict[str, list[str]] = field(default_factory=dict)   # gold id -> groups not in DataGov


def gate():
    from eval.gold import run_quality_gate
    return run_quality_gate


# --------------------------------------------------------------------------- the recorded run
def baseline_path() -> Path:
    from eval.gold import judge
    return Path(judge.GOLD_PATH).with_name(BASELINE_NAME)


def load_baseline(path: Path | None = None) -> dict:
    path = Path(path) if path else baseline_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"luccy's recorded run {path} cannot be read ({type(e).__name__}: {e}); it ships in the "
                         "luccy image next to gold_set.jsonl") from e
    if not isinstance(data, dict) or not isinstance(data.get("scores"), list) \
            or not isinstance(data.get("manifest"), dict):
        raise ValueError(f"{path} is not a luccy quality-gate summary (no scores or manifest)")
    return data


def recorded_judge(baseline: dict) -> tuple[str | None, int]:
    """(model, votes) of the judge that scored the recorded run."""
    manifest = baseline.get("manifest") or {}
    stage = ((manifest.get("stages") or {}).get("judge") or {})
    return baseline.get("judge_model") or stage.get("model"), int(manifest.get("judge_votes") or 1)


def judge_problem(judge, baseline: dict) -> str | None:
    """Why `judge` is not the judge of the recorded run (model or votes per answer), or None."""
    model, votes = recorded_judge(baseline)
    now_model, now_votes = getattr(judge, "model", None), int(getattr(judge, "votes", 1) or 1)
    problems = []
    if not model:
        problems.append("the recorded run names no judge model (it was judged without the judge LLM)")
    elif now_model != model:
        problems.append(f"model {now_model}, the recorded run used {model} (set SERVING_JUDGE_MODEL={model})")
    if now_votes != votes:
        problems.append(f"{now_votes} vote(s) per answer, the recorded run used {votes}")
    if not problems:
        return None
    return "the judge is not the one luccy's recorded score used: " + "; ".join(problems)


def unmeasured_ids(baseline: dict) -> set[str]:
    return {str(i) for i in baseline.get("unmeasured_ids") or []}


def recorded_reference(baseline: dict) -> dict:
    """The recorded run as the summary shows it (summary.luccy_recorded). pass_by_id holds every
    row it measured; a row that raised is a failure, as in luccy's gate."""
    unmeasured = unmeasured_ids(baseline)
    pass_by_id: dict[str, bool] = {}
    for s in baseline.get("scores") or []:
        if isinstance(s, dict) and s.get("id") and s["id"] not in unmeasured:
            pass_by_id[s["id"]] = bool(s.get("pass"))
    for e in baseline.get("errors") or []:
        if isinstance(e, dict) and e.get("id") and e["id"] not in unmeasured:
            pass_by_id.setdefault(e["id"], False)
    model, votes = recorded_judge(baseline)
    cost = baseline.get("cost") if isinstance(baseline.get("cost"), dict) else {}
    return {"ts": baseline.get("ts"), "n": baseline.get("n"), "n_pass": baseline.get("n_pass"),
            "pass_rate": baseline.get("pass_rate"), "fact_ratio": baseline.get("fact_ratio"),
            "judge_model": model, "judge_votes": votes, "gold_digest": baseline.get("gold_digest"),
            "latency_p50_s": baseline.get("latency_p50_s"), "latency_p90_s": baseline.get("latency_p90_s"),
            "usd": cost.get("usd"), "pass_by_id": pass_by_id}


# --------------------------------------------------------------------------- the golden set
def gold_identity(gold: list[dict], baseline: dict) -> dict:
    """{rows, digest, baseline_digest, same_as_recorded, changed_ids}: luccy's own digest of the
    whole gold set against the recorded run's, and the rows with no recorded verdict for them
    as they are now."""
    g = gate()
    digest = g.gold_digest(gold)
    recorded = baseline.get("gold_digest")
    then = baseline.get("row_digests") if isinstance(baseline.get("row_digests"), dict) else None
    now = g.row_digests(gold)
    if then is not None:
        changed = [rid for rid, d in now.items() if then.get(rid) != d]
    else:
        changed = [] if digest == recorded else list(now)
    unmeasured = unmeasured_ids(baseline)
    changed += [rid for rid in now if rid in unmeasured and rid not in changed]
    return {"rows": len(gold), "digest": digest, "baseline_digest": recorded,
            "same_as_recorded": bool(recorded) and digest == recorded, "changed_ids": changed}


def recorded_outcomes(baseline: dict, gold: list[dict], changed_ids) -> dict[str, dict]:
    """{gold id: outcome} for the rows whose recorded verdict is for the row as it is now, in the
    shape metrics reads: passed, facts_supported/facts_total and latency_s (luccy's own)."""
    changed, unmeasured = set(changed_ids), unmeasured_ids(baseline)
    wanted = {r["id"] for r in gold} - changed - unmeasured
    out: dict[str, dict] = {}
    for s in baseline.get("scores") or []:
        if isinstance(s, dict) and s.get("id") in wanted:
            out[s["id"]] = {"passed": bool(s.get("pass")), "infra_error": False,
                            "facts_supported": int(s.get("facts_supported") or 0),
                            "facts_total": int(s.get("facts_total") or 0), "latency_s": s.get("latency_s")}
    for e in baseline.get("errors") or []:
        if isinstance(e, dict) and e.get("id") in wanted and e["id"] not in out:
            out[e["id"]] = {"passed": False, "infra_error": False, "facts_supported": 0, "facts_total": 0,
                            "latency_s": None}
    return out


# --------------------------------------------------------------------------- configuration
def current_manifest(judge_model: str | None) -> dict:
    """The configuration now, with luccy's own functions (the judge as configured, also on a dry run)."""
    g = gate()
    return {"retrieval": g.retrieval_knobs(), "env": g.env_manifest(), "stages": g.stage_clients(judge_model),
            "prompts": g.prompt_shas()}


def shown(value) -> str:
    return "not set" if value is None else str(value)


def drift_text(field_name: str, was, now) -> str:
    """One configuration difference in plain words."""
    head, _, rest = field_name.partition(".")
    if head == "prompts":
        name = f"{PROMPT_NAMES[rest]} prompt ({rest})" if rest in PROMPT_NAMES else f"{rest} prompt"
        if was is None:
            return f"The {name} is new since the recorded run."
        if now is None:
            return f"The {name} is gone since the recorded run."
        return f"The {name} changed since the recorded run."
    if head == "stages":
        stage, _, key = rest.partition(".")
        what = {"model": "model", "door": "access"}.get(key, key)
        return f"The {STAGE_NAMES.get(stage, stage)} {what}: {shown(was)} when recorded, {shown(now)} now."
    if head == "retrieval":
        return f"Retrieval setting {rest}: {shown(was)} when recorded, {shown(now)} now."
    return f"{field_name}: {shown(was)} when recorded, {shown(now)} now."


def config_drift(baseline: dict, current: dict, documents: dict | None = None) -> dict:
    """{same, differences: [{field, recorded, current, text}], documents: {recorded, current, text}}:
    the recorded run's manifest against `current`, by luccy's manifest_drift plus every prompt and
    stage. The index the recorded run read is not a difference: it is `documents`, expected."""
    from eval.gold.compare_runs import flatten, manifest_diff
    base = baseline.get("manifest") or {}
    moved = {name: (was, now) for name, was, now in gate().manifest_drift(base, current)}
    sub_base, sub_now = ({k: m.get(k) for k in ALSO_COMPARED} for m in (base, current))
    flat_base, flat_now = flatten(sub_base), flatten(sub_now)
    for name in manifest_diff(sub_base, sub_now):
        moved.setdefault(name, (flat_base.get(name), flat_now.get(name)))
    differences = [{"field": name, "recorded": was, "current": now, "text": drift_text(name, was, now)}
                   for name, (was, now) in sorted(moved.items()) if name not in DOCUMENT_FIELDS]
    index = base.get("index_before") if isinstance(base.get("index_before"), dict) else {}
    recorded = {"index_dir": (base.get("env") or {}).get("LUCCY_INDEX_DIR"), "index_sig": index.get("sig"),
                "pages_sig": index.get("pages_sig")}
    return {"same": not differences, "differences": differences,
            "documents": {"recorded": recorded, "current": dict(documents or {}), "text": DOCUMENTS_TEXT}}


# --------------------------------------------------------------------------- everything
def reference(gold: list[dict], rows: list[dict], baseline: dict, judge, corpus,
              documents: dict | None = None) -> Reference:
    """The run's Reference: the gold set's identity (`rows` = the questions it asks), luccy's
    recorded run, the configuration drift and the expected documents DataGov does not hold."""
    identity = dict(gold_identity(gold, baseline), selected=len(rows))
    current = current_manifest(getattr(judge, "model", None))
    return Reference(gold=identity, recorded=recorded_reference(baseline),
                     outcomes=recorded_outcomes(baseline, gold, identity["changed_ids"]),
                     drift=config_drift(baseline, current, documents),
                     missing={r["id"]: corpus.missing(r) for r in rows})
