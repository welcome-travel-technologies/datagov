"""Fakes for the comparison runner: DataGov (documents listing and digest, eval answer, eval runs)
and luccy (/health, /ask, /records) behind one httpx.MockTransport. No network, no model."""
from __future__ import annotations

import json
import re
import sys
import uuid

import httpx
import pytest

sys.dont_write_bytecode = True

from luccy_qdrant.compare import clients
from luccy_qdrant.compare.runner import ensure_luccy_path

ensure_luccy_path()

LUCCY = "http://luccy.test"
DATAGOV = "http://datagov.test"
ENV = {"LUCCY_URL": LUCCY, "LUCCY_API_KEY": "luccy-key", "DATAGOV_URL": DATAGOV,
       "LUCCY_EVAL_TOKEN": "eval-token", "LUCCY_SYNC_TOKEN": "sync-token"}
# A page DataGov does not hold (sh-001's only document) and one it holds as two parts.
MISSING_PAGE = "4435476587"
SPLIT_PAGE = "6836781135"
# DataGov's eval answer endpoint (backend evaluation.py): the arm, then the question, are
# checked before anything runs.
EVAL_ARMS = ("chat_analyzer_only",)
UNKNOWN_ARM = "Choose an arm from: " + ", ".join(EVAL_ARMS) + "."
BAD_QUESTION = "Ask a question of 1 to 10000 characters."


def document_id(external_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "datagov-test:" + external_id))


def gold_rows() -> list[dict]:
    from eval.gold import judge
    return list(judge.load_gold().values())


def listing_row(external_id: str) -> dict:
    """One C2 export row as DataGov's internal documents listing sends it."""
    kind, key = external_id.split(":")[0].lower(), external_id.split(":")[1]
    if kind == "confluence":
        source_type, url = "confluence", f"https://welcome.atlassian.net/wiki/spaces/OPS/pages/{key}"
    elif kind == "jira":
        source_type, url = "jira", f"https://welcome.atlassian.net/browse/{key}"
    else:
        source_type, url = "upload", None
    return {"document_id": document_id(external_id), "version_id": document_id("v:" + external_id),
            "external_id": external_id, "title": external_id, "source_type": source_type, "source_url": url,
            "collection_name": "Docs", "group": "Welcome"}


def corpus_documents(rows: list[dict]) -> list[dict]:
    """DataGov's listing for the gold set's pages: Confluence ids and Jira keys are there (one page
    split in two parts), luccy's Drive pages (gd...) are not, MISSING_PAGE is not, and one DataGov
    upload luccy never had is."""
    externals = []
    for row in rows:
        for group in row.get("expected_doc_ids") or []:
            for page in group.split("|"):
                if page.startswith(("registry:", "gd")) or page == MISSING_PAGE:
                    continue
                if page == SPLIT_PAGE:
                    externals += [f"confluence:{page}:part:1", f"confluence:{page}:part:2"]
                elif re.fullmatch(r"\d+", page):
                    externals.append(f"confluence:{page}")
                else:
                    externals.append(f"jira:{page}")
    externals.append("upload:1AbCdEf")
    return [listing_row(e) for e in dict.fromkeys(externals)]


class World:
    """The fake services. `luccy_ask(question) -> (status, body[, headers])` and
    `datagov_answer(arm, question) -> (status, body)` are None unless a test makes paid calls.
    `eval_status` makes the eval endpoint answer that status for every known arm before it
    reads the question (a server that checks its own readiness first)."""

    def __init__(self, documents, *, page_size=150, luccy_ask=None, datagov_answer=None, records=None,
                 runs_status=200, health_status=200, sync_token="sync-token", digest_status=200,
                 eval_status=None, eval_arms=EVAL_ARMS):
        self.documents, self.page_size = documents, page_size
        self.luccy_ask, self.datagov_answer = luccy_ask, datagov_answer
        self.records = records if records is not None else {}
        self.runs_status, self.health_status, self.sync_token = runs_status, health_status, sync_token
        self.digest_status, self.eval_status, self.eval_arms = digest_status, eval_status, eval_arms
        self.calls: list[str] = []
        self.answers: list[tuple[str, str]] = []           # (eval arm, question) actually answered
        self.posts: list[dict] = []
        self.post_sizes: list[int] = []

    def paid_calls(self) -> list[str]:
        """luccy /ask calls and DataGov eval answers that ran (the free probes are refused)."""
        return [c for c in self.calls if c.endswith(" /ask")] + [f"answer {arm}" for arm, _ in self.answers]

    def eval_answer(self, request: httpx.Request) -> httpx.Response:
        req = json.loads(request.content)
        arm, question = req.get("arm"), req.get("question")
        if arm not in self.eval_arms:
            return httpx.Response(400, json={"detail": UNKNOWN_ARM})
        if self.eval_status is not None:
            return httpx.Response(self.eval_status, json={"detail": "LUCCY_EVAL_USER_ID must be the id of an "
                                                                    "active member of organization 1."})
        if not isinstance(question, str) or not question.strip():
            return httpx.Response(400, json={"detail": BAD_QUESTION})
        if not self.datagov_answer:
            return httpx.Response(500, json={"error": "unexpected paid call"})
        self.answers.append((arm, question))
        status, body = self.datagov_answer(arm, question)
        return httpx.Response(status, json=body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host, request.url.path, request.method
        auth = request.headers.get("authorization")
        self.calls.append(f"{method} {path}")
        if host == "datagov.test":
            if path in (clients.DOCUMENTS_PATH, clients.DIGEST_PATH) and method == "GET":
                if auth != "Bearer " + self.sync_token:
                    return httpx.Response(401, json={"detail": "bad token"})
                if path == clients.DIGEST_PATH:
                    return httpx.Response(self.digest_status, json={"organization_id": "1", "count": len(self.documents),
                                                                    "digest": "f" * 64})
                start = int(request.url.params.get("cursor") or 0)
                assert request.url.params.get("limit") == str(clients.PAGE_LIMIT)
                end = start + self.page_size
                return httpx.Response(200, json={"results": self.documents[start:end],
                                                 "next_cursor": str(end) if end < len(self.documents) else None})
            if auth != "Bearer eval-token":
                return httpx.Response(401, json={"detail": "Invalid service credentials."})
            if path == clients.EVAL_RUNS_PATH and method == "POST":
                statuses = self.runs_status if isinstance(self.runs_status, list) else [self.runs_status]
                status = statuses[min(len(self.posts), len(statuses) - 1)]
                self.post_sizes.append(len(request.content))
                self.posts.append(json.loads(request.content))
                return httpx.Response(status, json={"ok": status == 200})
            if path == clients.EVAL_ANSWER_PATH and method == "POST":
                return self.eval_answer(request)
        if host == "luccy.test":
            if path == "/health":
                return httpx.Response(self.health_status, json={
                    "status": "ok" if self.health_status == 200 else "unhealthy", "version": "0.13.1",
                    "git": "abc123", "index": {"sig": "sig-1"}, "corpus_pages": len(self.documents)})
            if auth != "Bearer luccy-key":
                return httpx.Response(401, json={"error": "missing or wrong key"})
            if path.startswith("/records/"):
                record = self.records.get(path.rsplit("/", 1)[1])
                return httpx.Response(200, json=record) if record else httpx.Response(404, json={"error": "no record"})
            if path == "/ask" and method == "POST" and self.luccy_ask:
                status, body, *headers = self.luccy_ask(json.loads(request.content)["question"])
                return httpx.Response(status, json=body, headers=headers[0] if headers else None)
        return httpx.Response(500, json={"error": f"unexpected call {method} {path}"})


@pytest.fixture
def gold():
    return gold_rows()


@pytest.fixture
def documents(gold):
    return corpus_documents(gold)


@pytest.fixture
def corpus(documents):
    from luccy_qdrant.compare.ids import Corpus
    return Corpus(documents)
