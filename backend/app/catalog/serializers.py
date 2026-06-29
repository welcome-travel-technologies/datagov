from rest_framework import serializers
from .models import Summary, Item, ItemGroup, Department, DataPerson, Category, GovernanceTask, MetricsMap

class SummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Summary
        fields = '__all__'

class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = '__all__'

class DataPersonSerializer(serializers.ModelSerializer):
    # M2M: surface the list of department IDs as `departments`, plus a
    # parallel list of names for display. Frontend uses `departments` to
    # filter the per-row dropdowns by membership.
    department_names = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email', read_only=True, default=None)
    class Meta:
        model = DataPerson
        fields = '__all__'

    def get_department_names(self, obj):
        return list(obj.departments.values_list('name', flat=True))

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = '__all__'


class MetricsMapSerializer(serializers.ModelSerializer):
    """Serializes a metrics map. ``organization`` and ``created_by`` are set
    server-side from the request user (see MetricsMapViewSet) so they can't be
    spoofed from the payload; ``metric_count`` is a convenience for list views."""
    metric_count = serializers.SerializerMethodField()
    created_by_email = serializers.CharField(source='created_by.email', read_only=True, default=None)

    class Meta:
        model = MetricsMap
        fields = '__all__'
        # `public_token` is rotated/cleared only through the dedicated `share`
        # action (never from a raw write payload).
        read_only_fields = ('organization', 'created_by', 'created_at', 'updated_at',
                            'public_token')
        # Canvas maps (kind='canvas') carry their state in `graph` and omit
        # `metrics`; the model default ([]) backfills it.
        extra_kwargs = {'metrics': {'required': False}}

    def get_metric_count(self, obj):
        return len(obj.metrics) if isinstance(obj.metrics, list) else 0

    def validate_metrics(self, value):
        if value in (None, ''):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError('metrics must be a list of metric objects.')
        for i, m in enumerate(value):
            if not isinstance(m, dict):
                raise serializers.ValidationError(f'metrics[{i}] must be an object.')
        return value


class PublicMetricsMapSerializer(serializers.ModelSerializer):
    """The anonymous, read-only projection of a shared metrics map.

    Served by the AllowAny ``metrics_map_public`` view to anyone holding the
    share link. Deliberately narrow: only the diagram (``graph``), its name /
    description, and the viewer-drag toggle — never ``organization``,
    ``created_by``, or ``public_token`` — so a public link can't leak tenant
    metadata or let a viewer enumerate other maps."""

    class Meta:
        model = MetricsMap
        fields = ('name', 'description', 'graph', 'public_can_drag')


class ItemGroupSerializer(serializers.ModelSerializer):
    """Governance is curated here. PATCHing this is how the Data Dictionary
    edits owner / steward / status / category / annotation for a whole
    measure group (or a single-item singleton group)."""
    ownership_department_name = serializers.CharField(source='ownership_department.name', read_only=True, default=None)
    ownership_person_name = serializers.CharField(source='ownership_person.name', read_only=True, default=None)
    ownership_person_slack = serializers.CharField(source='ownership_person.slack_handle', read_only=True, default=None)
    steward_name = serializers.CharField(source='steward.name', read_only=True, default=None)
    steward_slack = serializers.CharField(source='steward.slack_handle', read_only=True, default=None)
    category_name = serializers.CharField(source='category.name', read_only=True, default=None)

    class Meta:
        model = ItemGroup
        fields = '__all__'
        # The key and kind are derived by the ETL / migration — never edited
        # through the API. primary_item is changed via the set_primary action.
        # deleted_at is driven by the status coupling (DELETED <-> stamped),
        # never set directly through the API.
        read_only_fields = ('group_key', 'kind', 'organization', 'created_at', 'updated_at', 'deleted_at')


class ItemSerializer(serializers.ModelSerializer):
    # Governance now lives on ItemGroup; expose it on the item under the same
    # keys the frontend already uses (read-only — writes go to the group).
    ownership_department = serializers.IntegerField(source='item_group.ownership_department_id', read_only=True, default=None)
    ownership_person = serializers.IntegerField(source='item_group.ownership_person_id', read_only=True, default=None)
    steward = serializers.IntegerField(source='item_group.steward_id', read_only=True, default=None)
    category = serializers.IntegerField(source='item_group.category_id', read_only=True, default=None)
    # `status` is a real (denormalized) column, exposed as a writable field
    # (validated against Item.STATUS_CHOICES by ModelSerializer). A write here
    # is ROUTED to the item's ItemGroup by ItemViewSet (the group stays the
    # single source of truth) and cascaded back to every item — so acting on an
    # item updates the whole group and fires one alert/task.
    custom_description = serializers.CharField(source='item_group.custom_description', read_only=True, default=None, allow_null=True)

    ownership_department_name = serializers.CharField(source='item_group.ownership_department.name', read_only=True, default=None)
    ownership_person_name = serializers.CharField(source='item_group.ownership_person.name', read_only=True, default=None)
    ownership_person_slack = serializers.CharField(source='item_group.ownership_person.slack_handle', read_only=True, default=None)
    steward_name = serializers.CharField(source='item_group.steward.name', read_only=True, default=None)
    steward_slack = serializers.CharField(source='item_group.steward.slack_handle', read_only=True, default=None)
    category_name = serializers.CharField(source='item_group.category.name', read_only=True, default=None)
    organization_name = serializers.CharField(source='organization.name', read_only=True, default=None)

    # Group linkage for the Data Dictionary: `group` is the ItemGroup pk to
    # PATCH for governance edits; `is_primary` flags the group's primary item.
    group = serializers.IntegerField(source='item_group_id', read_only=True, default=None)
    group_kind = serializers.CharField(source='item_group.kind', read_only=True, default=None)
    is_primary = serializers.SerializerMethodField()

    type = serializers.CharField(source='item_type', read_only=True)
    is_used = serializers.SerializerMethodField()

    def get_is_used(self, obj):
        return not obj.is_unused

    def get_is_primary(self, obj):
        return bool(obj.item_group_id and obj.item_group.primary_item_id == obj.pk)

    class Meta:
        model = Item
        fields = '__all__'


class GovernanceTaskSerializer(serializers.ModelSerializer):
    """Read-mostly task feed for the Task Manager page. Tasks are created by the
    backend on status changes; the only client write is the `done` action."""
    assignee_name = serializers.CharField(source='assignee.name', read_only=True, default=None)
    assignee_slack = serializers.CharField(source='assignee.slack_handle', read_only=True, default=None)
    item_name = serializers.SerializerMethodField()
    asset_context = serializers.SerializerMethodField()
    web_url = serializers.SerializerMethodField()

    class Meta:
        model = GovernanceTask
        fields = (
            'id', 'title', 'state', 'trigger_status', 'created_at', 'completed_at',
            'item_group', 'assignee', 'assignee_name', 'assignee_slack', 'assignee_role',
            'item_name', 'asset_context', 'web_url',
        )

    def _rep(self, obj):
        grp = obj.item_group
        if grp is None:
            return None
        return grp.primary_item or grp.items.first()

    def get_item_name(self, obj):
        rep = self._rep(obj)
        if rep is not None:
            return rep.item_name or rep.item_id
        return obj.item_group.group_key if obj.item_group else None

    def get_asset_context(self, obj):
        rep = self._rep(obj)
        if rep is None:
            return None
        return f"{rep.workspace_name or '—'} / {rep.dataset_name or '—'} / {rep.table_name or '—'}"

    def get_web_url(self, obj):
        rep = self._rep(obj)
        return getattr(rep, 'web_url', None) if rep is not None else None
