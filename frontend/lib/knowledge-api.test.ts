import { afterEach, describe, expect, it, vi } from "vitest";
import { documentHref, knowledgeApi, safeSourceUrl } from "./knowledge-api";

afterEach(() => { vi.unstubAllGlobals(); document.cookie = "csrftoken=; max-age=0; path=/"; });

describe("Document Explorer API", () => {
  it("sends PDF multipart data with session credentials and CSRF, letting the browser set the boundary", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ unchanged: false }), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    document.cookie = "csrftoken=document-test; path=/";
    const file = new File(["%PDF-1.7 test"], "handbook.pdf", { type: "application/pdf" });
    await knowledgeApi.upload(file, "source-1", "doc-1");
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/knowledge/uploads/");
    expect(options?.credentials).toBe("include");
    expect(options?.method).toBe("POST");
    expect(options?.headers).toMatchObject({ "X-CSRFToken": "document-test" });
    expect(options?.headers).not.toHaveProperty("Content-Type");
    expect((options?.body as FormData).get("file")).toBe(file);
    expect((options?.body as FormData).get("source_id")).toBe("source-1");
    expect((options?.body as FormData).get("document_id")).toBe("doc-1");
  });

  it("escapes identifiers in canonical links and ignores invalid page numbers", () => {
    expect(documentHref({ document_id: "doc/1", version_id: "ver&1", page: -2, chunk_id: "chunk/#" }))
      .toBe("/knowledge/documents/doc%2F1?version=ver%261#chunk-chunk%2F%23");
  });

  it("accepts web provider links and rejects unsafe or credential-bearing source URLs", () => {
    expect(safeSourceUrl("https://workspace.slack.com/archives/C123/p456")).toBe("https://workspace.slack.com/archives/C123/p456");
    for (const value of ["javascript:alert(1)", "data:text/html,hello", "//example.com", "https://user:password@example.com", "file:///private", null]) {
      expect(safeSourceUrl(value)).toBeNull();
    }
  });
});
