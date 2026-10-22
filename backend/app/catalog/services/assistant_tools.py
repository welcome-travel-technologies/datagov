"""Per-member assistant tool policy, intersected with organization capabilities."""
import functools
import inspect
import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import PermissionDenied

from catalog.models import OrganizationMembership

logger = logging.getLogger(__name__)

# 'analyzer' is the documents tool: the Document Analyzer answers every document question.
TOOL_GROUPS = {
    'analyzer': ('Documents', 'Answer from company documents with the Document Analyzer and cite its sources.'),
    'powerbi': ('Power BI catalog', 'Read measure definitions, reports and usage.'),
    'dbt': ('dbt catalog', 'Read model definitions and dependencies.'),
    'bigquery': ('BigQuery schema', 'Read the configured dataset schemas.'),
    'lineage': ('Catalog lineage', 'Explore connections between catalog assets.'),
    'powerbi_live': ('Live Power BI queries', 'Run read-only DAX against connected models.'),
    'bigquery_live': ('Live BigQuery queries', 'Run read-only SQL against connected datasets.'),
}
# The former document search group, which the Document Analyzer replaced (migration 0069).
LEGACY_DOCUMENTS = 'documents'


def validate_overrides(value):
    """Known tool groups with true/false values. A legacy ``documents`` flag (an older
    client) is dropped; a False one still switches documents off as ``analyzer`` unless
    the same request says otherwise, as migration 0069 did for stored ones."""
    if not isinstance(value, dict) or any(
            (key not in TOOL_GROUPS and key != LEGACY_DOCUMENTS) or type(flag) is not bool
            for key, flag in value.items()):
        raise ValueError('Use known assistant tool names with true or false values.')
    value = dict(value)
    if value.pop(LEGACY_DOCUMENTS, True) is False:
        value.setdefault('analyzer', False)
    return value


def member_policy(org, user):
    """Empty overrides preserve existing defaults; absent memberships deny tools."""
    def rollout_policy(policy):
        if not settings.LUCCY_CHAT_ENABLED:
            policy['analyzer'] = False
        return policy

    if user is None:
        # Legacy system callers use org policy; user-facing callers must bind identity.
        return rollout_policy({key: True for key in TOOL_GROUPS})
    if not org or not user.is_active:
        return {key: False for key in TOOL_GROUPS}
    row = OrganizationMembership.objects.filter(organization=org, user=user).values('assistant_tool_access', 'assistant_tool_preferences').first()
    if row is None:
        return rollout_policy({key: bool(user.is_superuser) for key in TOOL_GROUPS})
    access, preferences = row['assistant_tool_access'], row['assistant_tool_preferences']
    return rollout_policy({key: access.get(key, True) is True and preferences.get(key, True) is True for key in TOOL_GROUPS})


def tool_group(name):
    return {
        'get_lineage': 'lineage',
        'get_pb_item_details': 'powerbi', 'get_pb_usage_analytics': 'powerbi',
        'get_dbt_item_details': 'dbt', 'powerbi_run_dax_query': 'powerbi_live',
        'bigquery_execute_query': 'bigquery_live', 'ask_document_analyzer': 'analyzer',
    }.get(name.removeprefix('safe_'))


def tool_allowed(org, user, name):
    group = tool_group(name)
    if group == 'analyzer' and not settings.LUCCY_CHAT_ENABLED:
        return False
    if user is None:
        return True
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


def _analyzer_available(org, user):
    """False when the analyzer's rule fails: an analyzer problem never breaks the settings page."""
    from document_analyzer.permissions import can_use, org_available
    try:
        return org_available(org) and can_use(user)
    except Exception as exc:  # noqa: BLE001 - fail closed, keep the page
        logger.warning('Document Analyzer access check failed (%s).', type(exc).__name__)
        return False


def tool_settings(org, membership):
    available = {
        'powerbi': org.powerbi_tools_enabled, 'dbt': org.dbt_tools_enabled,
        'bigquery': org.bigquery_tools_enabled or org.bigquery_live_tools_enabled,
        'lineage': True, 'powerbi_live': org.powerbi_live_tools_enabled and org.powerbi_tools_enabled,
        'bigquery_live': org.bigquery_live_tools_enabled,
        # luccy holds one company's corpus: only its bound organization gets the tool,
        # and only for members with page access there (admin or Analytics group).
        'analyzer': settings.LUCCY_CHAT_ENABLED and _analyzer_available(org, membership.user),
    }
    effective = member_policy(org, membership.user)
    return [{'id': key, 'label': label,
             'description': ('Use the Document Analyzer page for document questions.'
                             if key == 'analyzer' and not settings.LUCCY_CHAT_ENABLED else description),
             'allowed': membership.assistant_tool_access.get(key, True),
             'selected': membership.assistant_tool_preferences.get(key, True),
             'available': bool(available[key]),
             'effective': bool(available[key] and effective[key]
                               and (effective['powerbi'] if key == 'powerbi_live' else effective['bigquery'] if key == 'bigquery_live' else True))}
            for key, (label, description) in TOOL_GROUPS.items()]
