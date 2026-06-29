"""Governance task creation & routing policy.

When an ``ItemGroup`` status flips to ``ATTENTION`` or ``DELETED`` a small
follow-up task is created and routed to a data person. Dedupe rule: at most one
*open* task per group — a repeat flip refreshes the existing open task instead
of spawning a new one.

Called explicitly from the two status-change sites in ``views.py`` (mirroring
how ``send_slack_item_alert`` is invoked there). Wrapped in try/except so a
failure here never blocks the originating request.

Routing policy (the one place who-gets-a-task is decided)
---------------------------------------------------------
Today tasks go to the asset's **steward only**. The policy is expressed as an
ordered list of governance roles (``ASSIGNEE_ROLES``) plus a resolver per role
(``_ROLE_RESOLVERS``). To also route to owners / others in the future, add the
role to ``ASSIGNEE_ROLES`` (and a resolver if it's a new role) — no other code,
model, or template changes are required. ``GovernanceTask.assignee_role`` then
records which role each task was routed from.
"""

# Statuses that warrant a follow-up task.
TASK_STATUSES = ('ATTENTION', 'DELETED')

_STATUS_LABELS = {
    'ATTENTION': 'Attention',
    'DELETED': 'Deletion',
}

# --- Routing policy ---------------------------------------------------------
# Governance roles that receive a task, in priority order. The first role that
# has a person set on the group wins. Steward-only today; append 'owner' (or
# others) here to expand routing.
ASSIGNEE_ROLES = ('steward',)

# role key -> how to pull that DataPerson off an ItemGroup. Extra roles are
# registered here once; they only take effect when listed in ASSIGNEE_ROLES.
_ROLE_RESOLVERS = {
    'steward': lambda g: g.steward,
    'owner': lambda g: g.ownership_person,
}


def _resolve_assignee(item_group):
    """Return ``(DataPerson | None, role | None)`` for the first configured
    role with a person set on the group. ``(None, None)`` when unassigned."""
    for role in ASSIGNEE_ROLES:
        resolver = _ROLE_RESOLVERS.get(role)
        person = resolver(item_group) if resolver else None
        if person is not None:
            return person, role
    return None, None


def _asset_label(item_group):
    """Human label for the asset, using the group's primary/first item."""
    rep = None
    if item_group is not None:
        rep = item_group.primary_item or item_group.items.first()
    if rep is not None:
        return rep.item_name or rep.item_id
    return item_group.group_key if item_group is not None else 'asset'


def sync_status_task(item_group, new_status, changed_by=None):
    """Create or refresh the open task for ``item_group`` after a status change.

    No-op unless ``new_status`` is one we track. Returns the task (or None).
    """
    try:
        if item_group is None or new_status not in TASK_STATUSES:
            return None

        from .models import GovernanceTask

        label = _asset_label(item_group)
        title = f'Review "{label}" — status set to {_STATUS_LABELS.get(new_status, new_status)}'
        assignee, role = _resolve_assignee(item_group)

        task = GovernanceTask.objects.filter(
            item_group=item_group, state=GovernanceTask.STATE_OPEN,
        ).first()
        if task is None:
            task = GovernanceTask(item_group=item_group)

        task.organization = item_group.organization
        task.assignee = assignee          # may be None -> unassigned
        task.assignee_role = role         # 'steward' today; None when unassigned
        task.trigger_status = new_status
        task.title = title
        task.state = GovernanceTask.STATE_OPEN
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
