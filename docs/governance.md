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

**Soft delete** is a group-level flag that cascades down. Each new group-delete
episode receives a fresh `deleted_at` marker and stamps that exact value only on
children that are still active, while forcing the group's status to `DELETED`.
An item already deleted by its source keeps its own timestamp. Restoring the
group clears only children carrying the group's exact episode marker, so it
cannot resurrect independently source-obsolete rows. Nothing is hard-deleted;
items are hidden from views unless the org's `show_deleted_items` flag is on.

`DELETED` is the lifecycle state; **To Be Deleted is not a category**. Migration
`0062_integrity_cleanup` treats an exact-tenant legacy category named
`To Be Deleted` (case-insensitively, ignoring surrounding whitespace) as
deletion intent: every affected non-`DELETED` group first moves to `DELETED`,
receives an append-only `StatusChangeLog` transition with no acting user, gets
its lifecycle timestamp and item-status mirrors repaired, and only then loses
the category. Already-`DELETED` groups are not logged twice. Cross-tenant
category links are quarantined before conversion and never carry deletion
intent between organizations. The migration also clears the old physical
`catalog_item.category_id` references before deleting the category row. A
normal production `migrate` performs this cleanup; the marker must not be
recreated.

The `remove_category` management command applies the same rule when run later:
its dry run reports the status breakdown and exact conversion count, while
`--apply` converts, audits, mirrors, detaches, and reconciles tasks atomically.
Removing any other category does not change lifecycle status and follows the
normal `NO_CATEGORY` workflow.

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
The UI will not enable Apply until a successful preview exists for the current
definition values and exact membership. The preview token binds the selected
fields, the exact member IDs, and a digest of every member's current values for
those fields. Changing membership or any selected value invalidates the preview
and requires a new one; an unselected field may change without making an
unrelated apply stale.

Managed at `/definitions`. Groups can also be assigned straight from the Data
Dictionary's **Definition** column (a PATCH on the ItemGroup), and the grid has a
matching Definition filter; `?item_group__definition=<id>` narrows the item API
the same way `item_group__category` does.

Definition and item endpoints are paginated. The Definitions page and both
assignment surfaces follow every DRF `next` link rather than treating the first
50/200 rows as the complete catalog. Definition lists, member counts, search
results, and assignment candidates therefore remain complete for larger
organizations.

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
  everything it carried (including its definition) and becomes its primary
  item. A still-active manual dismissal moves to the destination too, so a
  rename cannot silently reopen work a person already dismissed;
* the destination group **already exists** → that group keeps its own values and
  task history, and the item adopts them, because one renamed instance must not
  rewrite a group somebody else curated. Its current assignees and active
  status/category work are reconciled after the link;
* several renamed items land in the same new group → the lowest `item_id` seeds
  it, so the outcome doesn't depend on row order;
* if every item leaves the source group, its active dismissal is moved to the
  new group, remaining open tasks are closed as auto-resolved, and the empty
  group is deleted. This prevents a ghost definition member and stale task while
  preserving the rest of the task audit through `item_group=SET_NULL`;
* if another item still uses the source group, it remains untouched and any
  still-active dismissal is cloned to the genuinely new destination so both
  real groups retain the episode.

The detach and the re-link run in one transaction — the carry lives in memory
between them, so a crash can't strand an item with its curation unrecoverable.
A rename-back creates or joins the appropriate current-name group and carries
the curation again; it does not depend on retaining an empty historical group.

Scenario coverage is in
[tests/test_definitions.py](../backend/app/catalog/tests/test_definitions.py).

---

## Task Manager

`catalog/governance_tasks.py` is the single place that decides who gets a task
and why. Tasks arrive two ways:

| | trigger | scope | Slack |
|---|---|---|---|
| **event** | `sync_status_task` on every status transition | `ATTENTION` / `DELETED` for any kind; `UNVERIFIED` for measures; reconcile `NO_CATEGORY` for any active kind | one message for a newly active status task; category-task creation/refresh/close is silent |
| **sweep** | `generate_tasks()` — the "Generate tasks" button, or `manage.py generate_governance_tasks` | `kind_scope`, default all assets | one digest per run |

### Reasons and routing (`REASON_POLICY`)

| reason | applies to | routes to |
|---|---|---|
| `UNVERIFIED` | measure groups with `status='UNVERIFIED'` | Owner only |
| `NO_CATEGORY` | active, populated groups of any kind with no `category` | Owner only |
| `ATTENTION` | groups with `status='ATTENTION'` | Steward only |
| `DELETED` | groups with `status='DELETED'` | Steward only |

Routing is strict. Missing the required role leaves the task unassigned; work
for an Owner is never silently handed to a Steward, and vice versa.

### The sweep is a reconciler, not an appender

Initial ingestion and historical rows have no interactive status transition to
hook onto, so the sweep remains the backstop for rules such as "is still
unverified." Interactive transitions now reconcile the current status
immediately: entering `UNVERIFIED` creates measure work, while
`ATTENTION`/`DELETED` create Steward work for any asset kind. Category work is
created when a category is explicitly removed, when a renamed/preserved group
receives its current member again, on a status transition for an active
uncategorized asset, or by Generate for historical gaps. A manually dismissed
Category episode is still respected until the category condition clears.
Metadata changes refresh existing category work and assignees without
per-person Slack alerts.
Re-running the sweep is idempotent (the partial unique constraint enforces it),
**assignees are re-resolved every run** so tasks pick up an owner as ownership
gets filled in, and tasks whose gap has been fixed are auto-closed with
`closed_reason='resolved'` — distinct from `'manual'` when a human pressed Done.

`dry_run=True` returns the same counts having written nothing; that is what the
UI's **Preview** shows before anyone commits to thousands of rows.
For the high-volume `singleton` and `all` scopes, the admin API/UI preview also
returns a short-lived `preview_token`. The apply request must echo that token
with the same reasons, scope, and `require_assignee` choice; changing any input
invalidates it. The default `all` scope therefore requires a preview token in
the admin API/UI. The management command has no token exchange, so operators
must run and review its explicit `--dry-run` first.

Done is a durable dismissal of the current condition episode. A manually
completed task stays Done and reconciliation will **not recreate it while the
same underlying condition remains true**. Once reconciliation observes that the
condition has cleared it stamps `condition_cleared_at`; if the asset later
relapses, a fresh task row is created and the old audit row remains intact.
Tasks closed because the condition cleared use `closed_reason='resolved'`;
tasks closed by a person use `closed_reason='manual'`.

### Why the default all-assets scope is guarded

`kind_scope` defaults to `all` so every applicable rule is represented:
Unverified remains measure-only, while Category, Attention and To Be Deleted
cover every asset kind. Every non-measure item has its own singleton group, so
on production data an all-assets Category sweep can be orders of magnitude
larger than a measure-only sweep. `KIND_SCOPES` still exposes `measure_name`,
`singleton`, and `all`; broad UI applies require a matching preview token, and
operators must preview broad management-command runs explicitly.

### Page behaviour (`/tasks`)

The page only displays **open** work. There is no Completed tab and all list
requests use `state=open`; Done rows remain in the database/API for audit but
never appear in the Task Manager UI. Open tasks are grouped by reason,
selectable per group, and closable in bulk.

For a non-admin, "Mine" is always the `DataPerson` linked to the signed-in user
through `DataPerson.user`. The server pins list and summary queries to that
identity and does not trust browser-supplied `person`, `assignee`,
`unassigned`, or `all` scopes. An unlinked non-admin receives an empty feed plus
an `identity_required` signal; they cannot pick a name and impersonate another
person.

Org admins may inspect another person's Mine view and may use the Unassigned and
Everyone scopes. `manage.py dedupe_data_persons` reports identity gaps, and
`manage.py link_data_persons` creates the explicit login links required by
non-admin feeds.

### Duplicate people

`DataPerson` historically allowed duplicate dropdown labels and duplicate login
links. A display name alone is not a safe identity key: two different people can
share it. The cleanup therefore merges rows only when they share a deterministic
identity within one organization (the same login or the same non-empty Slack
handle), repointing governance FKs including the deprecated `catalog_item`
person columns. Distinct namesakes are retained and given a stable
distinguishing suffix. The following constraints enforce one governance profile
per login per organization plus a non-blank, trimmed/case-insensitive display
name per organization (including organization-less legacy rows).
`access.upsert_data_person` never claims a login-less namesake automatically; it
reports the conflict so an admin can link the intended row explicitly.

Confirmed twins that do not share an automatic identity key can be merged only
through an operator-reviewed CSV:

```csv
survivor_id,loser_id
42,81
42,93
```

Preview with
`python manage.py dedupe_data_persons --org 1 --merge-csv reviewed.csv`, then
repeat with `--apply`. `--org` makes every row in that plan prove it belongs to
that exact organization; omitting it still requires each pair to share one
non-null organization. The command validates the entire file before any write
and applies it in one transaction. The linked-login row must be the survivor;
different linked logins, different non-empty Slack handles (across the complete
survivor cluster), reused losers, and merge chains/cycles are rejected. It
repoints ItemGroup ownership/stewardship, GovernanceTask assignees, Definition
ownership, and the legacy Item person columns, then unions roles and
same-organization department memberships. It never infers a pair from a display
name.

---

## Production rollout and governance-task backfill

Run these commands from the repository root on the production host, against the
newly deployed `web` service. Replace `1` with the organization id and repeat
the merge/link/sweep sequence for every organization. Keep every dry run and
apply scoped to the same organization and options.

Before starting, take a restorable database snapshot using the production
platform's normal backup mechanism and record its identifier. Use a maintenance
window: block governance writes and pause ETL/background workers while
migrations, reviewed identity merges, and the first task reconciliations run.
Keep the application service available to the operator so `manage.py` can run.
Do not proceed from a preview whose organization or counts are unexpected.

```bash
# 0. Apply schema/data migrations. This includes integrity quarantine and
# removal of the legacy "To Be Deleted" category.
docker compose -f docker-compose.yml exec -T web python manage.py migrate

# 1. Report deterministic duplicates and identity gaps. No rows are written.
docker compose -f docker-compose.yml exec -T web \
  python manage.py dedupe_data_persons --org 1

# 2. OPTIONAL: only when a human confirms twins, preview that organization's
# exact survivor_id,loser_id plan. Keep a linked-login row as survivor.
docker compose -f docker-compose.yml exec -T web \
  python manage.py dedupe_data_persons --org 1 \
  --merge-csv /app/reviewed-merges-org-1.csv

# 3. OPTIONAL: apply the identical reviewed plan only after preview approval.
docker compose -f docker-compose.yml exec -T web \
  python manage.py dedupe_data_persons --org 1 \
  --merge-csv /app/reviewed-merges-org-1.csv --apply

# 4. With confirmed twins consolidated, preview exact identity links. No rows
# are written.
docker compose -f docker-compose.yml exec -T web \
  python manage.py link_data_persons --org 1

# 5. After reviewing LINKED / AMBIGUOUS / UNMATCHED, apply the same link plan.
docker compose -f docker-compose.yml exec -T web \
  python manage.py link_data_persons --org 1 --apply

# 6. Preview historical status tasks across every asset kind. This covers all
# ATTENTION/DELETED groups without creating hygiene tasks for every singleton.
docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons ATTENTION,DELETED --kind-scope all --dry-run

# 7. After reviewing the counts, apply that exact status-task sweep.
docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons ATTENTION,DELETED --kind-scope all --confirm-broad

# 8. Separately preview all-asset category hygiene. This can be a large run;
# NO_CATEGORY intentionally applies to active uncategorized singleton assets.
docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons NO_CATEGORY --kind-scope all --dry-run

# 9. After reviewing the counts, apply that exact category sweep.
docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons NO_CATEGORY --kind-scope all --confirm-broad

# 10. Preview and apply measure-only Unverified work.
docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons UNVERIFIED --kind-scope measure_name --dry-run

docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons UNVERIFIED --kind-scope measure_name

# 11. Re-run the identity reports and all three task previews after the applies.
docker compose -f docker-compose.yml exec -T web \
  python manage.py dedupe_data_persons --org 1

docker compose -f docker-compose.yml exec -T web \
  python manage.py link_data_persons --org 1

docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons ATTENTION,DELETED --kind-scope all --dry-run

docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons NO_CATEGORY --kind-scope all --dry-run

docker compose -f docker-compose.yml exec -T web \
  python manage.py generate_governance_tasks \
  --org 1 --reasons UNVERIFIED --kind-scope measure_name --dry-run
```

`link_data_persons` writes only with `--apply`; ambiguous or unmatched identities
must be resolved deliberately (use its `--csv <container-path>` mode when
needed). `generate_governance_tasks` writes unless `--dry-run` is present and is
silent on Slack by default. A `singleton` or `all` apply is rejected unless
`--confirm-broad` is also present; this makes the reviewed broad dry run an
explicit operator decision. Append `--notify` to the apply command only when a
single aggregate digest for created, reassigned, or closed work is intentional.
In each final verification table, `created`,
`reassigned`, and `closed` must all be zero. `unassigned` may remain only for
targets whose required Owner or Steward role is genuinely missing. Future
interactive status changes reconcile ATTENTION and DELETED work event-by-event
for every asset kind and UNVERIFIED work for measures. Category removal
reconciles NO_CATEGORY work for any active asset kind. The broad sweeps above
are the historical backfill. Preview every `singleton` or `all` management
command before applying it because those scopes can be orders of magnitude
larger.

Keep the reviewed CSV, command output, deployed revision, and backup identifier
together as the operator audit record. Resume workers and governance writes only
after the post-apply checks are clean.

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
