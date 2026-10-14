"""Per-member assistant tool policy, intersected with organization capabilities."""
import functools
import inspect

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import PermissionDenied

from catalog.models import OrganizationMembership

TOOL_GROUPS = {
    'documents': ('Document search', 'Search permitted documents and cite their evidence.'),
    'powerbi': ('Power BI catalog', 'Read measure definitions, reports and usage.'),
    'dbt': ('dbt catalog', 'Read model definitions and dependencies.'),
    'bigquery': ('BigQuery schema', 'Read the configured dataset schemas.'),
    'lineage': ('Catalog lineage', 'Explore connections between catalog assets.'),
    'powerbi_live': ('Live Power BI queries', 'Run read-only DAX against connected models.'),
    'bigquery_live': ('Live BigQuery queries', 'Run read-only SQL against connected datasets.'),
}


def validate_overrides(value):
    if not isinstance(value, dict) or any(key not in TOOL_GROUPS or type(flag) is not bool for key, flag in value.items()):
        raise ValueError('Use known assistant tool names with true or false values.')
    return value


def member_policy(org, user):
    """Empty overrides preserve existing defaults; absent memberships deny tools."""
    if user is None:
        # Legacy system callers use org policy; user-facing callers must bind identity.
        return {key: True for key in TOOL_GROUPS}
    if not org or not user.is_active:
        return {key: False for key in TOOL_GROUPS}
    row = OrganizationMembership.objects.filter(organization=org, user=user).values('assistant_tool_access', 'assistant_tool_preferences').first()
    if row is None:
        return {key: bool(user.is_superuser) for key in TOOL_GROUPS}
    access, preferences = row['assistant_tool_access'], row['assistant_tool_preferences']
    return {key: access.get(key, True) is True and preferences.get(key, True) is True for key in TOOL_GROUPS}


def tool_group(name):
    return {
        'search_evidence': 'documents', 'get_lineage': 'lineage',
        'get_pb_item_details': 'powerbi', 'get_pb_usage_analytics': 'powerbi',
        'get_dbt_item_details': 'dbt', 'powerbi_run_dax_query': 'powerbi_live',
        'bigquery_execute_query': 'bigquery_live',
    }.get(name.removeprefix('safe_'))


def tool_allowed(org, user, name):
    if user is None:
        return True
    group = tool_group(name)
    policy = member_policy(org, user)
    if group == 'powerbi_live':
        return policy['powerbi_live'] and policy['powerbi']
    if group == 'bigquery_live':
        return policy['bigquery_live'] and policy['bigquery']
    return bool(group and policy[group])


def bind_tool_policy(func, org, user):
    """Recheck at execution, including changes made after agent construction."""
    def check():
        if not tool_allowed(org, user, func.__name__):
            raise PermissionDenied('This assistant tool is disabled for your account.')
    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            await sync_to_async(check)()
            return await func(*args, **kwargs)
    else:
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            check()
            return func(*args, **kwargs)
    return wrapped


def tool_settings(org, membership):
    available = {
        'documents': bool(settings.DOCUMENT_EXPLORER_ENABLED),
        'powerbi': org.powerbi_tools_enabled, 'dbt': org.dbt_tools_enabled,
        'bigquery': org.bigquery_tools_enabled or org.bigquery_live_tools_enabled,
        'lineage': True, 'powerbi_live': org.powerbi_live_tools_enabled and org.powerbi_tools_enabled,
        'bigquery_live': org.bigquery_live_tools_enabled,
    }
    effective = member_policy(org, membership.user)
    return [{'id': key, 'label': label, 'description': description,
             'allowed': membership.assistant_tool_access.get(key, True),
             'selected': membership.assistant_tool_preferences.get(key, True),
             'available': bool(available[key]),
             'effective': bool(available[key] and effective[key]
                               and (effective['powerbi'] if key == 'powerbi_live' else effective['bigquery'] if key == 'bigquery_live' else True))}
            for key, (label, description) in TOOL_GROUPS.items()]
