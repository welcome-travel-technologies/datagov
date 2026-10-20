import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import {
  analyzerApi,
  citationSource,
  condenseHistory,
  followAnswer,
  isComparisonHidden,
  isOpenableDocumentId,
  isReservedConversationId,
  newConversationId,
  normalizeUrl,
  openAskStream,
  readEventStream,
  isVerbatimAnalyzerAnswer,
  SseParser,
  splitClosingLine,
  staleWarning,
  StreamDroppedError,
  withoutImages,
  type HistoryTurn,
  type SseMessage,
} from "./document-analyzer-api";

afterEach(() => { vi.unstubAllGlobals(); document.cookie = "csrftoken=; max-age=0; path=/"; });

function parse(chunks: string[]) {
  const messages: SseMessage[] = [];
  const comments: string[] = [];
  const parser = new SseParser((m) => messages.push(m), (c) => comments.push(c));
  for (const chunk of chunks) parser.push(chunk);
  return { messages, comments, parser };
}

const encoder = new TextEncoder();
/** A body that hands out `chunks` one read at a time, then ends or breaks. */
function streamOf(chunks: (string | Uint8Array)[], fail?: Error) {
  const queue = chunks.map((chunk) => (typeof chunk === "string" ? encoder.encode(chunk) : chunk));
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      const next = queue.shift();
      if (next) controller.enqueue(next);
      else if (fail) controller.error(fail);
      else controller.close();
    },
  });
}
const sse = (chunks: string[], fail?: Error) => new Response(streamOf(chunks, fail), { headers: { "Content-Type": "text/event-stream" } });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

describe("SseParser", () => {
  it("parses luccy's events with ids and skips ping comments", () => {
    const { messages, comments } = parse([
      'event: stage\r\nid: 0\r\ndata: {"stage":"starting","job":"j1"}\r\n\r\n',
      ": ping - 2026-10-16 10:00:00\r\n\r\n",
      'event: done\r\nid: 1\r\ndata: {"answer":"Hi"}\r\n\r\n',
    ]);
    expect(messages).toEqual([
      { event: "stage", id: "0", data: '{"stage":"starting","job":"j1"}' },
      { event: "done", id: "1", data: '{"answer":"Hi"}' },
    ]);
    expect(comments).toEqual(["ping - 2026-10-16 10:00:00"]);
  });

  it("accepts LF, CRLF and bare CR line ends", () => {
    expect(parse(["event: a\ndata: 1\n\n"]).messages).toEqual([{ event: "a", data: "1", id: "" }]);
    expect(parse(["event: a\r\ndata: 1\r\n\r\n"]).messages).toEqual([{ event: "a", data: "1", id: "" }]);
    expect(parse(["event: a\rdata: 1\r\r"]).messages).toEqual([{ event: "a", data: "1", id: "" }]);
  });

  it("joins a CRLF split across chunks into one line end", () => {
    const { messages } = parse(["data: one\r", "\ndata: two\r", "\n\r", "\n"]);
    expect(messages).toEqual([{ event: "message", data: "one\ntwo", id: "" }]);
  });

  it("joins multi-line data with newlines and keeps empty data lines", () => {
    expect(parse(["data: first\ndata:\ndata:  indented\n\n"]).messages[0].data).toBe("first\n\n indented");
  });

  it("drops only one space after the colon and treats a line without colon as a field name", () => {
    const { messages } = parse(["data:no-space\n\n", "data\n\n", "event:x\ndata:  two\n\n"]);
    expect(messages.map((m) => m.data)).toEqual(["no-space", "", " two"]);
    expect(messages[2].event).toBe("x");
  });

  it("does not dispatch a block that has no data, and resets the event name after each block", () => {
    const { messages } = parse(["event: lonely\nid: 3\n\n", "data: x\n\n"]);
    expect(messages).toEqual([{ event: "message", data: "x", id: "3" }]);
  });

  it("carries the last id forward, ignores ids with NUL and unknown fields", () => {
    const { messages, parser } = parse(["id: 7\ndata: a\n\n", "data: b\nretry: 10\nfoo: bar\n\n", "id: x\u0000y\ndata: c\n\n"]);
    expect(messages.map((m) => m.id)).toEqual(["7", "7", "7"]);
    expect(parser.lastEventId).toBe("7");
  });

  it("handles chunk boundaries anywhere, identically to one chunk", () => {
    const text = '﻿event: stage\r\nid: 0\r\ndata: {"stage":"retrieve"}\r\n\r\n: ping\n\nevent: answer\nid: 1\ndata: {"a":\ndata: 1}\r\rid: 2\nevent: done\ndata: {}\n\n';
    const whole = parse([text]).messages;
    expect(whole).toHaveLength(3);
    expect(whole[0].event).toBe("stage");
    for (let i = 0; i <= text.length; i++) expect(parse([text.slice(0, i), text.slice(i)]).messages).toEqual(whole);
    expect(parse(text.split("")).messages).toEqual(whole);
  });

  it("strips a leading byte-order mark only once", () => {
    expect(parse(["﻿data: a\n\n"]).messages[0].data).toBe("a");
  });

  it("never dispatches an event cut off by the end of the stream, nor counts its id", () => {
    const { messages, parser } = parse(["id: 1\ndata: a\n\n", "event: answer\nid: 2\ndata: {\"partial\""]);
    parser.end();
    expect(messages.map((m) => m.data)).toEqual(["a"]);
    expect(parser.lastEventId).toBe("1");
  });
});

describe("readEventStream", () => {
  it("decodes UTF-8 split inside a character and stops when told to", async () => {
    const bytes = encoder.encode("data: café …\n\ndata: second\n\ndata: never\n\n");
    const seen: string[] = [];
    const stopped = await readEventStream(streamOf([bytes.slice(0, 9), bytes.slice(9, 13), bytes.slice(13)]), (m) => {
      seen.push(m.data);
      return m.data === "second";
    });
    expect(stopped).toBe(true);
    expect(seen).toEqual(["café …", "second"]);
  });

  it("reports a stream that ended without being stopped", async () => {
    expect(await readEventStream(streamOf(["data: a\n\n"]), () => false)).toBe(false);
  });

  it("rejects when the connection breaks", async () => {
    await expect(readEventStream(streamOf(["data: a\n\n"], new TypeError("network")), () => false)).rejects.toThrow("network");
  });
});

describe("Document Analyzer API", () => {
  it("posts the question with CSRF, session credentials and an SSE accept header", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(sse(["data: {}\n\n"]));
    vi.stubGlobal("fetch", fetchMock);
    document.cookie = "csrftoken=analyzer-test; path=/";
    await openAskStream({ question: "Hotel limit?", conversation: "abc", sources: ["jira"] });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/document-analyzer/ask/stream/");
    expect(options).toMatchObject({ method: "POST", credentials: "include", cache: "no-store" });
    expect(options?.headers).toMatchObject({ "X-CSRFToken": "analyzer-test", "Content-Type": "application/json", Accept: "text/event-stream" });
    expect(JSON.parse(options?.body as string)).toEqual({ question: "Hotel limit?", conversation: "abc", sources: ["jira"] });
  });

  it("maps a refused question to ApiError with the backend's words", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(json({ detail: "The monthly spend cap is reached." }, 429)));
    const error = await openAskStream({ question: "x" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 429, message: "The monthly spend cap is reached." });
  });

  it("sends feedback with CSRF and cuts the note to 500 characters", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    document.cookie = "csrftoken=fb; path=/";
    await analyzerApi.feedback("0a1b2c3d", "wrong", "x".repeat(600));
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/document-analyzer/feedback/");
    expect(options?.headers).toMatchObject({ "X-CSRFToken": "fb" });
    expect(JSON.parse(options?.body as string)).toEqual({ answer_id: "0a1b2c3d", verdict: "wrong", note: "x".repeat(500) });
  });

  it("encodes document ids, passes only a valid answer reference, and refuses dot-only ids", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => json({ id: "x" }));
    vi.stubGlobal("fetch", fetchMock);
    await analyzerApi.document("gd:abc@1", "0a1b2c3d");
    await analyzerApi.document("12345", "not-hex");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/document-analyzer/documents/gd%3Aabc%401/?answer=0a1b2c3d",
      "/api/document-analyzer/documents/12345/",
    ]);
    await expect(analyzerApi.document("..")).rejects.toMatchObject({ status: 404 });
    await expect(analyzerApi.document("a/b")).rejects.toMatchObject({ status: 404 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("lists comparison runs, plain or paginated, and opens one by its encoded id", async () => {
    const run = { run_id: "2026-10-16T10:00", status: "done" };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(json([run]))
      .mockResolvedValueOnce(json({ results: [run] }))
      .mockResolvedValueOnce(json({ ...run, rows: [] }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(analyzerApi.comparisons()).resolves.toEqual([run]);
    await expect(analyzerApi.comparisons()).resolves.toEqual([run]);
    await expect(analyzerApi.comparison("2026-10-16T10:00")).resolves.toMatchObject({ rows: [] });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/document-analyzer/comparisons/",
      "/api/document-analyzer/comparisons/",
      "/api/document-analyzer/comparisons/2026-10-16T10%3A00/",
    ]);
    for (const bad of ["", "..", "a..b", "a/b", "../x"]) await expect(analyzerApi.comparison(bad)).rejects.toMatchObject({ status: 404 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("treats only 403 and 404 from the comparison endpoints as 'hide the view'", () => {
    expect(isComparisonHidden(new ApiError(403, "Forbidden"))).toBe(true);
    expect(isComparisonHidden(new ApiError(404, "Not found"))).toBe(true);
    expect(isComparisonHidden(new ApiError(500, "Boom"))).toBe(false);
    expect(isComparisonHidden(new TypeError("offline"))).toBe(false);
    expect(isComparisonHidden(null)).toBe(false);
  });

  it("reads the status endpoint", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(json({ enabled: true, available: false }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(analyzerApi.status()).resolves.toMatchObject({ available: false });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/document-analyzer/status/");
  });
});

describe("followAnswer", () => {
  const events = (name: string, id: number, data: object) => `event: ${name}\nid: ${id}\ndata: ${JSON.stringify(data)}\n\n`;

  it("re-attaches after a dropped connection with Last-Event-ID and finishes on done", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sse([events("stage", 0, { stage: "starting", job: "job_1" }), events("stage", 1, { stage: "retrieve", job: "job_1" })], new TypeError("dropped")))
      .mockResolvedValueOnce(sse([": ping\n\n", events("answer", 2, { answer: "A", log: "0a1b2c3d" }), events("done", 3, { answer: "A2" })]));
    vi.stubGlobal("fetch", fetchMock);
    const seen: string[] = [];
    await followAnswer({ ask: { question: "q" }, signal: new AbortController().signal, retryDelayMs: 0, onEvent: (name, data, position) => seen.push(`${name}:${position.job}:${position.lastEventId}:${String(data.stage ?? data.answer)}`) });
    expect(seen).toEqual(["stage:job_1:0:starting", "stage:job_1:1:retrieve", "answer:job_1:2:A", "done:job_1:3:A2"]);
    const [url, options] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/document-analyzer/ask/stream/job_1/");
    expect(options?.method).toBe("GET");
    expect(options?.headers).toMatchObject({ "Last-Event-ID": "1" });
  });

  it("gives up after three re-attach tries", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sse([events("stage", 0, { stage: "starting", job: "j" })], new TypeError("dropped")))
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(json({ detail: "Bad gateway" }, 502))
      .mockResolvedValueOnce(sse([], new TypeError("dropped")));
    vi.stubGlobal("fetch", fetchMock);
    await expect(followAnswer({ ask: { question: "q" }, signal: new AbortController().signal, retryDelayMs: 0, onEvent: () => undefined }))
      .rejects.toBeInstanceOf(StreamDroppedError);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("stops at once when luccy no longer knows the job", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(json({ detail: "unknown job" }, 404));
    vi.stubGlobal("fetch", fetchMock);
    await expect(followAnswer({ resume: { job: "gone", lastEventId: "4" }, signal: new AbortController().signal, retryDelayMs: 0, onEvent: () => undefined }))
      .rejects.toMatchObject({ status: 404 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]?.headers).toMatchObject({ "Last-Event-ID": "4" });
  });

  it("cannot re-attach before the job id is known", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValueOnce(sse([], new TypeError("dropped"))));
    await expect(followAnswer({ ask: { question: "q" }, signal: new AbortController().signal, retryDelayMs: 0, onEvent: () => undefined }))
      .rejects.toBeInstanceOf(StreamDroppedError);
  });

  it("rejects with the abort when the caller aborts while waiting to re-attach", async () => {
    const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValueOnce(sse([events("stage", 0, { stage: "starting", job: "j" })], new TypeError("dropped"))));
    const run = followAnswer({ ask: { question: "q" }, signal: controller.signal, retryDelayMs: 60_000, onEvent: () => controller.abort() });
    await expect(run).rejects.toMatchObject({ name: "AbortError" });
  });

  it("ends on an error event", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValueOnce(sse([events("error", 0, { message: "That one failed on our side." }), events("done", 1, {})])));
    const seen: string[] = [];
    await followAnswer({ ask: { question: "q" }, signal: new AbortController().signal, onEvent: (name) => seen.push(name) });
    expect(seen).toEqual(["error"]);
  });
});

describe("conversation helpers", () => {
  it("condenses history as luccy does", () => {
    const turns: HistoryTurn[] = [
      { who: "you", text: "First?" },
      { who: "luccy", text: "Line one\nLine two" },
      { who: "you", text: "  " },
      { who: "you", text: "Second?" },
      { who: "luccy", text: "y".repeat(5000) },
    ];
    expect(condenseHistory(turns)).toEqual([
      { who: "you", text: "First?" },
      { who: "luccy", text: "Line one" },
      { who: "you", text: "Second?" },
      { who: "luccy", text: "y".repeat(4000) },
    ]);
  });

  it("keeps the last 50 turns and stays inside 64,000 characters", () => {
    const many = Array.from({ length: 60 }, (_, i): HistoryTurn => ({ who: "you", text: `q${i}` }));
    const kept = condenseHistory(many);
    expect(kept).toHaveLength(50);
    expect(kept[0].text).toBe("q10");
    const long = Array.from({ length: 10 }, (): HistoryTurn => ({ who: "you", text: "z".repeat(10_000) }));
    const fitted = condenseHistory(long);
    expect(fitted.reduce((n, t) => n + t.text.length, 0)).toBeLessThanOrEqual(64_000);
    expect(fitted).toHaveLength(6);
  });

  it("makes 24-character conversation ids", () => {
    expect(newConversationId()).toMatch(/^[a-z0-9]{24}$/);
    expect(newConversationId()).not.toBe(newConversationId());
  });

  it("never makes a conversation id in DataGov's reserved \"dg\" namespace", () => {
    // Random bytes that would spell "dg…" without the fixed prefix (3 → "d", 6 → "g").
    const spy = vi.spyOn(crypto, "getRandomValues").mockImplementation(<T extends ArrayBufferView | null>(array: T): T => {
      if (array instanceof Uint8Array) array.set(Array.from({ length: array.length }, (_, i) => (i % 2 ? 6 : 3)));
      return array;
    });
    try {
      const id = newConversationId();
      expect(id).toMatch(/^[a-z0-9]{24}$/);
      expect(isReservedConversationId(id)).toBe(false);
    } finally {
      spy.mockRestore();
    }
    for (let i = 0; i < 200; i++) expect(newConversationId().slice(0, 2).toLowerCase()).not.toBe("dg");
    expect(isReservedConversationId("dgpage-abc")).toBe(true);
    expect(isReservedConversationId("DGx")).toBe(true);
    expect(isReservedConversationId("dGx")).toBe(true);
    expect(isReservedConversationId("pdg")).toBe(false);
  });

  it("names citation sources, normalizes links and knows which ids open", () => {
    expect(citationSource({ id: "gd123", url: null })).toBe("drive");
    expect(citationSource({ id: "PBI-1", url: null })).toBeNull();
    expect(citationSource({ id: "x", url: "https://github.com/org/repo" })).toBe("github");
    expect(citationSource({ id: "4242", url: null })).toBe("confluence");
    expect(citationSource({ id: "DATA-12", url: "https://acme.atlassian.net/browse/DATA-12" })).toBe("jira");
    expect(normalizeUrl(" https://Acme.net/Page/// ")).toBe("https://acme.net/page");
    expect(isOpenableDocumentId("wh:bookings")).toBe(false);
    expect(isOpenableDocumentId("...")).toBe(false);
    // The backend refuses these with 400 (documents/<doc_id>/), so they stay labels.
    expect(isOpenableDocumentId("_conf")).toBe(false);
    expect(isOpenableDocumentId("a..b")).toBe(false);
    expect(isOpenableDocumentId("DATA-12")).toBe(true);
    expect(isOpenableDocumentId("gd:1a2B@x.y")).toBe(true);
  });

  it("escapes every image opener that is not escaped already, and nothing else", () => {
    const raw = String.raw;
    expect(withoutImages("See ![chart](https://x/p.png?d=1) and [a link](https://x).")).toBe(raw`See \![chart](https://x/p.png?d=1) and [a link](https://x).`);
    expect(withoutImages("![a][ref] ![b] [![c](u)](v)")).toBe(raw`\![a][ref] \![b] [\![c](u)](v)`);
    expect(withoutImages(raw`\![kept](u)`)).toBe(raw`\![kept](u)`);              // \! is already a literal "!"
    expect(withoutImages(raw`\\![img](u)`)).toBe(raw`\\\![img](u)`);            // \\ is a backslash, then an image
    expect(withoutImages(raw`\\\![kept](u)`)).toBe(raw`\\\![kept](u)`);
    expect(withoutImages("Wow! [x] !important")).toBe("Wow! [x] !important");
  });

  it("knows a verbatim analyzer answer only by the API's flag", () => {
    expect(isVerbatimAnalyzerAnswer({ verbatim: true, ref: "0a1b2c3d" })).toBe(true);
    expect(isVerbatimAnalyzerAnswer({ verbatim: true, ref: null })).toBe(true);
    expect(isVerbatimAnalyzerAnswer({ verbatim: false, ref: "0a1b2c3d" })).toBe(false);
    expect(isVerbatimAnalyzerAnswer(null)).toBe(false);
    expect(isVerbatimAnalyzerAnswer(undefined)).toBe(false);
    expect(isVerbatimAnalyzerAnswer({ verbatim: "true" } as never)).toBe(false);
  });

  it("splits the closing line off a verbatim answer, whatever it says", () => {
    const footnote = "Answered by the Document Analyzer · ref 0a1b2c3d";
    expect(splitClosingLine(`Hotels up to **€150**.\n\n- [Travel policy](https://x/wiki/1)\n\n${footnote}\n`))
      .toEqual({ body: "Hotels up to **€150**.\n\n- [Travel policy](https://x/wiki/1)", footnote });
    expect(splitClosingLine(`Answer\r\n\r\n${footnote}  \r\n`)).toEqual({ body: "Answer", footnote });
    expect(splitClosingLine("Nothing found.\n\nAnswered by the Document Analyzer · ref -"))
      .toEqual({ body: "Nothing found.", footnote: "Answered by the Document Analyzer · ref -" });
    expect(splitClosingLine(footnote)).toEqual({ body: "", footnote });
    expect(splitClosingLine("")).toEqual({ body: "", footnote: "" });
  });

  it("rebuilds luccy's old-documents warning from the end of answer_html", () => {
    const warning = "⚠ These documents are 21 days old (copied 2026-09-01) — recent changes may not be reflected.";
    expect(staleWarning(`<p>Hotels up to €150.</p>\n<p>${warning}</p>\n`)).toBe(warning);
    expect(staleWarning("<p>A</p>\n<p>⚠ This context index is 30 days old (built 2026-08-01T10:00:00) — old.</p>"))
      .toBe("⚠ These documents are 30 days old (copied 2026-08-01) — recent changes may not be reflected.");
    expect(staleWarning(`<p>${warning}</p>\n<p>Later text</p>`)).toBe("");          // only luccy's closing line
    expect(staleWarning("<p>Fresh answer.</p>")).toBe("");
    expect(staleWarning(undefined)).toBe("");
  });
});
