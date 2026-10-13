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

describe("Document Explorer chat", () => {
  it("retains citations and source mode when reopening a saved conversation", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Travel policy" }));
    await screen.findByText("The hotel limit is €150.");
    expect(screen.getByRole("link", { name: "Open Travel policy.pdf in Document Explorer" })).toHaveAttribute("href", "/knowledge/documents/doc-1?version=version-1&page=2");
    expect(screen.getByRole("button", { name: "Documents" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Catalog" })).toBeDisabled();
  });

  it("starts a document-mode conversation from the library link and preserves that mode while pending", async () => {
    window.history.replaceState({}, "", "/chat?mode=documents");
    mount();
    const input = await screen.findByRole("textbox", { name: "Ask a question about your documents" });
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { value: "What is our travel policy?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/chat/", expect.objectContaining({
      method: "POST", body: JSON.stringify({ message: "What is our travel policy?", session_id: null, mode: "documents" }),
    })));
    await waitFor(() => expect(JSON.parse(sessionStorage.getItem("datagov:chat:pending") || "{}")).toMatchObject({ mode: "documents", sessionId: 24 }));
  });
});
