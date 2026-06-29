# Assistant — prompts, context & tools, per case

What the chatbot's system prompt contains, what catalog **context** gets
front-loaded into it, and which **tools** are callable — all as a function of the
org's tool toggles. Assembled in
[`tools/agent.py`](../backend/app/catalog/tools/agent.py) (`get_agent`) from the
prompt fragments in [`tools/prompts.py`](../backend/app/catalog/tools/prompts.py)
and the per-integration context/tools in
[`tools/assistant/`](../backend/app/catalog/tools/assistant/); wired to the org by
`build_chatbot_agent_for_org` in [`views.py`](../backend/app/catalog/views.py).
Higher-level flow/timeouts: [`assistant.md`](./assistant.md).

**Design — context-first, tool-light.** The relevant catalog is *dumped into the
prompt as text* (the "context" below). The model resolves names by reading that
listing, so there is **no search/resolve tool** — only small per-integration
profilers, plus live DAX/SQL when a live tier is on.

---

## 1. Toggles (the only inputs)

Each integration has a **catalog tier** (`*_tools_enabled`, read-only) and, where
live execution exists, a **live tier** (`*_live_tools_enabled`). **Only the
PowerBI catalog tier defaults ON** — every other tier (dbt/BigQuery catalog, and
all live tiers) defaults OFF and is opt-in. Set on Org Settings → Assistant.

| Org flag | Tier | Default | Front-loads context | Registers tool |
|---|---|---|---|---|
| `powerbi_tools_enabled` | PowerBI catalog | **ON** | PowerBI measures + reports + tables | `get_pb_item_details`, `get_pb_usage_analytics` |
| `powerbi_live_tools_enabled` | PowerBI live | OFF | — | `powerbi_run_dax_query` |
| `dbt_tools_enabled` | dbt catalog | OFF | dbt models + columns | `get_dbt_item_details` |
| `bigquery_tools_enabled` | BigQuery catalog | OFF | BigQuery dataset schema | — (no execution) |
| `bigquery_live_tools_enabled` | BigQuery live | OFF | — | `bigquery_execute_query` |

So a fresh org's assistant starts with **PowerBI catalog only**: the PowerBI
listing in context, `get_pb_item_details` + `get_pb_usage_analytics` + the shared
`get_lineage`, and nothing else until an admin turns on more.

Always registered (no flag): **`get_lineage`** — a rare one-hop neighbour check on
an exact name, capped at 2 calls/turn. It's the *only* shared tool.

**Clients.** The PowerBI **live** client is built only when `powerbi_live_tools_enabled`
([views.py:159](../backend/app/catalog/views.py#L159)). The BigQuery client is
built when **either** BigQuery tier is on — the catalog tier needs it to fetch
schema live ([views.py:169](../backend/app/catalog/views.py#L169)). PowerBI/dbt
catalog profilers are DB-only and need no client.

---

## 2. The context each case front-loads

This is the "context" the model gets — built by each provider's `build_context`
in [`tools/assistant/`](../backend/app/catalog/tools/assistant/), cached per
org+scope. Skeletons below show the *exact* shape (abbreviated rows).

### PowerBI catalog — `powerbi_tools_enabled`
Scoped to the org's selected workspaces (`pb_workspace_ids`; none = all).
Measures are collapsed to **one row per measure-group** with group-level
`owner:`/status inline (so "who owns X" needs no tool call); descriptions capped
at 100 chars. [powerbi.py:52](../backend/app/catalog/tools/assistant/powerbi.py#L52)

```
## PowerBI catalog (authoritative — the full measure & report list is here; do NOT search the catalog)

### Measures (142) — one row per measure GROUP; `owner:` and status are group-level governance …
- **Transfers Operated** — count of transfers operated in period  ·  owner: Jane Doe · verified
- **Revenue (Net)** — net revenue after refunds  ·  owner: Finance
…
### Reports (37)
- **Ops Daily** (09.2 Drivers' Operations) — daily ops summary
- **Ops Weekly** (09.2 Drivers' Operations) — …
…
### Tables (58) — for table-level questions …, call get_pb_item_details(name) …
**driver_operations**, **bookings**, **pricing**, …
```

### dbt catalog — `dbt_tools_enabled`
Whole org (no scope). Each model line is `name (materialization, ` + FQN + `) —
desc`, with its columns indented (`name (type) — desc`).
[dbt.py:33](../backend/app/catalog/tools/assistant/dbt.py#L33)

```
## dbt catalog (authoritative — the full model & column list is here; do NOT search the catalog)

### Models (88)
- **stg_bookings** (view, `analytics.staging.stg_bookings`) — staged bookings
    - booking_id (INT64) — surrogate key
    - created_at (TIMESTAMP) — booking creation time
- **fct_revenue** (table, `analytics.marts.fct_revenue`) — revenue fact
    …
```

### BigQuery schema — `bigquery_tools_enabled` (or live)
Requires the BigQuery **client** and an explicit dataset selection
(`bq_dataset_ids`); empty otherwise. Fetched live from the API (slow → cached).
Per dataset, each table becomes a Markdown column table.
[bigquery.py:56](../backend/app/catalog/tools/assistant/bigquery.py#L56)

```
## BigQuery schema (authoritative — the full schema for the in-scope datasets is here; do NOT list or describe tables)

### dataset `analytics`

**`my-proj.analytics.bookings`** (TABLE)
| Column | Type | Mode | Description |
| --- | --- | --- | --- |
| booking_id | INT64 | REQUIRED | surrogate key |
| created_at | TIMESTAMP | NULLABLE | booking creation time |
…
```

---

## 3. The prompt fragments (actual text)

The system prompt is `BASE` + the blocks for whichever tiers are on. Load-bearing
excerpts below (trimmed with …); full strings in
[`prompts.py`](../backend/app/catalog/tools/prompts.py).

### `SYSTEM_PROMPT_BASE` — always
Defines the whole working method. Key passages:

> **There are no search tools — do NOT try to search or browse the catalog.**
> 1. **Find the exact names first.** … map the user's wording to EXACT catalog
> item names by READING the front-loaded listings … A loose phrase, an acronym,
> or a PLURAL / group ("the Ops reports", "pricing measures") is resolved by
> scanning the listing and taking EVERY matching name. Never pass a guessed or
> partial name to a … tool …
> 2. If you need depth on ONE item … call the item profiler … `get_pb_item_details(name)` … or `get_dbt_item_details(name)` … ONE call returns the complete profile …
> 5. For PowerBI measure↔report USAGE … call `get_pb_usage_analytics` … in ONE call from precomputed data …

Plus a **Disambiguation vs. group** section (singular-but-ambiguous → ask;
plural/group → enumerate & aggregate, don't ask, unless >~15), a **When you
cannot answer** section (be honest, don't loop), and **Guardrails** (never invent
names; don't re-call a tool with the same args; for a wrong-kind/ambiguous
singular match, STOP and ask — never re-call the profiler with guessed variants).

### `build_date_context_block()` — only when a live tier is on
Authoritative "today" block (current year, ISO today, and pre-computed
last/this/previous-week, last/this-month, YTD boundaries) to stop date
hallucination. Prepended before the provider sections.

### `SYSTEM_PROMPT_POWERBI_ADDENDUM` — PowerBI catalog on
> ### Usage analytics (measures ↔ reports) — … call `get_pb_usage_analytics` …
> pass `report_name` … `measure_name` … or NEITHER for the catalog-wide overview …
> ### Measure depth & live values — … `get_pb_item_details(measure_name_or_id)` …
> for a LIVE value … WRITE a DAX `EVALUATE` query … call `powerbi_run_dax_query(...)` …
> ### Reports — A PLURAL / group ask ("the Ops reports", "top measures in the Ops
> reports") names a SET on purpose. Do this, do NOT ask: (1) read the EXACT report
> names from the PowerBI catalog listing above …; (2) call
> `get_pb_usage_analytics(report_name=…)` on EACH; (3) aggregate …

Also covers multi-metric compares, date-grain output, date anti-hallucination,
and "never invent dataset_id/workspace_id".

### `SYSTEM_PROMPT_POWERBI_LIVE_DISABLED_NOTE` — PowerBI catalog on **but no live client**
Appended right after the PowerBI addendum to cancel its live steps:

> ### Live DAX is disabled … `powerbi_run_dax_query` is NOT available — ignore the
> live-value / `EVALUATE` steps above. Answer from the front-loaded catalog,
> `get_pb_item_details`, and `get_pb_usage_analytics`. If the user needs an actual
> live number, tell them to enable **PowerBI live tools** in Org Settings.

### `SYSTEM_PROMPT_DBT_ADDENDUM` — dbt on
> … call `get_dbt_item_details(model_name_or_id)` … returns everything in a single
> shot … For "what models/columns exist" … answer directly from the listing above
> — no tool call needed. Use the BigQuery FQNs … verbatim if you go on to run live
> BigQuery SQL …

### BigQuery — one of two addenda, depending on the live tier
**Live on → `SYSTEM_PROMPT_BIGQUERY_ADDENDUM`:**
> ## BigQuery (live SQL) … Write a read-only `SELECT` / `WITH` query that
> references ONLY tables and columns from that schema … then call
> `bigquery_execute_query(sql)`. … READ-ONLY: only `SELECT` / `WITH`. Refuse DML /
> DDL … (plus partition-filtering, date anti-hallucination, WoW/MoM = two calls).

**Live off → `SYSTEM_PROMPT_BIGQUERY_CATALOG_ADDENDUM`:**
> ## BigQuery (schema, read-only) … Live SQL execution is DISABLED:
> `bigquery_execute_query` is NOT available. Do not attempt to run a query, and
> never invent row counts … If the user needs an actual value, tell them to enable
> **BigQuery live tools** in Org Settings.

### `_workspace_scope_block(...)` — PowerBI catalog on **and** a default workspace resolved
Tells the agent which `workspace_id` to assume when the user names none
([agent.py:86](../backend/app/catalog/tools/agent.py#L86)).

### `build_format_reminder_block()` — only when a live tier is on
The mandatory output shape for live results: bold header first, a **Quick Stats**
table AND a **Result** table, no prose/footnotes.

---

## 4. Assembly order

Concatenated in this order ([agent.py:213](../backend/app/catalog/tools/agent.py#L213)):

| # | Block | Included when |
|---|---|---|
| 1 | `SYSTEM_PROMPT_BASE` | always |
| 2 | date-context block | `has_live` |
| 3 | PowerBI context + `…POWERBI_ADDENDUM` | PowerBI catalog on |
| 3b | `…POWERBI_LIVE_DISABLED_NOTE` | PowerBI catalog on **and** no live client |
| 4 | dbt context + `…DBT_ADDENDUM` | dbt on |
| 5 | BigQuery schema + (live → `…BIGQUERY_ADDENDUM`, else `…BIGQUERY_CATALOG_ADDENDUM`) | BigQuery (either tier) on |
| 6 | workspace-scope block | PowerBI catalog on **and** a default is resolved |
| 7 | output-format rules | `has_live` |

`has_live` = PowerBI live client present **or** BigQuery live on. Catalog-only
tiers are **not** live: they skip the date/format blocks and get the
"live disabled" addendum variant, so the prompt never tells the model to run a
tool that isn't registered.

---

## 5. Per case — context + prompt + tools

| Tiers ON | Context in prompt | Prompt blocks | Tools |
|---|---|---|---|
| none | — | BASE | `get_lineage` |
| **PowerBI catalog** *(default)* | PowerBI listing | BASE + PB addendum + live-disabled note | + `get_pb_item_details`, `get_pb_usage_analytics` |
| PowerBI catalog + live | PowerBI listing | BASE + date + PB addendum + format | + `powerbi_run_dax_query` |
| dbt catalog | dbt listing | BASE + dbt addendum | + `get_dbt_item_details` |
| BigQuery catalog | BQ schema | BASE + schema (read-only) addendum | — (no execution) |
| BigQuery catalog + live | BQ schema | BASE + date + live-SQL addendum + format | + `bigquery_execute_query` |

(Multiple tiers stack — sections concatenate per §4.) Per-tool status bubbles:
`TOOL_STATUS_MESSAGES` ([views.py:264](../backend/app/catalog/views.py#L264)).

### Worked example: "top measures in the Ops reports" (PowerBI catalog, default)
1. Read the exact Ops report names from the front-loaded **Reports** listing (§2).
2. `get_pb_usage_analytics(report_name=…)` on **each** matching report.
3. Aggregate in the answer: count how many use each measure, drop excluded ones
   (e.g. LYT / period-over-period), rank, take top N.

A handful of reports = a handful of calls, well under budget — no live PowerBI
access needed; it's all from the catalog.

---

## 6. Why a run stops (budgets)

| Mechanism | Limit | Effect |
|---|---|---|
| per-tool repeat guard | 5/tool/turn (`get_lineage`: 2) | returns a "stop, answer now" directive ([agent.py:301](../backend/app/catalog/tools/agent.py#L301)) |
| soft time guard | 100 s | same directive |
| UsageLimits | 20 tool calls / 30 model requests | → `_finalize_partial_answer` |
| wall-clock timeout | `org.chat_timeout_seconds` (def 180 s) | → `_finalize_partial_answer` |

`_finalize_partial_answer` runs a **tool-less** agent over what was gathered; its
"…reached its tool-call budget… be honest about what is uncertain" prompt is the
source of any *"tool budget running out" / "What is uncertain:"* answer.

> **Historical note:** there used to be a shared `resolve_catalog_items` tool that
> DB-searched the catalog for names. It was removed — it duplicated the
> front-loaded listing, and (being always-on) returned items from disabled
> integrations, which made the agent loop on *"Resolving exact catalog item names…
> (query='Ops')"* and bail. Name resolution is now read straight from the in-prompt
> listing (§2).
