"""Keep every Item attached to an ItemGroup.

Called at the end of each ETL load (PowerBI + dbt). Idempotent and
governance-safe: it only links items that have NO group yet, and only
*creates* groups that don't exist. Existing ItemGroups (and their curated
owner / steward / status / annotation / primary_item) are never modified —
so a re-import that adds a new (workspace, dataset) instance of an existing
measure simply links it to that measure's existing group and inherits the
curation.

One exception heals *renamed* measures: when a measure is renamed in Power BI
the ETL upsert refreshes ``Item.group_id`` from the new name, but the item
stays linked to its OLD group (the linking pass only fills items with NO
group). Such an item then shows as its own card, split from the other
instances of its new name. We detach any PB_MEASURE whose ``group_id`` no
longer matches its linked group's ``group_key`` so the pass below re-files it
under the group for its current name (created if missing).

Grouping keys (match the 0029 migration exactly):
  * PB_MEASURE with a group_id -> key = group_id,         kind=measure_name
  * everything else            -> key = "item::{item_id}", kind=singleton
"""
from django.db.models import F

from ..models import Item, ItemGroup

_CHUNK = 900   # stays under SQLite's 999-variable limit for __in queries


def _key_kind(item_type, item_id, group_id):
    if item_type == 'PB_MEASURE' and group_id:
        return group_id, ItemGroup.KIND_MEASURE_NAME
    return f'item::{item_id}', ItemGroup.KIND_SINGLETON


def _chunked(seq, n=_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _detach_renamed_measures(organization_id=None):
    """Unlink any PB_MEASURE whose ``group_id`` (refreshed from a renamed
    name) no longer matches its linked group's ``group_key``. Resets the
    denormalized status to the default; ``ensure_item_groups`` re-links them
    below and the status-mirror step re-applies the destination group's
    status. Returns the number of items detached."""
    stale = (
        Item.objects
        .filter(item_type='PB_MEASURE', item_group__isnull=False,
                group_id__isnull=False)
        .exclude(group_id=F('item_group__group_key'))
    )
    if organization_id is not None:
        stale = stale.filter(organization_id=organization_id)
    stale_ids = list(stale.values_list('item_id', flat=True))
    for chunk in _chunked(stale_ids):
        Item.objects.filter(item_id__in=chunk).update(
            item_group=None, status='UNVERIFIED')
    return len(stale_ids)


def ensure_item_groups(organization_id=None, batch_size=2000):
    """Create/link ItemGroups for any items missing one. Returns the number
    of items linked."""
    _detach_renamed_measures(organization_id)

    qs = Item.objects.filter(item_group__isnull=True)
    if organization_id is not None:
        qs = qs.filter(organization_id=organization_id)

    pending = list(
        qs.values('item_id', 'item_type', 'group_id', 'organization_id')
          .iterator(chunk_size=batch_size)
    )
    if not pending:
        return 0

    keyed = []          # (item_id, key, kind, org_id)
    keys = set()
    for r in pending:
        k, kind = _key_kind(r['item_type'], r['item_id'], r['group_id'])
        keyed.append((r['item_id'], k, kind, r['organization_id']))
        keys.add(k)

    # Which groups already exist (chunked to respect SQLite's param limit).
    existing = {}
    for chunk in _chunked(keys):
        existing.update(
            ItemGroup.objects.filter(group_key__in=chunk)
            .values_list('group_key', 'id')
        )

    to_create, seen = [], set()
    for _item_id, k, kind, org_id in keyed:
        if k in existing or k in seen:
            continue
        seen.add(k)
        to_create.append(ItemGroup(group_key=k, kind=kind, organization_id=org_id))
    if to_create:
        ItemGroup.objects.bulk_create(
            to_create, batch_size=batch_size, ignore_conflicts=True)
        existing = {}
        for chunk in _chunked(keys):
            existing.update(
                ItemGroup.objects.filter(group_key__in=chunk)
                .values_list('group_key', 'id')
            )

    link = [
        Item(item_id=item_id, item_group_id=existing[k])
        for item_id, k, _kind, _org in keyed if k in existing
    ]
    Item.objects.bulk_update(link, ['item_group'], batch_size=batch_size)

    # Mirror each (possibly already-curated) group's status onto its freshly
    # linked items so the denormalized Item.status column starts consistent.
    # Only groups whose status differs from the default need a write.
    group_ids = {existing[k] for _id, k, _kind, _org in keyed if k in existing}
    gstatus = {}
    for chunk in _chunked(group_ids):
        gstatus.update(
            ItemGroup.objects.filter(id__in=chunk)
            .exclude(status='UNVERIFIED')
            .values_list('id', 'status')
        )
    if gstatus:
        status_link = [
            Item(item_id=item_id, status=gstatus[existing[k]])
            for item_id, k, _kind, _org in keyed
            if k in existing and existing[k] in gstatus
        ]
        if status_link:
            Item.objects.bulk_update(status_link, ['status'], batch_size=batch_size)

    # A singleton group's primary IS its one item — set it where unset.
    sing = {
        existing[k]: item_id
        for item_id, k, kind, _org in keyed
        if kind == ItemGroup.KIND_SINGLETON and k in existing
    }
    if sing:
        grps = []
        for chunk in _chunked(sing.keys()):
            for g in ItemGroup.objects.filter(id__in=chunk, primary_item__isnull=True):
                g.primary_item_id = sing[g.id]
                grps.append(g)
        if grps:
            ItemGroup.objects.bulk_update(grps, ['primary_item'], batch_size=batch_size)

    return len(link)
