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

Two things happen automatically on a status change:

- **`GovernanceTask`** — when a group flips to `ATTENTION` or `DELETED`, a task is
  created and routed to the asset's steward (or left unassigned and shown in the
  Task Manager's total view). The dedup rule is **at most one open task per
  group**. `assignee_role` records why the person was picked, so the routing
  policy (`catalog/governance_tasks.py`) can grow to owners/others without a
  schema change. Tasks are completed on the **Task Manager** page (`/tasks`).
- **`StatusChangeLog`** — an append-only row per transition (`old_status` →
  `new_status`, who, when), giving full history beyond the single `deleted_at`
  stamp. `group_key` is denormalized so the log outlives its group.

Both are written from the same two sites in `views.py` that also fire
[Slack alerts](etl.md#slack-alerts) — `send_slack_item_alert` (🔔 status / 🗑️
delete) and `send_slack_task_alert` (📋, tagging the assignee's `slack_handle`).

---

## CSV round-trip

The Data Dictionary supports bulk governance editing via CSV:

- `GET /api/governance/export-csv/` — download the current catalog + governance.
- `POST /api/governance/import-csv/` — multipart upload to apply changes (handles
  cp1252/cp1253 encodings; nginx allows up to 10 MB).

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
