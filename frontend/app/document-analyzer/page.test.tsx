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
  body_html: '<h1>Travel policy</h1><h2>Hotels</h2><p>Hotels up to <strong>\u20ac150</strong> a night.</p><h2>Meals</h2><p>Meals up to \u20ac40.</p><script>window.parent.hacked = true</script>',
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
const objectUrlBlobs = new Map<string, Blob>();
const createObjectURL = vi.fn((blob: Blob) => {
  const url = `blob:http://localhost/source-document-${createObjectURL.mock.calls.length}`;
  objectUrlBlobs.set(url, blob);
  return url;
});
const revokeObjectURL = vi.fn((url: string) => { objectUrlBlobs.delete(url); });
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

async function frameDocument(panel: HTMLElement) {
  const frame = await within(panel).findByTitle("Travel policy: page text");
  const url = frame.getAttribute("src")!.split("#")[0];
  const blob = objectUrlBlobs.get(url);
  expect(blob).toBeInstanceOf(Blob);
  const html = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob!);
  });
  const doc = new DOMParser().parseFromString(html, "text/html");
  fireEvent.load(frame);
  if (doc.getElementById("active-evidence")) {
    await waitFor(() => expect(frame).toHaveAttribute("src", `${url}#active-evidence`));
  } else {
    expect(frame).toHaveAttribute("src", url);
  }
  return { frame, url, html, doc };
}

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
  createObjectURL.mockClear();
  revokeObjectURL.mockClear();
  objectUrlBlobs.clear();
  routes();
  vi.stubGlobal("fetch", fetchMock);
  const BrowserURL = URL;
  vi.stubGlobal("URL", class extends BrowserURL {
    static createObjectURL = createObjectURL;
    static revokeObjectURL = revokeObjectURL;
  });
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
    expect(await screen.findByRole("heading", { name: "Luccy (Beta)" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "AI Assistant" })).not.toBeInTheDocument();
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

  it("opens cited documents beside chat with highlighted evidence in a sandboxed frame", async () => {
    mount();
    const stream = await askAndAnswer("What is the hotel limit?");
    stream.send(event("stage", 0, { stage: "starting", job: "job-1" }) + event("done", 1, ANSWER));
    stream.close();

    const link = await screen.findByRole("link", { name: "the travel policy" });
    expect(fireEvent.click(link, { ctrlKey: true })).toBe(true);   // Ctrl-click keeps the browser default
    expect(screen.queryByRole("complementary", { name: "Source document" })).not.toBeInTheDocument();
    expect(fireEvent.click(link)).toBe(false);                      // a plain click opens the panel instead

    const panel = await screen.findByRole("complementary", { name: "Source document" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(panel).not.toHaveAttribute("aria-modal");
    expect(panel).toHaveFocus();
    expect(await within(panel).findByText("HR › Policies")).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: "Travel policy" })).toBeInTheDocument();
    expect(within(panel).getByText("modified 2026-09-29")).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: /Open source/ })).toHaveAttribute("href", PAGE_URL);
    expect(within(panel).getByText("Passage 1 of 2")).toBeInTheDocument();
    const unloadedFrame = await within(panel).findByTitle("Travel policy: page text");
    expect(unloadedFrame.getAttribute("src")).not.toContain("#active-evidence");
    const first = await frameDocument(panel);
    expect(first.frame).toHaveAttribute("sandbox", "");
    expect(first.frame).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(first.frame).not.toHaveAttribute("srcdoc");
    expect(first.frame.getAttribute("src")).toBe(`${first.url}#active-evidence`);
    expect(first.doc.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute("content")).toBe("default-src 'none'; style-src 'unsafe-inline'; img-src data:");
    expect(first.doc.querySelector("script")).toBeNull();
    expect(first.doc.querySelector("strong mark.analyzer-evidence-active")?.textContent).toBe("\u20ac150");
    expect(Array.from(first.doc.querySelectorAll('mark[data-passage-index="0"]'), (mark) => mark.textContent).join("")).toBe("Hotels up to \u20ac150 a night.");
    expect(first.doc.querySelector('mark[data-passage-index="1"]')?.textContent).toBe("Meals up to \u20ac40.");
    expect(within(panel).queryByText(/could not be located/)).not.toBeInTheDocument();

    const scheduled = vi.spyOn(globalThis, "requestAnimationFrame");
    try {
      fireEvent.load(first.frame);
      fireEvent.load(first.frame);
      expect(scheduled).not.toHaveBeenCalled();
      expect(first.frame).toHaveAttribute("src", `${first.url}#active-evidence`);
      expect(createObjectURL).toHaveBeenCalledTimes(1);
      expect(revokeObjectURL).not.toHaveBeenCalled();
    } finally { scheduled.mockRestore(); }

    fireEvent.click(within(panel).getByRole("button", { name: "Next passage" }));
    expect(within(panel).getByText("Passage 2 of 2")).toBeInTheDocument();
    const second = await frameDocument(panel);
    expect(second.doc.getElementById("active-evidence")?.textContent).toBe("Meals up to \u20ac40.");
    expect(revokeObjectURL).toHaveBeenCalledWith(first.url);
    // luccy's n/j p/k keys work wherever focus is in the panel, Close included.
    const close = within(panel).getByRole("button", { name: "Close the document panel" });
    fireEvent.keyDown(close, { key: "n" });
    expect(within(panel).getByText("Passage 1 of 2")).toBeInTheDocument();
    fireEvent.keyDown(close, { key: "k" });
    expect(within(panel).getByText("Passage 2 of 2")).toBeInTheDocument();
    fireEvent.keyDown(close, { key: "n", ctrlKey: true });
    expect(within(panel).getByText("Passage 2 of 2")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/document-analyzer/documents/123/?answer=0a1b2c3d", expect.anything());

    fireEvent.click(close);
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "Source document" })).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Ask about company docs, metrics, decisions" })).toHaveFocus();
    expect(objectUrlBlobs.size).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Travel policy" }));
    expect(await screen.findByRole("complementary", { name: "Source document" })).toBeInTheDocument();
  });

  it("shows a retry when a document cannot be opened", async () => {
    routes({ "/api/document-analyzer/documents/123/?answer=0a1b2c3d": () => json({ detail: "Not found." }, 404) });
    mount();
    const stream = await askAndAnswer("What is the hotel limit?");
    stream.send(event("done", 0, ANSWER));
    stream.close();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));
    const panel = await screen.findByRole("complementary", { name: "Source document" });
    expect(await within(panel).findByText("This document isn't available any more. It may have been removed or not added yet.")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("keeps the document open and its frame stable while the reader asks a follow-up", async () => {
    mount();
    const stream = await askAndAnswer("What is the hotel limit?");
    stream.send(event("done", 0, ANSWER));
    stream.close();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));
    const panel = await screen.findByRole("complementary", { name: "Source document" });
    const initial = await frameDocument(panel);
    const created = createObjectURL.mock.calls.length;
    const revoked = revokeObjectURL.mock.calls.length;
    const box = screen.getByRole("textbox", { name: "Ask about company docs, metrics, decisions" });
    expect(box).not.toBeDisabled();

    act(() => box.focus());
    fireEvent.click(box);
    fireEvent.change(box, { target: { value: "And meals?" } });
    fireEvent.keyDown(box, { key: "n" });
    fireEvent.keyDown(box, { key: "p" });
    fireEvent.keyDown(box, { key: "Escape" });
    expect(within(panel).getByText("Passage 1 of 2")).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Source document" })).toBe(panel);
    expect(box).toHaveFocus();
    expect(createObjectURL).toHaveBeenCalledTimes(created);
    expect(revokeObjectURL).toHaveBeenCalledTimes(revoked);
    expect(within(panel).getByTitle("Travel policy: page text")).toBe(initial.frame);
    expect(initial.frame).toHaveAttribute("src", `${initial.url}#active-evidence`);

    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(streams).toHaveLength(2));
    expect(askBody(1).question).toBe("And meals?");
    expect(screen.getByRole("complementary", { name: "Source document" })).toBe(panel);
    expect(createObjectURL).toHaveBeenCalledTimes(created);
    expect(revokeObjectURL).toHaveBeenCalledTimes(revoked);
    streams[1].send(event("done", 0, { ...ANSWER, answer: "Meals have their own limit.", citations: [], log: "1a2b3c4d" }));
    streams[1].close();
    expect(await screen.findByText("Meals have their own limit.")).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Source document" })).toBe(panel);
    expect(createObjectURL).toHaveBeenCalledTimes(created);

    fireEvent.keyDown(within(panel).getByRole("button", { name: "Close the document panel" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "Source document" })).not.toBeInTheDocument());
    await waitFor(() => expect(box).toHaveFocus());
    expect(revokeObjectURL).toHaveBeenCalledWith(initial.url);
    expect(objectUrlBlobs.size).toBe(0);
  });

  it("revokes the document's Blob URL when leaving the app", async () => {
    const view = mount();
    const stream = await askAndAnswer("What is the hotel limit?");
    stream.send(event("done", 0, ANSWER));
    stream.close();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));
    const panel = await screen.findByRole("complementary", { name: "Source document" });
    const { url } = await frameDocument(panel);
    expect(objectUrlBlobs.has(url)).toBe(true);
    view.unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith(url);
    expect(objectUrlBlobs.size).toBe(0);
  });

  it("keeps an unmatched cited excerpt visible without marking unrelated document text", async () => {
    routes({ "/api/document-analyzer/documents/123/?answer=0a1b2c3d": () => json({ ...DOCUMENT, passages: [{ heading: "Hotels", text: "An older hotel policy passage." }] }) });
    mount();
    const stream = await askAndAnswer("What is the hotel limit?");
    stream.send(event("done", 0, ANSWER));
    stream.close();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));
    const panel = await screen.findByRole("complementary", { name: "Source document" });
    expect(await within(panel).findByText("An older hotel policy passage.")).toBeInTheDocument();
    expect(within(panel).getByText(/could not be located in the document/)).toBeInTheDocument();
    const { frame, doc } = await frameDocument(panel);
    expect(frame.getAttribute("src")).not.toContain("#active-evidence");
    expect(doc.querySelector("mark")).toBeNull();
    expect(doc.body.textContent).toContain("Hotels up to \u20ac150 a night.");
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

  it.each([false, true])("ignores old comparison links and never fetches results (admin=%s)", async (isAdmin) => {
    auth.user = { id: 7, perms: { can_view_document_analyzer: true, is_admin: isAdmin } };
    window.history.replaceState(null, "", "/document-analyzer?view=compare");
    mount();
    expect(await screen.findByRole("textbox", { name: "Ask about company docs, metrics, decisions" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Page view" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Compare with Luccy" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Luccy (Beta)" })).toBeInTheDocument();
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
