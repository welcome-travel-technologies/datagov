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

**A rename must not cost the curation.** A renamed measure used to land in a
brand-new, blank group — losing its owner, steward, category, definition and
status. So before unlinking we read the metadata off the group it is leaving
and carry it forward: when the destination group has to be created, the
incoming item seeds it (and becomes its primary item). The item is still
attached to its old group at that moment, which is why the metadata never has
to be stored on the item itself.

A destination group that ALREADY exists keeps its own metadata — it was curated
by someone, and one renamed instance must not rewrite it. Where several renamed
items land in the same new group, the lowest ``item_id`` seeds it, so the result
doesn't depend on row order.

Grouping keys (match the 0029 migration exactly):
  * PB_MEASURE with a group_id -> key = group_id,         kind=measure_name
  * everything else            -> key = "item::{item_id}", kind=singleton
"""
from django.db import transaction
from django.db.models import F

from ..models import Item, ItemGroup

_CHUNK = 900   # stays under SQLite's 999-variable limit for __in queries

# What a renamed item carries from its old group to the new one it creates.
# `definition` travels too: leaving a group must not silently drop the measure
# out of the business definition it belonged to.
_CARRIED_FIELDS = (
    'definition_id', 'ownership_department_id', 'ownership_person_id',
    'steward_id', 'category_id', 'status', 'custom_description',
)


def _key_kind(item_type, item_id, group_id):
    if item_type == 'PB_MEASURE' and group_id:
        return group_id, ItemGroup.KIND_MEASURE_NAME
    return f'item::{item_id}', ItemGroup.KIND_SINGLETON


def _chunked(seq, n=_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _detach_renamed_measures(organization_id=None):
    """Unlink any PB_MEASURE whose ``group_id`` (refreshed from a renamed name)
    no longer matches its linked group's ``group_key``.

    Returns ``{item_id: {field: value}}`` — the metadata each detached item is
    carrying out of the group it just left, for ``ensure_item_groups`` to seed
    the destination group with.
    """
    stale = (
        Item.objects
        .filter(item_type='PB_MEASURE', item_group__isnull=False,
                group_id__isnull=False)
        .exclude(group_id=F('item_group__group_key'))
    )
    if organization_id is not None:
        stale = stale.filter(organization_id=organization_id)

    # Read the old group's metadata while the link still exists — this is the
    # only moment it's reachable.
    carried = {}
    for row in stale.values('item_id', *[f'item_group__{f}' for f in _CARRIED_FIELDS]):
        carried[row['item_id']] = {
            field: row[f'item_group__{field}'] for field in _CARRIED_FIELDS
        }

    stale_ids = list(carried)
    for chunk in _chunked(stale_ids):
        # Status is NOT reset here any more. It is part of what the item carries;
        # blanking it was how a rename used to silently un-verify a measure.
        Item.objects.filter(item_id__in=chunk).update(item_group=None)
        # A detached item must not stay its old group's primary — that would
        # leave the old group pointing at a member it no longer owns. Clear it;
        # the read path falls back to the group's first remaining item.
        ItemGroup.objects.filter(primary_item_id__in=chunk).update(
            primary_item=None)
    return carried


def ensure_item_groups(organization_id=None, batch_size=2000):
    """Create/link ItemGroups for any items missing one. Returns the number
    of items linked."""
    # One transaction: the detach reads metadata that only exists while the old
    # link is intact, and carries it in memory to the create below. A crash
    # between the two would strand items with their curation unrecoverable.
    with transaction.atomic():
        carried = _detach_renamed_measures(organization_id)
        return _link_pending(carried, organization_id, batch_size)


def _link_pending(carried, organization_id=None, batch_size=2000):
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

    # Which carried metadata seeds which brand-new group. Sorted by item_id so a
    # group receiving several renamed items always takes the same one's values.
    seed_by_key = {}
    seed_item_by_key = {}
    for item_id, k, _kind, _org in sorted(keyed, key=lambda r: r[0]):
        if k in existing or item_id not in carried:
            continue           # existing groups keep their own curation
        if k not in seed_by_key:
            seed_by_key[k] = carried[item_id]
            seed_item_by_key[k] = item_id

    to_create, seen = [], set()
    for _item_id, k, kind, org_id in keyed:
        if k in existing or k in seen:
            continue
        seen.add(k)
        # A renamed measure hands its old group's metadata to the group it
        # creates, so the rename costs nothing.
        to_create.append(ItemGroup(
            group_key=k, kind=kind, organization_id=org_id, **seed_by_key.get(k, {})
        ))
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

    # Mirror each group's status onto its freshly linked items so the
    # denormalized Item.status column starts consistent. Every linked group is
    # read, not just the non-default ones: a renamed item now arrives carrying
    # its old status, so landing it in an UNVERIFIED group has to pull it back
    # down — skipping those groups would leave the item claiming VERIFIED while
    # its group says otherwise.
    group_ids = {existing[k] for _id, k, _kind, _org in keyed if k in existing}
    gstatus = {}
    for chunk in _chunked(group_ids):
        gstatus.update(
            ItemGroup.objects.filter(id__in=chunk).values_list('id', 'status')
        )
    status_link = [
        Item(item_id=item_id, status=gstatus[existing[k]])
        for item_id, k, _kind, _org in keyed
        if k in existing and existing[k] in gstatus
    ]
    if status_link:
        Item.objects.bulk_update(status_link, ['status'], batch_size=batch_size)

    # A singleton group's primary IS its one item — set it where unset. A
    # measure group born from a rename gets the item that seeded it: that item
    # supplied the group's metadata, so it's the natural representative rather
    # than whichever instance the heuristic would otherwise land on.
    sing = {
        existing[k]: item_id
        for item_id, k, kind, _org in keyed
        if kind == ItemGroup.KIND_SINGLETON and k in existing
    }
    for k, item_id in seed_item_by_key.items():
        if k in existing:
            sing.setdefault(existing[k], item_id)
    if sing:
        grps = []
        for chunk in _chunked(sing.keys()):
            for g in ItemGroup.objects.filter(id__in=chunk, primary_item__isnull=True):
                g.primary_item_id = sing[g.id]
                grps.append(g)
        if grps:
            ItemGroup.objects.bulk_update(grps, ['primary_item'], batch_size=batch_size)

    return len(link)
