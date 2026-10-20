import type { NextConfig } from "next";

/**
 * The React SPA talks to the existing Django backend. In dev, Next proxies
 * `/api/*` to Django (default http://localhost:8000) so the browser stays
 * single-origin — the Django session + csrftoken cookies flow through the proxy
 * with no CORS gymnastics. Point NEXT_PUBLIC_API_URL at a different host to
 * target a deployed backend.
 *
 * Django's API requires trailing slashes (/api/me/). The first rewrite keeps
 * the slash so Django answers directly; `skipTrailingSlashRedirect` stops Next
 * from 308-stripping it first (which would make Django APPEND_SLASH-redirect
 * "/api/me" -> "/api/me/" — a loop seen through the proxy).
 *
 * The proxy must never reach the workers' private endpoints
 * (/api/knowledge/internal/…, /api/document-analyzer/internal/…). nginx
 * refuses those paths first; here the /api rewrites skip any path with an
 * "internal" segment in any letter case or percent-encoding (Next matches
 * "/API/…" too and forwards it to Django as "/api/…"), which also keeps the
 * dev proxy off them. `next.config.test.ts` checks it with Next's own matcher.
 */
const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** One letter as Django may decode it: either case, or percent-encoded in either case. */
function anyCaseOrEncoded(letter: string): string {
  const hex = (char: string) => char.charCodeAt(0).toString(16).replace(/[a-f]/g, (d) => `[${d}${d.toUpperCase()}]`);
  const lower = letter.toLowerCase(), upper = letter.toUpperCase();
  return `(?:[${lower}${upper}]|%(?:${hex(lower)}|${hex(upper)}))`;
}

const SLASH = "(?:/|%2[fF])";
const INTERNAL = Array.from("internal", anyCaseOrEncoded).join("");
/** The rest of an /api/ path, unless one of its segments is "internal". */
const API_PATH =`(?!(?:.*${SLASH})?${INTERNAL}(?:${SLASH}|$)).+`;

const config: NextConfig = {
  // Emit a self-contained server (`.next/standalone/server.js` + trimmed
  // node_modules) so the production Docker image stays small and needs no
  // `npm install` at runtime.
  output: "standalone",
  skipTrailingSlashRedirect: true,
  async rewrites() {
    return [
      { source: `/api/:path(${API_PATH})/`, destination: `${apiUrl}/api/:path/` },
      { source: `/api/:path(${API_PATH})`, destination: `${apiUrl}/api/:path` },
      { source: "/media/:path*", destination: `${apiUrl}/media/:path*` },
    ];
  },
};

export default config;
