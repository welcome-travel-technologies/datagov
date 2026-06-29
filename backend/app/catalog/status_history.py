"""Status-change audit trail.

Records one ``StatusChangeLog`` row per ``ItemGroup`` status transition, written
from the same two sites in ``views.py`` that fire Slack alerts and governance
tasks (the Data Dictionary status edit, and the item-deletion auto-DEPRECATE).
Wrapped in try/except so an audit-write failure never blocks the request.
"""


def sync_group_deleted_at(item_group, new_status):
    """Keep ``ItemGroup.deleted_at`` in lockstep with DELETED status.

    Stamped (``now``) when the group enters DELETED, cleared when it leaves.
    The per-item ``Item.deleted`` / ``Item.deleted_at`` flags are independent and
    deliberately left untouched. Returns ``True`` if the stamp changed.
    """
    try:
        if item_group is None:
            return False
        if new_status == 'DELETED':
            if item_group.deleted_at is None:
                from django.utils import timezone
                item_group.deleted_at = timezone.now()
                item_group.save(update_fields=['deleted_at'])
                return True
            return False
        if item_group.deleted_at is not None:
            item_group.deleted_at = None
            item_group.save(update_fields=['deleted_at'])
            return True
        return False
    except Exception as e:
        print(f'[StatusChangeLog] deleted_at sync failed: {e}')
        return False


def log_status_change(item_group, old_status, new_status, changed_by=None):
    """Append a ``StatusChangeLog`` row for a group's status transition.

    No-op when the status didn't actually change or the group is missing.
    Returns the created log row (or ``None``).
    """
    try:
        if item_group is None or old_status == new_status:
            return None

        from .models import StatusChangeLog

        user = changed_by if (changed_by is not None and getattr(changed_by, 'is_authenticated', False)) else None
        return StatusChangeLog.objects.create(
            organization=item_group.organization,
            item_group=item_group,
            group_key=item_group.group_key,
            old_status=old_status,
            new_status=new_status,
            changed_by=user,
        )
    except Exception as e:
        print(f'[StatusChangeLog] log failed: {e}')
        return None
