import uuid

from django.conf import settings
from django.db import models
from django.contrib.auth.models import AbstractUser, Group
from django.utils.translation import gettext_lazy as _

class Department(models.Model):
    name = models.CharField(max_length=255)
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='departments',
    )

    class Meta:
        ordering = ['name']
        unique_together = ('name', 'organization')

    def __str__(self):
        return self.name

def _validate_slack_handle(value):
    # Slack handles like '@jane' — leading '@' required when set; the rest is
    # whatever Slack accepts (we don't enforce Slack's exact charset because
    # the value is informational, not used to dispatch messages).
    if value and not str(value).startswith('@'):
        from django.core.exceptions import ValidationError
        raise ValidationError("Slack handle must start with '@' (e.g. '@jane').")


class DataPerson(models.Model):
    """A person who can own or steward catalog items.

    Decoupled from CustomUser on purpose: stakeholders without a login still
    need to be addressable in the catalog. When a person also has a login,
    the optional `user` FK links the two.
    """
    name = models.CharField(max_length=255)
    # A person can belong to multiple departments. The dropdowns in the
    # data catalog filter the Owner / Steward lists by the row's
    # ownership_department, so a person shows up wherever they have membership.
    departments = models.ManyToManyField(Department, related_name='data_persons', blank=True)
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='data_persons',
    )
    # Optional link to the actual login account. Null when the person is a
    # stakeholder without a CustomUser.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='data_person_profiles',
    )
    # Roles control which dropdown the person appears in. Any combination can
    # be true simultaneously — a person can be both an owner and a steward.
    # `is_other` is a person-level tag only (no catalog-item assignee slot);
    # it just classifies people who aren't owners/stewards.
    is_owner = models.BooleanField(default=True)
    is_steward = models.BooleanField(default=False)
    is_other = models.BooleanField(default=False)
    slack_handle = models.CharField(
        max_length=80, blank=True, null=True, validators=[_validate_slack_handle],
        help_text="Slack handle, e.g. '@jane'. Optional.",
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

class Category(models.Model):
    name = models.CharField(max_length=255)
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='categories',
    )

    class Meta:
        ordering = ['name']
        unique_together = ('name', 'organization')

    def __str__(self):
        return self.name

class CustomUser(AbstractUser):
    email = models.EmailField(_('email address'), unique=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL)
    # Per-source default workspace: {str(integration_source_id): workspace_id}.
    # Used by the chatbot to scope PowerBI queries and by the lineage UI to
    # pre-select a workspace dropdown when a source has more than one workspace.
    default_workspaces = models.JSONField(default=dict, blank=True)
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

class Summary(models.Model):
    total_measures = models.IntegerField()
    unused_measures = models.IntegerField()
    total_columns = models.IntegerField()
    unused_columns = models.IntegerField()
    total_reports = models.IntegerField()
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='summaries',
    )

class ItemManager(models.Manager):
    """``Item.objects.create(...)`` still accepts the old governance kwargs
    (ownership_person, steward, status, ...) — they're transparently routed to
    the item's ItemGroup (created on demand). Keeps callers/tests that predate
    the ItemGroup split working. ETL uses raw SQL + ensure_item_groups, not
    this path."""
    _GOV = ('ownership_department', 'ownership_person', 'steward',
            'category', 'status', 'custom_description')

    def create(self, **kwargs):
        gov = {}
        for f in self._GOV:
            if f in kwargs:
                gov[f] = kwargs.pop(f)
            if f + '_id' in kwargs:
                gov[f + '_id'] = kwargs.pop(f + '_id')
        primary = kwargs.pop('is_group_primary', None)
        item = super().create(**kwargs)
        grp = item.item_group
        if grp is None:
            if (item.item_type or '') == 'PB_MEASURE':
                key = item.group_id or (
                    f"{item.organization_id or 0}::"
                    f"{(item.item_name or '').strip().lower()}"
                )
                kind = ItemGroup.KIND_MEASURE_NAME
            else:
                key, kind = f'item::{item.pk}', ItemGroup.KIND_SINGLETON
            grp, _ = ItemGroup.objects.get_or_create(
                group_key=key,
                defaults={'kind': kind, 'organization_id': item.organization_id},
            )
            item.item_group = grp
            item.save(update_fields=['item_group'])
        changed = False
        for k, v in gov.items():
            setattr(grp, k, v)
            changed = True
        if primary or grp.kind == ItemGroup.KIND_SINGLETON:
            grp.primary_item = item
            changed = True
        if changed:
            grp.save()
        # Keep the item's denormalized status mirror in lockstep with its group.
        if item.status != grp.status:
            item.status = grp.status
            item.save(update_fields=['status'])
        return item


class Item(models.Model):
    objects = ItemManager()

    class Meta:
        ordering = ['item_name']
        indexes = [
            models.Index(fields=['item_id']),
            models.Index(fields=['item_type']),
            models.Index(fields=['workspace_name']),
            models.Index(fields=['is_unused']),
            models.Index(fields=['deleted']),
            models.Index(fields=['dataset_name']),
            models.Index(fields=['organization'], name='catalog_item_org_idx'),
            models.Index(fields=['service'], name='catalog_item_service_idx'),
            models.Index(fields=['integration_source'], name='catalog_item_intsrc_idx'),
            models.Index(fields=['service', 'item_type', 'deleted'], name='cat_item_svc_type_del_idx'),
            models.Index(fields=['organization', 'service', 'item_type', 'deleted'], name='cat_item_org_svc_type_del_idx'),
            models.Index(fields=['organization', 'deleted', 'service'], name='cat_item_org_del_svc_idx'),
            models.Index(fields=['deleted', 'item_type'], name='cat_item_del_type_idx'),
            models.Index(fields=['group_id'], name='cat_item_group_idx'),
        ]
        
    STATUS_CHOICES = [
        ('UNVERIFIED', 'Unverified'),
        ('VERIFIED', 'Verified'),
        ('DELETED', 'Deleted'),
        ('ATTENTION', 'Attention'),
    ]
    # Core Data
    item_id = models.CharField(max_length=2000, unique=True, primary_key=True)
    lineage_tag = models.CharField(max_length=2000, blank=True, null=True)
    item_name = models.CharField(max_length=1000, blank=True, null=True)
    item_type = models.CharField(max_length=100, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    
    # Hierarchy
    workspace_id = models.TextField(blank=True, null=True)
    workspace_name = models.TextField(blank=True, null=True)
    dataset_id = models.TextField(blank=True, null=True)
    dataset_name = models.TextField(blank=True, null=True)
    table_name = models.TextField(blank=True, null=True)
    
    # Attributes
    datatype = models.TextField(blank=True, null=True)
    column_type = models.TextField(blank=True, null=True)
    expression = models.TextField(blank=True, null=True)
    # Compiled SQL for dbt models (manifest `compiled_code`), shown alongside the
    # raw `expression` in the lineage detail panel's Raw/Compiled toggle.
    compiled_expression = models.TextField(blank=True, null=True)
    # Authored dbt properties (schema.yml block) for the node, serialized as YAML
    # and shown in the lineage detail panel's YAML tab.
    properties_yaml = models.TextField(blank=True, null=True)
    formatstring = models.TextField(blank=True, null=True)
    
    # Usage Stats
    is_unused = models.BooleanField(default=False)
    connected_reports = models.IntegerField(default=0)
    connected_report_pages = models.IntegerField(default=0)
    connected_visuals = models.IntegerField(default=0)
    connected_measures = models.IntegerField(default=0)
    connected_columns = models.IntegerField(default=0)
    connected_tables = models.IntegerField(default=0)

    # Semantic-model relationships (TABLE / COLUMN only). Populated from
    # `definition/relationships.tmdl` by the Fabric ETL.
    is_related = models.BooleanField(default=False,
        help_text='True if this TABLE/COLUMN participates in a relationship in its semantic model.')
    relationships_json = models.JSONField(default=list, blank=True,
        help_text='List of relationship descriptors from the perspective of this item.')

    # Organization linkage (set by ETL load_data command)
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='items',
    )

    # List of dicts: [{"id": "...", "name": "...", "url": "..."}] for MEASURE / COLUMN.
    # Populated by the ETL transform step. Empty list if no downstream reports.
    connected_reports_json = models.JSONField(default=list, blank=True)

    # Extended metadata (populated by ETL, never edited by users)
    database_name = models.TextField(blank=True, null=True,
        help_text='Database part of the 3-part FQN (database.schema.table)')
    # dbt-side: schema and alias kept separately so the bridge matcher can
    # join (database_name, schema_name, alias) without re-parsing table_name
    # (which stores 'schema.alias' for legacy compatibility).
    schema_name = models.TextField(blank=True, null=True,
        help_text='Schema part of the 3-part FQN (database.schema.table)')
    alias = models.TextField(blank=True, null=True,
        help_text='dbt alias / materialized table name (without schema prefix)')
    # PowerBI-side: BigQuery FQN extracted from the M-query partition source.
    # Used by the dbt ↔ PowerBI bridge as the preferred join key.
    bq_project = models.CharField(max_length=255, blank=True, null=True)
    bq_schema = models.CharField(max_length=255, blank=True, null=True)
    bq_source_name = models.CharField(max_length=255, blank=True, null=True)
    tags = models.JSONField(default=list, blank=True,
        help_text='dbt tags, PowerBI sensitivity labels, etc.')
    meta = models.JSONField(default=dict, blank=True,
        help_text='Catch-all for source-specific metadata: dbt meta, constraints, '
                  'loader, access level, and any future niche fields.')

    # Source linkage (which IntegrationSource produced this item)
    integration_source = models.ForeignKey(
        'IntegrationSource', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='items',
        help_text='The IntegrationSource that produced this item via ETL',
    )

    # Manual Input (Django fields)
    deleted = models.BooleanField(default=False)
    # When the item was (most recently) marked for deletion. Stamped by the
    # API when `deleted` flips True; cleared when it flips back. Powers the
    # "Deleted Items" history view (sorted newest-first). Null for items
    # soft-deleted by older flows / the ETL that predate this column.
    deleted_at = models.DateTimeField(null=True, blank=True)
    web_url = models.URLField(max_length=2000, blank=True, null=True)
    service = models.CharField(max_length=255, blank=True, null=True)

    # Per-item status. The single source of truth is still ItemGroup.status —
    # this column is a denormalized MIRROR kept in lockstep by the cascade in
    # views.py (group edit / mark-to-delete) and services/item_groups.py (ETL
    # linking). It exists so item-level views (e.g. PowerBI Cleanup) and the
    # BigQuery export can read/filter status without joining to the group, and
    # so a group status change visibly propagates to every connected item.
    # Writes still go through the ItemGroup API, never directly to the item.
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='UNVERIFIED', db_index=True)

    # Governance lives on the ItemGroup now (single source of truth shared by
    # every instance of a measure group; a 1-item "singleton" group for
    # everything else). The old per-Item columns
    # (ownership_department_id, ownership_person_id, steward_id, status,
    # category_id, custom_description) are intentionally NOT declared on the
    # model anymore — migration 0029 removes them from Django state but keeps
    # the DB columns (deprecated; dropped in a later migration). The
    # properties below proxy reads to the group so existing read sites keep
    # working unchanged; writes go through the ItemGroup API.
    # Named `item_group` (not `group`) so its attname `item_group_id` does
    # NOT collide with the existing measure-key CharField named `group_id`.
    item_group = models.ForeignKey(
        'ItemGroup', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='items',
        help_text='The ItemGroup that owns this item\'s governance.',
    )

    # Read-proxies so existing call sites (chatbot, admin, slack, templates)
    # keep working unchanged now that governance lives on the group. Writes
    # go through the ItemGroup API, not these.
    @property
    def group(self):
        return self.item_group if self.item_group_id else None

    @property
    def ownership_department(self):
        return self.item_group.ownership_department if self.item_group_id else None

    @property
    def ownership_person(self):
        return self.item_group.ownership_person if self.item_group_id else None

    @property
    def steward(self):
        return self.item_group.steward if self.item_group_id else None

    @property
    def category(self):
        return self.item_group.category if self.item_group_id else None

    # NOTE: `status` is a real DB column (see above), no longer a proxy. It is
    # kept equal to item_group.status by the cascade; readers are unaffected.

    @property
    def custom_description(self):
        return self.item_group.custom_description if self.item_group_id else None

    @property
    def is_group_primary(self):
        """Deprecated alias — primary now lives on ItemGroup.primary_item."""
        return bool(self.item_group_id and self.item_group.primary_item_id == self.pk)

    # Measure grouping key. The same PB_MEASURE name can exist in many
    # datasets / workspaces; they all share one group_id, which is also the
    # `ItemGroup.group_key` for the measure group. Format:
    #   "{organization_id or 0}::{lower(trim(item_name))}"
    # Set by the ETL load step for PB_MEASURE rows only; NULL for every other
    # item type (those get a per-item singleton ItemGroup). NOT user-editable.
    group_id = models.CharField(max_length=1100, blank=True, null=True)


class ItemGroup(models.Model):
    """Owns the governance for one or more Items.

    Two kinds:
      * ``measure_name`` — every PB_MEASURE instance sharing a name (and
        ``Item.group_id``) collapses into ONE group, so owner / steward /
        status / etc. are curated once and shared across all instances.
      * ``singleton`` — every non-measure item gets its own 1-item group, so
        "everything has a group" and all code reads governance uniformly.

    Intentionally generic: future manual or automated grouping just reassigns
    ``Item.group`` — no schema change needed.
    """
    KIND_MEASURE_NAME = 'measure_name'
    KIND_SINGLETON = 'singleton'
    KIND_CHOICES = [
        (KIND_MEASURE_NAME, 'Measure (grouped by name)'),
        (KIND_SINGLETON, 'Singleton'),
    ]

    # Dedupe / natural key:
    #   measures   -> Item.group_id ("{org_id or 0}::{lower(trim(name))}")
    #   singletons -> "item::{item_id}"
    group_key = models.CharField(max_length=1100, unique=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_SINGLETON)
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='item_groups',
    )

    # Governance — single source of truth (moved off Item).
    ownership_department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL, related_name='owned_groups')
    ownership_person = models.ForeignKey(DataPerson, null=True, blank=True, on_delete=models.SET_NULL, related_name='owned_groups')
    steward = models.ForeignKey(DataPerson, null=True, blank=True, on_delete=models.SET_NULL, related_name='stewarded_groups')
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name='item_groups')
    status = models.CharField(max_length=20, choices=Item.STATUS_CHOICES, default='UNVERIFIED')
    custom_description = models.TextField(blank=True, null=True)

    # Soft-delete flag for the whole group. Setting this True (via the
    # ItemGroup API — e.g. "Mark to Delete" on the PowerBI Cleanup page) makes
    # the API cascade the deletion DOWN to every Item in the group (Item.deleted
    # = True, Item.deleted_at stamped) and force the group's status to
    # DELETED. Clearing it restores the items. Mirrors the per-item
    # Item.deleted flag at the group level.
    deleted = models.BooleanField(default=False)

    # When this group was deprecated / deleted. Kept in lockstep with DELETED
    # status by the API: stamped when the group enters DELETED, cleared when
    # it leaves.
    deleted_at = models.DateTimeField(null=True, blank=True)

    # Which Item supplies the group's default workspace / dataset / DAX.
    # For singletons this is the item itself; for measure groups it's the
    # user-pinned (or heuristic) representative. SET_NULL + '+' so deleting
    # the item doesn't cascade the group away.
    primary_item = models.ForeignKey(
        'Item', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['group_key'], name='cat_itemgroup_key_idx'),
            models.Index(fields=['kind'], name='cat_itemgroup_kind_idx'),
            models.Index(fields=['organization'], name='cat_itemgroup_org_idx'),
        ]

    def __str__(self):
        return f'{self.group_key} ({self.kind})'


class GovernanceTask(models.Model):
    """A small actionable task for a data person.

    Created automatically when an ``ItemGroup`` status flips to ``ATTENTION``
    or ``DELETED`` (the latter also fires when an item is marked for
    deletion, which auto-DEPRECATEs its group). Assigned to the asset's
    steward when one exists; otherwise left unassigned and shown in the
    Task Manager's total view. Dedupe rule: at most one *open* task per group.
    """
    STATE_OPEN = 'open'
    STATE_DONE = 'done'
    STATE_CHOICES = [
        (STATE_OPEN, 'Open'),
        (STATE_DONE, 'Done'),
    ]

    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='governance_tasks',
    )
    item_group = models.ForeignKey(
        ItemGroup, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='tasks',
    )
    # The person the task is routed to. May be null (unassigned task).
    # Today this is always the asset's steward, but the model is intentionally
    # role-agnostic: `assignee_role` records *why* this person was picked so the
    # routing policy can expand to owners / others later without a schema change.
    assignee = models.ForeignKey(
        DataPerson, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='tasks',
    )
    # Which governance role the assignee was resolved from ('steward', 'owner',
    # 'other', ...). Drives display and lets the routing policy grow over time.
    # See catalog/governance_tasks.py ASSIGNEE_ROLES for the active policy.
    assignee_role = models.CharField(max_length=20, blank=True, null=True)
    # The status that triggered / last refreshed this task ('ATTENTION' | 'DELETED').
    trigger_status = models.CharField(max_length=20, choices=Item.STATUS_CHOICES)
    title = models.CharField(max_length=512)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=STATE_OPEN)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='completed_tasks',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['state', 'created_at'], name='cat_task_state_created_idx'),
            models.Index(fields=['organization'], name='cat_task_org_idx'),
        ]

    def __str__(self):
        return f'{self.title} ({self.state})'


class StatusChangeLog(models.Model):
    """Append-only audit trail of ``ItemGroup`` status changes.

    One row per transition, written from the same two sites in ``views.py`` that
    fire Slack alerts and governance tasks. Answers "when did this asset move to
    DELETED / ATTENTION / VERIFIED, and who changed it" with full history —
    a superset of the single ``Item.deleted_at`` stamp.

    ``item_group`` is ``SET_NULL`` so the log outlives its group; ``group_key``
    keeps a denormalized label so a row stays readable after the group is gone.
    """
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='status_changes',
    )
    item_group = models.ForeignKey(
        ItemGroup, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='status_changes',
    )
    # Denormalized group_key snapshot so the row stays meaningful if the group
    # is later deleted (item_group goes null).
    group_key = models.CharField(max_length=1100, blank=True, null=True)
    # old_status is null for the first-ever record of a group.
    old_status = models.CharField(max_length=20, choices=Item.STATUS_CHOICES, blank=True, null=True)
    new_status = models.CharField(max_length=20, choices=Item.STATUS_CHOICES)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='status_changes',
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-changed_at']
        indexes = [
            models.Index(fields=['item_group', 'changed_at'], name='cat_statuslog_group_idx'),
            models.Index(fields=['organization', 'changed_at'], name='cat_statuslog_org_idx'),
            models.Index(fields=['new_status'], name='cat_statuslog_status_idx'),
        ]

    def __str__(self):
        return f'{self.group_key or self.item_group_id}: {self.old_status or "—"} -> {self.new_status}'


class NetworkNode(models.Model):
    # node_id is a composite unique id of the form "{TYPE}::{hash}" where:
    #   - TYPE is the node kind (TABLE, COLUMN, MEASURE, REPORT, PAGE, VISUAL, FIELD)
    #   - hash is the MD5 id used by catalog.Item for catalog-resident types
    #     (so TABLE/COLUMN/MEASURE/REPORT nodes can be joined 1:1 with Item),
    #     and a deterministic fallback hash for PAGE/VISUAL/FIELD which are not
    #     stored in catalog.Item.
    node_id = models.CharField(max_length=512, unique=True, primary_key=True)
    # Human-readable label shown in the graph UI (e.g. "Driver Availability").
    # Distinct from node_id because the same name can appear in multiple node types.
    name = models.CharField(max_length=255, blank=True, null=True)
    group = models.CharField(max_length=50, blank=True, null=True)
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='network_nodes',
    )

    class Meta:
        indexes = [
            models.Index(fields=['group']),
            models.Index(fields=['name']),
        ]

class NetworkEdge(models.Model):
    source = models.CharField(max_length=512)
    target = models.CharField(max_length=512)
    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='network_edges',
    )
    # Set on cross-tool bridge edges only (DBT_* ↔ TABLE/COLUMN). Values:
    # 'bq_fqn' (matched on BigQuery FQN), 'name_full' or 'name_tail' (matched
    # on display name). NULL for in-domain edges.
    bridge_reason = models.CharField(max_length=16, blank=True, null=True)
    # Persisted edge classification — the single source of truth lives in
    # catalog.services.network_classify. Populated at load time (and backfilled
    # for pre-existing rows) so the graph filters on an indexed column instead
    # of re-deriving the kind from node-id prefixes on every read.
    #   kind:  'contains' | 'column' | 'model' | 'join' | 'filter'
    #   level: 'asset' | 'column'   (which lineage view the edge belongs to)
    kind = models.CharField(max_length=16, blank=True, null=True)
    level = models.CharField(max_length=8, blank=True, null=True)
    # For column edges: how the target column was derived from the source —
    # 'pass-through' | 'rename' | 'transformation'. NULL for non-column edges.
    lineage_type = models.CharField(max_length=16, blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['source']),
            models.Index(fields=['target']),
            models.Index(fields=['level']),
            models.Index(fields=['kind']),
        ]
        unique_together = ('source', 'target')


class PowerBIReportUsage(models.Model):
    """Per (workspace, report, user, month, platform, distribution, page) view counts.

    Pulled from each workspace's usage-metrics dataset via DAX. The ETL only
    re-extracts the most recent N months (default 3); the loader does a *windowed*
    replace — it deletes and re-inserts only the months present in that run and
    leaves older months untouched, so usage history accumulates across runs. A
    re-pulled month is fully swapped to the latest rows, so a legacy→modern
    schema transition can't double-count a month.
    """
    month = models.DateField(help_text='First day of the month bucket (YYYY-MM-01).')
    workspace_id = models.TextField(blank=True, null=True)
    workspace_name = models.TextField(blank=True, null=True)
    report_id = models.TextField(
        blank=True, null=True,
        help_text='Power BI report GUID. Joins to Item.item_id via the same hash used by the Fabric ETL.',
    )
    report_name = models.TextField(blank=True, null=True)
    user_email = models.TextField(blank=True, null=True)
    user_display_name = models.TextField(blank=True, null=True)
    platform = models.TextField(blank=True, null=True)
    distribution_method = models.TextField(blank=True, null=True)
    report_page = models.TextField(blank=True, null=True)
    view_count = models.IntegerField(default=0)

    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='powerbi_usage',
    )
    integration_source = models.ForeignKey(
        'IntegrationSource', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='powerbi_usage',
    )

    class Meta:
        indexes = [
            models.Index(fields=['month'], name='pb_usage_month_idx'),
            models.Index(fields=['workspace_id'], name='pb_usage_ws_idx'),
            models.Index(fields=['report_id'], name='pb_usage_report_idx'),
            models.Index(fields=['user_email'], name='pb_usage_user_idx'),
            models.Index(fields=['organization'], name='pb_usage_org_idx'),
            models.Index(fields=['integration_source'], name='pb_usage_intsrc_idx'),
        ]


class ChatSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_sessions')
    title = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Stable thread_id used by the pb_live_query flow to key its
    # persisted state row in catalog_pblivequerythread. Empty string
    # when no live-query flow has run for this session. The column
    # name is historical and is kept to avoid an unnecessary migration.
    langgraph_thread_id = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        ordering = ['-updated_at']

class ChatMessage(models.Model):
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=50)
    content = models.TextField()
    debug_meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

class ChatbotModel(models.Model):
    """Catalogue of LLMs that can power the AI Assistant.

    `identifier` is passed verbatim to pydantic-ai's `Agent(model=...)`
    (e.g. `google:gemini-3.1-pro-preview`). `display_name` is shown in the UI.
    """
    identifier = models.CharField(max_length=200, unique=True)
    display_name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'display_name']

    def __str__(self):
        return self.display_name


class Organization(models.Model):
    name = models.CharField(max_length=255)
    slack_bot_token = models.CharField(max_length=255, blank=True, null=True)
    primary_color = models.CharField(max_length=50, default='#00cf95', blank=True)
    icon = models.ImageField(upload_to='organizations/icons/', blank=True, null=True)

    # Chatbot feature flags. Naming rule, consistent across integrations:
    #   <integration>_tools_enabled       → CATALOG tier (read-only)
    #   <integration>_live_tools_enabled  → LIVE-execution tier (default OFF)
    # Only the PowerBI catalog tier ships ON by default; every other tier
    # (dbt/BigQuery catalog, and all live tiers) defaults OFF and is opt-in.
    # dbt has no live tier (its tools all read the local catalog DB).
    powerbi_tools_enabled = models.BooleanField(
        default=True,
        help_text='PowerBI catalog assistant: front-load the measure/report '
                  'listing and register the read-only profiler (get_pb_item_details) '
                  '+ usage-analytics (get_pb_usage_analytics) tools. Local catalog '
                  'DB only — no external calls.',
    )
    powerbi_live_tools_enabled = models.BooleanField(
        default=False,
        help_text='Allow the AI Assistant to run live DAX queries against the '
                  'PowerBI REST API.',
    )
    dbt_tools_enabled = models.BooleanField(
        default=False,
        help_text='dbt catalog assistant: front-load the model/column listing and '
                  'register the dbt profiler + lineage tools. Local catalog DB only '
                  '(dbt has no live-query tier).',
    )
    bigquery_tools_enabled = models.BooleanField(
        default=False,
        help_text='BigQuery catalog assistant: load the in-scope dataset schema '
                  '(tables, columns, types) into context, read-only. No query '
                  'execution.',
    )
    bigquery_live_tools_enabled = models.BooleanField(
        default=False,
        help_text='Allow the AI Assistant to run read-only live SQL queries '
                  'against BigQuery.',
    )
    debug_responses_enabled = models.BooleanField(
        default=False,
        help_text=(
            'Append a hardcoded debug section (DAX, BigQuery SQL, tool calls, '
            'and timing stats) to every chatbot answer. Tool-call metadata is '
            'always persisted to ChatMessage.debug_meta regardless of this flag.'
        ),
    )
    show_deleted_items = models.BooleanField(
        default=False,
        help_text=(
            'When ON, items that no longer exist in the source (soft-deleted '
            'by ETL) are still counted and listed in the dashboard and catalog '
            'pages. When OFF (default), they are hidden from all views but '
            'preserved in the database for history.'
        ),
    )

    chatbot_model = models.ForeignKey(
        'ChatbotModel',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='organizations',
        help_text='Which LLM the AI Assistant uses to answer questions.',
    )

    # Assistant context scope — which PowerBI workspaces / BigQuery datasets
    # feed the front-loaded catalog context in the AI Assistant's prompt.
    # Empty list = all workspaces (PowerBI) / no datasets (BigQuery requires
    # an explicit selection because its schema is fetched live).
    assistant_powerbi_workspace_ids = models.JSONField(
        default=list, blank=True,
        help_text='PowerBI workspace ids whose measures/reports are loaded into '
                  'the AI Assistant context. Empty = all workspaces.',
    )
    assistant_bigquery_dataset_ids = models.JSONField(
        default=list, blank=True,
        help_text='BigQuery dataset ids whose schema is loaded into the AI '
                  'Assistant context. Empty = none (must be selected).',
    )

    chat_timeout_seconds = models.PositiveIntegerField(
        default=180,
        help_text=(
            'Maximum seconds the AI Assistant may run per question before the '
            'request is aborted with a timeout error (web chat).'
        ),
    )

    def __str__(self):
        return self.name


# ==========================================
# INTEGRATIONS
# ==========================================

class IntegrationSource(models.Model):
    SOURCE_TYPE_CHOICES = [
        ('powerbi_fabric', 'PowerBI / Microsoft Fabric API'),
        ('csv_upload', 'CSV Upload'),
        ('postgresql', 'PostgreSQL'),
        ('mysql', 'MySQL'),
        ('snowflake', 'Snowflake'),
        ('dbt', 'dbt (Data Build Tool)'),
    ]

    # Pipeline layer the source belongs to. Workflow runs all 'transformation'
    # sources to completion before starting any 'visualization' source — same
    # ordering as ETL: data must be reshaped in the warehouse before BI tools
    # read it.
    CATEGORY_TRANSFORMATION = 'transformation'
    CATEGORY_VISUALIZATION = 'visualization'
    CATEGORY_CHOICES = [
        (CATEGORY_TRANSFORMATION, 'Transformation'),
        (CATEGORY_VISUALIZATION, 'Visualization'),
    ]
    # Default category per source_type. Used at row creation time and by the
    # data migration that backfills existing rows.
    DEFAULT_CATEGORY_BY_TYPE = {
        'powerbi_fabric': CATEGORY_VISUALIZATION,
        'dbt':            CATEGORY_TRANSFORMATION,
        'postgresql':     CATEGORY_TRANSFORMATION,
        'mysql':          CATEGORY_TRANSFORMATION,
        'snowflake':      CATEGORY_TRANSFORMATION,
        'csv_upload':     CATEGORY_TRANSFORMATION,
    }

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='sources')
    source_type = models.CharField(max_length=50, choices=SOURCE_TYPE_CHOICES, default='powerbi_fabric')
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_VISUALIZATION,
        help_text='Pipeline layer. Transformation sources run before visualization sources.',
    )
    name = models.CharField(max_length=255, default='PowerBI Fabric')
    is_active = models.BooleanField(default=True)

    # PowerBI / Fabric credentials
    tenant_id = models.CharField(max_length=255, blank=True, null=True)
    client_id = models.CharField(max_length=255, blank=True, null=True)
    client_secret = models.CharField(max_length=500, blank=True, null=True)
    workspace_ids = models.JSONField(default=list, blank=True)
    # Org-level default workspace for this source. Used as a fallback when the
    # current user has not picked their own default in user settings.
    default_workspace_id = models.CharField(max_length=255, blank=True, null=True)

    # dbt / GitHub credentials
    github_repo_url = models.URLField(max_length=500, blank=True, null=True,
        help_text='GitHub repository URL (e.g. https://github.com/org/dbt-project)')
    github_token = models.CharField(max_length=500, blank=True, null=True,
        help_text='GitHub Personal Access Token (for private repos)')
    github_branch = models.CharField(max_length=100, default='main', blank=True, null=True,
        help_text='Branch to pull artifacts from')
    dbt_manifest_path = models.CharField(max_length=500, default='target/manifest.json',
        blank=True, null=True, help_text='Path to manifest.json in the repository')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.organization.name})"


class SourceSchedule(models.Model):
    FREQUENCY_CHOICES = [
        ('manual', 'Manual Only'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('custom', 'Custom Cron'),
    ]

    source = models.OneToOneField(IntegrationSource, on_delete=models.CASCADE, related_name='schedule')
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default='manual')
    cron_expression = models.CharField(max_length=100, blank=True, null=True, help_text='e.g. 0 2 * * *')
    is_enabled = models.BooleanField(default=False)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Schedule for {self.source.name}"


class SourceRunLog(models.Model):
    STATUS_CHOICES = [
        ('running', 'Running'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    source = models.ForeignKey(IntegrationSource, on_delete=models.CASCADE, related_name='run_logs')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='running')
    log_output = models.TextField(blank=True, null=True)
    triggered_by = models.CharField(max_length=50, default='manual', help_text='manual or scheduler')

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"Run {self.started_at} — {self.status}"


class IntegrationDestination(models.Model):
    DESTINATION_TYPE_CHOICES = [
        ('bigquery', 'Google BigQuery'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='destinations')
    destination_type = models.CharField(max_length=50, choices=DESTINATION_TYPE_CHOICES, default='bigquery')
    name = models.CharField(max_length=255, default='BigQuery')
    is_active = models.BooleanField(default=False)

    # BigQuery config
    bq_project_id = models.CharField(max_length=255, blank=True, null=True)
    bq_dataset_id = models.CharField(max_length=255, blank=True, null=True)
    bq_service_account_json = models.TextField(blank=True, null=True, help_text='Full GCP service account JSON')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.organization.name})"


class DestinationSchedule(models.Model):
    FREQUENCY_CHOICES = [
        ('manual', 'Manual Only'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('custom', 'Custom Cron'),
    ]

    destination = models.OneToOneField(IntegrationDestination, on_delete=models.CASCADE, related_name='schedule')
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default='manual')
    cron_expression = models.CharField(max_length=100, blank=True, null=True, help_text='e.g. 0 2 * * *')
    is_enabled = models.BooleanField(default=False)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Schedule for {self.destination.name}"


class DestinationRunLog(models.Model):
    STATUS_CHOICES = [
        ('running', 'Running'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    destination = models.ForeignKey(IntegrationDestination, on_delete=models.CASCADE, related_name='run_logs')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='running')
    log_output = models.TextField(blank=True, null=True)
    triggered_by = models.CharField(max_length=50, default='manual', help_text='manual or scheduler')

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"Run {self.started_at} — {self.status}"


class IntegrationHook(models.Model):
    HOOK_TYPE_CHOICES = [
        ('slack', 'Slack'),
        ('slack_alerts', 'Slack Alerts'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='hooks')
    hook_type = models.CharField(max_length=50, choices=HOOK_TYPE_CHOICES, default='slack')
    name = models.CharField(max_length=255, default='Slack')
    is_active = models.BooleanField(default=False)

    # Slack config (mirrors Organization.slack_bot_token for backward compat)
    slack_bot_token = models.CharField(max_length=500, blank=True, null=True)
    slack_channel = models.CharField(max_length=255, blank=True, null=True)
    # Alerts-specific channel (separate from bot channel)
    slack_alerts_channel = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.organization.name})"

class WorkflowRun(models.Model):
    """Tracks an end-to-end pipeline execution: init → sources → final → destinations."""
    STAGE_CHOICES = [
        ('pending', 'Pending'),
        ('init', 'Initializing'),
        ('sources', 'Running Sources'),
        ('final', 'Final Step'),
        ('destinations', 'Running Destinations'),
        ('done', 'Done'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='workflow_runs')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    current_stage = models.CharField(max_length=20, choices=STAGE_CHOICES, default='pending')
    triggered_by = models.CharField(max_length=50, default='manual')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    log_output = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"Workflow {self.started_at} — {self.status} ({self.current_stage})"


class WorkflowSchedule(models.Model):
    FREQUENCY_CHOICES = [
        ('manual', 'Manual Only'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('custom', 'Custom Cron'),
    ]

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name='workflow_schedule')
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default='manual')
    cron_expression = models.CharField(max_length=100, blank=True, null=True)
    is_enabled = models.BooleanField(default=False)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Workflow schedule for {self.organization.name}"


class WorkflowRawExport(models.Model):
    """When active, every source's raw extracted files are zipped and uploaded
    to GCS once extraction finishes. One zip per source per workflow run."""
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name='workflow_raw_export')
    is_active = models.BooleanField(default=False)
    gcs_bucket_name = models.CharField(max_length=255, blank=True, null=True)
    gcs_service_account_json = models.TextField(blank=True, null=True,
        help_text='Full GCP service account JSON with Cloud Storage access')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Workflow raw export for {self.organization.name}"


class OrganizationMembership(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='memberships')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memberships')
    is_admin = models.BooleanField(default=False)

    class Meta:
        unique_together = ('user', 'organization')

    def __str__(self):
        return f"{self.user.username} in {self.organization.name}"


class UserActivityLog(models.Model):
    EVENT_CHOICES = [
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('login_failed', 'Login Failed'),
        ('pageview', 'Page View'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='activity_logs',
    )
    # Captured separately so failed logins (no user) and deleted users are still searchable.
    email = models.CharField(max_length=255, blank=True, default='')
    event = models.CharField(max_length=20, choices=EVENT_CHOICES)
    path = models.CharField(max_length=500, blank=True, default='')
    method = models.CharField(max_length=10, blank=True, default='')
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['-timestamp']),
            models.Index(fields=['user', '-timestamp']),
            models.Index(fields=['event', '-timestamp']),
        ]

    def __str__(self):
        who = self.email or (self.user.email if self.user_id else 'anonymous')
        return f"{self.timestamp:%Y-%m-%d %H:%M} {who} {self.event} {self.path}"


class PbLiveQueryThread(models.Model):
    """One row per chat thread driving the pb_live_query flow.

    ``stage`` is the state-machine cursor. ``state`` is the merged
    flow state. Pause = persist with ``stage`` set to an awaiting_*
    value; resume = read by ``thread_id`` and dispatch.
    """

    STAGE_PLAN = 'plan'
    STAGE_AWAITING_PICK = 'awaiting_pick'
    STAGE_AWAITING_PLAN_CONFIRM = 'awaiting_plan_confirm'
    STAGE_DONE = 'done'

    thread_id = models.CharField(max_length=64, primary_key=True)
    stage = models.CharField(max_length=32, default=STAGE_PLAN)
    state = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['updated_at'])]

    def __str__(self):
        return f'{self.thread_id} @ {self.stage}'


class MetricsMap(models.Model):
    """A curated, org-scoped collection of metric definitions — a "metrics map".

    Authored in the React "Metrics Map" scratchpad. Each map is a single named
    document whose ``metrics`` JSON holds a list of metric definitions, each a
    dict like::

        {"name", "table", "type", "format", "displayFolder",
         "description", "expression"}

    Metrics can be hand-written or seeded from catalog measures (PB_MEASURE
    Items already in the DB), and the whole map round-trips to/from YAML for
    import/export. Stored inline as JSON because a map is one editable document
    — there's no need for a separate per-metric table.

    The same model also backs the visual **canvas** editor (``kind='canvas'``):
    those rows leave ``metrics`` empty and store the whole diagram document
    (nodes / edges / groups / viewport / meta) in ``graph``. The ``kind``
    discriminator keeps the scratchpad list and the canvas list separate.
    """

    KIND_SCRATCHPAD = 'scratchpad'
    KIND_CANVAS = 'canvas'
    KIND_CHOICES = [
        (KIND_SCRATCHPAD, 'Scratchpad'),
        (KIND_CANVAS, 'Canvas'),
    ]

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_SCRATCHPAD)
    metrics = models.JSONField(default=list, blank=True)
    # Full canvas document for kind='canvas' maps; null for scratchpad maps.
    graph = models.JSONField(null=True, blank=True)

    # Public sharing. When sharing is enabled (via the `share` API action) this is
    # set to a fresh uuid4 — the unguessable key in the anonymous share URL
    # (/share/metrics-map/<token>). Null = not shared; regenerating rotates it,
    # stopping sharing clears it. `unique` keeps the public lookup 1:1.
    public_token = models.UUIDField(
        null=True, blank=True, unique=True, db_index=True, editable=False,
        help_text='Set when public sharing is enabled; the anonymous share-URL key. Null = not shared.',
    )
    # Whether anonymous viewers of the shared link may drag nodes around. Their
    # rearrangements are purely local/ephemeral (no write path), this just toggles
    # whether the read-only viewer lets them move things at all.
    public_can_drag = models.BooleanField(
        default=True,
        help_text='Whether anonymous viewers of the shared link may drag nodes (never persisted).',
    )

    organization = models.ForeignKey(
        'Organization', null=True, blank=True,
        on_delete=models.CASCADE, related_name='metrics_maps',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='metrics_maps',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['organization', '-updated_at'], name='cat_metricsmap_org_idx'),
        ]

    def __str__(self):
        return self.name or f'Metrics map #{self.pk}'
