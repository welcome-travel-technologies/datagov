from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ItemViewSet, ItemGroupViewSet, DepartmentViewSet, DataPersonViewSet, CategoryViewSet,
    GovernanceTaskViewSet, MetricsMapViewSet, metrics_map_public,
    get_summary, get_dashboard, get_network, find_network_path, get_network_reachable,
    get_chat_sessions, get_chat_messages,
    delete_chat_session, chat_api_view, get_chat_task_status, get_filters, pb_cleanup_counts, dbt_insights,
    powerbi_usage,
    integrations_get_all, integrations_save_source,
    integrations_run_source_now, integrations_test_source,
    integrations_get_run_logs, integrations_get_run_log_detail,
    integrations_save_destination, integrations_run_destination_now, integrations_test_destination,
    integrations_get_dest_logs, integrations_get_dest_log_detail,
    integrations_kill_dest_run, integrations_delete_dest_run,
    integrations_save_hook, integrations_kill_run, integrations_delete_run,
    workflow_get_status, workflow_run_now, workflow_get_run_detail,
    workflow_save_schedule, workflow_kill_run, workflow_delete_run,
    workflow_toggle_step, integrations_clean_logs,
    workflow_save_raw_export, workflow_test_raw_export,
    governance_export_csv, governance_import_csv,
)
from .slack_views import slack_events, slack_oauth, slack_alerts_oauth
from .spa_auth import (
    me_view, branding_view, login_view, logout_view, change_password_view, me_workspaces_view,
    org_members_view, org_members_save_view, org_members_remove_view,
    org_settings_save_view, org_queues_view, org_queue_task_kill_view,
    org_assistant_scope_view,
)

router = DefaultRouter()
router.register(r'items', ItemViewSet)
router.register(r'item-groups', ItemGroupViewSet, basename='item-group')
router.register(r'departments', DepartmentViewSet, basename='department')
router.register(r'data-persons', DataPersonViewSet, basename='data-person')
# Legacy alias — kept so existing frontend JS keeps working without a coordinated
# template/JS deploy. New callers should use /api/data-persons/.
router.register(r'owners', DataPersonViewSet, basename='owner')
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'tasks', GovernanceTaskViewSet, basename='task')
router.register(r'metrics-maps', MetricsMapViewSet, basename='metrics-map')

urlpatterns = [
    # Anonymous, read-only share endpoint. Declared before the router include so
    # it can never be shadowed by the viewset's detail route (three path segments
    # vs the router's single-segment `metrics-maps/<pk>/`, so no real conflict).
    path('metrics-maps/public/<uuid:token>/', metrics_map_public, name='metrics-map-public'),
    path('', include(router.urls)),
    # SPA session auth (React frontend)
    path('me/', me_view, name='api-me'),
    path('branding/', branding_view, name='api-branding'),
    path('me/change-password/', change_password_view, name='api-change-password'),
    path('me/workspaces/', me_workspaces_view, name='api-me-workspaces'),
    path('auth/login/', login_view, name='api-auth-login'),
    path('auth/logout/', logout_view, name='api-auth-logout'),
    # SPA org-admin management (member CRUD + settings)
    path('org/members/', org_members_view, name='api-org-members'),
    path('org/members/save/', org_members_save_view, name='api-org-members-save'),
    path('org/members/remove/', org_members_remove_view, name='api-org-members-remove'),
    path('org/settings/', org_settings_save_view, name='api-org-settings-save'),
    path('org/assistant-scope/', org_assistant_scope_view, name='api-org-assistant-scope'),
    path('org/queues/', org_queues_view, name='api-org-queues'),
    path('org/queues/<int:ormq_id>/kill/', org_queue_task_kill_view, name='api-org-queue-kill'),
    path('summary/', get_summary, name='api-summary'),
    path('dashboard/', get_dashboard, name='api-dashboard'),
    path('filters/', get_filters, name='api-filters'),
    path('pb-cleanup-counts/', pb_cleanup_counts, name='api-pb-cleanup-counts'),
    path('dbt-insights/', dbt_insights, name='api-dbt-insights'),
    path('powerbi-usage/', powerbi_usage, name='api-powerbi-usage'),
    path('network/', get_network, name='api-network'),
    path('network/path/', find_network_path, name='api-network-path'),
    path('network/reachable/', get_network_reachable, name='api-network-reachable'),
    path('chat/', chat_api_view, name='chat-api'),
    path('chat/sessions/', get_chat_sessions, name='chat-sessions'),
    path('chat/sessions/<int:session_id>/messages/', get_chat_messages, name='chat-messages'),
    path('chat/sessions/<int:session_id>/', delete_chat_session, name='delete-chat-session'),
    path('chat/task/<str:task_id>/', get_chat_task_status, name='chat-task-status'),
    # Integrations
    path('integrations/', integrations_get_all, name='integrations-get-all'),
    path('integrations/sources/save/', integrations_save_source, name='integrations-save-source'),
    path('integrations/sources/<int:source_id>/run/', integrations_run_source_now, name='integrations-run-source'),
    path('integrations/sources/<int:source_id>/test/', integrations_test_source, name='integrations-test-source'),
    path('integrations/sources/<int:source_id>/logs/', integrations_get_run_logs, name='integrations-run-logs'),
    path('integrations/logs/<int:log_id>/', integrations_get_run_log_detail, name='integrations-log-detail'),
    path('integrations/logs/<int:log_id>/kill/', integrations_kill_run, name='integrations-kill-run'),
    path('integrations/logs/<int:log_id>/delete/', integrations_delete_run, name='integrations-delete-run'),
    
    path('integrations/destinations/save/', integrations_save_destination, name='integrations-save-destination'),
    path('integrations/destinations/<int:dest_id>/run/', integrations_run_destination_now, name='integrations-run-dest'),
    path('integrations/destinations/<int:dest_id>/test/', integrations_test_destination, name='integrations-test-dest'),
    path('integrations/destinations/<int:dest_id>/logs/', integrations_get_dest_logs, name='integrations-dest-logs'),
    path('integrations/destinations/logs/<int:log_id>/', integrations_get_dest_log_detail, name='integrations-dest-log-detail'),
    path('integrations/destinations/logs/<int:log_id>/kill/', integrations_kill_dest_run, name='integrations-kill-dest-run'),
    path('integrations/destinations/logs/<int:log_id>/delete/', integrations_delete_dest_run, name='integrations-delete-dest-run'),
    
    path('integrations/hooks/save/', integrations_save_hook, name='integrations-save-hook'),
    path('integrations/clean-logs/', integrations_clean_logs, name='integrations-clean-logs'),

    # Workflow
    path('integrations/workflow/', workflow_get_status, name='workflow-status'),
    path('integrations/workflow/run/', workflow_run_now, name='workflow-run'),
    path('integrations/workflow/<int:run_id>/', workflow_get_run_detail, name='workflow-run-detail'),
    path('integrations/workflow/schedule/', workflow_save_schedule, name='workflow-save-schedule'),
    path('integrations/workflow/<int:run_id>/kill/', workflow_kill_run, name='workflow-kill-run'),
    path('integrations/workflow/<int:run_id>/delete/', workflow_delete_run, name='workflow-delete-run'),
    path('integrations/workflow/toggle/', workflow_toggle_step, name='workflow-toggle-step'),
    path('integrations/workflow/raw-export/', workflow_save_raw_export, name='workflow-save-raw-export'),
    path('integrations/workflow/raw-export/test/', workflow_test_raw_export, name='workflow-test-raw-export'),

    # Governance CSV round-trip (Data Dictionary)
    path('governance/export-csv/', governance_export_csv, name='governance-export-csv'),
    path('governance/import-csv/', governance_import_csv, name='governance-import-csv'),

    # Slack
    path('slack/events/', slack_events, name='slack-events'),
    path('slack/oauth/', slack_oauth, name='slack_oauth'),
    path('slack/alerts-oauth/', slack_alerts_oauth, name='slack_alerts_oauth'),
]
