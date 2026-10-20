"""Plain-words overview of everything that feeds DataGov, for organization admins.

One row per source as a person thinks of it: each catalog connector, each
import group and uploads. Snapshot collections fold into their group by the
same explicit provenance as ``integrations.content_groups`` (connection id,
else the reviewed import_group label, else the uploads/imports fallback) and
are never inferred from names. Every query is aggregate, so the cost does not
grow with the number of collections. Catalog connectors stay visible when the
documents feature is off; only the document parts are omitted.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone
from itertools import groupby
from zoneinfo import ZoneInfo

from croniter import croniter
from django.conf import settings
from django.db.models import Count, OuterRef, Subquery
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from catalog.models import (IntegrationDestination, IntegrationSource, Item, SourceRunLog, WorkflowRun,
                            WorkflowSchedule)
from .models import Document, IngestionRun, KnowledgeSource
from .permissions import request_org, visible_documents

PROVIDER_LABELS = {'powerbi_fabric': 'Power BI / Fabric', 'dbt': 'dbt / GitHub'}
FETCH_STEPS = {'powerbi_fabric': 'Fetch reports, datasets and measures from Power BI / Fabric',
               'dbt': 'Fetch the dbt project from GitHub'}
DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
CATALOG_STATUS = {'success': 'success', 'failed': 'failed', 'running': 'running', 'pending': 'queued'}
DOCUMENT_STATUS = {'succeeded': 'success', 'partial': 'partial', 'failed': 'failed', 'running': 'running',
                   'cancel_requested': 'running', 'queued': 'queued'}
OVERDUE = timedelta(minutes=30)
HORIZON = timedelta(hours=24)
MAX_EVENTS = 40
# A schedule that fires more often than this in the next day shows once, as "repeats every hour".
REPEAT_AFTER = 4
# Where a problem is fixed: Advanced › Workflow (schedules, runs, on/off) or Advanced › Sources (connection, access).
WORKFLOW, SOURCES = 'workflow', 'sources'
IMPORT_UPDATES = ('Imported export — refreshed only when a new export is imported. '
                  'It does not sync live with the original system.')
WORKFLOW_NOTE = ('Transformation sources such as dbt run before reporting sources such as Power BI. '
                 'If a transformation fails, the reporting step is skipped so reports never load stale data.')
NOT_ACTIVE = 'The schedule is saved but not active. Open its schedule and save it again.'


def describe_cron(cron, zone):
    """Plain words for the schedule shapes the editors produce; a neutral phrase otherwise."""
    parts = (cron or '').split()
    if len(parts) != 5:
        return 'On a custom schedule'
    minute, hour, dom, month, dow = parts
    where = f' ({zone})'
    if minute.isdigit() and hour.isdigit() and month == '*':
        at = f'{int(hour):02d}:{int(minute):02d}'
        if dom == '*' and dow == '*':
            return f'Every day at {at}{where}'
        if dom == '*' and dow.isdigit() and int(dow) <= 7:
            return f'Every {DAYS[int(dow) % 7]} at {at}{where}'
        if dom == '*' and dow.upper() in ('1-5', 'MON-FRI'):
            return f'Every weekday at {at}{where}'
        if dom.isdigit() and dow == '*':
            return f'On day {int(dom)} of every month at {at}{where}'
    if dom == month == dow == '*':
        if minute.isdigit() and hour == '*':
            return 'Every hour' if int(minute) == 0 else f'Every hour at {int(minute):02d} minutes past'
        if minute.startswith('*/') and minute[2:].isdigit() and hour == '*':
            return f'Every {int(minute[2:])} minutes'
        if minute.isdigit() and hour.startswith('*/') and hour[2:].isdigit():
            return f'Every {int(hour[2:])} hours'
    return f'On a custom schedule{where}'


def _lower(text):
    return text[:1].lower() + text[1:]


def _occurrences(cron, zone, start, end, limit):
    try:
        schedule = croniter(cron, start.astimezone(ZoneInfo(zone)))
        result = []
        while len(result) < limit:
            at = schedule.get_next(datetime)
            if at > end:
                break
            result.append(at.astimezone(dt_timezone.utc))
        return result
    except (ValueError, KeyError):
        # Invalid cron/zone: the row reports it; the timeline simply omits it.
        return []


def _upcoming(cron, zone, stored, now, horizon):
    """(times, repeat) in the next day: the stored next run first, since that is what the worker acts on, then the cron.

    A schedule firing more than REPEAT_AFTER times shows once with its plain-words repeat,
    so the strip covers the whole day instead of the first few hours.
    """
    if stored and stored < now - OVERDUE:
        return [], None  # Nothing is running it; the row already says so.
    times = [stored] if stored and now < stored <= horizon else []
    start = stored if stored and stored > now else now
    times += _occurrences(cron, zone, start, horizon, REPEAT_AFTER + 1 - len(times))
    if len(times) > REPEAT_AFTER:
        return times[:1], _lower(describe_cron(cron, zone))
    return times, None


def _whole_blocks(events):
    """Keep at most MAX_EVENTS, cutting only between moments so one workflow run is never shown in half."""
    kept = []
    for _, block in groupby(events, key=lambda event: event[0]):
        block = list(block)
        if kept and len(kept) + len(block) > MAX_EVENTS:
            return kept, True
        kept += block
    return kept, False


def _catalog_zone():
    # django-q evaluates CRON schedules in its own configured zone.
    return getattr(settings, 'Q_CLUSTER', {}).get('time_zone') or settings.TIME_ZONE


def _admin_org(request):
    from catalog.access import is_org_admin, resolve_org
    org = resolve_org(request.user)
    if org is None or not is_org_admin(request.user, org):
        raise PermissionDenied('Integrations require organization admin access.')
    return org


def _documents_enabled(request, org):
    # Document parts follow the document admin policy exactly; their absence
    # never hides the catalog connectors.
    try:
        return request_org(request, manage=True).pk == org.pk
    except (NotFound, PermissionDenied):
        return False


def _group_key(row, org):
    """Same explicit provenance as integrations.content_groups. Never infer from names."""
    if row['integration_id'] and row['integration__organization_id'] == org.pk:
        return f"integration:{row['integration_id']}"
    if row['import_group']:
        return f"import:{row['import_group']}"
    return 'uploads' if row['kind'] == 'upload' else 'imports'


def _has_credentials(connection):
    if connection.source_type == 'dbt':
        return bool(connection.github_repo_url)
    if connection.source_type == 'powerbi_fabric':
        return bool(connection.client_secret and connection.tenant_id and connection.client_id)
    return True


def _registered(q_rows, name, cron, enabled, now):
    """(next_run_at, problem) for a catalog schedule kept in django-q's table."""
    if not (enabled and cron):
        return None, ''
    row = q_rows.get(name)
    if row is None:
        return None, NOT_ACTIVE
    if row['next_run'] and row['next_run'] < now - OVERDUE:
        return row['next_run'], 'A scheduled run is overdue. The background worker may be stopped.'
    return row['next_run'], ''


def _first_problem(checks):
    """(text, where to fix it) of the first check that holds; ('', None) when all is well."""
    return next(((text, where) for holds, text, where in checks if holds), ('', None))


def _document_status(part):
    return DOCUMENT_STATUS.get(part['last_status'], 'success')


def _document_last_run(parts):
    """Latest run of every part; the group shows activity first, then the worst outcome.

    The time is that of the run that set the status, so an old failure is never paired with today's success.
    """
    runs = [part for part in parts if part['last_status']]
    if not runs:
        return {'status': 'never', 'at': None}
    statuses = {_document_status(part) for part in runs}
    status = next((s for s in ['running', 'queued', 'failed', 'partial'] if s in statuses), 'success')
    return {'status': status, 'at': max(part['last_at'] for part in runs if _document_status(part) == status)}


def _document_problem(parts, scheduled, now):
    failed = sum(1 for part in parts if part['last_status'] and _document_status(part) == 'failed')
    return _first_problem([
        (failed and len(parts) == 1, 'The last update failed.', WORKFLOW),
        (failed, f'{failed} of {len(parts)} parts failed to update.', WORKFLOW),
        (any(part['last_status'] == 'partial' for part in parts), 'Some documents could not be processed.', WORKFLOW),
        (any(p['next_run_at'] and p['next_run_at'] < now - OVERDUE for p in scheduled),
         'A scheduled update is overdue. The background worker may be stopped.', WORKFLOW),
        (any(not p['next_run_at'] for p in scheduled), NOT_ACTIVE, WORKFLOW),
    ])


def _group_how(parts, imported):
    """(how, plain update sentence, scheduled parts) for an import group or uploads.

    Only parts the minute tick would actually refresh count as scheduled.
    """
    scheduled = [p for p in parts if p['is_active'] and not p['refresh_paused'] and not p['pipeline_enabled'] and p['cron']]
    if imported:
        updates = IMPORT_UPDATES
        how = {'mode': 'imported', 'text': 'Imported export · not synced live', 'next_run_at': None}
    else:
        updates = 'Processed each time someone uploads a file.'
        how = {'mode': 'manual', 'text': 'When someone uploads a file', 'next_run_at': None}
    if scheduled:
        shapes = {(p['cron'], p['timezone']) for p in scheduled}
        when = _lower(describe_cron(*next(iter(shapes))) if len(shapes) == 1 else 'On different schedules per part')
        upcoming = [p['next_run_at'] for p in scheduled if p['next_run_at']]
        how = {'mode': 'scheduled', 'text': f'Re-reads the export {when}' if imported else f'Reprocessed {when}',
               'next_run_at': min(upcoming) if upcoming else None}
        updates += f' The export is re-read {when}.' if imported else f' Also reprocessed {when}.'
    return how, updates, scheduled


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def integrations_overview(request):
    from django_q.models import Schedule
    org = _admin_org(request)
    documents = _documents_enabled(request, org)
    now, zone = timezone.now(), _catalog_zone()
    horizon = now + HORIZON

    latest_log = SourceRunLog.objects.filter(source=OuterRef('pk')).order_by('-started_at', '-pk')
    connectors = list(IntegrationSource.objects.filter(organization=org).select_related('schedule').annotate(
        last_status=Subquery(latest_log.values('status')[:1]),
        last_at=Subquery(latest_log.values('started_at')[:1]),
    ).order_by('name', 'pk'))
    destinations = list(IntegrationDestination.objects.filter(organization=org).select_related('schedule').order_by('name', 'pk'))
    names = [f'source_run_{c.pk}' for c in connectors] + [f'dest_run_{d.pk}' for d in destinations] + [f'workflow_run_{org.pk}']
    q_rows = {row['name']: row for row in Schedule.objects.filter(name__in=names).values('name', 'cron', 'next_run')}
    items = dict(Item.objects.filter(integration_source_id__in=[c.pk for c in connectors], deleted=False).order_by()
                 .values('integration_source_id').annotate(count=Count('pk')).values_list('integration_source_id', 'count')) if connectors else {}
    # The catalog load refuses two active connections of one type (load_scope.require_exact_load_scope).
    duplicates = {kind for kind, count in Counter(c.source_type for c in connectors if c.is_active).items() if count > 1}

    # Catalog workflow: the runner's own order (transformation before visualization, then name).
    wf_schedule = WorkflowSchedule.objects.filter(organization=org).first()
    wf_last = WorkflowRun.objects.filter(organization=org).order_by('-started_at', '-pk').values('status', 'started_at').first()
    wf_cron = (wf_schedule.cron_expression or '') if wf_schedule else ''
    wf_next, wf_problem = _registered(q_rows, f'workflow_run_{org.pk}', wf_cron, bool(wf_schedule and wf_schedule.is_enabled), now)
    wf_scheduled = bool(wf_schedule and wf_schedule.is_enabled and wf_cron and f'workflow_run_{org.pk}' in q_rows)
    wf_when = describe_cron(wf_cron, zone) if wf_scheduled else ''
    category = {IntegrationSource.CATEGORY_TRANSFORMATION: 0, IntegrationSource.CATEGORY_VISUALIZATION: 1}
    active = sorted((c for c in connectors if c.is_active), key=lambda c: (category.get(c.category, 99), c.name))
    active_destinations = [d for d in destinations if d.is_active]
    position = {c.pk: index + 2 for index, c in enumerate(active)}
    steps = ([{'order': 1, 'label': 'Check there is enough disk space', 'source_key': None}]
             + [{'order': position[c.pk], 'label': f'{c.name} ({PROVIDER_LABELS.get(c.source_type, c.get_source_type_display())})',
                 'source_key': f'integration:{c.pk}'} for c in active]
             + [{'order': len(active) + 2, 'label': 'Link the tools together and refresh summaries', 'source_key': None}]
             + [{'order': len(active) + 3 + index, 'label': f'Send to {d.name}', 'source_key': None}
                for index, d in enumerate(active_destinations)])
    workflow = {
        'how': {'mode': 'scheduled', 'text': wf_when, 'next_run_at': wf_next} if wf_scheduled
        else {'mode': 'manual', 'text': 'Manual only', 'next_run_at': None},
        'last_run': {'status': CATALOG_STATUS.get(wf_last['status'], 'success'), 'at': wf_last['started_at']} if wf_last
        else {'status': 'never', 'at': None},
        'steps': steps, 'note': WORKFLOW_NOTE,
        'needs_attention': bool(wf_problem) or bool(wf_last and wf_last['status'] == 'failed'),
        'attention': wf_problem or ('The last workflow run failed.' if wf_last and wf_last['status'] == 'failed' else ''),
    }

    parts_by_key = {}
    if documents:
        latest_run = IngestionRun.objects.filter(source=OuterRef('pk')).exclude(status='cancelled').order_by('-created_at', '-pk')
        doc_counts = dict(Document.objects.filter(source__organization=org, deleted_at__isnull=True).order_by()
                          .values('source_id').annotate(count=Count('id')).values_list('source_id', 'count'))
        # What "View documents" will show this admin: access rules apply to admins too.
        readable = dict(visible_documents(org, request.user).order_by()
                        .values('source_id').annotate(count=Count('id')).values_list('source_id', 'count'))
        for part in KnowledgeSource.objects.filter(organization=org).annotate(
                last_status=Subquery(latest_run.values('status')[:1]), last_at=Subquery(latest_run.values('created_at')[:1]),
        ).order_by('name', 'id').values('id', 'kind', 'integration_id', 'integration__organization_id', 'import_group',
                                       'is_active', 'refresh_paused', 'pipeline_enabled', 'cron', 'timezone',
                                       'next_run_at', 'last_status', 'last_at'):
            part['documents'], part['readable'] = doc_counts.get(part['id'], 0), readable.get(part['id'], 0)
            parts_by_key.setdefault(_group_key(part, org), []).append(part)

    def document_counts(parts):
        return {'documents': sum(p['documents'] for p in parts), 'visible_documents': sum(p['readable'] for p in parts),
                'parts': len(parts)}

    rows, events = [], []
    for c in connectors:
        key = f'integration:{c.pk}'
        provider = PROVIDER_LABELS.get(c.source_type, c.get_source_type_display())
        own = getattr(c, 'schedule', None)
        own_cron = (own.cron_expression or '') if own else ''
        own_next, own_problem = _registered(q_rows, f'source_run_{c.pk}', own_cron, bool(own and own.is_enabled), now)
        own_registered = bool(own and own.is_enabled and own_cron and f'source_run_{c.pk}' in q_rows)
        own_scheduled = c.is_active and own_registered
        in_workflow = c.is_active and wf_scheduled
        if not c.is_active:
            how = {'mode': 'manual', 'text': 'Turned off', 'next_run_at': None}
            updates = 'Turned off. Switch it on under Schedule & runs to include it in runs.'
        elif own_scheduled:
            upcoming = [t for t in [own_next, wf_next if in_workflow else None] if t]
            when = describe_cron(own_cron, zone)
            # Name both schedules, so the row agrees with "Next run" when the workflow comes first.
            how = {'mode': 'scheduled', 'text': f'{when}, and with the catalog workflow {_lower(wf_when)}' if in_workflow else when,
                   'next_run_at': min(upcoming) if upcoming else None}
            updates = f'Runs on its own {_lower(when)}' + (f', and with the catalog workflow {_lower(wf_when)}.' if in_workflow else '.')
        elif in_workflow:
            how = {'mode': 'scheduled', 'text': f'With the catalog workflow, {_lower(wf_when)}', 'next_run_at': wf_next}
            updates = f'Runs with the catalog workflow {_lower(wf_when)}, as step {position[c.pk]} of {len(steps)}.'
        else:
            how = {'mode': 'manual', 'text': 'Manual only', 'next_run_at': None}
            updates = 'Runs only when you press Run now or run the catalog workflow.'
        linked = parts_by_key.pop(key, [])
        indexing = documents and settings.DOCUMENT_INDEXING_PIPELINE_ENABLED and any(p['pipeline_enabled'] for p in linked)
        linked_run = _document_last_run(linked)
        last_run = {'status': CATALOG_STATUS.get(c.last_status, 'success'), 'at': c.last_at} if c.last_status else {'status': 'never', 'at': None}
        duplicate = c.is_active and c.source_type in duplicates
        run_blocked = f'Another active {provider} connection blocks runs. Turn one of them off first.' if duplicate else ''
        problem, fix_in = _first_problem([
            # Turning a connector off keeps its own schedule, which then fails (and alerts) on every run.
            (not c.is_active and own_registered,
             'Turned off, but its own schedule is still on, so every scheduled run fails. Switch it on or turn the schedule off.', WORKFLOW),
            (duplicate, run_blocked, WORKFLOW),
            (c.is_active and last_run['status'] == 'failed', 'The last run failed.', WORKFLOW),
            (c.is_active and own_problem, own_problem, WORKFLOW),
            (c.is_active and not _has_credentials(c), 'Connection details are incomplete.', SOURCES),
            (c.is_active and linked_run['status'] in ('failed', 'partial'), 'Indexing text for search failed on the last run.', WORKFLOW),
        ])
        rows.append({
            'key': key, 'name': c.name, 'provider': c.source_type, 'provider_label': provider, 'kind': 'connector',
            'connection_id': c.pk, 'is_active': c.is_active, 'how': how, 'order': None, 'last_run': last_run,
            'counts': {**(document_counts(linked) if documents else {'documents': None, 'visible_documents': None, 'parts': None}),
                       'catalog_items': items.get(c.pk, 0)},
            'needs_attention': bool(problem), 'attention': problem, 'fix_in': fix_in,
            'workflow_step': position.get(c.pk),
            'can_run_now': c.is_active and not duplicate and last_run['status'] not in ('running', 'queued'),
            'run_blocked': run_blocked,
            'detail': {'updates': updates, 'steps': [FETCH_STEPS.get(c.source_type, f'Fetch from {provider}'), 'Load into the catalog']
                       + (['Index text for search, in the background'] if indexing else [])},
            '_rank': position.get(c.pk, 999),
        })
        if own_scheduled:
            times, repeat = _upcoming(own_cron, zone, own_next, now, horizon)
            events += [(at, 0, {'source_key': key, 'label': c.name, 'kind': 'source', 'repeat': repeat}) for at in times]

    group_names = {'uploads': 'Uploaded documents', 'imports': 'Imported documents'}
    for key, parts in parts_by_key.items():
        # Kind follows the parts themselves: an upload collection may carry an import-group label too.
        kind = 'import' if any(p['kind'] == 'snapshot' for p in parts) else 'upload'
        how, updates, scheduled = _group_how(parts, kind == 'import')
        group_steps = (['Read the imported export', 'Extract the text of new and changed documents', 'Make them searchable']
                       if kind == 'import' else ['Someone uploads a file', 'Extract its text', 'Make it searchable'])
        problem, fix_in = _document_problem(parts, scheduled, now)
        name = group_names.get(key) or key.split(':', 1)[1]
        rows.append({
            'key': key, 'name': name, 'provider': kind, 'provider_label': 'Imported export' if kind == 'import' else 'Uploaded files',
            'kind': kind, 'connection_id': None, 'is_active': any(p['is_active'] for p in parts), 'how': how, 'order': None,
            'last_run': _document_last_run(parts), 'counts': {**document_counts(parts), 'catalog_items': None},
            'needs_attention': bool(problem), 'attention': problem, 'fix_in': fix_in, 'workflow_step': None,
            'can_run_now': False, 'run_blocked': '', 'detail': {'updates': updates, 'steps': group_steps}, '_rank': 999,
        })
        # Parts run at their stored next run (the minute tick acts on it), then on their cron.
        times, memo = {}, {}
        for p in scheduled:
            shape = (p['cron'], p['timezone'], p['next_run_at'])
            if shape[2] and shape not in memo:
                memo[shape] = _upcoming(*shape, now, horizon)
            moments, repeat = memo.get(shape, ([], None))
            for at in moments:
                times[(at, repeat)] = times.get((at, repeat), 0) + 1
        events += [(at, 50, {'source_key': key, 'label': f'Update {name}' + (f' ({n} parts)' if n > 1 else ''),
                             'kind': 'documents', 'repeat': repeat}) for (at, repeat), n in times.items()]

    if wf_scheduled:
        times, repeat = _upcoming(wf_cron, zone, wf_next, now, horizon)
        for at in times:
            # Steps of one workflow run share its start time and keep the runner's order.
            events += [(at, 10 + position[c.pk], {'source_key': f'integration:{c.pk}', 'label': c.name, 'kind': 'workflow',
                                                  'repeat': repeat}) for c in active]
            events += [(at, 100 + index, {'source_key': None, 'label': f'Send to {d.name}', 'kind': 'workflow', 'repeat': repeat})
                       for index, d in enumerate(active_destinations)]
    for d in destinations:
        sched = getattr(d, 'schedule', None)
        if d.is_active and sched and sched.is_enabled and sched.cron_expression and f'dest_run_{d.pk}' in q_rows:
            times, repeat = _upcoming(sched.cron_expression, zone, q_rows[f'dest_run_{d.pk}']['next_run'], now, horizon)
            events += [(at, 200, {'source_key': None, 'label': f'Send to {d.name}', 'kind': 'destination', 'repeat': repeat})
                       for at in times]

    # Order = the sequence sources run in next: only rows with a next run, by its time, then workflow position.
    runnable = sorted((r for r in rows if r['how']['next_run_at']),
                      key=lambda r: (r['how']['next_run_at'], r['_rank'], r['name'].lower()))
    for index, row in enumerate(runnable):
        row['order'] = index + 1
    for row in rows:
        del row['_rank']
    rows.sort(key=lambda r: (r['order'] is None, r['order'] or 0, r['name'].lower()))
    events.sort(key=lambda event: (event[0], event[1]))
    shown, truncated = _whole_blocks(events)
    return Response({
        'sources': rows,
        'run_order': [{'at': at, **payload} for at, _, payload in shown],
        'run_order_truncated': truncated,
        'workflow': workflow, 'documents_enabled': documents, 'timezone': zone, 'generated_at': now,
    })
