# Governance & access control

The governance layer puts owners, stewards, categories, a status workflow, an
audit trail, and tasks on top of the raw catalog — plus the per-page access model
that decides who can see what.

---

## Ownership lives on the group

Governance is curated on **`ItemGroup`**, not on individual `Item`s (see the
[schema](database.md#itemgroup)). This matters most for **measures**: the same
`PB_MEASURE` name often exists across many datasets and workspaces, but it's one
*concept* — so all its instances collapse into a single `measure_name` group and
its owner/steward/status is curated **once**. Everything else gets a 1-item
`singleton` group, so all code reads governance uniformly.

Each group can carry:

- **Owner** (`ownership_person`) and **Steward** (`steward`) — both `DataPerson`
  rows. A `DataPerson` is decoupled from login accounts so stakeholders without a
  user can still own assets; the dropdowns filter by role flags (`is_owner`,
  `is_steward`).
- **Owning department** (`ownership_department`) and **category** (`category`) —
  org-scoped classification.
- **Custom description** — a curated override of the source description.

`Item` exposes read-only proxies (`ownership_person`, `steward`, …) so legacy read
sites keep working, but **all writes go through the ItemGroup API**.

---

## Status workflow

`Item.STATUS_CHOICES` is shared across the governance models:

| Status | Meaning |
|---|---|
| `UNVERIFIED` | default — not yet reviewed |
| `VERIFIED` | reviewed and trusted |
| `ATTENTION` | needs review / flagged |
| `DELETED` | deprecated |

The single source of truth is `ItemGroup.status`. `Item.status` is a denormalized
mirror kept in lockstep by the API cascade, so item-level views (e.g. Power BI
Cleanup) and the BigQuery export can read/filter status without a join.

**Soft delete** is a group-level flag that cascades down: marking a group deleted
(e.g. "Mark to Delete" on the Cleanup page) sets `Item.deleted=True` +
`deleted_at` on every item in the group and forces the group's status to
`DELETED`. Clearing it restores the items. Nothing is hard-deleted; items are
hidden from views unless the org's `show_deleted_items` flag is on.

---

## Tasks & audit trail

- **`GovernanceTask`** — a follow-up for a data person. `reason` is the task
  *kind* and decides who it routes to; the dedup rule is **at most one open task
  per `(item_group, reason)`**, enforced by a partial unique constraint rather
  than convention. `assignee_role` records which role matched. See
  [Task Manager](#task-manager) below.
- **`StatusChangeLog`** — an append-only row per transition (`old_status` →
  `new_status`, who, when), giving full history beyond the single `deleted_at`
  stamp. `group_key` is denormalized so the log outlives its group.

Both are written from the same two sites in `views.py` that also fire
[Slack alerts](etl.md#slack-alerts) — `send_slack_item_alert` (🔔 status / 🗑️
delete) and `send_slack_task_alert` (📋, tagging the assignee's `slack_handle`).

---

## Definitions

`Definition` is the layer above `ItemGroup`: the business concept ("Revenue")
that several measure groups express. A group belongs to at most one definition.

**A definition does nothing on its own.** Assigning groups to it changes no
governance, and editing it changes none either — metadata moves down only when
someone runs an action. That is deliberate: a definition is a safe place to
organise measures, and pushing ownership across a hundred groups should be a
decision, not a side effect of a typo.

Today there is one action, `POST /api/definitions/<id>/apply/`, which writes the
definition's **owner and department** onto its member groups. It supports
`dry_run` (the UI's Preview) and reports how many groups actually changed.
Fields the definition hasn't set are **skipped, not blanked** — empty means "not
specified", so assigning groups to a fresh definition can never wipe curation.

Managed at `/definitions`. Groups can also be assigned straight from the Data
Dictionary's **Definition** column (a PATCH on the ItemGroup), and the grid has a
matching Definition filter; `?item_group__definition=<id>` narrows the item API
the same way `item_group__category` does.

Definition names are unique per organization, case-insensitively
(`uniq_definition_name_org`). That constraint is expression-based, which DRF's
automatic uniqueness validation does not cover, so the serializer checks it too —
otherwise a duplicate name surfaces as a 500 instead of a 400.

### Renaming a measure no longer costs its curation

The ETL derives an `ItemGroup` for measures from the measure *name*, so renaming
one in Power BI moves it to a different group. `_detach_renamed_measures`
([services/item_groups.py](../backend/app/catalog/services/item_groups.py)) used
to unlink the item and reset its status, landing it in a blank group — owner,
steward, category, definition and status all gone.

Now the metadata travels with it. The item is still attached to its old group at
the moment of detachment, which is the only moment that metadata is reachable,
so it is read there and carried forward:

* the destination group **has to be created** → the incoming item seeds it with
  everything it carried (including its definition) and becomes its primary item;
* the destination group **already exists** → that group keeps its own values and
  the item adopts them, because one renamed instance must not rewrite a group
  somebody else curated;
* several renamed items land in the same new group → the lowest `item_id` seeds
  it, so the outcome doesn't depend on row order;
* the emptied group is **kept**, which is what lets a rename-back recover.

The detach and the re-link run in one transaction — the carry lives in memory
between them, so a crash can't strand an item with its curation unrecoverable.

Scenario coverage is in
[tests/test_definitions.py](../backend/app/catalog/tests/test_definitions.py).

---

## Task Manager

`catalog/governance_tasks.py` is the single place that decides who gets a task
and why. Tasks arrive two ways:

| | trigger | scope | Slack |
|---|---|---|---|
| **event** | `sync_status_task` on a status flip to `ATTENTION` / `DELETED` | any asset kind | one message per task |
| **sweep** | `generate_tasks()` — the "Generate tasks" button, or `manage.py generate_governance_tasks` | `kind_scope`, default PowerBI measures | one digest per run |

### Reasons and routing (`REASON_POLICY`)

| reason | applies to | routes to |
|---|---|---|
| `UNVERIFIED` | measure groups with `status='UNVERIFIED'` | Owner → Steward |
| `NO_CATEGORY` | measure groups with no `category` | Owner → Steward |
| `ATTENTION` | groups with `status='ATTENTION'` | Steward → Owner |
| `DELETED` | groups with `status='DELETED'` | Steward → Owner |

Roles are an *ordered* tuple: the first one set on the group wins, so a measure
with no owner still reaches its steward instead of sitting unassigned.

### The sweep is a reconciler, not an appender

Rules like "is still unverified" have no status *transition* to hook onto, so
they can't be event-driven. Reconciling instead buys three things: re-running is
idempotent (the partial unique constraint enforces it), **assignees are
re-resolved every run** so tasks pick up an owner as ownership gets filled in,
and tasks whose gap has been fixed are auto-closed with
`closed_reason='resolved'` — distinct from `'manual'` when a human pressed Done.

`dry_run=True` returns the same counts having written nothing; that is what the
UI's **Preview** shows before anyone commits to thousands of rows.

### Why the default scope is measures

`kind_scope` defaults to `measure_name`. Every non-measure item has its own
singleton group, so on the production data the wider scopes are the difference
between ~4,100 tasks and ~130,000. `KIND_SCOPES` exposes the alternatives
(`singleton`, `all`) through the Generate dialog's dropdown and the command's
`--kind-scope`, so widening it is a choice rather than a code change.

### Page behaviour (`/tasks`)

Tabs are **Mine / Unassigned / Everyone / Completed**; the default fetch is
`state=open`, so Done tasks are out of the way but still auditable. Tasks are
grouped by reason, selectable per group, and closable in bulk.

"Mine" needs to know which `DataPerson` you are. `DataPerson.user` is the only
link between governance identity and a login, and most rows don't have it set,
so the page lets a person **pick their name** (remembered per browser) and falls
back to the linked profile when there is one. `manage.py dedupe_data_persons`
reports the gap both ways — people with no login, and logins with no person —
and `manage.py link_data_persons` fills it in.

### Duplicate people

`DataPerson` had no uniqueness rule, and the member-save upsert matched on
`user` alone, so a login-less row for someone was never found when they later
got an account — producing two identical names in every Owner / Steward
dropdown. Migrations `0056`–`0057` merge the duplicates (repointing governance
FKs, including the deprecated `catalog_item` person columns) and add the two
constraints that make it impossible. `access.upsert_data_person` now adopts a
login-less namesake instead of creating a twin.

---

## CSV round-trip

The Data Dictionary supports bulk governance editing via CSV:

- **Download CSV** button — built client-side from the **currently filtered**
  rows (one row per group), so what you see is what you download. Includes the
  read-only context columns `workspace` / `dataset` / `table` for local
  filtering in Sheets/Excel.
- `GET /api/governance/export-csv/` — full server-side export (every group,
  same columns).
- `POST /api/governance/import-csv/` — multipart upload to apply changes.
  Matches rows by `group_pk` (then `group_id`), ignores context columns and
  empty cells, handles cp1252/cp1253 encodings and `;`/tab delimiters, and
  strips a legacy leading `sep=,` line; nginx allows up to 10 MB.

Both exports start with the plain header row (UTF-8 BOM, no `sep=` hint line)
so the file opens directly in Google Sheets and other CSV consumers.

---

## Access control

Permissions are **per-organization**. The model has two independent axes:

### Org membership & admin

`OrganizationMembership` joins a user to an org with an `is_admin` flag. **Org
admin is org-scoped** (stored here, not in a global Django group). Superusers are
always admins. The two predicates in
[`catalog/access.py`](../backend/app/catalog/access.py) — `resolve_org(user)` and
`is_org_admin(user, org)` — are the single source of truth that every layer (page
views, the SPA API, DRF permission classes) routes through, so page visibility and
write authorization can never drift apart.

### Page-access tiers

Beyond admin, there are exactly **three** assignable access groups (Django
`auth.Group` rows), mapped to pages in `PAGE_ACCESS` — the single place the
page → group relationship lives:

| Tier | Unlocks |
|---|---|
| **Company** | Data Dictionary, Task Manager, Data Champions, AI Assistant, Power BI Catalog, Report Health & Usage |
| **Analytics** | Lineage Graph, Power BI Cleanup, Power BI Top Assets, dbt Catalog / Cleanup / Top Assets |
| **Admin** | Org Settings, Integrations — unlocked **only** by `is_org_admin()`, never by a group |

Dashboard and User Settings are always visible to any authenticated user.

Mechanically, group membership is turned into `perms.can_view_<key>` flags
(`get_user_permissions` in `frontend_views.py`, using `GROUP_PERM_KEYS` derived
from `PAGE_ACCESS`). The SPA reads these from `GET /api/me/` and hides nav items
accordingly ([`frontend/components/layout/nav-config.ts`](../frontend/components/layout/nav-config.ts)).
Org admins are granted **all** page keys, including the Admin-tier pages no group
unlocks.

Members and their tiers are managed on the **Org Settings** page (`/settings/org`).
