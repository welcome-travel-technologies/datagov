"""The judge record every arm is scored from, built from each arm's reply shape."""
import json
import re

import pytest
from conftest import listing_row

from luccy_qdrant.compare import arms, judging
from luccy_qdrant.compare.ids import Corpus

DOCS = [listing_row("confluence:1111"), listing_row("confluence:2222:part:2"), listing_row("jira:AP-1")]
CORPUS = Corpus(DOCS)
D1, D2, D3 = (d["document_id"] for d in DOCS)
ROW = {"id": "t-1", "question": "How long do refunds take?", "kind": "single_hop", "difficulty": "easy",
       "business_area": "customer_support", "expected_facts": ["Refunds are due within 14 days"],
       "expected_doc_ids": ["1111"], "expected_behaviour": "answer"}


def judge():
    from eval.gold.judge import Judge
    return Judge(dry_run=True)


# --------------------------------------------------------------------------- DataGov's own words
_MD_ESCAPE = re.compile(r'([\\`*_\[\]<>|~])')


def render_verbatim(answer, sources, ref="3f9a1c2e"):
    """DataGov's chat reply for a verbatim analyzer answer (backend assistant.py render_verbatim)."""
    parts = [answer]
    if sources:
        lines = [f"- [{_MD_ESCAPE.sub(r'\\\1', ' '.join(s['title'].split()))}]({s['url']})" for s in sources]
        parts.append("\n".join(["**Sources**"] + lines))
    parts.append(f"Answered by the Document Analyzer · ref {ref}" if ref else "Answered by the Document Analyzer")
    return "\n\n".join(parts)


DEBUG = ("\n\n---\n\n**🔧 Debug**\n\n| # | Tool | Args | Duration | Status |\n| --- | --- | --- | --- | --- |\n"
         "| 1 | `ask_document_analyzer` | q=outdated | 10 ms | ok |")


def test_datagov_prose_drops_sources_footer_and_debug():
    src = [{"title": "Old page (Outdated)", "url": "https://x/1"}, {"title": "New page", "url": "https://x/2"}]
    assert judging.datagov_prose(render_verbatim("Answer.\n\nSecond line.", src)) == "Answer.\n\nSecond line."
    assert judging.datagov_prose(render_verbatim("Answer.", [], ref=None)) == "Answer."
    assert judging.datagov_prose("Agent text." + DEBUG) == "Agent text."
    assert judging.datagov_prose("Agent text.\n\n**Sources**\n- [a](u)" + DEBUG) == "Agent text."
    kept = "Intro.\n\n**Sources**\nThese are not list lines.\n\nMore prose."
    assert judging.datagov_prose(kept) == kept                       # not a trailing sources list
    assert judging.datagov_prose("Plain answer [1].") == "Plain answer [1]."
    assert judging.datagov_prose("") == "" and judging.datagov_prose(None) == ""


def test_the_judge_reads_judged_answer_and_the_answer_is_posted_as_written():
    verbatim = render_verbatim("Refunds are due within 14 days.", [{"title": "Refund", "url": "https://x/1"}])
    leg = arms.parse_datagov(200, {"answer": verbatim, "judged_answer": "Refunds are due within 14 days.",
                                   "citations": [{"document_id": D1}]}, 2.0, CORPUS)
    assert leg["answer"] == verbatim and leg["judged_answer"] == "Refunds are due within 14 days."
    assert judging.judge_record(leg)["answer"]["answer"] == "Refunds are due within 14 days."
    older = arms.parse_datagov(200, {"answer": verbatim, "citations": [{"document_id": D1}]}, 2.0, CORPUS)
    assert older["judged_answer"] == "Refunds are due within 14 days."              # stripped fallback
    luccy = arms.parse_luccy(200, {"answer": "Refunds are due within 14 days.", "citations": [{"id": "1111"}]}, 2.0)
    assert judging.judge_record(luccy) == judging.judge_record(leg)                    # same text, same record


class FactLLM:
    """A deterministic stand-in for the judge model: prose facts are supported, facts with a
    digit missing, so only a difference in the judged text can change a verdict."""

    def complete(self, prompt, system=None, **kw):
        facts = json.loads(prompt)["facts"]
        verdicts = [{"i": f["i"], "verdict": "missing" if re.search(r"\d", f["fact"]) else "supported", "span": "x"}
                    for f in facts]
        return {"text": json.dumps({"verdicts": verdicts}), "usage": {"input_tokens": 1, "output_tokens": 1}}


URL = "https://welcome.atlassian.net/wiki/spaces/OT/pages/{}/x"
FLIPS = {
    # luccy's answer never says the old page is stale; only the Sources list's titles do.
    "st-001": (("Badly performing drivers are handled by the driver quality enforcement framework, which "
                "replaces the previous composite-score model with rule-based tracks "
                f"([Driver Quality Enforcement Framework v2.0]({URL.format(7012745764)})). It runs three parallel "
                "tracks and applies the same rules to every driver, with no essential-driver exception."),
               [("7012745764", "Driver Quality Enforcement Framework v2.0"),
                ("4506878179", "DEPRICATED - Driver Offboarding Process 2024"),
                ("6956366912", "👋 Driver Offboarding Process (Outdated, keep as reference for payments)")]),
    # the answer gives both targets without saying they disagree; a source title says "differs".
    "cf-001": (("One planning page sets the target at 350k rides, described as 37% growth from 255k. "
                "A later roadmap page sets the target at 420k rides booked, described as 59% growth."),
               [("6438813720", "Strategics 2026"), ("6782910515", "2026 Roadmap: what differs from the plan")]),
}


@pytest.mark.parametrize("row_id", sorted(FLIPS))
def test_a_sources_list_no_longer_flips_the_verdict(gold, row_id):
    """Review probe: the same luccy answer judged as A (luccy's answer) and as C (DataGov's
    verbatim reply). Judged on the reply as shown, C passed a behaviour check A failed only
    because of a source's title."""
    from eval.gold.judge import Judge
    row = next(r for r in gold if r["id"] == row_id)
    answer, sources = FLIPS[row_id]
    judge_ = Judge(llm=FactLLM())
    verbatim = render_verbatim(answer, [{"title": t, "url": URL.format(i)} for i, t in sources])
    cited = [i for i, _ in sources]
    leg_a = arms.parse_luccy(200, {"answer": answer, "citations": [{"id": i} for i in cited]}, 20.0)
    raw_c = {"answer": {"answer": verbatim, "citations": cited, "unanswerable_parts": []}}
    before = judge_.score(row, raw_c)                                       # the old way: the reply as shown
    a = judge_.score(row, judging.judge_record(leg_a))
    assert before["behaviour_ok"] and not a["behaviour_ok"], "the probe's flip reproduces"
    for body in ({"answer": verbatim, "judged_answer": answer}, {"answer": verbatim}):   # new server, older server
        leg_c = arms.parse_datagov(200, dict(body, citations=[{"luccy_id": i} for i in cited]), 20.0, CORPUS)
        c = judge_.score(row, judging.judge_record(leg_c))
        assert (c["pass"], c["behaviour_ok"], c["behaviour_reasons"]) == (a["pass"], a["behaviour_ok"],
                                                                           a["behaviour_reasons"])
        assert [f["verdict"] for f in c["facts"]] == [f["verdict"] for f in a["facts"]]
        assert leg_c["answer"] == verbatim                                   # posted as DataGov wrote it


# --------------------------------------------------------------------------- reply shapes
def test_strip_markers_keeps_markdown_links():
    assert judging.strip_markers("Green at 93% [1] [2].\n\nAmber [3]") == "Green at 93%.\n\nAmber"
    assert judging.strip_markers("Two sources [1, 2] and a range [3-4][5].") == "Two sources and a range."
    assert judging.strip_markers("See [Refund policy](https://x/1) and [1](https://x/2).") == \
        "See [Refund policy](https://x/1) and [1](https://x/2)."
    assert judging.strip_markers("") == "" and judging.strip_markers(None) == ""


def test_luccy_reply_becomes_the_judge_record():
    body = {"answer": "Refunds are due within 14 days ([Refund](https://x/1111)).", "log": "abcd1234",
            "citations": [{"id": "1111", "title": "Refund", "url": None}, {"id": "2222-part-2"},
                          {"id": "1111"}, {"title": "no id"}],
            "abstained": False, "gaps": ["the 2025 rule", 5, ""]}
    leg = arms.parse_luccy(200, body, 12.5)
    assert leg["status"] == "ok" and leg["cited"] == ["1111", "2222-part-2"] and leg["gaps"] == ["the 2025 rule"]
    assert leg["detail"]["log"] == "abcd1234" and leg["infra"] is False
    record = judging.judge_record(leg)
    assert record == {"answer": {"answer": body["answer"], "citations": ["1111", "2222"],
                                 "unanswerable_parts": ["the 2025 rule"]},
                      "latency_s": {"total": 12.5}}
    score = judge().score(dict(ROW, expected_doc_ids=["1111"]), record)
    assert score["pass"] and score["citation_ok"] and score["latency_s"] == 12.5


def test_an_unsafe_log_id_is_dropped():
    leg = arms.parse_luccy(200, {"answer": "x", "log": "../admin"}, 1.0)
    assert leg["detail"]["log"] is None


def test_datagov_reply_maps_document_ids_and_strips_markers():
    body = {"answer": "Refunds are due within 14 days [1]. See also [2].",
            "citations": [{"document_id": D1, "luccy_id": None, "title": "Refund", "url": None},
                          {"document_id": D2, "luccy_id": None},
                          {"document_id": "unknown", "luccy_id": "AP-9"},
                          {"document_id": "gone"}],
            "abstained": False, "gaps": [], "latency_s": 3.2, "tool_calls": 1, "verbatim": True, "error": None,
            "tool_error": False, "cost_usd": 0.012, "cost_source": "measured"}
    leg = arms.parse_datagov(200, body, 3.4, CORPUS)
    assert leg["cited"] == ["1111", "2222-part-2", "AP-9", "gd-dg-gone"]
    assert (leg["usd"], leg["cost_source"], leg["latency_s"]) == (0.012, "measured", 3.4)
    assert leg["detail"] == {"latency_reported_s": 3.2, "tool_calls": 1, "verbatim": True, "tool_error": False}
    record = judging.judge_record(leg)
    assert record["answer"]["answer"] == "Refunds are due within 14 days. See also."
    assert record["answer"]["citations"] == ["1111", "2222", "AP-9", "gd-dg-gone"]
    assert leg["answer"] == body["answer"]                                      # shown as written
    assert judge().score(ROW, record)["pass"]


def test_datagov_cost_is_measured_only_when_the_server_says_so():
    def cost(**kw):
        leg = arms.parse_datagov(200, dict({"answer": "x"}, **kw), 1.0, CORPUS)
        return leg["usd"], leg["cost_source"]
    assert cost(cost_usd=0.02, cost_source="measured") == (0.02, "measured")
    assert cost(cost_usd=0.02, cost_source="estimate") == (0.02, "estimate")
    assert cost(cost_usd=0.02) == (0.02, "estimate")                            # an older server: not measured
    assert cost(cost_usd=None, cost_source=None) == (None, None) and cost() == (None, None)
    assert cost(cost_usd=-1, cost_source="measured") == (None, None)
    assert cost(cost_usd=True, cost_source="measured") == (None, None)


def test_a_bracket_marker_reads_as_a_rival_number_unless_stripped():
    leg = arms.parse_datagov(200, {"answer": "Refunds are due within fourteen days [2].",
                                   "citations": [{"document_id": D1}]}, 1.0, CORPUS)
    raw = {"answer": {"answer": leg["answer"], "citations": ["1111"], "unanswerable_parts": []}}
    assert judge().score(ROW, raw)["facts_contradicted"] == 1                   # "[2]" vs 14 days
    assert judge().score(ROW, judging.judge_record(leg))["facts_contradicted"] == 0


def test_abstention_reaches_the_judge_through_gaps(gold):
    row = next(r for r in gold if r["expected_behaviour"] == "abstain")
    leg = arms.parse_datagov(200, {"answer": "Here is what I found.", "citations": [], "abstained": True,
                                   "gaps": ["Insufficient evidence in the documents."]}, 1.0, CORPUS)
    assert judge().score(row, judging.judge_record(leg))["behaviour_ok"]


def test_infrastructure_failures_and_answer_errors():
    busy = arms.parse_datagov(503, {"detail": "busy"}, 1.0, CORPUS)
    assert (busy["error"], busy["infra"], busy["usd"], busy["cost_source"]) == ("HTTP 503: busy", True, 0.0, "measured")
    timeout = arms.parse_datagov(0, {"error": "ReadTimeout: timed out"}, 330.0, CORPUS)
    assert timeout["status"] == "error" and timeout["infra"] and timeout["usd"] is None    # may have run: estimated
    assert arms.parse_datagov(502, {"detail": "bad gateway"}, 1.0, CORPUS)["infra"]
    tool = arms.parse_datagov(200, {"answer": "The analyzer could not answer.", "tool_error": True,
                                    "cost_usd": 0.01, "cost_source": "measured"}, 9.0, CORPUS)
    assert tool["status"] == "error" and tool["infra"] and tool["error"].startswith("tool error")
    assert tool["answer"] == "The analyzer could not answer." and tool["usd"] == 0.01
    own = arms.parse_datagov(200, {"answer": "partial", "error": "tool timed out"}, 9.0, CORPUS)
    assert own["status"] == "error" and not own["infra"] and own["error"] == "tool timed out"
    assert own["answer"] == "partial"
    capped = arms.parse_luccy(429, {"error": "spend cap reached"}, 0.1)
    assert capped["error"] == "HTTP 429: spend cap reached" and capped["infra"] and capped["usd"] == 0.0
    assert arms.parse_luccy(200, "not json", 0.1)["infra"]


def test_an_errored_leg_is_not_judged_and_a_judge_failure_is_kept():
    class Broken:
        dry_run = True

        def score(self, row, record):
            raise RuntimeError("judge unavailable")

    error = arms.error_leg("HTTP 502: failed", 2.0)
    assert judging.score_leg(Broken(), ROW, error) == (None, None, 0.0)
    ok = arms.parse_luccy(200, {"answer": "Refunds are due within 14 days.", "citations": [{"id": "1111"}]}, 1.0)
    score, why, usd = judging.score_leg(Broken(), ROW, ok)
    assert score is None and why.startswith("judge failed: RuntimeError") and usd == 0.0


def test_judge_cost_is_priced_with_luccys_prices():
    assert judging.judge_usd({"judge_model": "dry-run", "judge_usage": {"input_tokens": 0}}) == 0.0
    usd = judging.judge_usd({"judge_model": "gemini-3.5-flash-lite",
                             "judge_usage": {"input_tokens": 10_000, "output_tokens": 500, "thinking_tokens": 500}})
    assert usd == pytest.approx(10_000 / 1e6 * 0.30 + 1000 / 1e6 * 2.50)


def test_luccy_answer_cost_comes_from_its_run_record():
    assert arms.record_usd({"cost": {"usd": 0.0412}}) == 0.0412
    usage = {"synthesize": {"input_tokens": 1000, "output_tokens": 100, "thinking_tokens": 0}}
    assert arms.record_usd({"cost": {"usd": None}, "usage": usage}) == pytest.approx(1000 / 1e6 * 0.75 + 100 / 1e6 * 3.75)
    assert arms.record_usd({}) is None


class BusyLuccy:
    """luccy answering 503 "busy" `busy` times (with Retry-After), then an answer."""

    def __init__(self, busy, retry_after="2"):
        self.busy, self.retry_after, self.asked = busy, retry_after, 0

    def ask(self, question):
        self.asked += 1
        if self.asked <= self.busy:
            return 503, {"error": "busy"}, {"retry-after": self.retry_after}
        return 200, {"answer": "Refunds are due within 14 days.", "citations": [{"id": "1111"}]}, {}

    def record(self, log):                                                     # pragma: no cover - no log
        raise AssertionError("no record without a log")


def test_luccy_busy_is_retried_three_times_after_retry_after():
    waits = []
    client = BusyLuccy(busy=3)
    leg = arms.luccy_leg(client, ROW, pause=lambda s: waits.append(s) or False)
    assert leg["status"] == "ok" and client.asked == 4 and waits == [2.0, 2.0, 2.0]
    assert leg["detail"]["busy_retries"] == 3
    client, waits = BusyLuccy(busy=9, retry_after="soon"), []
    leg = arms.luccy_leg(client, ROW, pause=lambda s: waits.append(s) or False)
    assert leg["status"] == "error" and leg["infra"] and client.asked == 4               # then an infra error
    assert waits == [arms.BUSY_WAIT_S] * 3                                               # no usable Retry-After
    client = BusyLuccy(busy=9, retry_after="600")
    leg = arms.luccy_leg(client, ROW, pause=lambda s: True)                              # abandoned while waiting
    assert client.asked == 1 and leg["error"] == "HTTP 503: busy"
