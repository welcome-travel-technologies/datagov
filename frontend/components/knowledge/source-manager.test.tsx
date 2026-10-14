import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SourceManager } from "./source-manager";
import { RunList } from "./run-list";
import { UploadDialog } from "./upload-dialog";
import type { KnowledgeRun, KnowledgeSource } from "@/lib/knowledge-api";

vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: 7 } }) }));
const fetchMock = vi.fn<typeof fetch>();
let client: QueryClient;
const source: KnowledgeSource = { id: "source-1", name: "Travel", kind: "upload", is_active: true, cron: "", timezone: "Europe/Athens", last_run_at: null,
  config: {}, access: "restricted", allowed_user_ids: [7], allowed_group_ids: [] };
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
function mount(children: React.ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}
beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url) => {
    if (url === "/api/org/members/") return json({ members: [{ user_id: 7, display_name: "Alex", email: "alex@example.test" }], available_groups: [] });
    if (url === "/api/knowledge/connections/") return json({ results: [] });
    if (url === "/api/knowledge/groups/") return json({ results: [{ id: "uploads", name: "Uploaded documents", document_count: 0, collections: [source] }] });
    return json(source);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); });

describe("document source controls", () => {
  it("creates a schedule with explicit reader access, defaulting to the current user", async () => {
    mount(<SourceManager sources={[]} configured />);
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    expect(await screen.findByRole("checkbox", { name: /Alex/ })).toBeChecked();
    fireEvent.change(screen.getByLabelText("Source name"), { target: { value: "Travel policy" } });
    fireEvent.change(screen.getByLabelText("Cron expression"), { target: { value: "0 6 * * *" } });
    fireEvent.change(screen.getByLabelText("Time zone"), { target: { value: "Europe/Athens" } });
    fireEvent.click(screen.getByRole("button", { name: "Save source" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/knowledge/sources/", expect.objectContaining({ method: "POST" })));
    const write = fetchMock.mock.calls.find(([url, options]) => url === "/api/knowledge/sources/" && options?.method === "POST")!;
    expect(JSON.parse(String(write[1]?.body))).toMatchObject({ name: "Travel policy", cron: "0 6 * * *", timezone: "Europe/Athens", access: "restricted", allowed_user_ids: [7], kind: "upload" });
  });

  it("surfaces rejected source refreshes without claiming a run was queued", async () => {
    fetchMock.mockImplementation(async () => json({ detail: "This source already has an active run." }, 409));
    mount(<SourceManager sources={[source]} configured />);
    fireEvent.click(screen.getByText("Collections"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already has an active run");
    expect(screen.queryByText(/Refresh queued/)).not.toBeInTheDocument();
  });

  it("offers a retry for partially failed runs and sends the run identifier", async () => {
    const run: KnowledgeRun = { id: "run-1", source_id: source.id, source_name: source.name, status: "partial", stage: "complete", progress: 100,
      counters: { ready: 2, failed: 1 }, error: "One document failed.", created_at: "2026-10-13T09:00:00Z", started_at: null, finished_at: null, cancel_requested: false };
    mount(<RunList runs={[run]} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/knowledge/runs/run-1/retry/", expect.objectContaining({ method: "POST" })));
  });

  it("reports a duplicate upload as unchanged instead of a new processing run", async () => {
    const uploaded = vi.fn();
    fetchMock.mockResolvedValue(json({ unchanged: true, document: { id: "doc-1" } }));
    mount(<UploadDialog open onOpenChange={vi.fn()} sources={[source]} onUploaded={uploaded} />);
    fireEvent.change(screen.getByLabelText("Document file"), { target: { files: [new File(["%PDF-1.7"], "policy.pdf", { type: "application/pdf" })] } });
    fireEvent.click(screen.getByRole("button", { name: "Upload and process" }));
    await waitFor(() => expect(uploaded).toHaveBeenCalledWith(true));
  });

  it("uploads a replacement to the existing document and source", async () => {
    const uploaded = vi.fn();
    mount(<UploadDialog open onOpenChange={vi.fn()} sources={[]} onUploaded={uploaded}
      replacement={{ documentId: "doc-1", sourceId: "source-1", title: "policy.pdf" }} />);
    expect(screen.getByText(/published version stays available/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Document file"), { target: { files: [new File(["%PDF-1.7 new"], "policy-v2.pdf", { type: "application/pdf" })] } });
    fireEvent.click(screen.getByRole("button", { name: "Upload and process" }));
    await waitFor(() => expect(uploaded).toHaveBeenCalled());
    const body = fetchMock.mock.calls[0][1]?.body as FormData;
    expect(body.get("source_id")).toBe("source-1");
    expect(body.get("document_id")).toBe("doc-1");
  });
});
