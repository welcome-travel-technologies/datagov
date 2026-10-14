import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DocumentViewer } from "./document-viewer";
import { DocumentCitations } from "./citations";
import { EvidenceWorkspace } from "./evidence-workspace";
import type { DocumentDetail } from "@/lib/knowledge-api";

const auth = vi.hoisted(() => ({ user: { id: 7 } }));
const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => navigation }));
vi.mock("next/dynamic", () => ({ default: () => (props: { url: string; page: number }) => <div data-testid="pdf-viewer" data-url={props.url} data-page={props.page} /> }));
vi.mock("@/lib/auth", () => ({ useAuth: () => auth }));
const fetchMock = vi.fn<typeof fetch>();
let client: QueryClient;
const doc: DocumentDetail = {
  id: "doc-1", title: "Travel policy.pdf", mime_type: "application/pdf", source_type: "upload", source_name: "People",
  status: "ready", current_version_id: "version-new", version_id: "version-old", created_at: "2026-09-01T08:00:00Z", updated_at: "2026-10-01T08:00:00Z",
  pages: [{ page: 1, text: "Introduction" }, { page: 2, text: "Hotels are reimbursed up to €150 per night." }],
  chunks: [{ id: "chunk-2", page: 2, text: "Hotels are reimbursed up to €150 per night." }],
  content_url: "/api/knowledge/documents/doc-1/content/?version=version-old",
  versions: [{ id: "version-new", status: "ready", created_at: "2026-10-01T08:00:00Z" }, { id: "version-old", status: "ready", created_at: "2026-09-01T08:00:00Z" }],
  datagov_url: "https://untrusted.example/document",
  source_url: "https://company.example/policies/travel",
};
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
function mount(children: React.ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}
beforeEach(() => {
  auth.user = { id: 7 };
  navigation.push.mockReset();
  vi.stubGlobal("innerWidth", 1600);
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url) => json(url === "/api/knowledge/status/" ? { enabled: true, configured: true, can_manage: false } : doc));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); });

describe("authorized document reader", () => {
  it("opens the cited version and PDF page, with readable extracted text", async () => {
    mount(<DocumentViewer documentId="doc-1" versionId="version-old" initialPage={2} chunkId="chunk-2" compact />);
    await screen.findByRole("heading", { name: "Travel policy.pdf" });
    expect(fetchMock).toHaveBeenCalledWith("/api/knowledge/documents/doc-1/?version=version-old", expect.objectContaining({ credentials: "include" }));
    expect(screen.getByTestId("pdf-viewer")).toHaveAttribute("data-url", "/api/knowledge/documents/doc-1/content/?version=version-old");
    expect(screen.getByTestId("pdf-viewer")).toHaveAttribute("data-page", "2");
    expect(screen.getByText("Viewing a previous version")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open full page" })).toHaveAttribute("href", "/knowledge/documents/doc-1?version=version-old&page=2#chunk-chunk-2");
    expect(screen.getByRole("link", { name: "Open original source" })).toHaveAttribute("href", "https://company.example/policies/travel");
    expect(screen.getByRole("link", { name: "Open original source" })).toHaveAttribute("rel", "noopener noreferrer");
    fireEvent.click(screen.getByRole("button", { name: "Extracted text" }));
    expect(screen.getAllByText("Hotels are reimbursed up to €150 per night.")).toHaveLength(2);
    expect(document.querySelector("mark")).toHaveTextContent("Hotels are reimbursed up to €150 per night.");
    expect(screen.queryByTitle("Travel policy.pdf — page 2")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(screen.getByText("Introduction")).toBeInTheDocument();
  });

  it("requests a selected version and does not quietly replace an unavailable one", async () => {
    mount(<DocumentViewer documentId="doc-1" versionId="version-old" />);
    await screen.findByRole("combobox", { name: "Document version" });
    fetchMock.mockResolvedValue(json({ detail: "Not found." }, 404));
    fireEvent.change(screen.getByRole("combobox", { name: "Document version" }), { target: { value: "version-new" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("document or version is unavailable");
    expect(fetchMock).toHaveBeenCalledWith("/api/knowledge/documents/doc-1/?version=version-new", expect.anything());
    expect(screen.queryByRole("heading", { name: "Travel policy.pdf" })).not.toBeInTheDocument();
  });

  it("shows a useful state for a scanned document without extracted text", async () => {
    fetchMock.mockResolvedValue(json({ ...doc, status: "needs_ocr", pages: [], chunks: [] }));
    mount(<DocumentViewer documentId="doc-1" />);
    await screen.findByRole("heading", { name: "Travel policy.pdf" });
    expect(screen.getByRole("status")).toHaveTextContent("needs OCR");
    fireEvent.click(screen.getByRole("button", { name: "Extracted text" }));
    expect(screen.getByText("No extracted text for this page")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open PDF" })).toBeInTheDocument();
  });

  it("does not reuse another user's cached document", async () => {
    const view = mount(<DocumentViewer documentId="doc-1" versionId="version-old" />);
    await screen.findByRole("heading", { name: "Travel policy.pdf" });
    auth.user = { id: 8 };
    fetchMock.mockResolvedValue(json({ detail: "Access denied." }, 403));
    view.rerender(<QueryClientProvider client={client}><DocumentViewer documentId="doc-1" versionId="version-old" /></QueryClientProvider>);
    expect(screen.queryByRole("heading", { name: "Travel policy.pdf" })).not.toBeInTheDocument();
    await screen.findByRole("alert");
    expect(screen.queryByTitle("Travel policy.pdf — page 1")).not.toBeInTheDocument();
  });
});

describe("answer citations", () => {
  it("opens the protected reader beside a usable composer and supports resizing", async () => {
    mount(<EvidenceWorkspace><div><input aria-label="Chat composer" /><DocumentCitations citations={[{ document_id: "doc-1", version_id: "version-old", chunk_id: "chunk-2", page: 2,
      title: "Travel policy.pdf", datagov_url: "https://untrusted.example", excerpt: "Hotel reimbursement" }]} /></div></EvidenceWorkspace>);
    expect(screen.getByRole("link", { name: "Open Travel policy.pdf in Document Explorer" })).toHaveAttribute("href", "/knowledge/documents/doc-1?version=version-old&page=2#chunk-chunk-2");
    fireEvent.click(screen.getByRole("button", { name: "Preview source 1: Travel policy.pdf" }));
    await waitFor(() => expect(screen.getByRole("complementary", { name: "Evidence reader" })).toHaveTextContent("Cited passage"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Chat composer"), { target: { value: "A follow-up question" } });
    expect(screen.getByLabelText("Chat composer")).toHaveValue("A follow-up question");
    fireEvent.keyDown(screen.getByRole("separator"), { key: "ArrowLeft" });
    expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "51");
    fireEvent.click(screen.getByRole("button", { name: "Close evidence reader" }));
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  });

  it("opens canonical evidence links as a full page on narrow screens", () => {
    vi.stubGlobal("innerWidth", 390);
    mount(<EvidenceWorkspace><DocumentCitations citations={[{ document_id: "doc-1", version_id: "version-old", chunk_id: "chunk-2", page: 2, title: "Travel policy.pdf" }]} /></EvidenceWorkspace>);
    fireEvent.click(screen.getByRole("button", { name: "Preview source 1: Travel policy.pdf" }));
    expect(navigation.push).toHaveBeenCalledWith("/knowledge/documents/doc-1?version=version-old&page=2#chunk-chunk-2");
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  });
});
