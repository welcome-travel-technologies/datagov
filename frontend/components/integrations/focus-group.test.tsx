import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { WorkflowSources } from "./workflow-sources";
import { SourceManager } from "@/components/knowledge/source-manager";
import type { IntegrationSource } from "@/lib/api";

vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: 7 } }) }));
// The cards have their own tests; here they only need to be there.
vi.mock("./source-card", () => ({ SourceCard: ({ source }: { source: { name: string } }) => <p>Connector {source.name}</p> }));
vi.mock("./destination-card", () => ({ DestinationCard: () => <p>Destination</p> }));
vi.mock("@/components/knowledge/run-list", () => ({ RunList: () => <p>Runs</p> }));

const connection = { id: 12, name: "dbt core", source_type: "dbt", is_active: true, schedule: null, last_run: null } as unknown as IntegrationSource;
const GROUPS = [
  { id: "import:Jira", name: "Jira", provider: "jira", document_count: 5, collections: [] },
  { id: "uploads", name: "Uploaded documents", provider: "upload", document_count: 2, collections: [] },
];
const fetchMock = vi.fn<typeof fetch>();
const scrolled = vi.fn();
let client: QueryClient;
function json(value: unknown) { return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } }); }
function mount(children: React.ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}
const group = (key: string) => document.querySelector<HTMLElement>(`[data-group-key="${key}"]`);
const detailsOf = (key: string) => {
  const element = group(key)!;
  return element instanceof HTMLDetailsElement ? element : element.querySelector("details")!;
};

beforeEach(() => {
  scrolled.mockReset();
  Element.prototype.scrollIntoView = scrolled;
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url) => {
    if (url === "/api/integrations/") return json({ sources: [connection], destinations: [], hooks: [] });
    if (url === "/api/knowledge/status/") return json({ enabled: true, configured: true, can_manage: true });
    if (url === "/api/knowledge/groups/") return json({ results: GROUPS });
    return json({ results: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); });

describe("opening the source group an overview link points at", () => {
  it("Workflow: opens that group's flow and scrolls to it once, leaving the others closed", async () => {
    mount(<WorkflowSources focusGroup="import:Jira" />);
    await waitFor(() => expect(detailsOf("import:Jira").open).toBe(true));
    expect(detailsOf("integration:12").open).toBe(false);
    expect(detailsOf("uploads").open).toBe(false);
    expect(scrolled).toHaveBeenCalledTimes(1);
    expect(scrolled.mock.contexts[0]).toBe(group("import:Jira"));
    expect(group("import:Jira")).toHaveClass("ring-1");
    expect(group("uploads")).not.toHaveClass("ring-1");

    // The reader closes it again; a refetch neither reopens it nor scrolls again.
    detailsOf("import:Jira").open = false;
    await act(() => client.refetchQueries());
    expect(detailsOf("import:Jira").open).toBe(false);
    expect(scrolled).toHaveBeenCalledTimes(1);
  });

  it("Workflow: waits for a group that is known before the list is on screen", async () => {
    // The groups arrive while the integrations list is still loading.
    let release!: () => void;
    const integrations = new Promise<Response>((resolve) => { release = () => resolve(json({ sources: [connection], destinations: [], hooks: [] })); });
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => (url === "/api/integrations/" ? integrations : base(url, init)));
    mount(<WorkflowSources focusGroup="uploads" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/knowledge/groups/", expect.anything()));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(group("uploads")).toBeNull();
    release();
    await waitFor(() => expect(detailsOf("uploads").open).toBe(true));
    expect(scrolled).toHaveBeenCalledTimes(1);
  });

  it("Workflow: without a focus group nothing opens or scrolls", async () => {
    mount(<WorkflowSources />);
    await waitFor(() => expect(group("import:Jira")).not.toBeNull());
    await waitFor(() => expect(group("integration:12")).not.toBeNull());
    expect(detailsOf("import:Jira").open).toBe(false);
    expect(scrolled).not.toHaveBeenCalled();
  });

  it("Sources: opens that group's access settings and scrolls to it once", async () => {
    mount(<SourceManager sources={[]} configured connections={[connection]} focusGroup="integration:12" />);
    await waitFor(() => expect(detailsOf("integration:12").open).toBe(true));
    expect(group("integration:12")?.tagName).toBe("SECTION");
    await waitFor(() => expect(group("import:Jira")).not.toBeNull());
    expect(detailsOf("import:Jira").open).toBe(false);
    expect(scrolled).toHaveBeenCalledTimes(1);
    expect(scrolled.mock.contexts[0]).toBe(group("integration:12"));
    await act(() => client.refetchQueries());
    expect(scrolled).toHaveBeenCalledTimes(1);
  });

  it("Sources: an unknown group changes nothing", async () => {
    mount(<SourceManager sources={[]} configured connections={[connection]} focusGroup="import:Gone" />);
    await waitFor(() => expect(group("import:Jira")).not.toBeNull());
    expect(document.querySelectorAll("details[open]")).toHaveLength(0);
    expect(scrolled).not.toHaveBeenCalled();
  });
});
