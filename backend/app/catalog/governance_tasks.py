"""Governance task creation & routing policy.

Two ways a ``GovernanceTask`` comes into being, both defined here:

* **event** — :func:`sync_status_task` is called from the two status-change
  sites in ``views.py`` (mirroring how ``send_slack_item_alert`` is invoked
  there) so a manual flip to Attention / To Be Deleted reaches its assignee
  immediately, with a Slack ping.
* **sweep** — :func:`generate_tasks` reconciles the *whole* catalog against the
  governance rules. It creates what's missing, re-resolves assignees on tasks
  that are already open, and auto-closes tasks whose underlying gap has been
  fixed. This is what the Task Manager's "Generate tasks" button and the
  ``generate_governance_tasks`` management command run.

Both are wrapped so a failure here never blocks the originating request.

Routing policy (the one place who-gets-a-task is decided)
---------------------------------------------------------
``REASON_POLICY`` maps each task *reason* to an ordered tuple of governance
roles and ``GovernanceTask.assignee_role`` records which one matched. Routing is
strict: Unverified and Category go to the Owner; Attention and To Be Deleted go
to the Steward. A missing primary role leaves the task unassigned rather than
quietly handing an Owner's work to a Steward (or vice versa).

Why the sweep is a reconciler and not an appender
-------------------------------------------------
Rules like "is still Unverified" and "has no Category" have no status
*transition* to hook onto, so they can't be event-driven. Reconciling instead
buys three things for free: re-running is idempotent (the unique constraint on
``(item_group, reason)`` for open tasks enforces it), assignees self-heal as
ownership gets filled in, and finished work disappears from the board without
anyone pressing Done.
"""

import hashlib
import json

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

# Statuses that warrant a follow-up task from the *event* path.
TASK_STATUSES = ('ATTENTION', 'DELETED')

_STATUS_LABELS = {
    'ATTENTION': 'Attention',
    'DELETED': 'To Be Deleted',
}

# --- Routing policy ---------------------------------------------------------

REASON_UNVERIFIED = 'UNVERIFIED'
REASON_ATTENTION = 'ATTENTION'
REASON_DELETED = 'DELETED'
REASON_NO_CATEGORY = 'NO_CATEGORY'

# role key -> how to pull that DataPerson off an ItemGroup.
_ROLE_RESOLVERS = {
    'steward': lambda g: g.steward,
    'owner': lambda g: g.ownership_person,
}
# Same resolvers for the sweep, which works on plain FK ids rather than loading
# every related object (one dict lookup instead of a query per group).
_ROLE_ID_FIELDS = {
    'steward': 'steward_id',
    'owner': 'ownership_person_id',
}
_ROLE_ORG_ID_FIELDS = {
    'steward': 'steward__organization_id',
    'owner': 'ownership_person__organization_id',
}

# Each reason declares who it routes to, how its title reads, and which
# ItemGroup kinds it applies to.
#
# ``kinds`` is load-bearing and reason-specific. Routine verification remains
# limited to shared PowerBI measure groups, while explicit Attention/Deleted
# states and missing Category apply to every asset kind. The sweep-level scope
# below can narrow the candidate set further, but it never broadens a reason
# beyond this policy.
REASON_POLICY = {
    REASON_UNVERIFIED: {
        'roles': ('owner',),
        'label': 'Verify',
        'title': 'Verify "{asset}" — still Unverified',
        'hint': 'Move it to Verified, or flag it for Attention.',
        'kinds': ('measure_name',),
        'trigger_status': 'UNVERIFIED',
    },
    REASON_ATTENTION: {
        'roles': ('steward',),
        'label': 'Attention',
        'title': 'Review "{asset}" — flagged Attention',
        'hint': 'Agree with the Owner whether it should be deleted or not.',
        'kinds': None,
        'trigger_status': 'ATTENTION',
    },
    REASON_DELETED: {
        'roles': ('steward',),
        'label': 'To Be Deleted',
        'title': 'Confirm deletion of "{asset}"',
        'hint': 'Confirm the asset can go, or restore it.',
        'kinds': None,
        'trigger_status': 'DELETED',
    },
    REASON_NO_CATEGORY: {
        'roles': ('owner',),
        'label': 'Category',
        'title': 'Set a category for "{asset}"',
        'hint': 'Pick the category this asset belongs to.',
        'kinds': None,
        'trigger_status': None,
    },
}

# Order the sweep runs / the UI groups by.
REASON_ORDER = (REASON_UNVERIFIED, REASON_NO_CATEGORY, REASON_ATTENTION, REASON_DELETED)

# What the sweep may be pointed at. The default considers all assets so the
# all-kind Category/Attention/Deleted policies are complete; the reason policy
# still keeps routine Unverified work measure-only. Broad runs require a preview
# snapshot before mutation because Category can produce a high task volume.
KIND_SCOPES = {
    'measure_name': {
        'label': 'PowerBI measures',
        'kinds': ('measure_name',),
        'hint': 'Measure groups only — what governance actually curates.',
    },
    'singleton': {
        'label': 'Everything except measures',
        'kinds': ('singleton',),
        'hint': 'Tables, columns, reports, dbt models. Very high volume.',
    },
    'all': {
        'label': 'All assets',
        'kinds': None,
        'hint': (
            'All eligible assets. Unverified remains measure-only; Category '
            'may create tens of thousands of tasks. Preview is required.'
        ),
    },
}
DEFAULT_KIND_SCOPE = 'all'


class StaleGovernancePreview(Exception):
    """The catalog no longer matches the snapshot a broad preview confirmed."""


def _preview_snapshot_digest(group_rows, task_rows):
    """Stable, non-sensitive digest of every row a sweep may act upon."""
    payload = json.dumps(
        {'groups': group_rows, 'tasks': task_rows},
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def resolve_kinds(scope):
    """``scope`` key -> the tuple of ItemGroup kinds to sweep.

    Returns ``None`` for "no kind filter". Unknown scopes are rejected so a
    typo cannot silently run a different sweep than the caller requested.
    """
    entry = KIND_SCOPES.get(scope)
    if entry is None:
        raise ValueError(
            f'Unknown kind scope {scope!r}; expected one of {sorted(KIND_SCOPES)}.'
        )
    return entry['kinds']

# The event path speaks in statuses; map them onto reasons.
_STATUS_TO_REASON = {
    'ATTENTION': REASON_ATTENTION,
    'DELETED': REASON_DELETED,
}


def reason_label(reason):
    """Human label for a reason ('Verify', 'Category', ...)."""
    policy = REASON_POLICY.get(reason)
    return policy['label'] if policy else (reason or '—')


def _resolve_assignee(item_group, reason=REASON_ATTENTION):
    """Return ``(DataPerson | None, role | None)`` for the first configured role
    of ``reason`` that has a person set on the group. ``(None, None)`` when the
    group has nobody in any of them."""
    policy = REASON_POLICY.get(reason) or REASON_POLICY[REASON_ATTENTION]
    for role in policy['roles']:
        resolver = _ROLE_RESOLVERS.get(role)
        person = resolver(item_group) if resolver else None
        if (
            person is not None
            and person.organization_id == item_group.organization_id
        ):
            return person, role
    return None, None


def _resolve_assignee_ids(row, reason):
    """Sweep-side twin of :func:`_resolve_assignee` working on a values() row.

    Returns ``(assignee_id | None, role | None)``.
    """
    policy = REASON_POLICY.get(reason) or REASON_POLICY[REASON_ATTENTION]
    for role in policy['roles']:
        field = _ROLE_ID_FIELDS.get(role)
        org_field = _ROLE_ORG_ID_FIELDS.get(role)
        person_id = row.get(field) if field else None
        person_org_id = row.get(org_field) if org_field else None
        if (
            person_id is not None
            and person_org_id == row.get('organization_id')
        ):
            return person_id, role
    return None, None


def _asset_label(item_group):
    """Human label for the asset, using the group's primary/first item."""
    rep = None
    if item_group is not None:
        rep = item_group.primary_item or item_group.items.first()
    if rep is not None:
        return rep.item_name or rep.item_id
    return item_group.group_key if item_group is not None else 'asset'


def _title_for(reason, asset_label):
    policy = REASON_POLICY.get(reason)
    template = policy['title'] if policy else 'Review "{asset}"'
    return template.format(asset=asset_label)[:512]


def sync_status_task(item_group, new_status, changed_by=None, notify=True):
    """Reconcile status-derived tasks after one group status transition.

    The group and its task rows are locked in a single transaction. Leaving a
    status immediately closes that status's open task and marks a manually
    completed episode as cleared; returning to it later therefore opens a fresh
    task. A manually completed task whose condition never cleared remains
    durably dismissed.
    """
    if item_group is None:
        return None

    from .models import GovernanceTask, ItemGroup

    now = timezone.now()
    status_reasons = (
        REASON_UNVERIFIED, REASON_ATTENTION, REASON_DELETED,
    )
    with transaction.atomic():
        group = (
            ItemGroup.objects.select_for_update(of=('self',))
            .select_related('ownership_person', 'steward', 'primary_item')
            .get(pk=item_group.pk)
        )
        task_qs = (
            GovernanceTask.objects.select_for_update(of=('self',))
            .filter(
                organization=group.organization,
                item_group=group,
                reason__in=status_reasons,
            )
            .order_by('id')
        )
        tasks = list(task_qs)

        persisted_status = group.status
        active_reason = {
            'UNVERIFIED': REASON_UNVERIFIED,
            'ATTENTION': REASON_ATTENTION,
            'DELETED': REASON_DELETED,
        }.get(persisted_status)
        if active_reason in (REASON_ATTENTION, REASON_DELETED):
            has_actionable_item = group.items.exists()
        else:
            has_actionable_item = group.items.filter(deleted=False).exists()
        if not has_actionable_item:
            active_reason = None
        # Unverified is a measure-governance rule. Attention/Deleted remain
        # event-driven for every asset kind.
        if not group.items.exists():
            active_reason = None
        elif (
            active_reason == REASON_UNVERIFIED
            and (group.kind != ItemGroup.KIND_MEASURE_NAME or group.deleted)
        ):
            active_reason = None

        open_by_reason = {
            t.reason: t for t in tasks if t.state == GovernanceTask.STATE_OPEN
        }
        unresolved_manual = {}
        for task_row in tasks:
            if (
                task_row.state == GovernanceTask.STATE_DONE
                and task_row.closed_reason == GovernanceTask.CLOSED_MANUAL
                and task_row.condition_cleared_at is None
            ):
                unresolved_manual.setdefault(task_row.reason, []).append(task_row)

        to_update = []
        for reason in status_reasons:
            if reason == active_reason:
                continue
            open_task = open_by_reason.get(reason)
            if open_task is not None:
                open_task.state = GovernanceTask.STATE_DONE
                open_task.completed_at = now
                open_task.completed_by = None
                open_task.closed_reason = GovernanceTask.CLOSED_RESOLVED
                open_task.updated_at = now
                to_update.append(open_task)
            for dismissed in unresolved_manual.get(reason, []):
                dismissed.condition_cleared_at = now
                dismissed.updated_at = now
                to_update.append(dismissed)
        if to_update:
            GovernanceTask.objects.bulk_update(
                to_update,
                [
                    'state', 'completed_at', 'completed_by', 'closed_reason',
                    'condition_cleared_at', 'updated_at',
                ],
            )

        if active_reason is None:
            sync_group_metadata_tasks(
                [group.pk], create_missing_category=True,
            )
            return None

        # Done dismisses this condition episode until a transition away from the
        # condition (above) records that it cleared.
        if (
            active_reason not in open_by_reason
            and unresolved_manual.get(active_reason)
        ):
            sync_group_metadata_tasks(
                [group.pk], create_missing_category=True,
            )
            return None

        label = _asset_label(group)
        title = _title_for(active_reason, label)
        assignee, role = _resolve_assignee(group, active_reason)
        task = open_by_reason.get(active_reason)
        if task is None:
            task = GovernanceTask(item_group=group, reason=active_reason)
        task.organization = group.organization
        task.assignee = assignee
        task.assignee_role = role
        task.trigger_status = persisted_status
        task.title = title
        task.state = GovernanceTask.STATE_OPEN
        task.closed_reason = None
        task.completed_at = None
        task.condition_cleared_at = None
        task.completed_by = None
        task.save()

        sync_group_metadata_tasks(
            [group.pk], create_missing_category=True,
        )
        if notify:
            transaction.on_commit(
                lambda task_id=task.pk: _send_task_alert_after_commit(task_id)
            )
        return task


def _send_task_alert_after_commit(task_id):
    """Best-effort Slack side effect, deliberately outside DB transactions."""
    try:
        from .models import GovernanceTask
        from etl.hooks.slack.slack_alerts import send_slack_task_alert

        task = GovernanceTask.objects.select_related(
            'assignee', 'item_group', 'organization',
        ).filter(pk=task_id).first()
        if task is not None:
            send_slack_task_alert(task)
    except Exception as e:
        print(f'[GovernanceTask] Slack alert failed: {e}')


def _default_condition_active(row, reason):
    """Condition policy used by event/metadata reconciliation."""
    if reason in (REASON_ATTENTION, REASON_DELETED):
        has_actionable_item = row.get('has_items', True)
    else:
        has_actionable_item = row.get(
            'has_active_items', row.get('has_items', True),
        )
    if not has_actionable_item:
        return False
    if reason == REASON_UNVERIFIED:
        return (
            row['kind'] == 'measure_name'
            and row['status'] == 'UNVERIFIED'
            and not row['deleted']
        )
    if reason == REASON_ATTENTION:
        return row['status'] == 'ATTENTION'
    if reason == REASON_DELETED:
        return row['status'] == 'DELETED'
    if reason == REASON_NO_CATEGORY:
        return (
            row['category_id'] is None
            and not row['deleted']
            and row['status'] != 'DELETED'
        )
    return False


def sync_group_metadata_tasks(
        group_ids, create_missing_category=True, create_missing_status=False):
    """Batch-refresh open task assignees after governance metadata changes.

    Also reconciles Category tasks immediately in both directions: assigning a
    category resolves the open task and clears a manually dismissed episode;
    removing a category opens a new task unless the current uncategorized
    episode was manually dismissed. Callers that changed only another field
    pass ``create_missing_category=False`` so they refresh/close existing
    category work without manufacturing unrelated work. This helper is used by
    both a single ItemGroup PATCH and Definition.apply; all rows are fetched
    and updated in bounded bulk operations rather than per-group queries.

    ETL rename reconciliation passes ``create_missing_status=True``: destination
    groups are then reconciled for their current status plus Category without
    per-group queries or per-task Slack notifications.
    """
    from .models import GovernanceTask, Item, ItemGroup

    ids = sorted({int(pk) for pk in group_ids if pk is not None})
    if not ids:
        return {'created': 0, 'reassigned': 0, 'closed': 0}

    now = timezone.now()
    with transaction.atomic():
        # ``ensure_item_groups`` may hand us tens of thousands of destinations
        # after a load. Keep SQL bind counts bounded and acquire group locks in
        # one deterministic order.
        rows = {}
        for chunk in _chunks(ids, 2000):
            rows.update({
                row['id']: row
                for row in (
                    ItemGroup.objects.select_for_update(of=('self',))
                    .filter(id__in=chunk)
                    .annotate(
                        has_items=Exists(
                            Item.objects.filter(item_group_id=OuterRef('pk'))
                        ),
                        has_active_items=Exists(
                            Item.objects.filter(
                                item_group_id=OuterRef('pk'),
                                deleted=False,
                            )
                        ),
                    )
                    .order_by('id')
                    .values(
                        'id', 'organization_id', 'ownership_person_id',
                        'ownership_person__organization_id',
                        'steward_id', 'steward__organization_id',
                        'category_id', 'kind', 'status', 'deleted',
                        'group_key', 'primary_item__item_name',
                        'primary_item__item_id', 'has_items',
                        'has_active_items',
                    )
                )
            })
        if not rows:
            return {'created': 0, 'reassigned': 0, 'closed': 0}

        group_ids_by_org = {}
        for group_id, row in rows.items():
            group_ids_by_org.setdefault(row['organization_id'], []).append(group_id)
        tasks = []
        for organization_id, scoped_group_ids in sorted(
                group_ids_by_org.items(), key=lambda pair: (pair[0] is None, pair[0])):
            for chunk in _chunks(sorted(scoped_group_ids), 2000):
                tasks.extend(
                    GovernanceTask.objects.select_for_update(of=('self',))
                    .filter(
                        organization_id=organization_id,
                        item_group_id__in=chunk,
                    )
                    .filter(
                        Q(state=GovernanceTask.STATE_OPEN)
                        | Q(
                            state=GovernanceTask.STATE_DONE,
                            closed_reason=GovernanceTask.CLOSED_MANUAL,
                            condition_cleared_at__isnull=True,
                        )
                    )
                    .order_by('id')
                )
        status_reasons = (
            REASON_UNVERIFIED, REASON_ATTENTION, REASON_DELETED,
        )
        managed_reasons = (*status_reasons, REASON_NO_CATEGORY)
        changed = []
        open_conditions = set()
        active_dismissed_conditions = set()
        reassigned = closed = created = 0
        for task in tasks:
            row = rows[task.item_group_id]
            if task.reason not in managed_reasons:
                continue
            key = (task.item_group_id, task.reason)
            condition_active = _default_condition_active(row, task.reason)

            if task.state == GovernanceTask.STATE_OPEN:
                open_conditions.add(key)
                if not condition_active:
                    task.state = GovernanceTask.STATE_DONE
                    task.completed_at = now
                    task.completed_by = None
                    task.closed_reason = GovernanceTask.CLOSED_RESOLVED
                    closed += 1
                else:
                    assignee_id, role = _resolve_assignee_ids(row, task.reason)
                    if (
                        task.assignee_id != assignee_id
                        or (task.assignee_role or None) != role
                    ):
                        task.assignee_id = assignee_id
                        task.assignee_role = role
                        reassigned += 1
            else:
                if condition_active:
                    # Manual Done dismisses only the still-active episode for
                    # this exact reason. It must not suppress Category merely
                    # because an Attention task was dismissed (or vice versa).
                    active_dismissed_conditions.add(key)
                else:
                    task.condition_cleared_at = now

            task.updated_at = now
            changed.append(task)

        new_tasks = []
        for group_id, row in rows.items():
            # Legacy null-tenant groups stay quarantined; there is no safe
            # organization in which to expose their work.
            if row['organization_id'] is None:
                continue
            label = (
                row['primary_item__item_name']
                or row['primary_item__item_id']
                or row['group_key']
                or 'asset'
            )
            reasons_to_create = []
            if create_missing_status:
                for reason in status_reasons:
                    if _default_condition_active(row, reason):
                        reasons_to_create.append(reason)
            if (
                create_missing_category
                and _default_condition_active(row, REASON_NO_CATEGORY)
            ):
                reasons_to_create.append(REASON_NO_CATEGORY)

            for reason in reasons_to_create:
                key = (group_id, reason)
                if (
                    key in open_conditions
                    or key in active_dismissed_conditions
                ):
                    continue
                assignee_id, role = _resolve_assignee_ids(row, reason)
                new_tasks.append(GovernanceTask(
                    organization_id=row['organization_id'],
                    item_group_id=group_id,
                    assignee_id=assignee_id,
                    assignee_role=role,
                    reason=reason,
                    trigger_status=REASON_POLICY[reason]['trigger_status'],
                    title=_title_for(reason, label),
                    state=GovernanceTask.STATE_OPEN,
                ))

        if new_tasks:
            inserted = GovernanceTask.objects.bulk_create(new_tasks, batch_size=1000)
            created = len(inserted)

        if changed:
            # A manually completed category task can be in this list as well as
            # the open tasks; include condition_cleared_at for that transition.
            GovernanceTask.objects.bulk_update(
                changed,
                [
                    'state', 'completed_at', 'completed_by', 'closed_reason',
                    'condition_cleared_at', 'assignee', 'assignee_role',
                    'updated_at',
                ],
                batch_size=1000,
            )

    return {'created': created, 'reassigned': reassigned, 'closed': closed}


# --- The sweep --------------------------------------------------------------

def _labels_for(group_rows):
    """``{group_id: asset label}`` for the sweep, in at most one extra query.

    Prefers the group's primary item name (already joined in the values() row).
    Only groups without one fall back to a lookup of any member item. IDs are
    chunked so an all-assets sweep cannot exceed PostgreSQL's bind limit.
    """
    from .models import Item

    labels = {}
    missing = []
    for row in group_rows:
        name = row.get('primary_item__item_name') or row.get('primary_item__item_id')
        if name:
            labels[row['id']] = name
        else:
            missing.append(row['id'])

    if missing:
        rep = {}
        for ids in _chunks(missing, 2000):
            pairs = (
                Item.objects.filter(item_group_id__in=ids)
                .order_by('item_group_id', 'item_id')
                .values_list('item_group_id', 'item_name', 'item_id')
            )
            for gid, item_name, item_id in pairs.iterator():
                rep.setdefault(gid, item_name or item_id)
        for row in group_rows:
            if row['id'] in labels:
                continue
            labels[row['id']] = rep.get(row['id']) or row.get('group_key') or 'asset'
    return labels


def _row_targets_reason(row, reason):
    """Whether one row in the exact locked sweep snapshot needs ``reason``."""
    policy_kinds = REASON_POLICY[reason]['kinds']
    if policy_kinds and row['kind'] not in policy_kinds:
        return False
    if reason in (REASON_ATTENTION, REASON_DELETED):
        has_actionable_item = row.get('has_items', True)
    else:
        has_actionable_item = row.get(
            'has_active_items', row.get('has_items', True),
        )
    if not has_actionable_item:
        return False
    if reason == REASON_UNVERIFIED:
        return row['status'] == 'UNVERIFIED' and not row['deleted']
    if reason == REASON_ATTENTION:
        return row['status'] == 'ATTENTION'
    if reason == REASON_DELETED:
        return row['status'] == 'DELETED'
    if reason == REASON_NO_CATEGORY:
        return (
            row['category_id'] is None
            and not row['deleted']
            and row['status'] != 'DELETED'
        )
    return False


def _sweep_reason(
        reason, org, dry_run, require_assignee, now, group_rows, task_rows):
    """Reconcile one reason. Returns a counts dict."""
    from .models import GovernanceTask

    counts = {'created': 0, 'reassigned': 0, 'closed': 0, 'unassigned': 0, 'target': 0}

    target_rows = [
        row for row in group_rows if _row_targets_reason(row, reason)
    ]
    target_ids = {r['id'] for r in target_rows}
    counts['target'] = len(target_ids)

    # The caller already locked and snapshotted every actionable task in the
    # exact group-id set. Filtering in Python avoids a 67k-id SQL ``IN`` and
    # prevents a late-committing group from entering this confirmed sweep.
    reason_task_rows = [
        row for row in task_rows if row['reason'] == reason
    ]
    open_rows = [
        row for row in reason_task_rows
        if row['state'] == GovernanceTask.STATE_OPEN
    ]
    open_by_group = {r['item_group_id']: r for r in open_rows if r['item_group_id'] is not None}

    # A manual Done dismisses one condition episode. Keep it dismissed while
    # the group still qualifies; once reconciliation sees the condition clear,
    # stamp the audit row so a later relapse may create a new task.
    dismissed_rows = [
        row for row in reason_task_rows
        if (
            row['state'] == GovernanceTask.STATE_DONE
            and row['closed_reason'] == GovernanceTask.CLOSED_MANUAL
            and row['condition_cleared_at'] is None
        )
    ]
    dismissed_active_group_ids = {
        row['item_group_id']
        for row in dismissed_rows
        if row['item_group_id'] in target_ids
    }
    cleared_dismissal_ids = [
        row['id']
        for row in dismissed_rows
        if row['item_group_id'] is None or row['item_group_id'] not in target_ids
    ]

    # 1. Close what no longer qualifies — including tasks whose group has gone
    #    (SET_NULL), which can never resolve on their own.
    stale_ids = [r['id'] for r in open_rows
                 if r['item_group_id'] is None or r['item_group_id'] not in target_ids]
    counts['closed'] = len(stale_ids)

    # 2. Re-resolve assignees on the tasks that stay open. This is what makes
    #    "everything already in the Task Manager without an Owner should get
    #    one" a re-run rather than a special code path.
    to_reassign = []
    for row in target_rows:
        existing = open_by_group.get(row['id'])
        if existing is None:
            continue
        assignee_id, role = _resolve_assignee_ids(row, reason)
        if existing['assignee_id'] != assignee_id or (existing['assignee_role'] or None) != role:
            to_reassign.append((existing['id'], assignee_id, role))
    counts['reassigned'] = len(to_reassign)

    # 3. Create what's missing.
    labels = _labels_for([
        r for r in target_rows
        if (
            r['id'] not in open_by_group
            and r['id'] not in dismissed_active_group_ids
        )
    ])
    policy = REASON_POLICY[reason]
    new_tasks = []
    for row in target_rows:
        # `unassigned` counts every in-scope group that resolves to nobody, not
        # just the ones getting a NEW task. Counting only new ones would report
        # a truthful number on the first sweep and then zero on every re-run,
        # while thousands of tasks sat unassigned — exactly the figure the
        # command's warning and the UI's Preview lean on.
        assignee_id, role = _resolve_assignee_ids(row, reason)
        if assignee_id is None:
            counts['unassigned'] += 1
        if row['id'] in open_by_group:
            continue
        if row['id'] in dismissed_active_group_ids:
            continue
        if assignee_id is None and require_assignee:
            continue
        new_tasks.append(GovernanceTask(
            organization_id=row['organization_id'],
            item_group_id=row['id'],
            assignee_id=assignee_id,
            assignee_role=role,
            reason=reason,
            trigger_status=policy['trigger_status'],
            title=_title_for(reason, labels.get(row['id']) or row['group_key'] or 'asset'),
            state=GovernanceTask.STATE_OPEN,
        ))
    # On a preview this is the planned count. On a real sweep it is replaced
    # below with the number Django actually inserted.
    counts['created'] = len(new_tasks)

    if dry_run:
        return counts

    if stale_ids:
        for chunk in _chunks(stale_ids, 2000):
            GovernanceTask.objects.filter(id__in=chunk).update(
                state=GovernanceTask.STATE_DONE,
                completed_at=now,
                completed_by=None,
                closed_reason=GovernanceTask.CLOSED_RESOLVED,
                updated_at=now,
            )
    if cleared_dismissal_ids:
        for chunk in _chunks(cleared_dismissal_ids, 2000):
            GovernanceTask.objects.filter(id__in=chunk).update(
                condition_cleared_at=now,
                updated_at=now,
            )
    if to_reassign:
        # Group by the value being written so this is one UPDATE per distinct
        # (person, role) — bounded by the number of data people — instead of one
        # per task, which on a first sweep would be thousands of round trips.
        by_target = {}
        for task_id, assignee_id, role in to_reassign:
            by_target.setdefault((assignee_id, role), []).append(task_id)
        for (assignee_id, role), task_ids in by_target.items():
            for chunk in _chunks(task_ids, 2000):
                GovernanceTask.objects.filter(id__in=chunk).update(
                    assignee_id=assignee_id, assignee_role=role, updated_at=now,
                )
    if new_tasks:
        # All target groups were locked by generate_tasks before this query.
        # Event-path creation takes the same group lock, so a conflict here is a
        # genuine invariant violation and must not be hidden as a fake create.
        created = GovernanceTask.objects.bulk_create(new_tasks, batch_size=1000)
        counts['created'] = len(created)
    else:
        counts['created'] = 0
    return counts


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def generate_tasks(
        org, reasons=None, dry_run=False, notify=False, require_assignee=False,
        kind_scope=DEFAULT_KIND_SCOPE, expected_snapshot=None):
    """Reconcile the Task Manager against the current state of the catalog.

    ``reasons`` defaults to every reason in :data:`REASON_ORDER`.
    ``kind_scope`` is a key of :data:`KIND_SCOPES` deciding which ItemGroup
    kinds are swept — ``'measure_name'`` (PowerBI measures), ``'singleton'``,
    or ``'all'`` (the default). Each reason's policy remains authoritative, so
    an all-assets sweep still limits Unverified work to measure groups. Returns
    ``{'reasons': {reason: counts},
    'totals': counts}``; with ``dry_run=True`` nothing is written and the same
    counts are returned, which is what the UI's Preview shows before anyone
    commits to a few thousand rows.

    ``notify`` sends ONE digest to Slack — never one message per task. A first
    sweep over a few thousand measures would otherwise post a few thousand
    messages and get the workspace rate-limited; per-task pings stay on the
    interactive :func:`sync_status_task` path where the volume is bounded.
    """
    if org is None:
        raise ValueError('An organization is required to generate governance tasks.')

    if reasons is None:
        selected = list(REASON_ORDER)
    else:
        if isinstance(reasons, (str, bytes)):
            raise ValueError('Governance task reasons must be a non-empty iterable.')
        try:
            requested_reasons = list(reasons)
        except TypeError as exc:
            raise ValueError(
                'Governance task reasons must be a non-empty iterable.'
            ) from exc
        if not requested_reasons:
            raise ValueError('At least one governance task reason is required.')
        unknown_reasons = [
            reason for reason in requested_reasons
            if reason not in REASON_ORDER
        ]
        if unknown_reasons:
            raise ValueError(f'Unknown governance task reasons: {unknown_reasons!r}.')
        selected = [r for r in REASON_ORDER if r in requested_reasons]
    now = timezone.now()
    per_reason = {}
    # 'all' has to be an explicit sentinel: `None` already means "use the
    # reason's own default", so it can't double as "no filter".
    kinds = 'ALL' if kind_scope == 'all' else resolve_kinds(kind_scope)
    snapshot_digest = None

    with transaction.atomic():
        # Every writer that can create/reconcile a task locks ItemGroup first
        # and GovernanceTask second. This serializes a sweep with an interactive
        # status/metadata edit and makes the partial unique constraint a final
        # guard rather than normal race control.
        from .models import GovernanceTask, Item, ItemGroup

        lock_qs = ItemGroup.objects.filter(organization=org).annotate(
            has_items=Exists(
                Item.objects.filter(item_group_id=OuterRef('pk'))
            ),
            has_active_items=Exists(
                Item.objects.filter(
                    item_group_id=OuterRef('pk'),
                    deleted=False,
                )
            ),
        )
        if kinds != 'ALL' and kinds:
            lock_qs = lock_qs.filter(kind__in=kinds)
        group_rows = list(
            lock_qs.select_for_update(of=('self',))
            .order_by('id')
            .values(
                'id', 'group_key', 'kind', 'organization_id',
                'status', 'deleted', 'category_id',
                'ownership_person_id', 'ownership_person__organization_id',
                'steward_id', 'steward__organization_id', 'primary_item_id',
                'primary_item__item_name', 'primary_item__item_id',
                'has_items', 'has_active_items',
            )
        )
        scope_group_ids = {row['id'] for row in group_rows}
        task_scope = Q(item_group__isnull=True)
        if kinds == 'ALL':
            task_scope |= Q(item_group__organization=org)
        elif kinds:
            task_scope |= Q(
                item_group__organization=org,
                item_group__kind__in=kinds,
            )
        candidate_task_rows = list(
            GovernanceTask.objects.select_for_update(of=('self',))
            .filter(organization=org, reason__in=selected)
            .filter(task_scope)
            .filter(
                Q(state=GovernanceTask.STATE_OPEN)
                | Q(
                    state=GovernanceTask.STATE_DONE,
                    closed_reason=GovernanceTask.CLOSED_MANUAL,
                    condition_cleared_at__isnull=True,
                )
            )
            .order_by('id')
            .values(
                'id', 'item_group_id', 'reason', 'state', 'assignee_id',
                'assignee_role', 'trigger_status', 'closed_reason',
                'condition_cleared_at',
            )
        )
        task_rows = [
            row for row in candidate_task_rows
            if (
                row['item_group_id'] is None
                or row['item_group_id'] in scope_group_ids
            )
        ]
        snapshot_digest = _preview_snapshot_digest(group_rows, task_rows)
        if (
            expected_snapshot is not None
            and snapshot_digest != expected_snapshot
        ):
            raise StaleGovernancePreview(
                'The governance sweep target changed after preview.'
            )
        for reason in selected:
            per_reason[reason] = _sweep_reason(
                reason, org, dry_run, require_assignee, now,
                group_rows=group_rows,
                task_rows=task_rows,
            )
        if dry_run:
            # Belt and braces: _sweep_reason already writes nothing on a dry run,
            # but rolling back guarantees a preview can never leave a trace.
            transaction.set_rollback(True)

    totals = {k: sum(c[k] for c in per_reason.values())
              for k in ('created', 'reassigned', 'closed', 'unassigned', 'target')}

    if (
        notify
        and not dry_run
        and (
            totals['created']
            or totals['reassigned']
            or totals['closed']
        )
    ):
        try:
            from etl.hooks.slack.slack_alerts import send_slack_task_digest
            send_slack_task_digest(org, per_reason, totals)
        except Exception as e:
            print(f'[GovernanceTask] Slack digest failed: {e}')

    return {
        'reasons': per_reason,
        'totals': totals,
        'dry_run': bool(dry_run),
        'kind_scope': kind_scope,
        '_snapshot_digest': snapshot_digest,
    }
