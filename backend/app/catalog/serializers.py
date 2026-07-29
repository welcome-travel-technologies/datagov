from rest_framework import serializers
from .models import (
    Summary, Item, ItemGroup, Department, DataPerson, Category, Definition,
    GovernanceTask, MetricsMap,
)

class SummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Summary
        fields = '__all__'

class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = '__all__'
        read_only_fields = ('organization',)

    def validate(self, attrs):
        from .access import resolve_org

        request = self.context.get('request')
        org = resolve_org(request.user) if request else None
        if org is None:
            raise serializers.ValidationError(
                {'organization': 'Organization membership is required.'})
        if self.instance is not None and self.instance.organization_id != org.id:
            raise serializers.ValidationError(
                {'organization': 'This department does not belong to your organization.'})
        return attrs

class DataPersonSerializer(serializers.ModelSerializer):
    # M2M: surface the list of department IDs as `departments`, plus a
    # parallel list of names for display. Frontend uses `departments` to
    # filter the per-row dropdowns by membership.
    department_names = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email', read_only=True, default=None)
    class Meta:
        model = DataPerson
        fields = '__all__'
        read_only_fields = ('organization',)

    def get_department_names(self, obj):
        return [
            department.name
            for department in obj.departments.all()
            if department.organization_id == obj.organization_id
        ]

    def to_representation(self, instance):
        """Never serialize a corrupt cross-tenant M2M department link."""
        data = super().to_representation(instance)
        departments = [
            department
            for department in instance.departments.all()
            if department.organization_id == instance.organization_id
        ]
        data['departments'] = [department.pk for department in departments]
        data['department_names'] = [
            department.name for department in departments
        ]
        return data

    def validate_name(self, value):
        """Mirror the normalized database constraint as a readable API 400."""
        from django.db.models.functions import Lower, Trim
        from .access import resolve_org

        name = (value or '').strip()
        if not name:
            raise serializers.ValidationError('A name is required.')

        request = self.context.get('request')
        org = resolve_org(request.user) if request else None
        clash = (
            DataPerson.objects
            .filter(organization=org)
            .annotate(_normalized_name=Lower(Trim('name')))
            .filter(_normalized_name=name.lower())
        )
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                f'A person named "{name}" already exists in this organization.'
            )
        return name

    def validate(self, attrs):
        from .access import resolve_org
        from .models import OrganizationMembership

        request = self.context.get('request')
        org = resolve_org(request.user) if request else None
        if org is None:
            raise serializers.ValidationError(
                {'organization': 'Organization membership is required.'})
        if self.instance is not None and self.instance.organization_id != org.id:
            raise serializers.ValidationError(
                {'organization': 'This person does not belong to your organization.'})

        departments = attrs.get('departments')
        if departments is not None:
            foreign = [d.pk for d in departments if d.organization_id != org.id]
            if foreign:
                raise serializers.ValidationError({
                    'departments': 'Every department must belong to your organization.'
                })

        person_user = attrs.get('user')
        if person_user is not None:
            if not OrganizationMembership.objects.filter(
                organization=org, user=person_user,
            ).exists():
                raise serializers.ValidationError({
                    'user': 'The linked user must belong to your organization.'
                })
            linked = DataPerson.objects.filter(
                user=person_user, organization=org,
            )
            if self.instance is not None:
                linked = linked.exclude(pk=self.instance.pk)
            if linked.exists():
                raise serializers.ValidationError({
                    'user': 'This login is already linked to a data person.'
                })
        return attrs

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = '__all__'
        read_only_fields = ('organization',)

    def validate_name(self, value):
        name = (value or '').strip()
        if not name:
            raise serializers.ValidationError('A name is required.')
        if name.casefold() == 'to be deleted':
            raise serializers.ValidationError(
                '"To Be Deleted" is a reserved lifecycle status, not a category.'
            )
        return name

    def validate(self, attrs):
        from .access import resolve_org

        request = self.context.get('request')
        org = resolve_org(request.user) if request else None
        if org is None:
            raise serializers.ValidationError(
                {'organization': 'Organization membership is required.'})
        if self.instance is not None and self.instance.organization_id != org.id:
            raise serializers.ValidationError(
                {'organization': 'This category does not belong to your organization.'})
        return attrs


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
    definition_name = serializers.CharField(source='definition.name', read_only=True, default=None)

    class Meta:
        model = ItemGroup
        fields = '__all__'
        # The key and kind are derived by the ETL / migration — never edited
        # through the API. primary_item is changed via the set_primary action.
        # deleted_at is driven by the status coupling (DELETED <-> stamped),
        # never set directly through the API.
        read_only_fields = (
            'group_key', 'kind', 'organization', 'primary_item',
            'created_at', 'updated_at', 'deleted_at',
        )

    def validate(self, attrs):
        """Every governance relation must stay inside the caller's tenant."""
        from .access import resolve_org

        request = self.context.get('request')
        org = resolve_org(request.user) if request else None
        if org is None:
            raise serializers.ValidationError(
                {'organization': 'Organization membership is required.'})
        if self.instance is not None and self.instance.organization_id != org.id:
            raise serializers.ValidationError(
                {'organization': 'This group does not belong to your organization.'})
        if (
            'definition' in attrs
            and attrs.get('definition') is not None
            and self.instance is not None
            and self.instance.kind != ItemGroup.KIND_MEASURE_NAME
        ):
            raise serializers.ValidationError({
                'definition': 'Definitions may contain measure groups only.'
            })

        for field in (
            'definition', 'ownership_department', 'ownership_person',
            'steward', 'category',
        ):
            related = attrs.get(field)
            if related is not None and related.organization_id != org.id:
                raise serializers.ValidationError({
                    field: 'The selected value must belong to your organization.'
                })
        return attrs


class ItemSerializer(serializers.ModelSerializer):
    # Governance now lives on ItemGroup; expose it on the item under the same
    # keys the frontend already uses (read-only — writes go to the group).
    ownership_department = serializers.IntegerField(source='item_group.ownership_department_id', read_only=True, default=None)
    ownership_person = serializers.IntegerField(source='item_group.ownership_person_id', read_only=True, default=None)
    steward = serializers.IntegerField(source='item_group.steward_id', read_only=True, default=None)
    category = serializers.IntegerField(source='item_group.category_id', read_only=True, default=None)
    definition = serializers.IntegerField(source='item_group.definition_id', read_only=True, default=None)
    definition_name = serializers.CharField(source='item_group.definition.name', read_only=True, default=None)
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
        # Item rows are ingestion-owned. ``status`` is the sole legacy write
        # surface and the view routes it through ItemGroup, which remains the
        # source of truth. In particular, item-level ``deleted`` is never
        # accepted because deletion is a group lifecycle operation.
        read_only_fields = tuple(
            field.name for field in Item._meta.fields
            if field.name != 'status'
        )


class DefinitionSerializer(serializers.ModelSerializer):
    """A business definition plus the names needed to render it without extra
    round trips. ``organization`` is stamped server-side, never from the body."""
    ownership_person_name = serializers.CharField(
        source='ownership_person.name', read_only=True, default=None)
    ownership_department_name = serializers.CharField(
        source='ownership_department.name', read_only=True, default=None)
    group_count = serializers.SerializerMethodField()

    class Meta:
        model = Definition
        fields = (
            'id', 'name', 'description',
            'ownership_department', 'ownership_department_name',
            'ownership_person', 'ownership_person_name',
            'group_count', 'created_at', 'updated_at',
        )
        read_only_fields = ('created_at', 'updated_at')

    def get_group_count(self, obj):
        # Annotated by the viewset; fall back to a query for single-object reads.
        cached = getattr(obj, 'group_count_annotated', None)
        return (
            cached
            if cached is not None
            else obj.item_groups.filter(
                kind=ItemGroup.KIND_MEASURE_NAME,
                organization=obj.organization,
            ).count()
        )

    def validate_name(self, value):
        """Reject a duplicate name with a readable 400.

        ``uniq_definition_name_org`` is an *expression* constraint
        (``Lower('name')``), and DRF's automatic uniqueness validation only
        covers ``unique_together`` — so without this the clash surfaces as an
        IntegrityError, i.e. a 500.
        """
        from django.db.models.functions import Lower, Trim
        from .models import Definition
        from .access import resolve_org

        name = (value or '').strip()
        if not name:
            raise serializers.ValidationError('A name is required.')

        request = self.context.get('request')
        org = resolve_org(request.user) if request else None
        clash = (
            Definition.objects
            .filter(organization=org)
            .annotate(_normalized_name=Lower(Trim('name')))
            .filter(_normalized_name=name.lower())
        )
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                f'A definition named "{name}" already exists in this organization.')
        return name

    def validate(self, attrs):
        from .access import resolve_org

        request = self.context.get('request')
        org = resolve_org(request.user) if request else None
        if org is None:
            raise serializers.ValidationError(
                {'organization': 'Organization membership is required.'})
        if self.instance is not None and self.instance.organization_id != org.id:
            raise serializers.ValidationError(
                {'organization': 'This definition does not belong to your organization.'})
        for field in ('ownership_person', 'ownership_department'):
            related = attrs.get(field)
            if related is not None and related.organization_id != org.id:
                raise serializers.ValidationError({
                    field: 'The selected value must belong to your organization.'
                })
        return attrs


class GovernanceTaskSerializer(serializers.ModelSerializer):
    """Read-mostly task feed for the Task Manager page. Tasks are created by the
    backend (status change, or the governance sweep); the only client writes are
    the `done` / `bulk-done` actions."""
    assignee_name = serializers.CharField(source='assignee.name', read_only=True, default=None)
    assignee_slack = serializers.CharField(source='assignee.slack_handle', read_only=True, default=None)
    reason_label = serializers.SerializerMethodField()
    item_name = serializers.SerializerMethodField()
    asset_context = serializers.SerializerMethodField()
    web_url = serializers.SerializerMethodField()
    age_days = serializers.SerializerMethodField()

    class Meta:
        model = GovernanceTask
        fields = (
            'id', 'title', 'state', 'reason', 'reason_label', 'trigger_status',
            'created_at', 'completed_at', 'condition_cleared_at', 'closed_reason',
            'item_group', 'assignee', 'assignee_name', 'assignee_slack', 'assignee_role',
            'item_name', 'asset_context', 'web_url', 'age_days',
        )
        read_only_fields = fields

    def get_reason_label(self, obj):
        from .governance_tasks import reason_label
        return reason_label(obj.reason)

    def get_age_days(self, obj):
        """Whole days since the task opened — drives the ageing badge. No due
        date field: age answers "is this rotting" without anyone having to
        define what a deadline means for governance work."""
        if not obj.created_at:
            return None
        from django.utils import timezone
        return max(0, (timezone.now() - obj.created_at).days)

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
