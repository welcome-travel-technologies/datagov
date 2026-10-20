"""The body posted to DataGov's eval runs endpoint, kept under MAX_BODY_BYTES.

    {"run_id", "status": "running"|"done"|"failed", "params", "summary", "rows"}
    rows = [{"id", "kind", "question", "order": [arm codes, in the order they were asked],
             "arms": {"<arm>": {"verdict", "infra_error", "cited", "answer", "latency_s", "fact_ratio"}},
             "expected_docs_missing": [gold document groups DataGov does not hold],
             "recorded": "pass" | "fail" | null}]

`answer` is the arm's reply as it wrote it (for arm C, DataGov's verbatim reply with its
sources and footer); the judge read only its prose (judging.py). `infra_error` marks a leg
left out of the paired statistics (verdict "error": an HTTP error, a timeout, a failed tool
or judge call). `expected_docs_missing` explains a failure (the question is scored anyway);
`recorded` is luccy's recorded verdict on the question (null when it has none for the
question as it is now).

Answers are cut to ANSWER_CHARS. When the body is still too large, the answers of the oldest
rows are shortened first (to 1000 characters, then 200, then none), and only past that are the
oldest rows dropped. The summary is never cut.
"""
from __future__ import annotations

import json

MAX_BODY_BYTES = 2_000_000          # "under 2 MB", with headroom for any framing
ANSWER_CHARS = 4000
SHRINK_STEPS = (1000, 200, 0)
ELLIPSIS = "…"


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + ELLIPSIS if limit else ""


def public_row(row: dict, legs: dict[str, dict], arms: list[str], order: list[str] | None = None,
               missing=(), recorded: bool | None = None) -> dict:
    """One question as the frontend shows it. An error's message stands in for a missing answer."""
    out = {}
    for arm in arms:
        o = legs.get(arm)
        if o is None:
            continue
        answer = o["answer"] or (f"[error] {o['error']}" if o["verdict"] == "error" and o.get("error") else "")
        out[arm] = {"verdict": o["verdict"], "infra_error": bool(o.get("infra_error")), "cited": list(o["cited"]),
                    "answer": clip(answer, ANSWER_CHARS), "latency_s": o["latency_s"], "fact_ratio": o["fact_ratio"]}
    return {"id": row["id"], "kind": row["kind"], "question": row["question"],
            "order": list(order if order is not None else [a for a in arms if a in legs]), "arms": out,
            "expected_docs_missing": list(missing or []),
            "recorded": None if recorded is None else ("pass" if recorded else "fail")}


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fit(body: dict, cap: int = MAX_BODY_BYTES) -> bytes:
    """`body` encoded under `cap` bytes; its rows are shortened in place, oldest first."""
    data = encode(body)
    if len(data) <= cap:
        return data
    rows = body["rows"]
    total = len(data)
    for limit in SHRINK_STEPS:
        for row in rows:
            before = len(encode(row))
            for leg in row["arms"].values():
                leg["answer"] = clip(leg["answer"], limit)
            total -= before - len(encode(row))
            if total <= cap:
                return encode(body)
    while rows and total > cap:
        total -= len(encode(rows.pop(0))) + 1          # the row and its separator
    data = encode(body)
    while rows and len(data) > cap:                     # cannot happen; kept as a hard guard
        rows.pop(0)
        data = encode(body)
    return data


def build(run_id: str, status: str, params: dict, summary: dict, rows: list[dict],
          cap: int = MAX_BODY_BYTES) -> bytes:
    return fit({"run_id": run_id, "status": status, "params": params, "summary": summary, "rows": rows}, cap)
