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
from django.db import IntegrityError, connection, transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.utils import timezone

from ..models import GovernanceTask, Item, ItemGroup

_CHUNK = 900   # stays under SQLite's 999-variable limit for __in queries
_GROUP_LOCK_NAMESPACE = 0x574443  # "WDC"
_GROUP_LOCK_GLOBAL_KEY = 2


class ItemGroupTenantCollision(RuntimeError):
    """A globally unique group key is already owned by another tenant."""


# What a renamed item carries from its old group to the new one it creates.
# `definition` travels too: leaving a group must not silently drop the measure
# out of the business definition it belonged to.
_CARRIED_FIELDS = (
    'definition_id', 'ownership_department_id', 'ownership_person_id',
    'steward_id', 'category_id', 'status', 'custom_description',
    'deleted', 'deleted_at',
)
_CARRIED_TENANT_FIELDS = {
    'definition_id': 'item_group__definition__organization_id',
    'ownership_department_id':
        'item_group__ownership_department__organization_id',
    'ownership_person_id': 'item_group__ownership_person__organization_id',
    'steward_id': 'item_group__steward__organization_id',
    'category_id': 'item_group__category__organization_id',
}


def _key_kind(item_type, item_id, group_id):
    if item_type == 'PB_MEASURE' and group_id:
        return group_id, ItemGroup.KIND_MEASURE_NAME
    return f'item::{item_id}', ItemGroup.KIND_SINGLETON


def _chunked(seq, n=_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _lock_groups(group_ids):
    """Lock ItemGroups in deterministic order before Items and tasks."""
    for chunk in _chunked(sorted({pk for pk in group_ids if pk is not None})):
        list(
            ItemGroup.objects.select_for_update(of=('self',))
            .filter(pk__in=chunk)
            .order_by('pk')
            .values_list('pk', flat=True)
        )


def _lock_items(item_ids):
    """Lock Items in deterministic primary-key order after their groups."""
    for chunk in _chunked(sorted({pk for pk in item_ids if pk is not None})):
        list(
            Item.objects.select_for_update(of=('self',))
            .filter(pk__in=chunk)
            .order_by('pk')
            .values_list('pk', flat=True)
        )


def _acquire_grouping_advisory_lock(organization_id):
    """Serialize grouping per org; an unscoped pass excludes every org pass."""
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        if organization_id is None:
            cursor.execute(
                'SELECT pg_advisory_xact_lock(%s, %s)',
                [_GROUP_LOCK_NAMESPACE, _GROUP_LOCK_GLOBAL_KEY],
            )
            return
        cursor.execute(
            'SELECT pg_advisory_xact_lock_shared(%s, %s)',
            [_GROUP_LOCK_NAMESPACE, _GROUP_LOCK_GLOBAL_KEY],
        )
        # Keep the second key non-zero so it cannot alias the global gate.
        org_key = (int(organization_id) % 2_000_000_000) + 100
        cursor.execute(
            'SELECT pg_advisory_xact_lock(%s, %s)',
            [_GROUP_LOCK_NAMESPACE, org_key],
        )


def _detach_cross_tenant_links(organization_id=None):
    """Quarantine Item→ItemGroup links whose tenants are not exactly equal.

    This intentionally carries no metadata: governance from another tenant is
    never a valid rename seed. The item's ingestion-owned ``group_id`` remains
    intact, but the exact-tenant lookup in ``_link_pending`` will refuse to
    resolve it back to the foreign group.
    """
    linked = Item.objects.filter(item_group__isnull=False)
    if organization_id is not None:
        linked = linked.filter(organization_id=organization_id)
    valid_tenant = (
        Q(organization_id=F('item_group__organization_id'))
        | Q(
            organization_id__isnull=True,
            item_group__organization_id__isnull=True,
        )
    )
    prelim = list(
        linked.exclude(valid_tenant).values('item_id', 'item_group_id')
    )
    if not prelim:
        return 0

    # A foreign group may still point back at the invalid item as its primary.
    # Lock that group before the item, then re-check the relationship so a
    # concurrent repair cannot be undone by this pass.
    _lock_groups(row['item_group_id'] for row in prelim)
    _lock_items(row['item_id'] for row in prelim)
    invalid_ids = []
    for chunk in _chunked(sorted(row['item_id'] for row in prelim)):
        invalid_ids.extend(
            linked.exclude(valid_tenant)
            .filter(item_id__in=chunk)
            .values_list('item_id', flat=True)
        )
    for chunk in _chunked(invalid_ids):
        Item.objects.filter(item_id__in=chunk).update(item_group=None)
        ItemGroup.objects.filter(primary_item_id__in=chunk).update(
            primary_item=None,
        )
    return len(invalid_ids)


def _detach_renamed_measures(organization_id=None):
    """Unlink any PB_MEASURE whose ``group_id`` (refreshed from a renamed name)
    no longer matches its linked group's ``group_key``.

    Returns ``(carried, source_group_ids)``. ``carried`` is
    ``{item_id: {field: value}}`` — the metadata each detached item takes to a
    newly created destination. ``source_group_ids`` lets the caller retire a
    source group when every member left it.
    """
    stale = (
        Item.objects
        .filter(item_type='PB_MEASURE', item_group__isnull=False,
                group_id__isnull=False)
        .filter(
            Q(organization_id=F('item_group__organization_id'))
            | Q(
                organization_id__isnull=True,
                item_group__organization_id__isnull=True,
            )
        )
        .exclude(group_id=F('item_group__group_key'))
    )
    if organization_id is not None:
        stale = stale.filter(organization_id=organization_id)

    prelim = list(
        stale.values(
            'item_id', 'organization_id', 'item_group_id', 'group_id',
        )
    )
    if not prelim:
        return {}, set(), {}

    # Lock all existing source/destination groups before locking any source
    # member item. Concurrent curation completes first and is re-read below.
    destination_keys = {row['group_id'] for row in prelim}
    destination_group_ids = set()
    for chunk in _chunked(sorted(destination_keys)):
        destination_group_ids.update(
            ItemGroup.objects.filter(group_key__in=chunk)
            .values_list('id', flat=True)
        )
    preliminary_source_ids = {row['item_group_id'] for row in prelim}
    _lock_groups(preliminary_source_ids | destination_group_ids)

    source_member_ids = set()
    for chunk in _chunked(sorted(preliminary_source_ids)):
        source_member_ids.update(
            Item.objects.filter(item_group_id__in=chunk)
            .values_list('item_id', flat=True)
        )
    _lock_items(source_member_ids)

    # Defer a row changed while waiting for locks to the next idempotent pass.
    expected = {
        row['item_id']: (
            row['organization_id'], row['item_group_id'], row['group_id'],
        )
        for row in prelim
    }

    # Read the old group's metadata while the link still exists — this is the
    # only moment it's reachable.
    carried = {}
    source_by_item = {}
    for row in stale.values(
            'item_id', 'organization_id', 'item_group_id', 'group_id',
            *[f'item_group__{f}' for f in _CARRIED_FIELDS],
            *_CARRIED_TENANT_FIELDS.values()):
        if expected.get(row['item_id']) != (
            row['organization_id'], row['item_group_id'], row['group_id'],
        ):
            continue
        metadata = {
            field: row[f'item_group__{field}'] for field in _CARRIED_FIELDS
        }
        for field, related_org_key in _CARRIED_TENANT_FIELDS.items():
            if (
                metadata[field] is not None
                and row[related_org_key] != row['organization_id']
            ):
                metadata[field] = None
        carried[row['item_id']] = metadata
        source_by_item[row['item_id']] = row['item_group_id']

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
    return carried, set(source_by_item.values()), source_by_item


def _transfer_manual_dismissals(
        new_destination_by_seed_item, source_by_item):
    """Carry the active manual-Done episode across a pure rename.

    Only genuinely new destinations inherit history. If the deterministic seed
    source became empty, its dismissal row is moved; if it still has members,
    the row is cloned so both real groups retain the dismissal. Existing
    destinations always keep their own history.
    """
    pairs = sorted({
        (source_by_item[item_id], destination_id)
        for item_id, destination_id in new_destination_by_seed_item.items()
        if item_id in source_by_item
    })
    if not pairs:
        return 0

    from ..governance_tasks import (
        REASON_POLICY,
        _default_condition_active,
        _resolve_assignee_ids,
        _title_for,
    )

    source_ids = {source_id for source_id, _destination_id in pairs}
    destination_ids = {destination_id for _source_id, destination_id in pairs}
    all_group_ids = source_ids | destination_ids
    group_rows = {}
    for chunk in _chunked(sorted(all_group_ids)):
        group_rows.update({
            row['id']: row
            for row in (
                ItemGroup.objects.filter(id__in=chunk)
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
                .values(
                    'id', 'organization_id', 'kind', 'status', 'deleted',
                    'category_id', 'group_key',
                    'ownership_person_id',
                    'ownership_person__organization_id',
                    'steward_id', 'steward__organization_id',
                    'primary_item__item_name', 'primary_item__item_id',
                    'has_items', 'has_active_items',
                )
            )
        })

    nonempty_source_ids = set()
    for chunk in _chunked(sorted(source_ids)):
        nonempty_source_ids.update(
            Item.objects.filter(item_group_id__in=chunk)
            .values_list('item_group_id', flat=True)
            .distinct()
        )
    source_is_empty = {
        source_id: source_id not in nonempty_source_ids
        for source_id in source_ids
    }

    task_rows = []
    for chunk in _chunked(sorted(all_group_ids)):
        task_rows.extend(
            GovernanceTask.objects.select_for_update(of=('self',))
            .filter(item_group_id__in=chunk)
            .filter(
                state=GovernanceTask.STATE_DONE,
                closed_reason=GovernanceTask.CLOSED_MANUAL,
                condition_cleared_at__isnull=True,
            )
            .order_by('id')
        )
    by_source_reason = {}
    destination_dismissals = set()
    for task in task_rows:
        key = (task.item_group_id, task.reason)
        if task.item_group_id in destination_ids:
            destination_dismissals.add(key)
        elif task.item_group_id in source_ids:
            by_source_reason.setdefault(key, []).append(task)

    now = timezone.now()
    changed = []
    clones = []
    transferred = 0
    for source_id in sorted(source_ids):
        for reason in REASON_POLICY:
            candidates = by_source_reason.get((source_id, reason), [])
            if not candidates:
                continue

            active_destinations = []
            for pair_source_id, destination_id in pairs:
                if pair_source_id != source_id:
                    continue
                destination = group_rows.get(destination_id)
                destination_key = (destination_id, reason)
                if (
                    destination is not None
                    and _default_condition_active(destination, reason)
                    and destination_key not in destination_dismissals
                ):
                    active_destinations.append(destination)

            if not active_destinations:
                if source_is_empty[source_id]:
                    for task in candidates:
                        task.condition_cleared_at = now
                        task.updated_at = now
                        changed.append(task)
                continue

            source_task = candidates[0]
            for index, destination in enumerate(active_destinations):
                destination_id = destination['id']
                label = (
                    destination['primary_item__item_name']
                    or destination['primary_item__item_id']
                    or destination['group_key']
                    or 'asset'
                )
                assignee_id, role = _resolve_assignee_ids(
                    destination, reason,
                )
                common = {
                    'organization_id': destination['organization_id'],
                    'item_group_id': destination_id,
                    'assignee_id': assignee_id,
                    'assignee_role': role,
                    'trigger_status': REASON_POLICY[reason]['trigger_status'],
                    'title': _title_for(reason, label),
                }
                if source_is_empty[source_id] and index == 0:
                    for field, value in common.items():
                        setattr(source_task, field, value)
                    source_task.updated_at = now
                    changed.append(source_task)
                else:
                    clones.append(GovernanceTask(
                        **common,
                        reason=reason,
                        state=GovernanceTask.STATE_DONE,
                        closed_reason=GovernanceTask.CLOSED_MANUAL,
                        completed_at=source_task.completed_at,
                        completed_by_id=source_task.completed_by_id,
                        condition_cleared_at=None,
                    ))
                destination_dismissals.add((destination_id, reason))
                transferred += 1

            # Corrupt duplicate unresolved dismissals must not suppress future
            # episodes forever after their source disappears.
            if source_is_empty[source_id]:
                for duplicate in candidates[1:]:
                    duplicate.condition_cleared_at = now
                    duplicate.updated_at = now
                    changed.append(duplicate)

    if clones:
        GovernanceTask.objects.bulk_create(clones, batch_size=1000)
    if changed:
        GovernanceTask.objects.bulk_update(
            changed,
            [
                'organization', 'item_group', 'assignee', 'assignee_role',
                'trigger_status', 'title', 'condition_cleared_at',
                'updated_at',
            ],
        )
    return transferred


def _retire_empty_source_groups(source_group_ids):
    """Delete rename-source groups that no longer own an Item.

    Keeping them inflated Definition member counts and left open governance
    tasks pointing at an asset that no longer existed. Close those tasks as
    auto-resolved first so the audit trail survives the group's ``SET_NULL``
    deletion, then remove the empty group. A source with even one remaining
    member is real and remains untouched.
    """
    if not source_group_ids:
        return 0
    empty_ids = []
    for chunk in _chunked(sorted(source_group_ids)):
        empty_ids.extend(
            ItemGroup.objects.filter(
                id__in=chunk, items__isnull=True,
            ).values_list('id', flat=True)
        )
    if not empty_ids:
        return 0

    now = timezone.now()
    open_task_ids = []
    for chunk in _chunked(sorted(empty_ids)):
        open_task_ids.extend(
            GovernanceTask.objects.select_for_update(of=('self',))
            .filter(
                item_group_id__in=chunk,
                state=GovernanceTask.STATE_OPEN,
            )
            .order_by('id')
            .values_list('id', flat=True)
        )
    for chunk in _chunked(sorted(open_task_ids)):
        GovernanceTask.objects.filter(id__in=chunk).update(
            state=GovernanceTask.STATE_DONE,
            completed_at=now,
            completed_by=None,
            closed_reason=GovernanceTask.CLOSED_RESOLVED,
            updated_at=now,
        )
    for chunk in _chunked(sorted(empty_ids)):
        ItemGroup.objects.filter(id__in=chunk).delete()
    return len(empty_ids)


def _enforce_soft_deleted_group_items(organization_id=None):
    """Reassert group soft deletion after ETL refreshes a present child row.

    The loaders legitimately clear source-level ``Item.deleted`` when a row
    reappears. A curated soft-deleted group is the stronger one-way rule,
    though: every linked child remains deleted until that group is explicitly
    restored. Active groups never clear independent child deletion here.
    """
    linked = (
        Item.objects
        .filter(
            item_group__isnull=False,
            item_group__deleted=True,
            deleted=False,
        )
        .filter(
            Q(organization_id=F('item_group__organization_id'))
            | Q(
                organization_id__isnull=True,
                item_group__organization_id__isnull=True,
            )
        )
    )
    if organization_id is not None:
        linked = linked.filter(organization_id=organization_id)
    group_deleted_at = (
        ItemGroup.objects
        .filter(pk=OuterRef('item_group_id'))
        .values('deleted_at')[:1]
    )
    return linked.update(
        deleted=True,
        deleted_at=Subquery(group_deleted_at),
    )


def ensure_item_groups(organization_id=None, batch_size=2000):
    """Create/link ItemGroups for any items missing one. Returns the number
    of items linked."""
    # Cross-tenant quarantine is committed independently. If a later destination
    # key collision aborts grouping, an unsafe legacy link must not come back.
    with transaction.atomic():
        _detach_cross_tenant_links(organization_id)

    # Rename detach, destination creation/linking, dismissal transfer, source
    # retirement, and task reconciliation are one unit. Any key collision rolls
    # this transaction back to the original curated source group.
    with transaction.atomic():
        _acquire_grouping_advisory_lock(organization_id)
        carried, source_group_ids, source_by_item = (
            _detach_renamed_measures(organization_id)
        )
        (
            linked,
            new_destination_by_seed_item,
            reconcile_destination_ids,
        ) = _link_pending(carried, organization_id, batch_size)
        _transfer_manual_dismissals(
            new_destination_by_seed_item, source_by_item,
        )
        _retire_empty_source_groups(source_group_ids)

        # Rename destinations and preserved empty groups receiving their first
        # returning member need immediate reconciliation. Brand-new ordinary
        # pending groups do not: that would turn an ETL backfill into a sweep.
        if reconcile_destination_ids:
            from ..governance_tasks import sync_group_metadata_tasks

            sync_group_metadata_tasks(
                reconcile_destination_ids,
                create_missing_category=True,
                create_missing_status=True,
            )
        _enforce_soft_deleted_group_items(organization_id)

    return linked


def _link_pending(carried, organization_id=None, batch_size=2000):
    qs = Item.objects.filter(item_group__isnull=True)
    if organization_id is not None:
        qs = qs.filter(organization_id=organization_id)

    pending = list(
        qs.values('item_id', 'item_type', 'group_id', 'organization_id')
          .iterator(chunk_size=batch_size)
    )
    if not pending:
        return 0, {}, set()

    keyed = []          # (item_id, key, kind, org_id)
    keys = set()
    for r in pending:
        k, kind = _key_kind(r['item_type'], r['item_id'], r['group_id'])
        keyed.append((r['item_id'], k, kind, r['organization_id']))
        keys.add(k)

    preliminary_identity = {
        item_id: (item_type, group_id, org_id)
        for item_id, item_type, group_id, org_id in (
            (
                row['item_id'], row['item_type'], row['group_id'],
                row['organization_id'],
            )
            for row in pending
        )
    }

    # Lock every destination group visible for the preliminary key set before
    # locking pending Items. Lifecycle/metadata are then re-read after the lock
    # wait, so a concurrent curation write cannot be lost.
    visible_group_ids = set()
    for chunk in _chunked(sorted(keys)):
        visible_group_ids.update(
            ItemGroup.objects.filter(group_key__in=chunk)
            .values_list('id', flat=True)
        )
    _lock_groups(visible_group_ids)
    _lock_items(preliminary_identity)

    current_pending = []
    for chunk in _chunked(sorted(preliminary_identity)):
        current_pending.extend(
            qs.filter(item_id__in=chunk)
            .values('item_id', 'item_type', 'group_id', 'organization_id')
        )
    keyed = []
    keys = set()
    for row in current_pending:
        if preliminary_identity.get(row['item_id']) != (
            row['item_type'], row['group_id'], row['organization_id'],
        ):
            # Ingestion changed this row during the lock wait. Leave it pending
            # for the next idempotent pass rather than using the wrong group.
            continue
        key, kind = _key_kind(
            row['item_type'], row['item_id'], row['group_id'],
        )
        keyed.append((
            row['item_id'], key, kind, row['organization_id'],
        ))
        keys.add(key)
    if not keyed:
        return 0, {}, set()

    # Group identity is exact tenant + key. ``group_key`` is still globally
    # unique in the current schema, so the global read is retained only to
    # diagnose legacy collisions; every usable mapping is composite.
    wanted = {(org_id, key) for _item_id, key, _kind, org_id in keyed}
    existing = {}
    global_by_key = {}
    for chunk in _chunked(keys):
        for org_id, key, group_id in (
            ItemGroup.objects.select_for_update(of=('self',))
            .filter(group_key__in=chunk)
            .values_list('organization_id', 'group_key', 'id')
        ):
            global_by_key[key] = (org_id, group_id)
            composite = (org_id, key)
            if composite in wanted:
                existing[composite] = group_id

    requested_orgs = {}
    for _item_id, key, _kind, org_id in keyed:
        requested_orgs.setdefault(key, set()).add(org_id)

    collision_details = []
    for key in sorted(requested_orgs):
        org_ids = requested_orgs[key]
        global_row = global_by_key.get(key)
        if global_row is not None:
            existing_org_id, group_id = global_row
            for requested_org_id in sorted(
                    org_ids, key=lambda value: (-1 if value is None else value)):
                if requested_org_id != existing_org_id:
                    collision_details.append(
                        f'{key!r} requested for organization '
                        f'{requested_org_id!r}, but group #{group_id} belongs '
                        f'to organization {existing_org_id!r}'
                    )
        elif len(org_ids) > 1:
            requested = ', '.join(
                repr(value)
                for value in sorted(
                    org_ids, key=lambda value: (-1 if value is None else value))
            )
            collision_details.append(
                f'{key!r} requested concurrently for organizations {requested}'
            )

    if collision_details:
        raise ItemGroupTenantCollision(
            'Cannot link ItemGroups across organizations: '
            + '; '.join(collision_details)
            + '. Correct the legacy group_key/organization collision before '
              'retrying.'
        )

    # A migration-preserved empty group keeps its curation and cleared task
    # history. When its first member returns, reconcile only that exact
    # destination (plus carried rename destinations below), not every ordinary
    # ETL link.
    preexisting_destination_ids = set(existing.values())
    nonempty_preexisting_ids = set()
    for chunk in _chunked(sorted(preexisting_destination_ids)):
        nonempty_preexisting_ids.update(
            Item.objects.filter(item_group_id__in=chunk)
            .values_list('item_group_id', flat=True)
            .distinct()
        )
    reappeared_destination_ids = (
        preexisting_destination_ids - nonempty_preexisting_ids
    )

    # Which carried metadata seeds which brand-new group. Sorted by item_id so a
    # group receiving several renamed items always takes the same one's values.
    seed_by_identity = {}
    seed_item_by_identity = {}
    for item_id, k, _kind, org_id in sorted(keyed, key=lambda r: r[0]):
        composite = (org_id, k)
        if composite in existing or item_id not in carried:
            continue           # existing groups keep their own curation
        if composite not in seed_by_identity:
            seed_by_identity[composite] = carried[item_id]
            seed_item_by_identity[composite] = item_id

    to_create, seen = [], set()
    for _item_id, k, kind, org_id in keyed:
        composite = (org_id, k)
        if composite in existing or composite in seen:
            continue
        seen.add(composite)
        # A renamed measure hands its old group's metadata to the group it
        # creates, so the rename costs nothing.
        to_create.append(ItemGroup(
            group_key=k,
            kind=kind,
            organization_id=org_id,
            **seed_by_identity.get(composite, {}),
        ))
    if to_create:
        try:
            # Do not silently ignore a concurrent/global key collision. A
            # failed rename must roll back the detach and preserve its source
            # curation; the next pass can safely retry.
            with transaction.atomic():
                ItemGroup.objects.bulk_create(
                    to_create, batch_size=batch_size,
                )
        except IntegrityError as exc:
            raise ItemGroupTenantCollision(
                'An ItemGroup destination was claimed concurrently; rename '
                'repair was rolled back and can be retried safely.'
            ) from exc
        existing = {}
        global_by_key = {}
        for chunk in _chunked(keys):
            for org_id, key, group_id in (
                ItemGroup.objects.select_for_update(of=('self',))
                .filter(group_key__in=chunk)
                .values_list('organization_id', 'group_key', 'id')
            ):
                global_by_key[key] = (org_id, group_id)
                composite = (org_id, key)
                if composite in wanted:
                    existing[composite] = group_id

        # A concurrent writer can claim a globally unique key for another
        # tenant after the first read. Never convert that race into a foreign
        # link; report every unresolved exact identity.
        unresolved = wanted - set(existing)
        if unresolved:
            details = []
            for org_id, key in sorted(
                    unresolved,
                    key=lambda value: (
                        -1 if value[0] is None else value[0], value[1],
                    )):
                global_row = global_by_key.get(key)
                if global_row is None:
                    details.append(
                        f'{key!r} for organization {org_id!r} was not created'
                    )
                else:
                    foreign_org_id, group_id = global_row
                    details.append(
                        f'{key!r} requested for organization {org_id!r}, but '
                        f'group #{group_id} belongs to organization '
                        f'{foreign_org_id!r}'
                    )
            raise ItemGroupTenantCollision(
                'Could not create exact-tenant ItemGroups: '
                + '; '.join(details)
                + '. Correct the group_key/organization collision before '
                  'retrying.'
            )

    created_identities = {
        (group.organization_id, group.group_key) for group in to_create
    }
    # ``to_create`` succeeded atomically, so these are the genuinely new
    # destinations seeded from source curation (not concurrent/existing rows).
    new_destination_by_seed_item = {
        seed_item_by_identity[identity]: existing[identity]
        for identity in created_identities
        if identity in existing and identity in seed_item_by_identity
    }
    carried_destination_ids = {
        existing[(org_id, key)]
        for item_id, key, _kind, org_id in keyed
        if item_id in carried and (org_id, key) in existing
    }
    reconcile_destination_ids = (
        carried_destination_ids | reappeared_destination_ids
    )

    link = [
        Item(item_id=item_id, item_group_id=existing[(org_id, k)])
        for item_id, k, _kind, org_id in keyed
        if (org_id, k) in existing
    ]
    Item.objects.bulk_update(link, ['item_group'], batch_size=batch_size)

    # Mirror status onto every fresh link. A deleted destination always forces
    # child deletion. An active destination clears deletion only for a carried
    # current rename; quarantined legacy pending rows are not proof of return.
    group_ids = {
        existing[(org_id, k)]
        for _id, k, _kind, org_id in keyed
        if (org_id, k) in existing
    }
    lifecycle = {}
    for chunk in _chunked(group_ids):
        lifecycle.update({
            row['id']: row
            for row in (
                ItemGroup.objects.select_for_update(of=('self',))
                .filter(id__in=chunk)
                .values(
                'id', 'status', 'deleted', 'deleted_at')
            )
        })
    status_link = [
        Item(
            item_id=item_id,
            status=lifecycle[existing[(org_id, k)]]['status'],
        )
        for item_id, k, _kind, org_id in keyed
        if (
            (org_id, k) in existing
            and existing[(org_id, k)] in lifecycle
        )
    ]
    if status_link:
        Item.objects.bulk_update(
            status_link, ['status'], batch_size=batch_size,
        )

    active_link_item_ids = set()
    for chunk in _chunked(sorted({row[0] for row in keyed})):
        active_link_item_ids.update(
            Item.objects.filter(
                item_id__in=chunk,
                deleted=False,
            ).values_list('item_id', flat=True)
        )
    deleted_link = [
        Item(
            item_id=item_id,
            deleted=lifecycle[existing[(org_id, k)]]['deleted'],
            deleted_at=lifecycle[existing[(org_id, k)]]['deleted_at'],
        )
        for item_id, k, _kind, org_id in keyed
        if (
            (org_id, k) in existing
            and existing[(org_id, k)] in lifecycle
            and (
                lifecycle[existing[(org_id, k)]]['deleted']
                or item_id in carried
            )
            and item_id in active_link_item_ids
        )
    ]
    if deleted_link:
        Item.objects.bulk_update(
            deleted_link, ['deleted', 'deleted_at'],
            batch_size=batch_size,
        )

    # A singleton group's primary IS its one item — set it where unset. A
    # measure group born from a rename gets the item that seeded it: that item
    # supplied the group's metadata, so it's the natural representative rather
    # than whichever instance the heuristic would otherwise land on.
    sing = {
        existing[(org_id, k)]: item_id
        for item_id, k, kind, org_id in keyed
        if (
            kind == ItemGroup.KIND_SINGLETON
            and (org_id, k) in existing
        )
    }
    for composite, item_id in seed_item_by_identity.items():
        if composite in existing:
            sing.setdefault(existing[composite], item_id)
    if sing:
        grps = []
        for chunk in _chunked(sing.keys()):
            for g in ItemGroup.objects.filter(id__in=chunk, primary_item__isnull=True):
                g.primary_item_id = sing[g.id]
                grps.append(g)
        if grps:
            ItemGroup.objects.bulk_update(grps, ['primary_item'], batch_size=batch_size)

    return len(link), new_destination_by_seed_item, reconcile_destination_ids
