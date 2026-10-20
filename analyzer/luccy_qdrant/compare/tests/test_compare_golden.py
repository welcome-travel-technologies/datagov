"""luccy's golden set and its recorded run: identity, the recorded verdicts, the judge check and
the configuration drift, all with luccy's own functions over the files the image ships."""
import copy
import json

import pytest

from luccy_qdrant.compare import golden


@pytest.fixture(scope="module")
def baseline():
    return golden.load_baseline()


def test_the_image_ships_luccys_recorded_run_for_this_gold_set(gold, baseline):
    """The comparison is exact only because the gold set in the image is the one luccy's recorded
    score used: luccy's own gold_digest over its rows matches the baseline's."""
    assert golden.baseline_path().name == "baseline.summary.json"
    assert golden.baseline_path().parent.joinpath("gold_set.jsonl").is_file()
    identity = golden.gold_identity(gold, baseline)
    digest = baseline["gold_digest"]
    assert identity == {"rows": len(gold), "digest": digest, "baseline_digest": digest, "same_as_recorded": True,
                        "changed_ids": []}
    assert len(gold) == baseline["n"] == len(baseline["scores"])


def test_an_edited_row_is_named_and_has_no_recorded_verdict(gold, baseline):
    edited = [dict(r, question=r["question"] + " (reworded)") if r["id"] == "mh-002" else r for r in gold]
    added = edited + [dict(gold[0], id="zz-001", question="A new question?")]
    identity = golden.gold_identity(added, baseline)
    assert identity["same_as_recorded"] is False and identity["changed_ids"] == ["mh-002", "zz-001"]
    assert identity["digest"] != baseline["gold_digest"] and identity["rows"] == len(gold) + 1
    outcomes = golden.recorded_outcomes(baseline, added, identity["changed_ids"])
    assert "mh-002" not in outcomes and "zz-001" not in outcomes and len(outcomes) == len(gold) - 1
    # A row the baseline carried unmeasured (carry_baseline.py) is not comparable either.
    carried = dict(baseline, unmeasured_ids=["sh-002"])
    assert golden.gold_identity(gold, carried)["changed_ids"] == ["sh-002"]
    assert "sh-002" not in golden.recorded_reference(carried)["pass_by_id"]


def test_the_recorded_reference(gold, baseline):
    rec = golden.recorded_reference(baseline)
    assert (rec["n"], rec["n_pass"], rec["pass_rate"], rec["fact_ratio"]) == (
        baseline["n"], baseline["n_pass"], baseline["pass_rate"], baseline["fact_ratio"])
    assert rec["judge_model"] == baseline["judge_model"] and rec["judge_votes"] == baseline["manifest"]["judge_votes"]
    assert rec["usd"] == baseline["cost"]["usd"] and rec["ts"] == baseline["ts"]
    assert sum(rec["pass_by_id"].values()) == baseline["n_pass"] and len(rec["pass_by_id"]) == baseline["n"]
    outcomes = golden.recorded_outcomes(baseline, gold, [])
    first = baseline["scores"][0]
    assert outcomes[first["id"]] == {"passed": first["pass"], "infra_error": False,
                                     "facts_supported": first["facts_supported"], "facts_total": first["facts_total"],
                                     "latency_s": first["latency_s"]}
    raised = dict(baseline, errors=[{"id": "zz-9", "error": "boom"}])        # a row that raised is a failure
    assert golden.recorded_reference(raised)["pass_by_id"]["zz-9"] is False


def test_the_judge_must_be_the_recorded_runs(baseline):
    from eval.gold.judge import Judge
    model = baseline["judge_model"]
    assert golden.judge_problem(Judge(dry_run=True, model=model), baseline) is None
    assert golden.judge_problem(Judge(dry_run=False, model=model), baseline) is None
    other = golden.judge_problem(Judge(dry_run=True, model="gemini-x"), baseline)
    assert other == ("the judge is not the one luccy's recorded score used: model gemini-x, the recorded run used "
                     f"{model} (set SERVING_JUDGE_MODEL={model})")
    both = golden.judge_problem(Judge(dry_run=True, model="gemini-x", votes=3), baseline)
    assert both.endswith("; 3 vote(s) per answer, the recorded run used 1")
    unjudged = dict(baseline, judge_model=None, manifest=dict(baseline["manifest"], stages={}))
    assert "names no judge model" in golden.judge_problem(Judge(dry_run=True), unjudged)


def test_a_missing_or_broken_baseline_is_a_plain_error(tmp_path):
    with pytest.raises(ValueError, match="cannot be read"):
        golden.load_baseline(tmp_path / "baseline.summary.json")
    (tmp_path / "b.json").write_text(json.dumps({"n": 1}))
    with pytest.raises(ValueError, match="not a luccy quality-gate summary"):
        golden.load_baseline(tmp_path / "b.json")


def test_drift_is_luccys_own_and_the_documents_are_expected(baseline):
    manifest = baseline["manifest"]
    same = {"retrieval": manifest["retrieval"], "env": dict(manifest["env"], LUCCY_INDEX_DIR=None),
            "stages": manifest["stages"], "prompts": manifest["prompts"]}
    drift = golden.config_drift(baseline, same, {"documents": 40, "digest": "d" * 64, "index_sig": "s1"})
    assert drift["same"] is True and drift["differences"] == []               # only the index moved: expected
    assert drift["documents"]["recorded"] == {"index_dir": manifest["env"]["LUCCY_INDEX_DIR"],
                                              "index_sig": manifest["index_before"]["sig"],
                                              "pages_sig": manifest["index_before"]["pages_sig"]}
    assert drift["documents"]["current"] == {"documents": 40, "digest": "d" * 64, "index_sig": "s1"}
    assert drift["documents"]["text"] == golden.DOCUMENTS_TEXT

    moved = copy.deepcopy(same)
    moved["retrieval"]["RERANK_TOPK"] = 30                                     # manifest_drift's own field
    moved["stages"]["synthesize"]["model"] = "gemini-4-flash"                  # ... and the writer model
    moved["stages"]["router"]["door"] = "vertex"                               # beyond it: every stage
    moved["prompts"]["route"] = "000000000000"                                 # ... and every prompt
    moved["prompts"]["extra"] = "111111111111"
    del moved["prompts"]["style_plain"]
    drift = golden.config_drift(baseline, moved)
    assert drift["same"] is False
    assert [(d["field"], d["text"]) for d in drift["differences"]] == [
        ("prompts.extra", "The extra prompt is new since the recorded run."),
        ("prompts.route", "The router prompt (route) changed since the recorded run."),
        ("prompts.style_plain", "The plain style prompt (style_plain) is gone since the recorded run."),
        ("retrieval.RERANK_TOPK", "Retrieval setting RERANK_TOPK: 15 when recorded, 30 now."),
        ("stages.router.door", "The router access: public_api when recorded, vertex now."),
        ("stages.synthesize.model", "The writer model: gemini-3.8-flash when recorded, gemini-4-flash now."),
    ]
    assert drift["differences"][3]["recorded"] == 15 and drift["differences"][3]["current"] == 30


def test_the_current_configuration_is_built_with_luccys_functions(baseline):
    from eval.gold import run_quality_gate as gate
    current = golden.current_manifest(baseline["judge_model"])
    assert current["retrieval"] == gate.retrieval_knobs() and current["prompts"] == gate.prompt_shas()
    assert current["stages"]["judge"]["model"] == baseline["judge_model"]
    assert set(current["stages"]) == set(baseline["manifest"]["stages"])
    drift = golden.config_drift(baseline, current)
    assert all(d["field"].startswith(("retrieval.", "stages.", "prompts.")) for d in drift["differences"])
