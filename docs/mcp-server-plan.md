# Analytics assistant — architecture plan & MCP server design

Status: **Phase M1 (MCP server, bearer auth) implemented** — see [mcp-server.md-worthy
sections below]. Everything else in this document is planned, not built.

This plan is the outcome of a deep-research pass (26 sources, 25 claims
adversarially verified) over text-to-data agent architectures (nao, Wren AI,
Snowflake Cortex Analyst, Databricks Genie, dbt MetricFlow, Spider2 literature)
plus the MCP 2025-11-25 authorization spec, grounded in this repo's actual
assistant ([docs/assistant.md](assistant.md), `catalog/tools/`).

---

## 1. Research conclusions that drive the plan

1. **Semantic-layer-first + constrained agent beats free-form ReAct.** The
   accuracy lever is *grounding* (explicit business semantics in context), not
   model capability: with a semantics doc, frontier models converge (~68% on a
   hard paired benchmark vs ~46-50% without); the top published architecture
   (94.15% on Spider2-snow) lets **only identifiers validated against the
   semantic model reach executed SQL**. Sources: arXiv:2604.25149,
   arXiv:2606.31041, spider2-sql.github.io, Canner/WrenAI, Snowflake
   engineering blog.
2. **Our assistant already implements most of this** — context-first
   front-loaded catalog, few coarse tools, loop guards, read-only execution
   guardrails. The *gap* is identifier validation before execution and a
   governed-metric fast path.
3. **"100% understood" is not achievable**; Execution Accuracy as a metric is
   itself unreliable (FLEX, NAACL 2025). So the plan optimizes for
   *verifiability*: deterministic paths where possible, visible SQL/DAX,
   an LLM-judge eval harness to catch regressions.
4. **MCP auth (spec 2025-11-25):** OAuth 2.1 + mandatory PKCE; server is an
   OAuth **resource server** (it MAY also be the authorization server); RFC
   9728 protected-resource metadata discovered via `401` +
   `WWW-Authenticate`; RFC 8414/OIDC AS metadata; RFC 8707 audience-bound
   tokens; **no token passthrough** to upstream APIs (BigQuery / Power BI keep
   using our own stored service credentials). Auth applies to HTTP transports
   only.

## 2. Phases

| Phase | What | Status |
|---|---|---|
| **M1** | MCP server over the existing assistant tools, static bearer-key auth | **built** |
| M2 | SQL/DAX **identifier validation** pre-flight (sqlglot vs front-loaded schema; DAX names vs synced catalog) | planned |
| M3 | Eval harness: `replay_chat` fixtures → scored suite with an LLM judge; 👍/👎 feedback on chat messages | planned |
| M4 | Governed **metric registry** + `run_metric(...)` deterministic tool (compile, don't generate) | planned |
| M5 | MCP **OAuth 2.1** upgrade (spec-compliant: RFC 9728 + 8414 + 8707 + PKCE), org-scoped consent | planned |

Rationale for M1-first (user decision): the MCP server reuses the tool layer
as-is, creates immediate external value (Claude Desktop / Claude Code / IDE
agents can query the catalog), and later phases (M2, M4) improve both chat
and MCP surfaces simultaneously because they share the tool layer.

---

## 3. M1 — MCP server (implemented)

### 3.1 Shape

- **Transport:** MCP *Streamable HTTP*, **stateless JSON mode** — a single
  Django view at **`POST /api/mcp/`** (`catalog/mcp/views.py`). Each JSON-RPC
  request gets one `application/json` response (the spec's allowed
  alternative to SSE streaming); no `Mcp-Session-Id` is issued or required;
  `GET /api/mcp/` returns 405 (no server-initiated streams). This fits the
  existing WSGI/gunicorn deployment — no ASGI sub-app, no new service — and
  nginx already proxies `/api/` with `proxy_buffering off` and a 300s read
  timeout (matching gunicorn's 300s).
- **Protocol:** `initialize` negotiates from
  `{2025-03-26, 2025-06-18, 2025-11-25}` (echo the client's if supported,
  else `2025-06-18`). Methods: `initialize`, `ping`, `tools/list`,
  `tools/call`; `notifications/*` → HTTP 202.
- **Why stateless:** every worker/replica can serve any request; chat-side
  behaviour is unaffected; no sticky sessions through nginx.

### 3.2 Auth (Stage A — bearer keys)

- New model **`McpApiKey`**: per **user + org**, label, `key_prefix` (display),
  **SHA-256 hash** of the token (raw token shown once at mint time, never
  stored), JSON `scopes` list, `is_active`, `last_used_at`.
- Token format `wdc_<token_urlsafe(32)>` in `Authorization: Bearer …`.
  Missing/invalid → `401` + `WWW-Authenticate: Bearer` (the shape M5's RFC
  9728 metadata slots into).
- Mint two ways, both sharing `auth.mint_key` (one minting path):
  1. **Org Settings → MCP Keys** (org admins) — a self-service tab that lists
     keys, mints via a dialog (name, "acts as" member, scopes), shows the raw
     token + a ready-to-paste client config **once**, and revokes. This is why
     minting needs an API the UI can surface a one-time reveal on — the Django
     admin add form can't (hence `has_add_permission = False` there).
  2. `python manage.py create_mcp_key --email <user> [--name X]
     [--scopes catalog:read powerbi:query bigquery:query]` for automation.
  Revoke from the same tab or by unchecking `is_active` in admin. This
  intentionally replaces the never-wired `MCP_TOKEN` env var.

### 3.3 Scopes × org flags → tools

Effective toolset = **key scopes ∩ org feature flags** (the same flags the
chat agent uses). Tool implementations are the *same functions* the chat
agent registers — one tool layer, two surfaces.

| Scope | Org flag gate | Tools |
|---|---|---|
| `catalog:read` | — | `get_catalog_overview` (the front-loaded context, exposed as a tool because MCP clients don't get our system prompt), `get_lineage` |
| `catalog:read` | `powerbi_tools_enabled` | `get_pb_item_details`, `get_pb_usage_analytics` |
| `catalog:read` | `dbt_tools_enabled` | `get_dbt_item_details` |
| `powerbi:query` | `powerbi_live_tools_enabled` + client | `powerbi_run_dax_query` (EVALUATE-only guardrails apply) |
| `bigquery:query` | `bigquery_live_tools_enabled` + client | `bigquery_execute_query` (SELECT/WITH-only, 1 GB dry-run cap, 50 rows) |

Key design point: **`get_catalog_overview`** compensates for the missing
front-loaded system prompt — an MCP client calls it first to get the exact
measure/report/model/table names, then uses the profilers, mirroring the
chat agent's "read the catalog, then profile" flow.

- Input schemas are generated by introspecting each tool function's
  signature (they stay in sync with the chat agent automatically);
  descriptions are the same docstrings the LLM already uses.
- Tool exceptions are returned as MCP tool results with `isError: true`
  (never HTTP 500), matching the `make_safe_tool` philosophy.
- No token passthrough: MCP callers authenticate to *us*; BigQuery/Power BI
  calls use the org's stored integration credentials, as in chat.

### 3.4 Files

- `catalog/mcp/` — `auth.py` (keys, scopes), `registry.py` (tool specs per
  key), `views.py` (JSON-RPC endpoint)
- `catalog/models.py` — `McpApiKey` (+ migration `0055_mcpapikey`)
- `catalog/management/commands/create_mcp_key.py`
- Self-service UI: `org_mcp_keys_view` / `_create` / `_revoke` in
  `catalog/spa_auth.py` (`/api/org/mcp-keys/…`), the `McpKeysManager` tab in
  `frontend/components/org/mcp-keys-manager.tsx`, and `api.org.mcpKeys` /
  `createMcpKey` / `revokeMcpKey` in `frontend/lib/api.ts`
- `catalog/tests/test_mcp_server.py`, `catalog/tests/test_org_mcp_keys_api.py`
- URL: `path('mcp/', mcp_endpoint)` in `catalog/urls.py`

### 3.5 Client config example

```json
{
  "mcpServers": {
    "welcome-data-catalog": {
      "type": "http",
      "url": "https://datagov.welcomd.com/api/mcp/",
      "headers": { "Authorization": "Bearer wdc_…" }
    }
  }
}
```

---

## 4. M2 — identifier validation (next)

Pre-flight inside the two live tools, before any query is sent:

- **BigQuery:** parse generated SQL with `sqlglot` (already a dependency),
  extract table/column refs, validate against the org's selected datasets'
  schema (already fetched for the front-loaded context). Unknown identifier →
  structured error with nearest-name candidates. This is the Spider2 papers'
  "only lifted names may execute" property as validate-and-reject.
- **Power BI:** extract `[Measure]` / `'Table'[Column]` refs from the DAX and
  validate against the synced catalog for that dataset.

## 5. M3 — eval harness

- Promote `catalog/tests/fixtures/problematic_chat_questions.json` +
  `replay_chat` into a scored suite (Django-Q nightly): run each question,
  judge with an LLM evaluator given question + schema + tool trace + answer
  (FLEX-style rubric), trend the score. EX-style string matching is
  explicitly *not* the metric (research: unreliable).
- Add 👍/👎 + optional comment on chat messages → grows the ground-truth set.

## 6. M4 — metric registry

- `Metric` model (or YAML in repo): name, description, canonical DAX/SQL
  template, allowed dimensions/filters/time grains, owner. Authoring UI can
  reuse the Metrics Map concepts later.
- New tool `run_metric(metric, dimensions=[], filters={}, period=...)` that
  *compiles* the query deterministically; the LLM only selects. Registered in
  both chat agent and MCP.

## 7. M5 — OAuth 2.1 for MCP

When third-party / per-user-consent clients matter:

- `django-oauth-toolkit` (OIDC + PKCE) as the authorization server inside
  Django (spec permits combined AS+RS), or Entra ID if we prefer delegating.
- Add: `/.well-known/oauth-protected-resource` (RFC 9728) and point the
  existing `401 WWW-Authenticate` at it; `/.well-known/oauth-authorization-server`
  (RFC 8414); enforce RFC 8707 `resource`/`aud` binding (canonical URI
  `https://datagov.welcomd.com/api/mcp/`); map OAuth scopes 1:1 onto the M1
  scope names, which is why M1 already uses OAuth-style scope strings.
- `McpApiKey` bearer keys remain as a "personal access token" fallback.

## 8. Non-goals / guardrails (from refuted research claims)

- Do **not** promise ">90% accuracy because semantic layer" — refuted framing.
- Do **not** cite vendor benchmark numbers (Cortex 90%/2×/+14% — refuted).
- Mutating tools (create measure/column) stay unregistered on all surfaces.
