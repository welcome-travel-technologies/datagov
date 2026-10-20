import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SourcesOverview } from "./sources-overview";
import type { IntegrationsOverview, OverviewSource } from "@/lib/integrations-overview-api";

vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: 7 } }) }));
const fetchMock = vi.fn<typeof fetch>();
let client: QueryClient;
const soon = (hours: number) => new Date(Date.now() + hours * 3600_000).toISOString();
const ago = (hours: number) => new Date(Date.now() - hours * 3600_000).toISOString();

function source(overrides: Partial<OverviewSource>): OverviewSource {
  return {
    key: "uploads", name: "Uploaded documents", provider: "upload", provider_label: "Uploaded files", kind: "upload",
    connection_id: null, is_active: true, how: { mode: "manual", text: "When someone uploads a file", next_run_at: null }, order: null,
    last_run: { status: "never", at: null }, counts: { documents: 4, visible_documents: 4, parts: 1, catalog_items: null }, needs_attention: false, attention: "",
    fix_in: null, workflow_step: null, can_run_now: false, run_blocked: "", detail: { updates: "Processed each time someone uploads a file.", steps: ["Someone uploads a file", "Extract its text", "Make it searchable"] },
    ...overrides,
  };
}

const overview: IntegrationsOverview = {
  sources: [
    source({ key: "integration:3", name: "Warehouse models", provider: "dbt", provider_label: "dbt / GitHub", kind: "connector", connection_id: 3,
      how: { mode: "scheduled", text: "With the catalog workflow, every day at 02:00 (UTC)", next_run_at: soon(3) }, order: 1,
      last_run: { status: "success", at: ago(20) }, counts: { documents: 0, visible_documents: 0, parts: 0, catalog_items: 1204 }, workflow_step: 2, can_run_now: true,
      detail: { updates: "Runs with the catalog workflow every day at 02:00 (UTC), as step 2 of 4.", steps: ["Fetch the dbt project from GitHub", "Load into the catalog"] } }),
    source({ key: "import:Jira", name: "Jira", provider: "import", provider_label: "Imported export", kind: "import", order: 2,
      how: { mode: "scheduled", text: "Re-reads the export every day at 06:00 (Europe/Athens)", next_run_at: soon(5) },
      last_run: { status: "partial", at: ago(2) }, counts: { documents: 25, visible_documents: 25, parts: 2, catalog_items: null }, needs_attention: true,
      attention: "Some documents could not be processed.", fix_in: "workflow", detail: { updates: "Imported export — refreshed only when a new export is imported.", steps: ["Read the imported export"] } }),
    source({ key: "import:Confluence", name: "Confluence", provider: "import", provider_label: "Imported export", kind: "import",
      how: { mode: "imported", text: "Imported export · not synced live", next_run_at: null }, counts: { documents: 312, visible_documents: 20, parts: 34, catalog_items: null },
      detail: { updates: "Imported export — refreshed only when a new export is imported. It does not sync live with the original system.",
        steps: ["Read the imported export", "Extract the text of new and changed documents", "Make them searchable"] } }),
    source({}),
  ],
  run_order: [
    { at: soon(3), source_key: "integration:3", label: "Warehouse models", kind: "workflow", repeat: "every hour" },
    { at: soon(3), source_key: null, label: "Send to BigQuery", kind: "workflow", repeat: "every hour" },
    { at: soon(5), source_key: "import:Jira", label: "Update Jira (2 parts)", kind: "documents", repeat: null },
  ],
  run_order_truncated: false,
  workflow: {
    how: { mode: "scheduled", text: "Every day at 02:00 (UTC)", next_run_at: soon(3) }, last_run: { status: "success", at: ago(20) },
    steps: [{ order: 1, label: "Check there is enough disk space", source_key: null }, { order: 2, label: "Warehouse models (dbt / GitHub)", source_key: "integration:3" },
      { order: 3, label: "Link the tools together and refresh summaries", source_key: null }, { order: 4, label: "Send to BigQuery", source_key: null }],
    note: "Transformation sources such as dbt run before reporting sources such as Power BI.", needs_attention: false, attention: "",
  },
  documents_enabled: true, timezone: "UTC", generated_at: new Date().toISOString(),
};

function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
function connectorRow(overrides: Partial<OverviewSource>) {
  return source({ key: "integration:5", name: "Company reports", provider: "powerbi_fabric", provider_label: "Power BI / Fabric", kind: "connector",
    connection_id: 5, how: { mode: "manual", text: "Manual only", next_run_at: null }, last_run: { status: "success", at: ago(1) },
    counts: { documents: 0, visible_documents: 0, parts: 0, catalog_items: 3 }, can_run_now: true,
    detail: { updates: "Runs only when you press Run now or run the catalog workflow.", steps: ["Fetch reports"] }, ...overrides });
}
function serve(data: IntegrationsOverview, runResponse?: Response) {
  fetchMock.mockImplementation(async (url) => url === "/api/knowledge/integrations/overview/" ? json(data) : runResponse ?? json({ status: "queued", run_log_id: 9 }));
}
function mount(onNavigate = vi.fn()) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><SourcesOverview onNavigate={onNavigate} /></QueryClientProvider>);
  return onNavigate;
}
beforeEach(() => {
  fetchMock.mockReset();
  serve(overview);
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); });

describe("sources overview", () => {
  it("shows one row per source with how it runs, its last run and its order, in plain words", async () => {
    mount();
    const models = await screen.findByRole("button", { name: /Warehouse models/ });
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(4);
    expect(rows.map((row) => within(row).getByRole("button").textContent)).toEqual([
      expect.stringContaining("Warehouse models"), expect.stringContaining("Jira"), expect.stringContaining("Confluence"), expect.stringContaining("Uploaded documents")]);
    expect(within(rows[0]).getByText("With the catalog workflow, every day at 02:00 (UTC)")).toBeInTheDocument();
    expect(within(rows[0]).getByText("in 3 h")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Success")).toBeInTheDocument();
    expect(within(rows[0]).getByText("#1")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Needs attention")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Partially complete")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Imported export · not synced live")).toBeInTheDocument();
    expect(within(rows[3]).getByText("Never run")).toBeInTheDocument();
    expect(models).toHaveAttribute("aria-expanded", "false");
    // Parts are only counted inside a detail; no storage jargon on the overview.
    expect(document.body.textContent).not.toMatch(/collection|artifact|snapshot|manifest|shard|pipeline profile/i);
  });

  it("explains the whole flow in four plain steps above the sources", async () => {
    mount();
    const flow = await screen.findByRole("list", { name: "How DataGov works" });
    expect(within(flow).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "1. ConnectWarehouse models, Jira, Confluence, Uploaded documents",
      "2. UpdateAutomatically, next in 3 h",
      "3. OrganiseCatalog definitions and searchable documents",
      "4. AskIn Chat, with sources",
    ]);
  });

  it("lists the next day's runs in time order and the catalog workflow order", async () => {
    mount();
    const strip = await screen.findByRole("region", { name: "Run order" });
    const blocks = within(strip).getAllByRole("listitem").filter((item) => item.querySelector("time"));
    expect(blocks.map((block) => block.textContent)).toEqual([
      expect.stringContaining("Catalog workflow: Warehouse models → Send to BigQuery"), expect.stringContaining("Update Jira (2 parts)")]);
    // A schedule that fires all day is listed once, with how often it repeats.
    expect(within(blocks[0]).getByText("Repeats every hour")).toBeInTheDocument();
    expect(within(blocks[1]).queryByText(/Repeats/)).not.toBeInTheDocument();
    const steps = within(screen.getByRole("list", { name: "Catalog workflow order" })).getAllByRole("listitem").map((item) => item.textContent);
    expect(steps).toEqual(["1Check there is enough disk space", "2Warehouse models (dbt / GitHub)", "3Link the tools together and refresh summaries", "4Send to BigQuery"]);
  });

  it("opens the workflow from the run order, where its schedule is fixed", async () => {
    const onNavigate = mount();
    fireEvent.click(await screen.findByRole("button", { name: "Manage workflow" }));
    expect(onNavigate).toHaveBeenLastCalledWith("workflow");
  });

  it("expands an import to what it brings and how it updates, and opens its detailed views", async () => {
    const onNavigate = mount();
    const confluence = await screen.findByRole("button", { name: /Confluence/ });
    fireEvent.click(confluence);
    expect(confluence).toHaveAttribute("aria-expanded", "true");
    const detail = document.getElementById(confluence.getAttribute("aria-controls")!)!;
    // Counts match what "View documents" will open for this admin.
    expect(within(detail).getByText("312 documents in 34 parts (20 visible to you)")).toBeInTheDocument();
    expect(within(detail).getByText(/refreshed only when a new export is imported/)).toBeInTheDocument();
    expect(within(detail).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["Read the imported export", "Extract the text of new and changed documents", "Make them searchable"]);
    expect(within(detail).queryByRole("button", { name: "Run now" })).not.toBeInTheDocument();
    fireEvent.click(within(detail).getByRole("button", { name: "Schedule & runs" }));
    expect(onNavigate).toHaveBeenLastCalledWith("workflow", "import:Confluence");
    fireEvent.click(within(detail).getByRole("button", { name: "Access & settings" }));
    expect(onNavigate).toHaveBeenLastCalledWith("sources", "import:Confluence");
    fireEvent.click(within(detail).getByRole("button", { name: "View documents" }));
    expect(onNavigate).toHaveBeenLastCalledWith("content", "import:Confluence");
    fireEvent.click(confluence);
    expect(confluence).toHaveAttribute("aria-expanded", "false");
  });

  it("runs a connector with the existing run call and manages its schedule and its connection", async () => {
    const onNavigate = mount();
    fireEvent.click(await screen.findByRole("button", { name: /Warehouse models/ }));
    expect(screen.getByText("1,204 catalog items")).toBeInTheDocument();
    expect(screen.getByText("Step 2 of the catalog workflow.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View documents" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/integrations/sources/3/run/", expect.objectContaining({ method: "POST" })));
    expect(await screen.findByText("Run started.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Schedule & runs" }));
    expect(onNavigate).toHaveBeenLastCalledWith("workflow", "integration:3");
    fireEvent.click(screen.getByRole("button", { name: "Connection & access" }));
    expect(onNavigate).toHaveBeenLastCalledWith("sources", "integration:3");
  });

  it("says in the label or next to it why Run now is unavailable", async () => {
    const blocked = "Another active Power BI / Fabric connection blocks runs. Turn one of them off first.";
    serve({ ...overview, sources: [
      connectorRow({ key: "integration:5", name: "Old reports", is_active: false, can_run_now: false, how: { mode: "manual", text: "Turned off", next_run_at: null } }),
      connectorRow({ key: "integration:6", name: "Busy reports", connection_id: 6, last_run: { status: "running", at: ago(0.1) }, can_run_now: false }),
      connectorRow({ key: "integration:7", name: "Twin reports", connection_id: 7, can_run_now: false, run_blocked: blocked, needs_attention: true, attention: blocked, fix_in: "workflow" }),
    ] });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Old reports/ }));
    expect(screen.getByRole("button", { name: "Activate to run" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /Busy reports/ }));
    expect(screen.getByRole("button", { name: "Running…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /Twin reports/ }));
    const run = screen.getByRole("button", { name: "Run now" });
    expect(run).toBeDisabled();
    expect(run).toHaveAccessibleDescription(expect.stringContaining(blocked));
    expect(screen.getByText(blocked)).toBeVisible();
  });

  it("shows why a run could not start", async () => {
    serve({ ...overview, sources: [connectorRow({})] }, json({ error: "A run is already in progress. Please wait for it to complete." }, 400));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Company reports/ }));
    fireEvent.click(screen.getByRole("button", { name: "Run now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A run is already in progress. Please wait for it to complete.");
    expect(screen.queryByText("Run started.")).not.toBeInTheDocument();
  });

  it("links a problem to the view that fixes it", async () => {
    serve({ ...overview, sources: [connectorRow({ needs_attention: true, attention: "Connection details are incomplete.", fix_in: "sources" })] });
    const onNavigate = mount();
    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Company reports/ }));
    fireEvent.click(screen.getByRole("button", { name: "Fix in connection & access" }));
    expect(onNavigate).toHaveBeenLastCalledWith("sources", "integration:5");
  });

  it("hides View documents when none of the documents are open to this admin", async () => {
    serve({ ...overview, sources: [source({ counts: { documents: 4, visible_documents: 0, parts: 2, catalog_items: null } })] });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Uploaded documents/ }));
    expect(screen.getByText("4 documents in 2 parts (0 visible to you)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View documents" })).not.toBeInTheDocument();
  });

  it("explains a load failure with a retry instead of a false admin error", async () => {
    fetchMock.mockImplementation(async () => json({ detail: "Temporarily unavailable." }, 503));
    mount();
    expect(await screen.findByText("Temporarily unavailable.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText(/requires organization admin/)).not.toBeInTheDocument();
  });
});
