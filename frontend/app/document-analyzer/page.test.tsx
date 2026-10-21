import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import DocumentAnalyzerPage from "./page";

const auth = vi.hoisted(() => ({ user: { id: 7, perms: { can_view_document_analyzer: true } as Record<string, boolean> } as { id: number; perms: Record<string, boolean> } | null }));
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: auth.user }) }));

const STATUS = { enabled: true, configured: true, available: true, healthy: true, reason: "", sources: ["confluence", "jira", "drive", "github"], tool_available: true };
const STATUS_URL = "/api/document-analyzer/status/";
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

const fetchMock = vi.fn<typeof fetch>();
let client: QueryClient;
function mount() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><DocumentAnalyzerPage /></QueryClientProvider>);
}
function status(reply: () => Response | Promise<Response>) {
  fetchMock.mockImplementation(async (input) => {
    if (String(input) === STATUS_URL) return reply();
    throw new Error(`Unexpected request ${String(input)}`);
  });
}
const failing = () => { throw new TypeError("Failed to fetch"); };

beforeEach(() => {
  auth.user = { id: 7, perms: { can_view_document_analyzer: true } };
  fetchMock.mockReset();
  status(() => json(STATUS));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); });

describe("Luccy (Beta) page", () => {
  it("shows luccy's own page, full window, without DataGov's header", async () => {
    mount();
    const frame = await screen.findByTitle("Luccy");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame).toHaveAttribute("src", "/api/document-analyzer/luccy/");
    expect(frame).toHaveClass("h-full", "w-full", "border-0");
    expect(screen.queryByRole("heading", { name: "Luccy (Beta)" })).not.toBeInTheDocument();
    // DataGov asks only for the status; the page itself talks to luccy through the gate.
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([STATUS_URL]);
  });

  it("explains why it is unavailable", async () => {
    status(() => json({ ...STATUS, available: false, reason: "The Document Analyzer only serves the Acme organization." }));
    mount();
    expect(await screen.findByText("The Document Analyzer only serves the Acme organization.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Luccy (Beta)" })).toBeInTheDocument();
    expect(screen.getByText("Luccy unavailable")).toBeInTheDocument();
    expect(screen.queryByTitle("Luccy")).not.toBeInTheDocument();
  });

  it("asks the status endpoint for the reason even when the permission is off", async () => {
    auth.user = { id: 7, perms: { can_view_document_analyzer: false } };
    status(() => json({ ...STATUS, enabled: false, available: false, reason: "" }));
    mount();
    expect(await screen.findByText("Luccy isn't available for your account.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(STATUS_URL, expect.anything());
  });

  it("shows the status error with Try again, then opens luccy's page", async () => {
    status(failing);
    mount();
    expect(await screen.findByText("Its status could not be loaded. Please try again in a minute.")).toBeInTheDocument();
    expect(screen.queryByTitle("Luccy")).not.toBeInTheDocument();
    status(() => json(STATUS));
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTitle("Luccy")).toHaveAttribute("src", "/api/document-analyzer/luccy/");
  });

  it("keeps luccy's page when a background status check fails", async () => {
    mount();
    const frame = await screen.findByTitle("Luccy");
    status(failing);
    await act(() => client.refetchQueries({ queryKey: ["document-analyzer", "status"] }));
    expect(client.getQueryState(["document-analyzer", "status", 7])?.status).toBe("error");
    expect(screen.getByTitle("Luccy")).toBe(frame);
    expect(screen.queryByText("Its status could not be loaded. Please try again in a minute.")).not.toBeInTheDocument();
  });

  it("shows loading while the status is checked", async () => {
    let answer!: (response: Response) => void;
    status(() => new Promise<Response>((resolve) => { answer = resolve; }));
    mount();
    expect(screen.getByRole("heading", { name: "Luccy (Beta)" })).toBeInTheDocument();
    expect(screen.getByText("Checking Luccy…")).toBeInTheDocument();
    expect(screen.queryByTitle("Luccy")).not.toBeInTheDocument();
    await act(async () => answer(json(STATUS)));
    expect(await screen.findByTitle("Luccy")).toBeInTheDocument();
  });

  it("waits for the signed-in user before asking anything", () => {
    auth.user = null;
    mount();
    expect(screen.getByRole("heading", { name: "Luccy (Beta)" })).toBeInTheDocument();
    expect(screen.queryByTitle("Luccy")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
