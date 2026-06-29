# Frontend

The UI is a Next.js 15 (App Router) single-page app in
[`frontend/`](../frontend/) — React 19 + TypeScript, Tailwind, Radix UI, TanStack
Query, and React Flow. It talks only to the Django REST API under `/api/`.

```
frontend/
├── app/            # App Router routes — one folder per page
├── components/     # UI primitives, layout, feature views (lineage, metrics canvas, …)
└── lib/            # typed API client, auth/query providers, pure logic + unit tests
```

---

## Talking to the backend

### Single-origin proxy

[`next.config.ts`](../frontend/next.config.ts) is `output: "standalone"` and
rewrites `/api/:path*` and `/media/:path*` to Django (`NEXT_PUBLIC_API_URL`,
default `http://localhost:8000`). Keeping everything same-origin means the Django
`sessionid` + `csrftoken` cookies flow with **no CORS setup**. In production nginx
does the same routing; in dev Next's rewrites do it.

> `skipTrailingSlashRedirect: true` is critical: Django's API requires trailing
> slashes, and without this Next would 308-strip the slash and create a redirect
> loop. The trailing-slash rewrite is listed first so Django answers directly.

### Typed API client

[`lib/api.ts`](../frontend/lib/api.ts) wraps `fetch` in a single `request<T>`
(base `/api`, `credentials: "include"`, `cache: "no-store"`). On unsafe methods it
attaches `X-CSRFToken` from the cookie. Errors throw a typed `ApiError(status,
message, body)`. Everything is exposed through one namespaced `api` object —
`api.auth`, `api.network`, `api.items`, `api.itemGroups`, `api.tasks`,
`api.integrations`, `api.workflow`, `api.chat`, `api.org`, … — with exported
TypeScript interfaces for every payload, so the whole surface is typed end to end.
Add new endpoints here, never as ad-hoc `fetch` calls.

### Auth

[`lib/auth.tsx`](../frontend/lib/auth.tsx) is a React context that calls
`api.auth.me()` on mount and exposes `user`, `login()`, `logout()`. Any
`ApiError` is treated as signed-out (never crashes the shell). `AuthGuard` (in the
app shell) redirects unauthenticated users to `/login?next=<path>`. Sessions are
entirely cookie-based — no tokens in JS.

### Data fetching

[`lib/query.tsx`](../frontend/lib/query.tsx) provides one TanStack Query client
(defaults: `staleTime` 30s, no refetch-on-focus, `retry` 1). The helper
`useMutationWithInvalidate(fn, [keys], options)` wraps `useMutation` and
auto-invalidates the given query keys on success.

---

## Shell & navigation

[`app/layout.tsx`](../frontend/app/layout.tsx) nests the providers
(`QueryProvider` → `AuthProvider` → `AppShell`).
[`components/layout/app-shell.tsx`](../frontend/components/layout/app-shell.tsx)
renders bare on `/login`, otherwise a sidebar + topbar + scrollable main.
[`components/layout/nav-config.ts`](../frontend/components/layout/nav-config.ts)
declares the nav groups with per-item `perm` keys (and `orAdmin`); `canSee`
defaults a missing perm to **visible** so a not-yet-wired `/api/me/` never hides
the whole app. Permission keys come from
[access control](governance.md#access-control).

---

## Pages

By nav group ([`nav-config.ts`](../frontend/components/layout/nav-config.ts)):

**Company**
- `/dashboard` — KPI landing page (measure-group KPIs, workspace stats). `/` is the entry point.
- `/dictionary` — Data Dictionary: the searchable catalog + governance editing + CSV round-trip.
- `/tasks` — Task Manager: governance tasks, mark-done.
- `/champions` — Data Champions: Power BI usage leaderboard.
- `/chat` — AI Assistant.
- `/powerbi/catalog` — Power BI catalog.
- `/powerbi/reports` — Report Health & Usage pivot.

**Analytics**
- `/lineage` — the [lineage explorer](lineage.md#part-b--exploring-the-graph-frontend).
- `/powerbi/cleanup` — unused Power BI assets.
- `/powerbi/top-assets` — top Power BI assets by impact.
- `/dbt/catalog`, `/dbt/cleanup`, `/dbt/top-assets` — the dbt equivalents.

**Tools**
- `/powerbi/metrics-map` — the Metrics Map diagram/canvas editor.

**Settings / footer**
- `/settings/user` — default workspaces, change password.
- `/settings/org` — members, org settings, assistant scope (admin).
- `/settings/queues` — Django-Q queue monitor (admin).
- `/integrations` — sources / destinations / hooks / workflow (admin).
- `/login` — bare login page.

---

## Notable feature modules

- **Lineage** — [`components/lineage/`](../frontend/components/lineage/) +
  [`lib/lineage/`](../frontend/lib/lineage/). React Flow graph; the pure logic
  (model build, layout, lens, saved views) is unit-tested. See [Lineage](lineage.md).
- **Metrics canvas** — [`components/metrics-canvas/`](../frontend/components/metrics-canvas/)
  + [`lib/metrics-canvas/`](../frontend/lib/metrics-canvas/). A draw.io/Miro-style
  diagram editor backed by `MetricsMap` (`kind='canvas'`), with YAML import/export.
- **Integrations** — [`components/integrations/`](../frontend/components/integrations/).
  Source/destination/hook config, run logs, and the workflow DAG.
- **UI primitives** — [`components/ui/`](../frontend/components/ui/). Radix-based
  building blocks. Note: dropdowns use the custom `SimpleSelect`
  (Radix-based, rounded popover) — **never the native `<select>`**.

---

## Scripts & tooling

From `frontend/`:

| Script | Description |
|---|---|
| `npm run dev` | Dev server on `:3000`, proxies `/api` → `:8000` |
| `npm run build` | Production build (`output: standalone`) |
| `npm run start` | Serve the production build |
| `npm run lint` | ESLint |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest unit tests (jsdom) |

Pure logic under `lib/` carries `*.test.ts` siblings (lineage build/layout/lens,
metrics-canvas serialize/layout, nav-config, utils). Keep business logic out of
components and in `lib/` so it stays testable.
