"""Repair legacy tenant/name/group data before strengthening constraints.

Data changes deliberately live in their own migration. PostgreSQL refuses to
build an index while the same transaction still has pending trigger events, so
0063 adds the expression-based uniqueness and non-blank-name constraints only
after this migration commits.
"""

from collections import defaultdict

from django.db import migrations
from django.db.models import OuterRef, Subquery
from django.utils import timezone


def _chunks(values, size=2000):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _unique_name(base, row_id, organization_id, label, taken, max_length=255):
    """Return a stable, fitting name unique under lower(trim(name))+org."""
    candidate = base[:max_length]
    key = (organization_id, candidate.strip().lower())
    if key not in taken:
        taken.add(key)
        return candidate

    suffixes = [f' ({label} {row_id})']
    suffixes.extend(f' ({label} {row_id}.{n})' for n in range(2, 100))
    for suffix in suffixes:
        candidate = f'{base[:max(1, max_length - len(suffix))]}{suffix}'[:max_length]
        key = (organization_id, candidate.strip().lower())
        if key not in taken:
            taken.add(key)
            return candidate
    raise RuntimeError(f'Could not disambiguate {label} id={row_id}')


def _clean_names(Model, label, using):
    """Trim/fill/disambiguate without ever merging name-only matches."""
    rows = list(Model.objects.using(using).order_by('id'))
    taken = set()
    final_names = {}
    for row in rows:
        base = (row.name or '').strip() or f'Unnamed {label} {row.id}'
        final_names[row.id] = _unique_name(
            base, row.id, row.organization_id, label, taken,
        )

    changed = [
        row for row in rows if final_names[row.id] != row.name
    ]
    if not changed:
        return 0

    # The old index already enforces Lower(name)+organization. Trimming an
    # older ``" Jane "`` row straight to ``"Jane"`` can therefore collide
    # transiently with a later row that is about to receive a suffix. Move all
    # changing rows to collision-free placeholders first, then write the final
    # deterministic names.
    occupied = {
        (row.organization_id, (row.name or '').lower())
        for row in rows
    }
    occupied.update(
        (row.organization_id, final_names[row.id].lower())
        for row in rows
    )
    for row in changed:
        base = f'__0062_{Model._meta.model_name}_{row.id}__'
        temporary = base[:255]
        attempt = 1
        while (row.organization_id, temporary.lower()) in occupied:
            attempt += 1
            suffix = f'_{attempt}'
            temporary = f'{base[:255 - len(suffix)]}{suffix}'
        occupied.add((row.organization_id, temporary.lower()))
        Model.objects.using(using).filter(pk=row.pk).update(name=temporary)

    for row in changed:
        Model.objects.using(using).filter(pk=row.pk).update(
            name=final_names[row.id],
        )
    return len(changed)


def _resolve_empty_group_tasks(apps, using):
    """Preserve empty groups and close only their still-open work.

    Empty legacy groups can contain curated metadata, and some become empty
    only because this migration quarantines cross-tenant Item links. Deleting
    every such group is irreversible and loses that evidence. Keep the group
    intact; an open task no longer has an actionable catalog asset while the
    group has no members, so mark only open tasks resolved and retain their
    group FK as an audit trail. Runtime rename reconciliation remains free to
    retire the exact source group it has positively identified.
    """
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
    empty_ids = list(
        ItemGroup.objects.using(using)
        .filter(items__isnull=True)
        .values_list('id', flat=True)
    )
    if not empty_ids:
        return 0, 0

    now = timezone.now()
    closed = 0
    for chunk in _chunks(empty_ids):
        closed += (
            GovernanceTask.objects.using(using)
            .filter(item_group_id__in=chunk, state='open')
            .update(
                state='done', completed_at=now, completed_by=None,
                closed_reason='resolved', updated_at=now,
            )
        )
    return len(empty_ids), closed


def _clear_nonmember_primary_items(apps, using):
    """A primary item is valid only when it is a member of that same group."""
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    invalid_ids = [
        group_id
        for group_id, member_group_id in (
            ItemGroup.objects.using(using)
            .filter(primary_item__isnull=False)
            .values_list('id', 'primary_item__item_group_id')
            .iterator()
        )
        if member_group_id != group_id
    ]
    cleared = 0
    for chunk in _chunks(invalid_ids):
        cleared += (
            ItemGroup.objects.using(using)
            .filter(id__in=chunk)
            .update(primary_item_id=None)
        )
    return cleared


def _backfill_group_organizations(apps, using):
    Item = apps.get_model('catalog', 'Item')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    group_ids = list(
        ItemGroup.objects.using(using)
        .filter(organization__isnull=True)
        .values_list('id', flat=True)
    )
    if not group_ids:
        return 0

    candidates = defaultdict(set)
    for chunk in _chunks(group_ids):
        for group_id, organization_id in (
                Item.objects.using(using)
                .filter(item_group_id__in=chunk, organization_id__isnull=False)
                .values_list('item_group_id', 'organization_id')):
            candidates[group_id].add(organization_id)
        for group_id, organization_id in (
                ItemGroup.objects.using(using)
                .filter(id__in=chunk, primary_item__organization_id__isnull=False)
                .values_list('id', 'primary_item__organization_id')):
            candidates[group_id].add(organization_id)

    by_org = defaultdict(list)
    for group_id, organizations in candidates.items():
        if len(organizations) == 1:
            by_org[next(iter(organizations))].append(group_id)
    for organization_id, ids in by_org.items():
        for chunk in _chunks(ids):
            ItemGroup.objects.using(using).filter(id__in=chunk).update(
                organization_id=organization_id)
    return sum(len(ids) for ids in by_org.values())


def _quarantine_item_group_relations(apps, using):
    """Detach cross-tenant Item memberships and clear invalid primaries.

    Equality is exact: a known organization never inherits governance from an
    unscoped group (or vice versa). Two still-unscoped rows may remain linked.
    """
    Item = apps.get_model('catalog', 'Item')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')

    invalid_item_ids = [
        item_id
        for item_id, organization_id, group_org_id in (
            Item.objects.using(using)
            .filter(item_group__isnull=False)
            .values_list('pk', 'organization_id', 'item_group__organization_id')
            .iterator()
        )
        if organization_id != group_org_id
    ]
    detached = 0
    for chunk in _chunks(invalid_item_ids):
        detached += (
            Item.objects.using(using)
            .filter(pk__in=chunk)
            .update(item_group_id=None)
        )

    invalid_primary_ids = [
        group_id
        for group_id, organization_id, primary_org_id, member_group_id in (
            ItemGroup.objects.using(using)
            .filter(primary_item__isnull=False)
            .values_list(
                'id', 'organization_id', 'primary_item__organization_id',
                'primary_item__item_group_id',
            )
            .iterator()
        )
        if (
            organization_id != primary_org_id
            or member_group_id != group_id
        )
    ]
    primaries_cleared = 0
    for chunk in _chunks(invalid_primary_ids):
        primaries_cleared += (
            ItemGroup.objects.using(using)
            .filter(id__in=chunk)
            .update(primary_item_id=None)
        )
    return detached, primaries_cleared


def _backfill_definition_organizations(apps, using):
    Definition = apps.get_model('catalog', 'Definition')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    definition_ids = list(
        Definition.objects.using(using)
        .filter(organization__isnull=True)
        .values_list('id', flat=True)
    )
    if not definition_ids:
        return 0

    candidates = defaultdict(set)
    ambiguous = set()
    for chunk in _chunks(definition_ids):
        for definition_id, organization_id in (
                ItemGroup.objects.using(using)
                .filter(definition_id__in=chunk)
                .values_list('definition_id', 'organization_id')):
            if organization_id is None:
                ambiguous.add(definition_id)
            else:
                candidates[definition_id].add(organization_id)

    moves = {}
    for definition_id, organizations in candidates.items():
        if definition_id not in ambiguous and len(organizations) == 1:
            moves[definition_id] = next(iter(organizations))
    if not moves:
        return 0

    # 0061 already has Lower(name)+organization uniqueness. A null-org
    # Definition can therefore share a name with a row in its inferred target
    # org, but assigning the org first would violate that old index before the
    # normalized 0063 cleanup has a chance to run. Reserve all names already in
    # place, then disambiguate movers against their *future* org before updating
    # name and organization together.
    taken = {
        (organization_id, (name or '').strip().lower())
        for _row_id, name, organization_id in (
            Definition.objects.using(using)
            .exclude(id__in=moves)
            .values_list('id', 'name', 'organization_id')
        )
    }
    for row in (
            Definition.objects.using(using)
            .filter(id__in=moves)
            .order_by('id')
            .iterator()):
        target_org_id = moves[row.id]
        base = (row.name or '').strip() or f'Unnamed definition {row.id}'
        candidate = _unique_name(
            base, row.id, target_org_id, 'definition', taken,
        )
        Definition.objects.using(using).filter(pk=row.pk).update(
            name=candidate,
            organization_id=target_org_id,
        )
    return len(moves)


def _quarantine_non_measure_definitions(apps, using):
    """Definitions curate measure groups only; clear legacy singleton links."""
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    return (
        ItemGroup.objects.using(using)
        .exclude(kind='measure_name')
        .filter(definition__isnull=False)
        .update(definition_id=None)
    )


def _quarantine_person_departments(apps, using):
    """Remove DataPerson↔Department memberships crossing exact org scope."""
    DataPerson = apps.get_model('catalog', 'DataPerson')
    through = DataPerson.departments.through
    invalid_ids = [
        relation_id
        for relation_id, person_org_id, department_org_id in (
            through.objects.using(using)
            .values_list(
                'pk', 'dataperson__organization_id',
                'department__organization_id',
            )
            .iterator()
        )
        if person_org_id != department_org_id
    ]
    removed = 0
    for chunk in _chunks(invalid_ids):
        deleted, _ = (
            through.objects.using(using)
            .filter(pk__in=chunk)
            .delete()
        )
        removed += deleted
    return removed


def _repair_tasks(apps, using):
    GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')

    group_org = (
        ItemGroup.objects.using(using)
        .filter(pk=OuterRef('item_group_id'))
        .values('organization_id')[:1]
    )
    aligned = (
        GovernanceTask.objects.using(using)
        .filter(item_group__isnull=False)
        .update(organization_id=Subquery(group_org))
    )

    invalid_assignee_ids = [
        task_id
        for task_id, organization_id, assignee_org_id in (
            GovernanceTask.objects.using(using)
            .filter(assignee__isnull=False)
            .values_list(
                'id', 'organization_id', 'assignee__organization_id',
            )
            .iterator()
        )
        if organization_id != assignee_org_id
    ]
    assignees_cleared = 0
    for chunk in _chunks(invalid_assignee_ids):
        assignees_cleared += (
            GovernanceTask.objects.using(using)
            .filter(id__in=chunk)
            .update(assignee_id=None, assignee_role=None)
        )

    now = timezone.now()
    closed = (
        GovernanceTask.objects.using(using)
        .filter(item_group__isnull=True, state='open')
        .update(
            state='done', completed_at=now, completed_by=None,
            closed_reason='resolved', updated_at=now,
        )
    )
    return aligned, assignees_cleared, closed


def _repair_group_item_lifecycle(apps, using):
    """Restore ItemGroup lifecycle invariants and mirror them to linked Items.

    ``deleted_at`` on ItemGroup is the provenance marker for its DELETED
    episode. Existing deleted children may instead be source-obsolete, so their
    timestamps are never adopted or overwritten.
    """
    Item = apps.get_model('catalog', 'Item')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    now = timezone.now()

    groups_status_forced = (
        ItemGroup.objects.using(using)
        .filter(deleted=True)
        .exclude(status='DELETED')
        .update(status='DELETED')
    )

    group_deleted_at_set = (
        ItemGroup.objects.using(using)
        .filter(status='DELETED', deleted_at__isnull=True)
        .update(deleted_at=now)
    )
    group_deleted_at_cleared = (
        ItemGroup.objects.using(using)
        .exclude(status='DELETED')
        .filter(deleted_at__isnull=False)
        .update(deleted_at=None)
    )

    group_status = (
        ItemGroup.objects.using(using)
        .filter(pk=OuterRef('item_group_id'))
        .values('status')[:1]
    )
    mismatched_status = (
        Item.objects.using(using)
        .filter(item_group__isnull=False)
        .exclude(status=Subquery(group_status))
    )
    item_status_mirrored = mismatched_status.count()
    mismatched_status.update(status=Subquery(group_status))

    group_deleted_at = (
        ItemGroup.objects.using(using)
        .filter(pk=OuterRef('item_group_id'))
        .values('deleted_at')[:1]
    )
    soft_deleted_items = (
        Item.objects.using(using)
        .filter(item_group__deleted=True, deleted=False)
    )
    # A soft-deleted group makes all of its members deleted. The inverse is
    # deliberately not true: Item.deleted can record source-level obsolescence
    # independently of an active group. Clearing it here would resurrect rows
    # that the source had already retired.
    item_delete_mirrored = soft_deleted_items.count()
    soft_deleted_items.update(
        deleted=True,
        deleted_at=Subquery(group_deleted_at),
    )

    return (
        groups_status_forced,
        group_deleted_at_set,
        group_deleted_at_cleared,
        item_status_mirrored,
        item_delete_mirrored,
    )


def _quarantine_cross_tenant_relations(apps, using):
    """Clear governance links whose tenant cannot match their parent.

    The deterministic backfills run first. Unequal tenant ids (including a
    known tenant linked to an unscoped row) are quarantined instead of guessing
    which tenant should own the relationship; two still-unscoped rows remain
    untouched.
    """
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    Definition = apps.get_model('catalog', 'Definition')

    fields = (
        (ItemGroup, 'definition', 'definition__organization_id'),
        (ItemGroup, 'ownership_person', 'ownership_person__organization_id'),
        (ItemGroup, 'steward', 'steward__organization_id'),
        (ItemGroup, 'ownership_department', 'ownership_department__organization_id'),
        (ItemGroup, 'category', 'category__organization_id'),
        (Definition, 'ownership_person', 'ownership_person__organization_id'),
        (Definition, 'ownership_department', 'ownership_department__organization_id'),
    )
    quarantined = {}
    for Model, field, related_org_field in fields:
        ids = []
        for row_id, organization_id, related_id, related_org_id in (
                Model.objects.using(using)
                .exclude(**{f'{field}_id': None})
                .values_list(
                    'id', 'organization_id', f'{field}_id', related_org_field)
                .iterator()):
            if organization_id != related_org_id:
                ids.append(row_id)

        count = 0
        for chunk in _chunks(ids):
            count += (
                Model.objects.using(using)
                .filter(id__in=chunk)
                .update(**{f'{field}_id': None})
            )
        quarantined[f'{Model._meta.model_name}.{field}'] = count
    return quarantined


def _remove_legacy_category(apps, schema_editor, using):
    """Convert the legacy deletion marker, then remove it idempotently.

    Production used ``To Be Deleted`` as the only deletion signal on many
    groups. Detaching it first would erase that intent and turn active measures
    into NO_CATEGORY work. Promote every affected non-DELETED group before the
    category disappears and append the same audit transition an interactive
    status edit would have produced.
    """
    Category = apps.get_model('catalog', 'Category')
    GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    StatusChangeLog = apps.get_model('catalog', 'StatusChangeLog')
    category_ids = list(
        Category.objects.using(using)
        .filter(name__iregex=r'^\s*to be deleted\s*$')
        .values_list('id', flat=True)
    )
    if not category_ids:
        return 0, 0, 0, 0, 0

    group_rows = list(
        ItemGroup.objects.using(using)
        .filter(category_id__in=category_ids)
        .order_by('id')
        .values('id', 'organization_id', 'group_key', 'status')
    )
    group_ids = [row['id'] for row in group_rows]
    transitions = [
        row for row in group_rows if row['status'] != 'DELETED'
    ]
    transition_ids = [row['id'] for row in transitions]
    if transitions:
        StatusChangeLog.objects.using(using).bulk_create(
            [
                StatusChangeLog(
                    organization_id=row['organization_id'],
                    item_group_id=row['id'],
                    group_key=row['group_key'],
                    old_status=row['status'],
                    new_status='DELETED',
                    changed_by_id=None,
                )
                for row in transitions
            ],
            batch_size=1000,
        )
        for chunk in _chunks(transition_ids):
            ItemGroup.objects.using(using).filter(
                id__in=chunk,
            ).update(status='DELETED')

    # Any previously open task for the old status (or an already-invalid
    # Category task) stopped being actionable in this transaction. A later
    # explicit task backfill creates/reconciles DELETED work with the Steward.
    stale_tasks_closed = 0
    if group_ids:
        now = timezone.now()
        for chunk in _chunks(group_ids):
            stale_tasks_closed += (
                GovernanceTask.objects.using(using)
                .filter(item_group_id__in=chunk, state='open')
                .exclude(reason='DELETED')
                .update(
                    state='done', completed_at=now, completed_by_id=None,
                    closed_reason='resolved', updated_at=now,
                )
            )

    detached = (
        ItemGroup.objects.using(using)
        .filter(category_id__in=category_ids)
        .update(category_id=None)
    )

    connection = schema_editor.connection
    table = 'catalog_item'
    column = 'category_id'
    with connection.cursor() as cursor:
        columns = {
            description.name
            for description in connection.introspection.get_table_description(
                cursor, table)
        }
        if column in columns:
            placeholders = ', '.join(['%s'] * len(category_ids))
            cursor.execute(
                f'UPDATE {connection.ops.quote_name(table)} '
                f'SET {connection.ops.quote_name(column)} = NULL '
                f'WHERE {connection.ops.quote_name(column)} '
                f'IN ({placeholders})',
                category_ids,
            )

    Category.objects.using(using).filter(id__in=category_ids).delete()
    return (
        len(category_ids),
        detached,
        len(transitions),
        len(transitions),
        stale_tasks_closed,
    )


def cleanup_integrity(apps, schema_editor):
    using = schema_editor.connection.alias
    DataPerson = apps.get_model('catalog', 'DataPerson')
    Definition = apps.get_model('catalog', 'Definition')

    primary_items_cleared = _clear_nonmember_primary_items(apps, using)
    groups_backfilled = _backfill_group_organizations(apps, using)
    items_detached, foreign_primaries_cleared = _quarantine_item_group_relations(
        apps, using,
    )
    primary_items_cleared += foreign_primaries_cleared
    empty_groups_preserved, empty_group_tasks_closed = (
        _resolve_empty_group_tasks(apps, using)
    )
    non_measure_definitions_cleared = _quarantine_non_measure_definitions(
        apps, using,
    )
    definitions_backfilled = _backfill_definition_organizations(apps, using)
    person_departments_removed = _quarantine_person_departments(apps, using)
    quarantined = _quarantine_cross_tenant_relations(apps, using)
    (
        categories_removed,
        category_groups_detached,
        category_groups_promoted,
        category_status_logs_created,
        category_tasks_closed,
    ) = _remove_legacy_category(apps, schema_editor, using)
    (
        groups_status_forced,
        group_deleted_at_set,
        group_deleted_at_cleared,
        item_status_mirrored,
        item_delete_mirrored,
    ) = _repair_group_item_lifecycle(apps, using)
    tasks_aligned, task_assignees_cleared, orphan_tasks_closed = _repair_tasks(
        apps, using,
    )
    people_renamed = _clean_names(DataPerson, 'data person', using)
    definitions_renamed = _clean_names(Definition, 'definition', using)

    print(
        '[0062] integrity cleanup: '
        f'empty_groups_preserved={empty_groups_preserved}, '
        f'empty_group_tasks_closed={empty_group_tasks_closed}, '
        f'group_orgs={groups_backfilled}, '
        f'items_detached={items_detached}, '
        f'primary_items_cleared={primary_items_cleared}, '
        f'person_departments_removed={person_departments_removed}, '
        f'non_measure_definitions_cleared={non_measure_definitions_cleared}, '
        f'definition_orgs={definitions_backfilled}, tasks_aligned={tasks_aligned}, '
        f'task_assignees_cleared={task_assignees_cleared}, '
        f'orphan_tasks_closed={orphan_tasks_closed}, '
        f'groups_status_forced={groups_status_forced}, '
        f'group_deleted_at_set={group_deleted_at_set}, '
        f'group_deleted_at_cleared={group_deleted_at_cleared}, '
        f'item_status_mirrored={item_status_mirrored}, '
        f'item_delete_mirrored={item_delete_mirrored}, '
        f'quarantined={quarantined}, '
        f'people_renamed={people_renamed}, '
        f'definitions_renamed={definitions_renamed}, '
        f'legacy_categories_removed={categories_removed}, '
        f'category_groups_detached={category_groups_detached}, '
        f'category_groups_promoted={category_groups_promoted}, '
        f'category_status_logs_created={category_status_logs_created}, '
        f'category_tasks_closed={category_tasks_closed}'
    )


def noop_reverse(apps, schema_editor):
    """Cleanup is intentionally irreversible; deleted identities are never guessed."""


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0061_definition_layer'),
    ]

    operations = [
        migrations.RunPython(cleanup_integrity, noop_reverse),
    ]
