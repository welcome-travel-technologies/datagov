from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.html import format_html
from .models import (
    CustomUser, Department, DataPerson, Summary,
    Item, ItemGroup, NetworkNode, NetworkEdge, Category,
    ChatSession, ChatMessage, Organization, OrganizationMembership,
    IntegrationSource, SourceSchedule, SourceRunLog,
    IntegrationDestination, IntegrationHook,
    UserActivityLog, StatusChangeLog, MetricsMap,
)

admin.site.register(CustomUser, UserAdmin)

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization')
    list_filter = ('organization',)

@admin.register(DataPerson)
class DataPersonAdmin(admin.ModelAdmin):
    list_display = ('name', 'department_list', 'organization', 'is_owner', 'is_steward', 'is_other', 'slack_handle', 'user')
    list_filter = ('organization', 'departments', 'is_owner', 'is_steward', 'is_other')
    search_fields = ('name', 'slack_handle', 'user__email')
    autocomplete_fields = ('user',)
    filter_horizontal = ('departments',)

    def department_list(self, obj):
        return ", ".join(obj.departments.values_list('name', flat=True)) or '-'
    department_list.short_description = 'Departments'

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization')
    list_filter = ('organization',)

@admin.register(Summary)
class SummaryAdmin(admin.ModelAdmin):
    list_display = ('organization', 'total_measures', 'total_columns', 'total_reports')
    list_filter = ('organization',)
@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    # Governance now lives on item_group; the columns below are read-proxies.
    list_display = ('item_name', 'item_type', 'organization', 'category', 'ownership_person', 'steward', 'status', 'deleted')
    # `status` is now a real (synced) column on Item, so filter on it directly.
    list_filter = ('organization', 'item_type', 'status', 'deleted',
                   'item_group__category', 'item_group__ownership_department')
    search_fields = ('item_name', 'item_id')
    list_select_related = (
        'organization', 'item_group', 'item_group__category',
        'item_group__ownership_department', 'item_group__ownership_person',
        'item_group__steward',
    )
    raw_id_fields = ('item_group',)


@admin.register(ItemGroup)
class ItemGroupAdmin(admin.ModelAdmin):
    list_display = ('group_key', 'kind', 'organization', 'category',
                    'ownership_person', 'steward', 'status')
    list_filter = ('kind', 'organization', 'status', 'category', 'ownership_department')
    search_fields = ('group_key',)
    list_select_related = ('organization', 'category', 'ownership_department',
                           'ownership_person', 'steward', 'primary_item')
    raw_id_fields = ('ownership_person', 'steward', 'primary_item')


admin.site.register(NetworkNode)
admin.site.register(NetworkEdge)
admin.site.register(ChatSession)
admin.site.register(ChatMessage)

class OrganizationMembershipInline(admin.TabularInline):
    model = OrganizationMembership
    extra = 1

@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'icon_preview', 'color_swatch', 'powerbi_tools_enabled',
                    'powerbi_live_tools_enabled', 'dbt_tools_enabled',
                    'bigquery_tools_enabled', 'bigquery_live_tools_enabled')
    list_editable = ('powerbi_tools_enabled', 'powerbi_live_tools_enabled',
                     'dbt_tools_enabled', 'bigquery_tools_enabled',
                     'bigquery_live_tools_enabled')
    readonly_fields = ('icon_preview',)
    fieldsets = (
        ('Branding', {
            'fields': ('name', 'primary_color', 'icon', 'icon_preview'),
            'description': 'Name, accent colour (hex, e.g. #00cf95) and logo icon '
                           'shown across the app. Upload a square PNG/SVG; it is '
                           'used in the sidebar, login screen and favicon.',
        }),
        ('AI Assistant', {
            'fields': ('chatbot_model', 'chat_timeout_seconds', 'slack_bot_token',
                       'powerbi_tools_enabled', 'powerbi_live_tools_enabled',
                       'dbt_tools_enabled', 'bigquery_tools_enabled',
                       'bigquery_live_tools_enabled', 'debug_responses_enabled',
                       'show_deleted_items', 'assistant_powerbi_workspace_ids',
                       'assistant_bigquery_dataset_ids'),
            'classes': ('collapse',),
        }),
    )
    inlines = [OrganizationMembershipInline]

    @admin.display(description='Logo')
    def icon_preview(self, obj):
        if obj and obj.icon:
            return format_html(
                '<img src="{}" style="height:32px;width:32px;object-fit:contain;'
                'border-radius:6px;background:#f4f4f5" />', obj.icon.url
            )
        return '—'

    @admin.display(description='Colour')
    def color_swatch(self, obj):
        c = (obj.primary_color or '').strip()
        if not c:
            return '—'
        return format_html(
            '<span style="display:inline-block;width:14px;height:14px;border-radius:3px;'
            'border:1px solid #ccc;vertical-align:middle;background:{}"></span> {}', c, c
        )

@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'organization', 'is_admin')
    list_filter = ('organization', 'is_admin')


class SourceScheduleInline(admin.StackedInline):
    model = SourceSchedule
    extra = 0

class SourceRunLogInline(admin.TabularInline):
    model = SourceRunLog
    extra = 0
    readonly_fields = ('started_at', 'finished_at', 'status', 'triggered_by')
    can_delete = False
    max_num = 10

@admin.register(IntegrationSource)
class IntegrationSourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'source_type', 'organization', 'is_active', 'updated_at')
    list_filter = ('source_type', 'is_active', 'organization')
    inlines = [SourceScheduleInline, SourceRunLogInline]

@admin.register(SourceRunLog)
class SourceRunLogAdmin(admin.ModelAdmin):
    list_display = ('source', 'started_at', 'finished_at', 'status', 'triggered_by')
    list_filter = ('status', 'triggered_by', 'source')
    readonly_fields = ('started_at', 'finished_at', 'log_output')

@admin.register(IntegrationDestination)
class IntegrationDestinationAdmin(admin.ModelAdmin):
    list_display = ('name', 'destination_type', 'organization', 'is_active', 'bq_dataset_id')
    list_filter = ('destination_type', 'is_active', 'organization')

@admin.register(IntegrationHook)
class IntegrationHookAdmin(admin.ModelAdmin):
    list_display = ('name', 'hook_type', 'organization', 'is_active')
    list_filter = ('hook_type', 'is_active', 'organization')


@admin.register(StatusChangeLog)
class StatusChangeLogAdmin(admin.ModelAdmin):
    list_display = ('changed_at', 'group_key', 'old_status', 'new_status', 'changed_by', 'organization')
    list_filter = ('new_status', 'old_status', 'organization', 'changed_at')
    search_fields = ('group_key', 'changed_by__email')
    readonly_fields = ('item_group', 'group_key', 'old_status', 'new_status',
                       'changed_by', 'changed_at', 'organization')
    list_select_related = ('item_group', 'changed_by', 'organization')
    date_hierarchy = 'changed_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MetricsMap)
class MetricsMapAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization', 'metric_count', 'created_by', 'updated_at')
    list_filter = ('organization', 'updated_at')
    search_fields = ('name', 'description')
    readonly_fields = ('created_by', 'created_at', 'updated_at')
    list_select_related = ('organization', 'created_by')
    date_hierarchy = 'updated_at'

    def metric_count(self, obj):
        return len(obj.metrics) if isinstance(obj.metrics, list) else 0
    metric_count.short_description = 'Metrics'


@admin.register(UserActivityLog)
class UserActivityLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'email', 'event', 'path', 'status_code', 'ip')
    list_filter = ('event', 'timestamp')
    search_fields = ('email', 'path', 'ip', 'user__email', 'user__username')
    readonly_fields = ('user', 'email', 'event', 'path', 'method', 'status_code', 'ip', 'user_agent', 'timestamp')
    list_select_related = ('user',)
    date_hierarchy = 'timestamp'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
