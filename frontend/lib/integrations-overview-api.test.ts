import { afterEach, describe, expect, it, vi } from "vitest";
import { describeCounts, fixLink, integrationsOverviewApi, manageLinks, relativeTime } from "./integrations-overview-api";

afterEach(() => { vi.unstubAllGlobals(); });

describe("integrations overview API", () => {
  it("reads the overview with session credentials", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ sources: [] }), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(integrationsOverviewApi.get()).resolves.toEqual({ sources: [] });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/knowledge/integrations/overview/");
    expect(options?.method).toBe("GET");
    expect(options?.credentials).toBe("include");
  });

  it("surfaces the server's reason when the overview is refused", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ detail: "Integrations require organization admin access." }), { status: 403 })));
    await expect(integrationsOverviewApi.get()).rejects.toMatchObject({ status: 403, message: "Integrations require organization admin access." });
  });

  it("manages a source's schedule and runs in Workflow and its connection or access in Sources", () => {
    expect(manageLinks({ key: "integration:3", kind: "connector" })).toEqual([
      { label: "Schedule & runs", tab: "workflow", group: "integration:3" }, { label: "Connection & access", tab: "sources", group: "integration:3" }]);
    expect(manageLinks({ key: "import:Confluence", kind: "import" })).toEqual([
      { label: "Schedule & runs", tab: "workflow", group: "import:Confluence" }, { label: "Access & settings", tab: "sources", group: "import:Confluence" }]);
    expect(manageLinks({ key: "uploads", kind: "upload" }).map((link) => link.tab)).toEqual(["workflow", "sources"]);
  });

  it("points a problem at the view that fixes it", () => {
    expect(fixLink({ key: "integration:3", kind: "connector", fix_in: "sources" })).toEqual({ label: "Connection & access", tab: "sources", group: "integration:3" });
    expect(fixLink({ key: "import:Jira", kind: "import", fix_in: "workflow" })).toEqual({ label: "Schedule & runs", tab: "workflow", group: "import:Jira" });
    expect(fixLink({ key: "uploads", kind: "upload", fix_in: null })).toBeNull();
  });

  it("counts what a source brings without listing its parts", () => {
    expect(describeCounts({ documents: 312, visible_documents: 312, parts: 34, catalog_items: null })).toBe("312 documents in 34 parts");
    expect(describeCounts({ documents: 1, visible_documents: 1, parts: 1, catalog_items: null })).toBe("1 document");
    expect(describeCounts({ documents: 0, visible_documents: 0, parts: 0, catalog_items: null })).toBe("0 documents");
    expect(describeCounts({ documents: 0, visible_documents: 0, parts: 0, catalog_items: 1204 })).toBe("1,204 catalog items");
    expect(describeCounts({ documents: 12, visible_documents: 12, parts: 1, catalog_items: 1 })).toBe("1 catalog item · 12 documents");
    expect(describeCounts({ documents: null, visible_documents: null, parts: null, catalog_items: 7 })).toBe("7 catalog items");
    // Restricted documents are counted, and the count says how many this admin can open.
    expect(describeCounts({ documents: 312, visible_documents: 20, parts: 34, catalog_items: null })).toBe("312 documents in 34 parts (20 visible to you)");
    expect(describeCounts({ documents: 1, visible_documents: 0, parts: 1, catalog_items: null })).toBe("1 document (0 visible to you)");
  });

  it("describes times relative to now", () => {
    const now = Date.parse("2026-10-16T12:00:00Z");
    expect(relativeTime("2026-10-16T15:00:00Z", now)).toBe("in 3 h");
    expect(relativeTime("2026-10-16T11:55:00Z", now)).toBe("5 min ago");
    expect(relativeTime("2026-10-16T12:00:20Z", now)).toBe("just now");
    expect(relativeTime("2026-10-13T12:00:00Z", now)).toBe("3 days ago");
    expect(relativeTime(null, now)).toBe("—");
    expect(relativeTime("not a date", now)).toBe("—");
  });
});
