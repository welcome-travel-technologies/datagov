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
    """Apply or restore one group soft-delete episode without losing provenance.

    ``Item.deleted`` can already be true because the source retired that one
    asset. A group delete therefore stamps only active children with the
    group's exact ``deleted_at`` marker. Restore clears only rows carrying that
    same marker; independently source-obsolete rows remain deleted.
    """
    if group is None:
        return 0
    qs = Item.objects.filter(item_group=group)
    if deleted:
        from django.utils import timezone
        marker = group.deleted_at
        if marker is None:
            marker = timezone.now()
            group.deleted_at = marker
            group.save(update_fields=['deleted_at'])
        return qs.filter(deleted=False).update(
            deleted=True, deleted_at=marker, status=group.status,
        )
    marker = group.deleted_at
    if marker is None:
        # Without an episode marker there is no safe way to distinguish a
        # group-induced delete from source-level obsolescence.
        return 0
    return qs.filter(
        deleted=True,
        deleted_at=marker,
    ).update(deleted=False, deleted_at=None)
