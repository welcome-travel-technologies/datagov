"""The arms of the comparison and how each one is asked.

  A, A2   luccy itself: POST {LUCCY_URL}/ask {"question"}. A2 asks the same again, so the pair
          measures luccy's own run-to-run variation (the noise floor).
  C       DataGov chat with only the Luccy tool enabled   (eval arm chat_analyzer_only)

DataGov answers document questions only through the Document Analyzer, so C is DataGov's one
arm: it is judged against A, and A against A2 gives the noise floor. luccy's recorded score on
the same gold set (RECORDED) is shown next to them as a reference; it is never asked.

Every leg comes back in one shape (a "leg"), whatever answered it:
  {"status": "ok"|"error", "error", "infra": bool, "answer", "judged_answer", "cited": [luccy ids],
   "abstained", "gaps", "latency_s", "usd", "cost_source": "measured"|"estimate"|None,
   "detail": {...}}

`answer` is what the arm wrote (posted as is); `judged_answer` is the prose the judge reads
(judging.py). `infra` marks an infrastructure failure, not a wrong answer: an HTTP error, a
timeout or transport failure, a reply that is not JSON, or a DataGov reply whose analyzer
tool failed (`tool_error`). Such a leg is left out of the paired statistics (metrics.py).
A reply that arrived but carries the arm's own `error` is an answer that failed: it counts
as a fail.
"""
from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass

from .clients import RECORD_ID_RE, DataGovClient, LuccyClient, error_text, retry_after_s
from .ids import Corpus, expected_groups, unique
from .judging import FOOTER, SOURCES_HEAD, datagov_prose

MAX_GAPS = 20
MAX_GAP_CHARS = 500
# luccy answers 503 "busy" before it starts work (DataGov's assistant.NOT_STARTED), so that
# one is retried, free, after its Retry-After; nothing paid is ever retried.
BUSY_RETRIES = 3
BUSY_WAIT_S = 5.0
# Replies refused before any work was done: the answer cost nothing.
FREE_STATUSES = frozenset({400, 401, 403, 404, 413, 429, 503})
COST_SOURCES = ("measured", "estimate")


@dataclass(frozen=True)
class Arm:
    code: str
    label: str
    system: str                    # "luccy" | "datagov"
    eval_arm: str | None = None    # the DataGov eval endpoint's arm name


ARMS = {a.code: a for a in (
    Arm("A", "Luccy", "luccy"),
    Arm("A2", "Luccy (2nd run)", "luccy"),
    Arm("C", "DataGov chat · Luccy only", "datagov", "chat_analyzer_only"),
)}
ORDER = tuple(ARMS)
DEFAULT_ARMS = ("A", "A2", "C")
# (a, b) pass/fail agreement tables; a pair is reported when both arms ran.
PAIRS = (("A", "A2"), ("C", "A"))
# Arms judged against luccy (A) with a verdict.
VERDICT_ARMS = ("C",)
# luccy's recorded run (eval/gold/baseline.summary.json, golden.py) is never asked: it is the
# reference the arms are set against, under this key in the agreement and per-kind tables.
RECORDED = "recorded"
RECORDED_LABEL = "Luccy, recorded"
RECORDED_PAIRS = (("A", RECORDED), ("C", RECORDED))


def clean_gaps(gaps) -> list[str]:
    if not isinstance(gaps, list):
        return []
    return [g[:MAX_GAP_CHARS] for g in gaps[:MAX_GAPS] if isinstance(g, str) and g.strip()]


def error_leg(error: str, latency_s: float, answer: str = "", detail: dict | None = None, *,
              infra: bool = True, usd: float | None = None, cost_source: str | None = None) -> dict:
    return {"status": "error", "error": error, "infra": infra, "answer": answer or "", "judged_answer": "",
            "cited": [], "abstained": False, "gaps": [], "latency_s": latency_s, "usd": usd,
            "cost_source": cost_source if usd is not None else None, "detail": detail or {}}


def http_error_leg(status: int, body, latency_s: float) -> dict:
    """An HTTP error, a timeout or a transport failure (status 0): always infrastructure. A
    reply refused before any work cost nothing."""
    free = status in FREE_STATUSES
    return error_leg(f"HTTP {status}: {error_text(body)}", latency_s, detail={"http_status": status},
                     usd=0.0 if free else None, cost_source="measured" if free else None)


def number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


# --------------------------------------------------------------------------- parsing
def parse_luccy(status: int, body, latency_s: float) -> dict:
    """A luccy /ask reply as a leg (citations are already luccy ids)."""
    if status != 200 or not isinstance(body, dict):
        return http_error_leg(status, body, latency_s)
    log = body.get("log")
    citations = [c for c in body.get("citations") or [] if isinstance(c, dict)]
    answer = str(body.get("answer") or "")
    return {"status": "ok", "error": None, "infra": False, "answer": answer, "judged_answer": answer,
            "cited": unique(str(c.get("id") or "").strip() for c in citations),
            "abstained": body.get("abstained") is True, "gaps": clean_gaps(body.get("gaps")),
            "latency_s": latency_s, "usd": None, "cost_source": None,
            "detail": {"log": log if isinstance(log, str) and RECORD_ID_RE.fullmatch(log) else None}}


def datagov_cost(body: dict) -> tuple[float | None, str | None]:
    """(usd, source): `cost_usd` as measured when the server says so, as its estimate when it
    says "estimate" or says nothing (an older server); (None, None) without a cost."""
    usd = number(body.get("cost_usd"))
    if usd is None:
        return None, None
    return usd, ("measured" if body.get("cost_source") == "measured" else "estimate")


def parse_datagov(status: int, body, latency_s: float, corpus: Corpus) -> dict:
    """A DataGov eval reply as a leg; its citations' document ids are mapped into luccy ids."""
    if status != 200 or not isinstance(body, dict):
        return http_error_leg(status, body, latency_s)
    detail = {"latency_reported_s": body.get("latency_s"), "tool_calls": body.get("tool_calls"),
              "verbatim": body.get("verbatim"), "tool_error": body.get("tool_error") is True}
    answer = str(body.get("answer") or "")
    judged = body.get("judged_answer")
    judged = judged if isinstance(judged, str) else datagov_prose(answer)
    usd, cost_source = datagov_cost(body)
    if body.get("tool_error") is True:
        why = str(body.get("error") or "the Document Analyzer tool was called but gave no answer")[:300]
        return error_leg(f"tool error: {why}", latency_s, answer, detail, infra=True, usd=usd,
                         cost_source=cost_source)
    if body.get("error"):
        return error_leg(str(body["error"])[:300], latency_s, answer, detail, infra=False, usd=usd,
                         cost_source=cost_source)
    return {"status": "ok", "error": None, "infra": False, "answer": answer, "judged_answer": judged,
            "cited": unique(corpus.cited_id(c) for c in body.get("citations") or []),
            "abstained": body.get("abstained") is True, "gaps": clean_gaps(body.get("gaps")),
            "latency_s": latency_s, "usd": usd, "cost_source": cost_source, "detail": detail}


# --------------------------------------------------------------------------- live legs
def record_usd(record: dict) -> float | None:
    """luccy's own cost for an answer: the run record's cost.usd, else its token usage priced
    with serving/pricing.py; None when neither prices it."""
    cost = record.get("cost") if isinstance(record.get("cost"), dict) else {}
    usd = cost.get("usd")
    if isinstance(usd, (int, float)) and not isinstance(usd, bool):
        return float(usd)
    from serving import pricing
    report = pricing.cost_report(record.get("usage") or {})
    value = report["usd"] if report["usd"] is not None else report["usd_priced_subset"]
    return float(value) if value is not None else None


def luccy_leg(client: LuccyClient, row: dict, pause=None) -> dict:
    """luccy's answer to one question. A "busy" 503 is retried up to BUSY_RETRIES times after its
    Retry-After (free: luccy refuses before it starts); `pause(seconds) -> bool` waits and says
    whether the run was abandoned meanwhile (then no retry)."""
    pause = pause or (lambda seconds: time.sleep(seconds) or False)
    retries = 0
    while True:
        t0 = time.monotonic()
        status, body, headers = client.ask(row["question"])
        latency = round(time.monotonic() - t0, 2)
        if status != 503 or retries >= BUSY_RETRIES or pause(retry_after_s(headers, BUSY_WAIT_S)):
            break
        retries += 1
    leg = parse_luccy(status, body, latency)
    leg["detail"]["busy_retries"] = retries
    log = leg["detail"].get("log")
    if leg["status"] == "ok" and log:
        # free: the stored run record carries luccy's own cost of the answer
        rstatus, record = client.record(log)
        if rstatus == 200 and isinstance(record, dict):
            leg["usd"] = record_usd(record)
            leg["cost_source"] = "measured" if leg["usd"] is not None else None
    return leg


def datagov_leg(client: DataGovClient, arm: Arm, row: dict, corpus: Corpus) -> dict:
    t0 = time.monotonic()
    status, body = client.answer(arm.eval_arm, row["question"])
    return parse_datagov(status, body, round(time.monotonic() - t0, 2), corpus)


# --------------------------------------------------------------------------- dry-run fakes
def _unit(*parts) -> float:
    digest = hashlib.sha256("\0".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


# Fake A answers well on a share of the questions; A2 and C mostly agree with it.
FAKE_SKILL = 0.7
FAKE_FOLLOWS_A = {"A2": 0.1, "C": 0.12}            # chance to flip A's outcome
BEHAVIOUR_WORDING = {
    "abstain": "This is not documented in the available sources.",
    "refuse_privacy": "I cannot share personal contact details.",
    "flag_conflict": "Note that the two sources conflict on this point.",
    "flag_stale": "One of the pages is outdated; the more recent page is current.",
}


def fake_good(arm: str, row_id: str) -> bool:
    good_a = _unit("A", row_id) < FAKE_SKILL
    return good_a != (_unit(arm, row_id) < FAKE_FOLLOWS_A.get(arm, 0.0))


def fake_answer(arm: str, row: dict, corpus: Corpus) -> tuple[str, list[str], bool, list[str]]:
    """(answer, cited luccy pages, abstained, gaps): the expected facts and documents when the
    arm 'knows' the row, an abstention otherwise. No model, no network."""
    behaviour = row.get("expected_behaviour")
    if not fake_good(arm, row["id"]):
        return "I could not find this in the available documents.", [], True, ["Not covered by the documents."]
    parts = list(row.get("expected_facts") or [])
    value = (row.get("warehouse_expected") or {}).get("value")
    if value is not None:
        parts.append(f"The {str((row.get('warehouse_expected') or {}).get('metric_key') or 'value').replace('_', ' ')}"
                     f" is {value}")
    if behaviour in BEHAVIOUR_WORDING:
        parts.append(BEHAVIOUR_WORDING[behaviour])
    cited = []
    for group in expected_groups(row):
        cited.extend([x for x in group.split("|") if x in corpus.pages][:1])
    abstained = behaviour == "abstain"
    return ". ".join(p.rstrip(".") for p in parts) + ".", cited, abstained, (
        ["Not covered by the documents."] if abstained else [])


def fake_luccy_body(arm: str, row: dict, corpus: Corpus) -> dict:
    answer, cited, abstained, gaps = fake_answer(arm, row, corpus)
    return {"answer": answer, "answer_html": "", "footer": "dry run", "log": None,
            "citations": [{"id": c, "title": f"Page {c}", "url": None} for c in cited],
            "abstained": abstained, "gaps": gaps}


def fake_verbatim(answer: str, cited: list[str]) -> str:
    """Arm C's reply as DataGov's chat stores it: luccy's answer, its sources, the footer."""
    parts = [answer]
    if cited:
        parts.append("\n".join([SOURCES_HEAD] + [f"- Page {c} (outdated copy)" for c in cited]))
    parts.append(f"{FOOTER} · ref 0dd1e000")
    return "\n\n".join(parts)


def fake_datagov_body(arm: str, row: dict, corpus: Corpus) -> dict:
    """DataGov's eval reply for arm C: luccy's answer, verbatim, after one analyzer tool call."""
    answer, cited, abstained, gaps = fake_answer(arm, row, corpus)
    citations = [{"document_id": corpus.document_of_page.get(c), "luccy_id": None, "title": f"Page {c}",
                  "url": None} for c in cited]
    return {"answer": fake_verbatim(answer, cited), "judged_answer": answer,
            "citations": citations, "abstained": abstained, "gaps": gaps,
            "latency_s": round(1 + 9 * _unit("latency", arm, row["id"]), 2), "tool_calls": 1,
            "verbatim": True, "tool_error": False, "error": None,
            "cost_usd": 0.0, "cost_source": "measured"}


def fake_leg(arm: Arm, row: dict, corpus: Corpus) -> dict:
    """A dry-run leg: a fake reply run through the same parser as a real one, at no cost."""
    latency = round(1 + 9 * _unit("latency", arm.code, row["id"]), 2)
    if arm.system == "luccy":
        leg = parse_luccy(200, fake_luccy_body(arm.code, row, corpus), latency)
    else:
        leg = parse_datagov(200, fake_datagov_body(arm.code, row, corpus), latency, corpus)
    leg["usd"], leg["cost_source"] = 0.0, "measured"
    return leg
