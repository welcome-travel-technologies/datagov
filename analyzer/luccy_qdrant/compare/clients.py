"""HTTP clients for luccy (POST /ask) and DataGov's internal eval endpoints. One call per
request: nothing here retries (the one free retry, luccy's "busy" 503, is arms.luccy_leg's).
A transport failure or a timeout comes back as status 0."""
from __future__ import annotations

import json
import math
import re
from urllib.parse import parse_qs, urlsplit

import httpx

# serving/chats.py RECORD_ID_RE: the only run-record ids luccy issues and its routes accept.
RECORD_ID_RE = re.compile(r"[0-9a-f]{8}")
DOCUMENTS_PATH = "/api/document-analyzer/internal/documents/"
DIGEST_PATH = "/api/document-analyzer/internal/documents/digest/"
EVAL_ANSWER_PATH = "/api/document-analyzer/internal/eval/answer/"
EVAL_RUNS_PATH = "/api/document-analyzer/internal/eval/runs/"
PAGE_LIMIT = 500
MAX_PAGES = 2000                                   # a cursor loop guard: 1,000,000 documents
FREE_CALL_TIMEOUT = 30.0
# An arm name no DataGov server knows: its eval answer endpoint refuses it with a 400 after
# the token check and before any work, so the probe proves the token for free.
PROBE_ARM = "compare-preflight-unknown-arm"
MAX_RETRY_AFTER_S = 60.0


def error_text(body) -> str:
    if isinstance(body, dict):
        return str(body.get("error") or body.get("detail") or body.get("message") or body)[:300]
    return str(body)[:300]


class Http:
    """A thin, thread-safe wrapper over one httpx.Client: (status, parsed body)."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kw):
        """(status, parsed body)."""
        status, body, _ = self.request_full(method, url, **kw)
        return status, body

    def request_full(self, method: str, url: str, *, token: str | None = None, json_body=None,
                     content: bytes | None = None, params: dict | None = None, timeout: float = FREE_CALL_TIMEOUT):
        """(status, parsed body, response headers); status 0 and no headers on a transport failure."""
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if json_body is not None:
            content = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        if content is not None:
            headers["Content-Type"] = "application/json"
        self.calls.append(f"{method} {urlsplit(url).path}")
        try:
            response = self.client.request(method, url, headers=headers, content=content, params=params,
                                           timeout=timeout)
        except httpx.HTTPError as e:
            return 0, {"error": f"{type(e).__name__}: {str(e)[:300]}"}, {}
        raw = response.content
        try:
            body = response.json() if raw else None
        except ValueError:
            body = {"error": raw[:300].decode("utf-8", "replace")}
        return response.status_code, body, response.headers


def retry_after_s(headers, default: float) -> float:
    """A Retry-After header in seconds (1..MAX_RETRY_AFTER_S); `default` when it is missing or
    an HTTP date."""
    try:
        value = float(str((headers or {}).get("retry-after") or "").strip())
    except ValueError:
        return default
    return min(MAX_RETRY_AFTER_S, max(1.0, value)) if math.isfinite(value) else default


class LuccyClient:
    def __init__(self, http: Http, base: str, key: str, timeout: float):
        self.http, self.base, self.key, self.timeout = http, base.rstrip("/"), key, timeout

    def health(self):
        return self.http.request("GET", self.base + "/health", timeout=FREE_CALL_TIMEOUT)

    def auth_ok(self) -> tuple[bool, int, str]:
        """A free bearer check: an unknown record id answers 404 only past the key check."""
        status, body = self.http.request("GET", self.base + "/records/00000000", token=self.key)
        if status == 404:
            return True, status, "bearer key accepted"
        return False, status, f"HTTP {status}: {error_text(body)}"

    def ask(self, question: str):
        """(status, body, headers): the headers carry a busy 503's Retry-After."""
        return self.http.request_full("POST", self.base + "/ask", token=self.key, json_body={"question": question},
                                      timeout=self.timeout)

    def record(self, log: str):
        if not isinstance(log, str) or not RECORD_ID_RE.fullmatch(log):
            raise ValueError(f"not a luccy record id: {str(log)[:40]!r}")
        return self.http.request("GET", f"{self.base}/records/{log}", token=self.key)


def next_cursor(body: dict) -> str | None:
    """The next page's cursor from a listing body: next_cursor / cursor_next, or a `next` that is
    either a cursor or a URL carrying ?cursor=."""
    for key in ("next_cursor", "cursor_next"):
        if body.get(key):
            return str(body[key])
    nxt = body.get("next")
    if not nxt:
        return None
    nxt = str(nxt)
    if "://" in nxt or nxt.startswith("/") or "cursor=" in nxt:
        values = parse_qs(urlsplit(nxt).query).get("cursor")
        return values[0] if values else None
    return nxt


def page_documents(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("results", "documents", "items", "data"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


class DataGovClient:
    def __init__(self, http: Http, base: str, eval_token: str, sync_token: str, timeout: float):
        self.http, self.base = http, base.rstrip("/")
        self.eval_token, self.sync_token, self.timeout = eval_token, sync_token, timeout

    def documents(self) -> tuple[int, list[dict] | str]:
        """(200, every in-scope document) or (status, why) on the first failed page."""
        docs, cursor, seen = [], "", set()
        for _ in range(MAX_PAGES):
            status, body = self.http.request("GET", self.base + DOCUMENTS_PATH, token=self.sync_token,
                                             params={"cursor": cursor, "limit": PAGE_LIMIT})
            if status != 200 or not isinstance(body, (dict, list)):
                return status, f"HTTP {status}: {error_text(body)}"
            page = page_documents(body)
            docs.extend(page)
            cursor = next_cursor(body) if isinstance(body, dict) else None
            if not cursor or not page or cursor in seen:
                return 200, docs
            seen.add(cursor)
        return 200, docs

    def digest(self):
        """The corpus digest (free): {"organization_id", "count", "digest"}."""
        return self.http.request("GET", self.base + DIGEST_PATH, token=self.sync_token)

    def answer(self, arm: str, question: str):
        return self.http.request("POST", self.base + EVAL_ANSWER_PATH, token=self.eval_token,
                                 json_body={"arm": arm, "question": question}, timeout=self.timeout)

    def probe(self, arm: str):
        """A free eval answer call: an unknown arm, or a known arm with an empty question, is
        refused (400) after the token check and before any work."""
        question = "preflight" if arm == PROBE_ARM else ""
        return self.http.request("POST", self.base + EVAL_ANSWER_PATH, token=self.eval_token,
                                 json_body={"arm": arm, "question": question}, timeout=FREE_CALL_TIMEOUT)

    def post_run(self, body: bytes):
        return self.http.request("POST", self.base + EVAL_RUNS_PATH, token=self.eval_token, content=body,
                                 timeout=60.0)
