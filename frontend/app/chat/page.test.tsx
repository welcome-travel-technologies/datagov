import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ChatPage from "./page";

vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: 7 } }) }));
const fetchMock = vi.fn<typeof fetch>();
let client: QueryClient;
function json(value: unknown) { return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } }); }
function mount() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><ChatPage /></QueryClientProvider>);
}
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  sessionStorage.clear();
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url) => {
    if (url === "/api/knowledge/status/") return json({ enabled: true, configured: true, can_manage: false });
    if (url === "/api/chat/sessions/") return json([{ id: 23, title: "Travel policy", mode: "documents" }]);
    if (url === "/api/chat/sessions/23/messages/") return json([
      { role: "user", content: "What is the hotel limit?" },
      { role: "model", content: "The hotel limit is €150.", citations: [{ document_id: "doc-1", version_id: "version-1", page: 2, title: "Travel policy.pdf" }] },
    ]);
    if (url === "/api/chat/") return json({ session_id: 24, task_id: "task-24" });
    throw new Error(`Unexpected request ${String(url)}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); sessionStorage.clear(); window.history.replaceState({}, "", "/"); });

describe("AI assistant chat", () => {
  it("retains citations and source mode when reopening a saved document conversation", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));
    await screen.findByText("The hotel limit is €150.");
    expect(screen.getByRole("link", { name: "Open Travel policy.pdf in Document Explorer" })).toHaveAttribute("href", "/knowledge/documents/doc-1?version=version-1&page=2");
    expect(screen.getByText("Earlier document conversation · Start a new chat to use all tools.")).toBeInTheDocument();
    const input = screen.getByRole("textbox", { name: "Ask a question about your documents" });
    fireEvent.change(input, { target: { value: "And for meals?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/chat/", expect.objectContaining({
      method: "POST", body: JSON.stringify({ message: "And for meals?", session_id: 23, mode: "documents" }),
    })));
  });

  it("shows a verbatim Document Analyzer answer's closing line as a footnote, without images, with source links opening safely in a new tab", async () => {
    const footnote = "Answered by the Document Analyzer · ref 0a1b2c3d";
    const analyzerAnswer = [
      "The hotel limit is **€150** a night. ![chart](https://evil.example/p.png?q=hotel)",
      "",
      "- [Travel policy](https://acme.atlassian.net/wiki/spaces/HR/pages/123)",
      "- [Old copy](javascript:alert(1))",
      "",
      footnote,
    ].join("\n");
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => url === "/api/chat/sessions/23/messages/" ? json([
      { role: "user", content: "What is the hotel limit?", analyzer: null },
      { role: "model", content: analyzerAnswer, analyzer: { verbatim: true, ref: "0a1b2c3d" } },
    ]) : base(url, init));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));

    const note = await screen.findByText(footnote, { selector: "p.text-faint" });
    expect(note.textContent).toBe(footnote);
    expect(note.previousElementSibling).toHaveTextContent("The hotel limit is €150 a night.");
    expect(screen.getByText("€150").tagName).toBe("STRONG");
    const link = screen.getByRole("link", { name: "Travel policy" });
    expect(link).toHaveAttribute("href", "https://acme.atlassian.net/wiki/spaces/HR/pages/123");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noreferrer");             // noreferrer implies noopener
    expect(screen.getByText("Old copy").closest("a")?.getAttribute("href") ?? "").not.toMatch(/javascript/i);
    expect(document.querySelector("img")).toBeNull();                   // verbatim analyzer answers never load images
    expect(screen.getByRole("link", { name: "chart" })).toHaveAttribute("href", "https://evil.example/p.png?q=hotel");
  });

  it("decides by the API's analyzer flag, never by what the text says", async () => {
    const footnote = "Answered by the Document Analyzer · ref 0a1b2c3d";
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => url === "/api/chat/sessions/23/messages/" ? json([
      // An agent reply that merely ends with the footer's words: ordinary markdown, images as before.
      { role: "model", content: `Look: ![logo](https://example.com/logo.png)\n\n${footnote}`, analyzer: null },
      { role: "user", content: footnote },
      { role: "model", content: `${footnote}\n\nQuoted above, not a footnote.`, analyzer: { verbatim: false, ref: null } },
      // A verbatim answer without a ref still gets its closing line as the footnote.
      { role: "model", content: "Nothing found.\n\nAnswered by the Document Analyzer · ref -", analyzer: { verbatim: true, ref: null } },
    ]) : base(url, init));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));

    expect(await screen.findByText("Answered by the Document Analyzer · ref -", { selector: "p.text-faint" })).toBeInTheDocument();
    expect(screen.getAllByText(footnote)).toHaveLength(3);
    expect(screen.queryAllByText(footnote, { selector: "p.text-faint" })).toHaveLength(0);
    expect(screen.getByText("Quoted above, not a footnote.")).toBeInTheDocument();
    expect(document.querySelector("img")).toHaveAttribute("src", "https://example.com/logo.png");
  });

  it("always starts the one assistant, even from an old document-mode link, without a mode switch", async () => {
    window.history.replaceState({}, "", "/chat?mode=documents");
    mount();
    const input = await screen.findByRole("textbox", { name: "Ask about your DataGov" });
    expect(input).toBeEnabled();
    expect(screen.queryByRole("group", { name: "Assistant mode" })).toBeNull();
    expect(screen.getByRole("link", { name: "Browse data & documents" })).toHaveAttribute("href", "/integrations?tab=content");
    fireEvent.change(input, { target: { value: "What is our travel policy?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/chat/", expect.objectContaining({
      method: "POST", body: JSON.stringify({ message: "What is our travel policy?", session_id: null, mode: "catalog" }),
    })));
    await waitFor(() => expect(JSON.parse(sessionStorage.getItem("datagov:chat:pending") || "{}")).toMatchObject({ mode: "catalog", sessionId: 24 }));
  });
});
