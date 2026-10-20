import { afterEach, describe, expect, it, vi } from "vitest";
import { checkCustomRoutes, type Rewrite } from "next/dist/lib/load-custom-routes";
import { modifyRouteRegex } from "next/dist/lib/redirect-status";
import { getPathMatch } from "next/dist/shared/lib/router/utils/path-match";
import { prepareDestination } from "next/dist/shared/lib/router/utils/prepare-destination";
import config from "./next.config";

/**
 * The rewrites as next-server runs them: each source is compiled the way
 * server/lib/router-utils/filesystem.js builds a rewrite (strict, an optional
 * trailing slash, case-insensitive unless experimental.caseSensitiveRoutes) and
 * matched against the raw request path, as resolve-routes does; the first
 * match's destination is compiled with Next's own prepareDestination.
 */
async function rewrites(): Promise<Rewrite[]> {
  const found = await config.rewrites!();
  return Array.isArray(found) ? found : [...(found.beforeFiles ?? []), ...(found.afterFiles ?? []), ...(found.fallback ?? [])];
}

async function proxiedTo(pathname: string): Promise<string | null> {
  for (const rewrite of await rewrites()) {
    const match = getPathMatch(rewrite.source, {
      strict: true,
      removeUnnamedParams: true,
      regexModifier: (regex) => modifyRouteRegex(regex),
      sensitive: config.experimental?.caseSensitiveRoutes,
    });
    const params = match(pathname);
    if (!params) continue;
    const { parsedDestination } = prepareDestination({ appendParamsToQuery: true, destination: rewrite.destination, params, query: {} });
    const { protocol, hostname, port, pathname: path } = parsedDestination;
    return `${protocol}//${hostname}${port ? `:${port}` : ""}${path}`;
  }
  return null;
}

const backend = new URL(process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").origin;

afterEach(() => { vi.restoreAllMocks(); });

describe("next.config rewrites", () => {
  it("are rewrites Next accepts", async () => {
    const errors: unknown[][] = [];
    vi.spyOn(console, "error").mockImplementation((...args) => { errors.push(args); });
    vi.spyOn(process, "exit").mockImplementation(((code?: number) => { throw new Error(`process.exit(${code})`); }) as never);
    // Sanity: Next's check does refuse a bad source (capturing groups are not allowed)…
    expect(() => checkCustomRoutes([{ source: "/api/:path((x))", destination: "/x" }], "rewrite")).toThrow("process.exit(1)");
    errors.length = 0;
    // …and accepts ours.
    const list = await rewrites();
    expect(() => checkCustomRoutes(list, "rewrite")).not.toThrow();
    expect(errors).toEqual([]);
  });

  it("proxy the public API and media to Django, trailing slash and encoded ids kept", async () => {
    expect(await proxiedTo("/api/me/")).toBe(`${backend}/api/me/`);
    expect(await proxiedTo("/api/me")).toBe(`${backend}/api/me`);
    expect(await proxiedTo("/api/document-analyzer/comparisons/2026-10-16T10%3A00/")).toBe(`${backend}/api/document-analyzer/comparisons/2026-10-16T10%3A00/`);
    expect(await proxiedTo("/api/document-analyzer/ask/stream/job-1/")).toBe(`${backend}/api/document-analyzer/ask/stream/job-1/`);
    expect(await proxiedTo("/api/knowledge/internals/")).toBe(`${backend}/api/knowledge/internals/`);           // only a whole "internal" segment is private
    expect(await proxiedTo("/api/knowledge/international/x/")).toBe(`${backend}/api/knowledge/international/x/`);
    expect(await proxiedTo("/media/org/logo.png")).toBe(`${backend}/media/org/logo.png`);
  });

  it("route case-insensitively (Next's default), and still never proxy /API/…/internal/…", async () => {
    expect(config.experimental?.caseSensitiveRoutes).toBeFalsy();
    expect(await proxiedTo("/API/me/")).toBe(`${backend}/api/me/`);
    for (const path of [
      "/API/document-analyzer/internal/documents/",
      "/API/DOCUMENT-ANALYZER/INTERNAL/documents/",
      "/Api/Document-Analyzer/Internal/documents/",
      "/API/knowledge/internal/jobs/claim/",
      "/API/KNOWLEDGE/INTERNAL/jobs/claim/",
      "/Api/Knowledge/Internal/jobs/claim/",
      "/aPI/knowledge/%49nternal/jobs/claim/",
    ]) expect(await proxiedTo(path), path).toBeNull();
  });

  it("never proxy a path with an internal segment, in any case or encoding", async () => {
    for (const path of [
      "/api/document-analyzer/internal/documents/",
      "/api/document-analyzer/internal/documents/digest/",
      "/api/document-analyzer/internal/eval/answer/",
      "/api/knowledge/internal/jobs/claim/",
      "/api/knowledge/internal/jobs/3f1c/heartbeat",
      "/api/knowledge/internal",
      "/api/knowledge/internal/",
      "/api/internal/",
      "/api/knowledge/INTERNAL/jobs/claim/",
      "/api/knowledge/Internal/jobs/claim/",
      "/api/knowledge/%69nternal/jobs/claim/",
      "/api/knowledge/%49NTERNAL/jobs/claim/",
      "/api/knowledge/inter%6Eal/jobs/claim/",
      "/api/knowledge/inter%6eal/jobs/claim/",
      "/api/knowledge%2Finternal%2Fjobs%2Fclaim/",
      "/api/knowledge%2finternal/",
      "/api/knowledge/x/../internal/jobs/claim/",
      "/api/document-analyzer//internal/documents/",
    ]) expect(await proxiedTo(path), path).toBeNull();
  });
});
