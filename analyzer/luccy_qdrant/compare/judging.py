"""Every arm is scored by luccy's own judge (eval/gold/judge.py: same model, same rubric,
same deterministic pre-checks), from the run-record shape it reads:

    {"answer": {"answer": str, "citations": [luccy page ids], "unanswerable_parts": [str]},
     "latency_s": {"total": float}}

Every arm is judged on the same kind of text, its answer prose (leg["judged_answer"]):
  - A, A2: luccy's /ask `answer`.
  - C: the eval reply's `judged_answer` (for a verbatim chat reply, luccy's raw answer). An older server sends none; then the reply's `answer` with what DataGov adds
    around it removed (datagov_prose): a trailing debug section, the "Answered by the
    Document Analyzer" footer line and a trailing "**Sources**" list. Those words are not the
    answer's: a source titled "(Outdated ...)" would otherwise pass a stale-flag check that
    luccy's own answer fails.
The answer is posted as the arm wrote it; only the judge reads the prose.

Two normalisations, applied to every arm alike:
  - Bracket citation markers ("[1]", "[2, 3]") are removed from the text the judge scores. The
    judge strips markdown links ([title](url)) before reading figures, but reads a bare "[1]" as
    a rival number. Markdown links are left alone.
  - Cited ids are reduced to their page ("123-part-2" -> "123"), the id the gold set names.
"""
from __future__ import annotations

import re

from .ids import page_of, unique

MARKER_RE = re.compile(r"[ \t]*\[\d+(?:\s*[,–-]\s*\d+)*\](?!\()")
# DataGov's own additions to a chat reply (backend/app/document_analyzer/assistant.py
# FOOTER and render_verbatim, catalog/services/debug_render.py render_debug_section).
FOOTER = "Answered by the Document Analyzer"
SOURCES_HEAD = "**Sources**"
DEBUG_RE = re.compile(r"\n[ \t]*---[ \t]*\n\s*\*\*[^\n]*Debug\*\*.*\Z", re.DOTALL)


def strip_markers(text: str) -> str:
    return MARKER_RE.sub("", text or "")


def datagov_prose(text: str) -> str:
    """A DataGov reply without what DataGov adds around the answer: a trailing debug section,
    the analyzer footer line, then a trailing "**Sources**" list (every line "- ...")."""
    body = DEBUG_RE.sub("", text or "").rstrip()
    lines = body.split("\n")
    if lines and lines[-1].lstrip().startswith(FOOTER):
        body = "\n".join(lines[:-1]).rstrip()
    head, marker, tail = body.rpartition("\n" + SOURCES_HEAD + "\n")
    if not marker and body.startswith(SOURCES_HEAD + "\n"):
        head, marker, tail = "", SOURCES_HEAD, body[len(SOURCES_HEAD) + 1:]
    if marker and tail.strip() and all(line.startswith("- ") for line in tail.strip().split("\n")):
        body = head.rstrip()
    return body


def judged_text(leg: dict) -> str:
    """The text the judge reads for a leg: its `judged_answer` (every parsed leg has one), else
    its answer without DataGov's additions."""
    judged = leg.get("judged_answer")
    return judged if isinstance(judged, str) else datagov_prose(leg.get("answer") or "")


def judge_record(leg: dict) -> dict:
    return {"answer": {"answer": strip_markers(judged_text(leg)),
                       "citations": unique(page_of(c) for c in leg.get("cited") or []),
                       "unanswerable_parts": list(leg.get("gaps") or [])},
            "latency_s": {"total": leg.get("latency_s")}}


def make_judge(dry_run: bool):
    from eval.gold.judge import Judge
    return Judge(dry_run=dry_run)


def judge_model(judge) -> str:
    return "dry-run" if getattr(judge, "dry_run", False) else str(getattr(judge, "model", "unknown"))


def judge_usd(score: dict | None) -> float:
    """The judge call's cost, priced with luccy's serving/pricing.py (0 for the dry-run judge)."""
    usage = (score or {}).get("judge_usage") or {}
    if not any(usage.values()):
        return 0.0
    from serving import pricing
    usd = pricing.call_usd(score.get("judge_model"), pricing.DOOR, usage.get("input_tokens") or 0,
                           (usage.get("output_tokens") or 0) + (usage.get("thinking_tokens") or 0))
    return float(usd or 0.0)


def unpriced_judge(judge) -> str | None:
    """The judge model when serving/pricing.py has no price for it (the spend cap needs one)."""
    if getattr(judge, "dry_run", False):
        return None
    from serving import pricing
    model = getattr(judge, "model", None)
    return None if pricing.PRICES.get((pricing.DOOR, model)) else str(model)


def score_leg(judge, row: dict, leg: dict) -> tuple[dict | None, str | None, float]:
    """(score, judge error, judge usd). An errored leg is never judged; a judge failure is
    recorded, never retried."""
    if leg["status"] != "ok":
        return None, None, 0.0
    try:
        score = judge.score(row, judge_record(leg))
    except Exception as e:                        # noqa: BLE001 - a judge failure is a result
        return None, f"judge failed: {type(e).__name__}: {str(e)[:300]}", 0.0
    return score, None, judge_usd(score)
