import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { IntegrationsManager } from "@/components/integrations/integrations-manager";
import { resolveTab } from "./tabs";
import type { OverviewNavigate } from "./sources-overview";

vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: 7 } }) }));
// The detailed views are covered by their own tests; here they only show which one is open.
// The group an overview link asked for shows after the view's name.
vi.mock("@/components/integrations/workflow-panel", () => ({ WorkflowPanel: ({ focusGroup }: { focusGroup?: string }) => <p>Workflow view{focusGroup ? ` ${focusGroup}` : ""}</p> }));
vi.mock("@/components/knowledge/source-manager", () => ({ SourceManager: ({ focusGroup }: { focusGroup?: string }) => <p>Sources view{focusGroup ? ` ${focusGroup}` : ""}</p> }));
vi.mock("@/components/integrations/destination-card", () => ({ DestinationCard: () => <p>Destinations view</p> }));
vi.mock("@/components/integrations/notifications-panel", () => ({ AlertsTab: () => <p>Alerts view</p>, BotTab: () => <p>Bot view</p> }));
vi.mock("@/components/integrations/integration-content", () => ({ IntegrationContent: ({ initialGroup }: { initialGroup?: string }) => <p>Documents view {initialGroup}</p> }));
const overviewMounted = vi.hoisted(() => vi.fn());
vi.mock("./sources-overview", () => ({
  SourcesOverview: ({ onNavigate }: { onNavigate: OverviewNavigate }) => {
    overviewMounted();
    return <button onClick={() => onNavigate("sources", "import:Jira")}>Overview view</button>;
  },
}));

const fetchMock = vi.fn<typeof fetch>();
let client: QueryClient;
const integrations = { sources: [], destinations: [{ id: 1, name: "BigQuery", destination_type: "bigquery", is_active: true, schedule: null, last_run: null }], hooks: [] };
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
function mount(path: string) {
  window.history.replaceState(null, "", path);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><IntegrationsManager /></QueryClientProvider>);
}
const selected = (name: string) => screen.getByRole("tab", { name: new RegExp(`^${name}`) });
beforeEach(() => {
  overviewMounted.mockReset();
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url) => {
    if (url === "/api/integrations/") return json(integrations);
    if (url === "/api/knowledge/status/") return json({ enabled: true, configured: true, can_manage: true });
    return json({ results: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

describe("integrations tabs", () => {
  it("maps old and new ?tab values to the visible tab", () => {
    expect(resolveTab(null)).toEqual({ tab: "overview", view: null });
    expect(resolveTab("unknown")).toEqual({ tab: "overview", view: null });
    expect(resolveTab("content")).toEqual({ tab: "content", view: null });
    expect(resolveTab("advanced")).toEqual({ tab: "advanced", view: "workflow" });
    for (const legacy of ["workflow", "sources", "destinations", "alerts", "bot"]) {
      expect(resolveTab(legacy)).toEqual({ tab: "advanced", view: legacy });
    }
  });

  it("opens on the overview without waiting for the detailed settings, even when they fail", async () => {
    fetchMock.mockImplementation(async () => json({ error: "Not an organization admin" }, 403));
    mount("/integrations");
    expect(await screen.findByText("Overview view")).toBeInTheDocument();
    expect(selected("Overview")).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText(/requires organization admin/)).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith("/api/integrations/", expect.anything());
  });

  it("lands old bookmarks on the matching Advanced view", async () => {
    mount("/integrations?tab=sources&group=import:Jira");
    expect(await screen.findByText("Sources view import:Jira")).toBeInTheDocument();
    expect(selected("Advanced")).toHaveAttribute("aria-selected", "true");
    expect(selected("Sources")).toHaveAttribute("aria-selected", "true");
    // The overview is never mounted (and never loads) on the way to an old bookmark.
    expect(overviewMounted).not.toHaveBeenCalled();
    cleanup();
    mount("/integrations?tab=bot");
    expect(await screen.findByText("Bot view")).toBeInTheDocument();
    cleanup();
    mount("/integrations?tab=content&group=import:Jira");
    expect(await screen.findByText("Documents view import:Jira")).toBeInTheDocument();
    expect(selected("Data & documents")).toHaveAttribute("aria-selected", "true");
  });

  it("hands ?group= to the Workflow and Sources views, and drops it when another view is picked", async () => {
    mount("/integrations?tab=workflow&group=integration:12");
    expect(await screen.findByText("Workflow view integration:12")).toBeInTheDocument();
    fireEvent.mouseDown(selected("Sources"));
    expect(await screen.findByText("Sources view")).toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get("group")).toBeNull();
    fireEvent.mouseDown(selected("Workflow"));
    expect(await screen.findByText("Workflow view")).toBeInTheDocument();
  });

  it("keeps an admin error inside Advanced", async () => {
    fetchMock.mockImplementation(async (url) => url === "/api/integrations/" ? json({ error: "Not an organization admin" }, 403) : json({ results: [] }));
    mount("/integrations?tab=workflow");
    expect(await screen.findByText("This area requires organization admin access.")).toBeInTheDocument();
    fireEvent.mouseDown(selected("Overview"));
    expect(await screen.findByText("Overview view")).toBeInTheDocument();
    expect(screen.queryByText("This area requires organization admin access.")).not.toBeInTheDocument();
  });

  it("follows the browser's Back and Forward buttons", async () => {
    mount("/integrations");
    fireEvent.click(await screen.findByText("Overview view"));
    expect(await screen.findByText("Sources view import:Jira")).toBeInTheDocument();
    act(() => {
      window.history.replaceState(null, "", "/integrations");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByText("Overview view")).toBeInTheDocument();
    act(() => {
      window.history.replaceState(null, "", "/integrations?tab=bot");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByText("Bot view")).toBeInTheDocument();
    expect(selected("Advanced")).toHaveAttribute("aria-selected", "true");
  });

  it("follows Manage from the overview and remembers the Advanced view in the address", async () => {
    mount("/integrations");
    fireEvent.click(await screen.findByText("Overview view"));
    expect(await screen.findByText("Sources view import:Jira")).toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("sources");
    expect(new URLSearchParams(window.location.search).get("group")).toBe("import:Jira");
    fireEvent.mouseDown(selected("Destinations"));
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("tab")).toBe("destinations"));
    fireEvent.mouseDown(selected("Overview"));
    await screen.findByText("Overview view");
    fireEvent.mouseDown(selected("Advanced"));
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("tab")).toBe("destinations"));
  });
});
