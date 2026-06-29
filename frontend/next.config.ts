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
 */
const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const config: NextConfig = {
  // Emit a self-contained server (`.next/standalone/server.js` + trimmed
  // node_modules) so the production Docker image stays small and needs no
  // `npm install` at runtime.
  output: "standalone",
  skipTrailingSlashRedirect: true,
  async rewrites() {
    return [
      { source: "/api/:path*/", destination: `${apiUrl}/api/:path*/` },
      { source: "/api/:path*", destination: `${apiUrl}/api/:path*` },
      { source: "/media/:path*", destination: `${apiUrl}/media/:path*` },
    ];
  },
};

export default config;
