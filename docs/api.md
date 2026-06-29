# REST API reference

The React SPA's only data source is the Django REST Framework API under `/api/`,
defined in [`catalog/urls.py`](../backend/app/catalog/urls.py) and implemented in
[`views.py`](../backend/app/catalog/views.py),
[`spa_auth.py`](../backend/app/catalog/spa_auth.py), and
[`slack_views.py`](../backend/app/catalog/slack_views.py).

**Conventions**

- **Auth** — Django session cookie. Call `GET /api/me/` to discover the current
  user; unauthenticated requests get 401/403.
- **CSRF** — unsafe methods (POST/PUT/PATCH/DELETE) must send `X-CSRFToken` (read
  from the `csrftoken` cookie). The typed client
  ([`frontend/lib/api.ts`](../frontend/lib/api.ts)) does this for you.
- **Trailing slashes are required** (`/api/me/`, not `/api/me`).
- **Pagination** — list endpoints use DRF `PageNumberPagination`, `PAGE_SIZE=50`.
- **Org scoping** — every endpoint is implicitly scoped to the caller's resolved
  organization.

The frontend never builds URLs by hand — see the typed `api` object in
`frontend/lib/api.ts` for the canonical surface and response types.

---

## Auth & identity (`spa_auth.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/me/` | Current user + `can_view_*` permission flags + org |
| POST | `/api/auth/login/` | Log in (email **or** username + password) |
| POST | `/api/auth/logout/` | Log out |
| POST | `/api/me/change-password/` | Change own password |
| GET | `/api/me/workspaces/` | The user's per-source default workspaces |

## Catalog (DRF ViewSets)

Standard REST collections (`GET` list / `POST` create / `GET`,`PUT`,`PATCH`,
`DELETE` detail):

| Collection | Notes |
|---|---|
| `/api/items/` | Catalog items (`ItemViewSet`) |
| `/api/item-groups/` | Governance groups — **the write path** for owner/steward/status/category |
| `/api/departments/` | Departments |
| `/api/data-persons/` | Owners/stewards (`/api/owners/` is a legacy alias) |
| `/api/categories/` | Categories |
| `/api/tasks/` | Governance tasks (list + complete) |
| `/api/metrics-maps/` | Metrics Map scratchpads & canvases |

## Read models / dashboards

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/summary/` | Roll-up counts (`Summary`) |
| GET | `/api/dashboard/` | Precomputed dashboard payload |
| GET | `/api/filters/` | Filter option lists for the catalog views |
| GET | `/api/pb-cleanup-counts/` | Unused-asset counts for Power BI Cleanup |
| GET | `/api/dbt-insights/` | dbt catalog / cleanup / top-assets data |
| GET | `/api/powerbi-usage/` | Report usage pivots (Champions / Report Health) |

## Lineage graph

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/network/` | Ego graph around a node (`node_id`, `depth`, `direction`, `mode`) |
| GET | `/api/network/path/` | Shortest path(s) between two nodes |
| GET | `/api/network/reachable/` | Nodes reachable from a start (populates the path dropdown) |

`mode` is `asset` / `column` / `unified`; `direction` is `both` / `upstream` /
`downstream`. See [Lineage](lineage.md).

## AI assistant

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chat/` | Ask a question — returns `{session_id, task_id}` (async) |
| GET | `/api/chat/task/<task_id>/` | Poll task status / result |
| GET | `/api/chat/sessions/` | List the user's chat sessions |
| GET | `/api/chat/sessions/<id>/messages/` | Messages in a session |
| DELETE | `/api/chat/sessions/<id>/` | Delete a session |

See [Assistant](assistant.md) for the async flow.

## Governance CSV

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/governance/export-csv/` | Export catalog + governance as CSV |
| POST | `/api/governance/import-csv/` | Import governance edits (multipart) |

## Integrations (admin)

Sources:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/integrations/` | All sources, destinations, hooks + state |
| POST | `/api/integrations/sources/save/` | Create/update a source |
| POST | `/api/integrations/sources/<id>/run/` | Queue a run now |
| POST | `/api/integrations/sources/<id>/test/` | Connectivity test |
| GET | `/api/integrations/sources/<id>/logs/` | Run history |
| GET | `/api/integrations/logs/<id>/` | One run log detail |
| POST | `/api/integrations/logs/<id>/kill/` · `/delete/` | Cancel / delete a run |

Destinations mirror the same shape under `/api/integrations/destinations/…`.
Hooks: `POST /api/integrations/hooks/save/`. Log cleanup:
`POST /api/integrations/clean-logs/`.

Workflow (full pipeline):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/integrations/workflow/` | Current status + recent runs |
| POST | `/api/integrations/workflow/run/` | Run the full pipeline now |
| GET | `/api/integrations/workflow/<run_id>/` | Run detail |
| POST | `/api/integrations/workflow/schedule/` | Save the cron schedule |
| POST | `/api/integrations/workflow/<run_id>/kill/` · `/delete/` | Cancel / delete |
| POST | `/api/integrations/workflow/toggle/` | Enable/disable a step |
| POST | `/api/integrations/workflow/raw-export/` · `/raw-export/test/` | GCS raw-export config + test |

## Org administration (admin)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/org/members/` | List members + tiers |
| POST | `/api/org/members/save/` · `/remove/` | Add/update / remove a member |
| POST | `/api/org/settings/` | Save org settings (flags, model, branding) |
| POST | `/api/org/assistant-scope/` | Save assistant context scope |
| GET | `/api/org/queues/` | Django-Q queue snapshot |
| POST | `/api/org/queues/<ormq_id>/kill/` | Kill a queued task |

## Slack (webhooks / OAuth)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/slack/events/` | Slack Events API (assistant in Slack) |
| GET | `/api/slack/oauth/` | Bot OAuth callback |
| GET | `/api/slack/alerts-oauth/` | Alerts-app OAuth callback |
