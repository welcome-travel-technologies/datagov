"""Cascade an ItemGroup's governance state DOWN to its Items.

The ItemGroup remains the single source of truth for ``status`` and the
group-level ``deleted`` flag; these helpers mirror that state onto every
``Item`` in the group so item-level views (PowerBI Cleanup, BigQuery export)
stay consistent and a group change visibly propagates to its items.

Called from the two API write sites in ``views.py`` (the Data Dictionary group
edit / mark-to-delete, and the per-item delete that auto-DEPRECATEs its group),
mirroring how ``sync_status_task`` / ``log_status_change`` are invoked. Bulk
``update()`` is used so a measure group with many instances is one query and no
per-row signals fire.
"""
from ..models import Item


def cascade_status_to_items(group):
    """Mirror ``group.status`` onto every Item in the group.

    Returns the number of rows updated. No-op for a missing group.
    """
    if group is None:
        return 0
    return (
        Item.objects.filter(item_group=group)
        .exclude(status=group.status)
        .update(status=group.status)
    )


def cascade_delete_to_items(group, deleted):
    """Soft-delete (``deleted=True``) or restore (``deleted=False``) every Item
    in the group to match the group-level flag.

    On delete we also stamp ``deleted_at`` and force the item's status mirror to
    the group's (DELETED by the time this runs). On restore we clear both the
    flag and the timestamp. Returns the number of rows updated.
    """
    if group is None:
        return 0
    qs = Item.objects.filter(item_group=group)
    if deleted:
        from django.utils import timezone
        return qs.exclude(deleted=True).update(
            deleted=True, deleted_at=timezone.now(), status=group.status,
        )
    return qs.filter(deleted=True).update(deleted=False, deleted_at=None)
