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
roles; the first role with a person set on the group wins, and
``GovernanceTask.assignee_role`` records which one matched. The ordering is the
whole point: "Unverified goes to the Owner" is the rule, but a measure with no
owner still has to land somewhere, so Steward is the fallback rather than
leaving the task unassigned. ``_ROLE_RESOLVERS`` registers how to pull each
role off an ``ItemGroup`` — adding a role is a one-line change in both places.

Why the sweep is a reconciler and not an appender
-------------------------------------------------
Rules like "is still Unverified" and "has no Category" have no status
*transition* to hook onto, so they can't be event-driven. Reconciling instead
buys three things for free: re-running is idempotent (the unique constraint on
``(item_group, reason)`` for open tasks enforces it), assignees self-heal as
ownership gets filled in, and finished work disappears from the board without
anyone pressing Done.
"""

from django.db import transaction
from django.db.models import Q
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

# Each reason declares who it routes to, how its title reads, and which
# ItemGroup kinds it applies to.
#
# ``kinds`` is load-bearing. The whole feature is scoped to PowerBI measures,
# which is exactly what ``kind='measure_name'`` means (only PB_MEASURE items get
# a shared measure group; everything else gets a singleton). On the production
# data that is the difference between ~4,000 tasks and ~67,000: there are 64,825
# UNVERIFIED singleton groups — tables, columns, reports, dbt models — that
# nobody intends to curate one by one.
#
# The *sweep* is bounded this way; ``sync_status_task`` deliberately is not, so
# manually flagging any asset still raises a task for it.
REASON_POLICY = {
    REASON_UNVERIFIED: {
        'roles': ('owner', 'steward'),
        'label': 'Verify',
        'title': 'Verify "{asset}" — still Unverified',
        'hint': 'Move it to Verified, or flag it for Attention.',
        'kinds': ('measure_name',),
        'trigger_status': 'UNVERIFIED',
    },
    REASON_ATTENTION: {
        'roles': ('steward', 'owner'),
        'label': 'Attention',
        'title': 'Review "{asset}" — flagged Attention',
        'hint': 'Agree with the Owner whether it should be deleted or not.',
        'kinds': ('measure_name',),
        'trigger_status': 'ATTENTION',
    },
    REASON_DELETED: {
        'roles': ('steward', 'owner'),
        'label': 'To Be Deleted',
        'title': 'Confirm deletion of "{asset}"',
        'hint': 'Confirm the asset can go, or restore it.',
        'kinds': ('measure_name',),
        'trigger_status': 'DELETED',
    },
    REASON_NO_CATEGORY: {
        'roles': ('owner', 'steward'),
        'label': 'Category',
        'title': 'Set a category for "{asset}"',
        'hint': 'Pick the category this measure belongs to.',
        'kinds': ('measure_name',),
        'trigger_status': None,
    },
}

# Order the sweep runs / the UI groups by.
REASON_ORDER = (REASON_UNVERIFIED, REASON_NO_CATEGORY, REASON_ATTENTION, REASON_DELETED)

# What the sweep may be pointed at. The policy above defaults every rule to
# PowerBI measures, but the scope is a *parameter*, not a hard-coded assumption:
# callers (the Generate dialog's dropdown, the management command's --kinds) can
# widen it without a code change. ALL is deliberately last and unrecommended —
# it takes the target set from ~4k to ~67k on the production data.
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
        'hint': 'Every group of every kind. Very high volume.',
    },
}
DEFAULT_KIND_SCOPE = 'measure_name'


def resolve_kinds(scope):
    """``scope`` key -> the tuple of ItemGroup kinds to sweep.

    Returns ``None`` for "no kind filter". An unknown scope falls back to the
    per-reason policy default rather than silently sweeping everything.
    """
    if not scope:
        return None
    entry = KIND_SCOPES.get(scope)
    return entry['kinds'] if entry else None

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
        if person is not None:
            return person, role
    return None, None


def _resolve_assignee_ids(row, reason):
    """Sweep-side twin of :func:`_resolve_assignee` working on a values() row.

    Returns ``(assignee_id | None, role | None)``.
    """
    policy = REASON_POLICY.get(reason) or REASON_POLICY[REASON_ATTENTION]
    for role in policy['roles']:
        field = _ROLE_ID_FIELDS.get(role)
        person_id = row.get(field) if field else None
        if person_id is not None:
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


def sync_status_task(item_group, new_status, changed_by=None):
    """Create or refresh the open task for ``item_group`` after a status change.

    No-op unless ``new_status`` is one we track. Returns the task (or None).
    """
    try:
        if item_group is None or new_status not in TASK_STATUSES:
            return None

        from .models import GovernanceTask

        reason = _STATUS_TO_REASON[new_status]
        label = _asset_label(item_group)
        title = _title_for(reason, label)
        assignee, role = _resolve_assignee(item_group, reason)

        task = GovernanceTask.objects.filter(
            item_group=item_group, reason=reason, state=GovernanceTask.STATE_OPEN,
        ).first()
        if task is None:
            task = GovernanceTask(item_group=item_group, reason=reason)

        task.organization = item_group.organization
        task.assignee = assignee          # may be None -> unassigned
        task.assignee_role = role         # None when unassigned
        task.trigger_status = new_status
        task.title = title
        task.state = GovernanceTask.STATE_OPEN
        task.closed_reason = None
        task.completed_at = None
        task.completed_by = None
        task.save()

        try:
            from etl.hooks.slack.slack_alerts import send_slack_task_alert
            send_slack_task_alert(task)
        except Exception as e:
            print(f'[GovernanceTask] Slack alert failed: {e}')

        return task
    except Exception as e:
        print(f'[GovernanceTask] sync failed: {e}')
        return None


# --- The sweep --------------------------------------------------------------

def _target_groups_qs(reason, org, kinds=None):
    """The set of ItemGroups that *should* have an open task of ``reason``.

    ``kinds`` overrides the reason's default scope (see ``KIND_SCOPES``); pass
    ``'ALL'`` to sweep every kind.
    """
    from .models import ItemGroup

    policy = REASON_POLICY[reason]
    qs = ItemGroup.objects.all()
    if org is not None:
        # Mirrors how every read path scopes groups: the org's own rows plus the
        # unattributed ones, so the sweep can't see a neighbour's catalog.
        qs = qs.filter(Q(organization=org) | Q(organization__isnull=True))
    if kinds == 'ALL':
        effective_kinds = None
    else:
        effective_kinds = kinds if kinds is not None else policy['kinds']
    if effective_kinds:
        qs = qs.filter(kind__in=effective_kinds)

    if reason == REASON_UNVERIFIED:
        # deleted=False: something already on its way out doesn't need verifying.
        qs = qs.filter(status='UNVERIFIED', deleted=False)
    elif reason == REASON_ATTENTION:
        qs = qs.filter(status='ATTENTION')
    elif reason == REASON_DELETED:
        qs = qs.filter(status='DELETED')
    elif reason == REASON_NO_CATEGORY:
        # Exclude both flavours of "on its way out": the `deleted` flag AND a
        # DELETED status set straight from the dictionary dropdown (which does
        # not set the flag). Asking someone to categorise a measure that is
        # being deleted is pure noise.
        qs = qs.filter(category__isnull=True, deleted=False).exclude(status='DELETED')
    else:
        return ItemGroup.objects.none()
    return qs


def _labels_for(group_rows):
    """``{group_id: asset label}`` for the sweep, in at most one extra query.

    Prefers the group's primary item name (already joined in the values() row).
    Only groups without one fall back to a lookup of any member item — done as a
    single ``IN`` query rather than ``group.items.first()`` per row, which would
    be one query per task on a sweep of thousands.
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
        pairs = (Item.objects.filter(item_group_id__in=missing)
                 .order_by('item_group_id', 'item_id')
                 .values_list('item_group_id', 'item_name', 'item_id'))
        for gid, item_name, item_id in pairs.iterator():
            rep.setdefault(gid, item_name or item_id)
        for row in group_rows:
            if row['id'] in labels:
                continue
            labels[row['id']] = rep.get(row['id']) or row.get('group_key') or 'asset'
    return labels


def _sweep_reason(reason, org, dry_run, require_assignee, now, kinds=None):
    """Reconcile one reason. Returns a counts dict."""
    from .models import GovernanceTask

    counts = {'created': 0, 'reassigned': 0, 'closed': 0, 'unassigned': 0, 'target': 0}

    target_rows = list(_target_groups_qs(reason, org, kinds).values(
        'id', 'group_key', 'organization_id', 'ownership_person_id', 'steward_id',
        'primary_item__item_name', 'primary_item__item_id',
    ))
    target_ids = {r['id'] for r in target_rows}
    counts['target'] = len(target_ids)

    open_qs = GovernanceTask.objects.filter(
        reason=reason, state=GovernanceTask.STATE_OPEN,
    )
    if org is not None:
        open_qs = open_qs.filter(Q(organization=org) | Q(organization__isnull=True))
    # Only tasks INSIDE the swept scope may be auto-closed. Without this, running
    # a measures-only sweep would quietly resolve tasks the event path had raised
    # for other kinds of asset, purely because they weren't in this run's target
    # set. Orphans (group deleted) are always in scope — they can never resolve
    # themselves.
    policy_kinds = REASON_POLICY[reason]['kinds']
    scope_kinds = None if kinds == 'ALL' else (kinds if kinds is not None else policy_kinds)
    if scope_kinds:
        open_qs = open_qs.filter(
            Q(item_group__kind__in=scope_kinds) | Q(item_group__isnull=True)
        )
    open_rows = list(open_qs.values('id', 'item_group_id', 'assignee_id', 'assignee_role'))
    open_by_group = {r['item_group_id']: r for r in open_rows if r['item_group_id'] is not None}

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
    labels = _labels_for([r for r in target_rows if r['id'] not in open_by_group])
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
        if assignee_id is None and require_assignee:
            continue
        new_tasks.append(GovernanceTask(
            organization_id=row['organization_id'] or (org.id if org is not None else None),
            item_group_id=row['id'],
            assignee_id=assignee_id,
            assignee_role=role,
            reason=reason,
            trigger_status=policy['trigger_status'],
            title=_title_for(reason, labels.get(row['id']) or row['group_key'] or 'asset'),
            state=GovernanceTask.STATE_OPEN,
        ))
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
        # ignore_conflicts leans on uniq_open_task_per_group_reason: if a
        # concurrent event-path write already opened the same task, the row is
        # skipped instead of blowing up the whole sweep.
        GovernanceTask.objects.bulk_create(new_tasks, batch_size=1000, ignore_conflicts=True)
    return counts


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def generate_tasks(org, reasons=None, dry_run=False, notify=False, require_assignee=False,
                   kind_scope=DEFAULT_KIND_SCOPE):
    """Reconcile the Task Manager against the current state of the catalog.

    ``reasons`` defaults to every reason in :data:`REASON_ORDER`.
    ``kind_scope`` is a key of :data:`KIND_SCOPES` deciding which ItemGroup
    kinds are swept — ``'measure_name'`` (PowerBI measures, the default),
    ``'singleton'``, or ``'all'``. Returns ``{'reasons': {reason: counts},
    'totals': counts}``; with ``dry_run=True`` nothing is written and the same
    counts are returned, which is what the UI's Preview shows before anyone
    commits to a few thousand rows.

    ``notify`` sends ONE digest to Slack — never one message per task. A first
    sweep over a few thousand measures would otherwise post a few thousand
    messages and get the workspace rate-limited; per-task pings stay on the
    interactive :func:`sync_status_task` path where the volume is bounded.
    """
    selected = [r for r in REASON_ORDER if not reasons or r in reasons]
    now = timezone.now()
    per_reason = {}
    # 'all' has to be an explicit sentinel: `None` already means "use the
    # reason's own default", so it can't double as "no filter".
    kinds = 'ALL' if kind_scope == 'all' else resolve_kinds(kind_scope)

    with transaction.atomic():
        for reason in selected:
            per_reason[reason] = _sweep_reason(
                reason, org, dry_run, require_assignee, now, kinds,
            )
        if dry_run:
            # Belt and braces: _sweep_reason already writes nothing on a dry run,
            # but rolling back guarantees a preview can never leave a trace.
            transaction.set_rollback(True)

    totals = {k: sum(c[k] for c in per_reason.values())
              for k in ('created', 'reassigned', 'closed', 'unassigned', 'target')}

    if notify and not dry_run and (totals['created'] or totals['closed']):
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
    }
