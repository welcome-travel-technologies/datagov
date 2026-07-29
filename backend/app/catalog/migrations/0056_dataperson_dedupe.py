"""Merge duplicate DataPerson rows.

The Owner / Steward dropdowns were listing the same person twice. Root cause:
``DataPerson`` had no uniqueness rule at all, and the member-save upsert matched
on ``user`` alone — so a login-less row for someone (added via the Django admin
or a bulk import before they had an account) was never found when they later got
a login, and a second row with the identical name appeared beside it.

Deliberately split from the AddConstraint migration that follows: Postgres
refuses to CREATE INDEX on a table that still has pending FK trigger events from
row changes in the same transaction, and this migration changes a lot of rows.
Each Django migration gets its own transaction, so ending here flushes them.
"""

from django.db import migrations


# Frozen migration-local copy. Do not replace this with the runtime cleaner:
# migrations must keep their original semantics even as operational tooling
# evolves after deployment.
_LEGACY_ITEM_PERSON_COLUMNS = ('ownership_person_id', 'steward_id')


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, value):
        self.parent.setdefault(value, value)
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, first, second):
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self.parent[second_root] = first_root


def _norm(value):
    return (value or '').strip().lower()


def _clusters(DataPerson, using):
    rows = list(DataPerson.objects.using(using).all().values(
        'id', 'name', 'organization_id', 'user_id',
        'is_owner', 'is_steward', 'is_other', 'slack_handle',
    ))
    union_find = _UnionFind()
    by_user = {}
    by_slack = {}
    for row in rows:
        union_find.find(row['id'])
        if row['user_id'] is not None:
            by_user.setdefault(
                (row['organization_id'], row['user_id']), [],
            ).append(row['id'])
        slack = _norm(row['slack_handle'])
        if slack and row['organization_id'] is not None:
            by_slack.setdefault(
                (row['organization_id'], slack), [],
            ).append(row['id'])
    for ids in list(by_user.values()) + list(by_slack.values()):
        for other_id in ids[1:]:
            union_find.union(ids[0], other_id)
    grouped = {}
    for row in rows:
        grouped.setdefault(union_find.find(row['id']), []).append(row)
    return [
        sorted(cluster, key=lambda row: row['id'])
        for cluster in grouped.values()
        if len(cluster) > 1
    ]


def _reference_counts(cluster, ItemGroup, GovernanceTask, using):
    counts = {}
    for row in cluster:
        person_id = row['id']
        counts[person_id] = (
            ItemGroup.objects.using(using)
            .filter(ownership_person_id=person_id)
            .count()
            + ItemGroup.objects.using(using)
            .filter(steward_id=person_id)
            .count()
            + GovernanceTask.objects.using(using)
            .filter(assignee_id=person_id)
            .count()
        )
    return counts


def _pick_survivor(cluster, references):
    return sorted(
        cluster,
        key=lambda row: (
            0 if row['user_id'] is not None else 1,
            -references.get(row['id'], 0),
            row['id'],
        ),
    )[0]


def _legacy_columns(connection_obj):
    with connection_obj.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'catalog_item' AND column_name = ANY(%s)
            """,
            [list(_LEGACY_ITEM_PERSON_COLUMNS)],
        )
        return [row[0] for row in cursor.fetchall()]


def _repoint_legacy_items(loser_ids, survivor_id, connection_obj):
    moved = 0
    with connection_obj.cursor() as cursor:
        for column in _legacy_columns(connection_obj):
            cursor.execute(
                f'UPDATE catalog_item SET {column} = %s '
                f'WHERE {column} = ANY(%s)',
                [survivor_id, list(loser_ids)],
            )
            moved += cursor.rowcount
    return moved


def _merge(
        DataPerson, ItemGroup, GovernanceTask, survivor_row, loser_rows,
        summary, using, connection_obj):
    loser_ids = [row['id'] for row in loser_rows]
    if not loser_ids:
        return
    summary['repointed_groups'] += (
        ItemGroup.objects.using(using)
        .filter(ownership_person_id__in=loser_ids)
        .count()
        + ItemGroup.objects.using(using)
        .filter(steward_id__in=loser_ids)
        .count()
    )
    summary['repointed_tasks'] += (
        GovernanceTask.objects.using(using)
        .filter(assignee_id__in=loser_ids)
        .count()
    )
    summary['merged_rows'] += len(loser_ids)

    survivor = DataPerson.objects.using(using).get(pk=survivor_row['id'])
    ItemGroup.objects.using(using).filter(
        ownership_person_id__in=loser_ids,
    ).update(ownership_person_id=survivor.pk)
    ItemGroup.objects.using(using).filter(
        steward_id__in=loser_ids,
    ).update(steward_id=survivor.pk)
    GovernanceTask.objects.using(using).filter(
        assignee_id__in=loser_ids,
    ).update(assignee_id=survivor.pk)
    summary['repointed_legacy_items'] += _repoint_legacy_items(
        loser_ids, survivor.pk, connection_obj,
    )

    department_ids = set(
        survivor.departments
        .filter(organization_id=survivor.organization_id)
        .values_list('id', flat=True)
    )
    for loser in DataPerson.objects.using(using).filter(pk__in=loser_ids):
        department_ids.update(
            loser.departments
            .filter(organization_id=survivor.organization_id)
            .values_list('id', flat=True)
        )
        survivor.is_owner = survivor.is_owner or loser.is_owner
        survivor.is_steward = survivor.is_steward or loser.is_steward
        survivor.is_other = survivor.is_other or loser.is_other
        if not survivor.slack_handle and loser.slack_handle:
            survivor.slack_handle = loser.slack_handle
        if survivor.user_id is None and loser.user_id is not None:
            survivor.user_id = loser.user_id
    survivor.save(using=using)
    survivor.departments.set(sorted(department_ids))
    DataPerson.objects.using(using).filter(pk__in=loser_ids).delete()


def _unique_name(person, base, taken):
    candidate = base[:255]
    key = (person.organization_id, _norm(candidate))
    if key not in taken:
        taken.add(key)
        return candidate
    suffix = f' (data person {person.pk})'
    candidate = f'{base[:max(1, 255 - len(suffix))]}{suffix}'[:255]
    attempt = 1
    while (person.organization_id, _norm(candidate)) in taken:
        attempt += 1
        suffix = f' (data person {person.pk}.{attempt})'
        candidate = f'{base[:max(1, 255 - len(suffix))]}{suffix}'[:255]
    taken.add((person.organization_id, _norm(candidate)))
    return candidate


def _clean_names(DataPerson, summary, using):
    taken = set()
    collisions = set()
    for person in DataPerson.objects.using(using).order_by('pk').iterator():
        base = (person.name or '').strip() or f'Unnamed person {person.pk}'
        original_key = (person.organization_id, _norm(base))
        if original_key in taken:
            collisions.add(original_key)
        candidate = _unique_name(person, base, taken)
        if candidate != person.name:
            DataPerson.objects.using(using).filter(pk=person.pk).update(
                name=candidate,
            )
            summary['renamed_rows'] += 1
    summary['name_conflicts'] = len(collisions)


def _dedupe_data_persons_v0056(
        DataPerson, ItemGroup, GovernanceTask, *, using, connection_obj):
    summary = {
        'clusters': 0,
        'merged_rows': 0,
        'renamed_rows': 0,
        'repointed_groups': 0,
        'repointed_tasks': 0,
        'repointed_legacy_items': 0,
        'identity_conflicts': 0,
        'name_conflicts': 0,
    }
    for cluster in _clusters(DataPerson, using):
        summary['clusters'] += 1
        references = _reference_counts(
            cluster, ItemGroup, GovernanceTask, using,
        )
        user_ids = {
            row['user_id'] for row in cluster if row['user_id'] is not None
        }
        if len(user_ids) > 1:
            summary['identity_conflicts'] += 1
            for user_id in sorted(user_ids):
                same_login = [
                    row for row in cluster if row['user_id'] == user_id
                ]
                survivor = _pick_survivor(same_login, references)
                _merge(
                    DataPerson, ItemGroup, GovernanceTask, survivor,
                    [row for row in same_login if row['id'] != survivor['id']],
                    summary, using, connection_obj,
                )
            continue
        survivor = _pick_survivor(cluster, references)
        _merge(
            DataPerson, ItemGroup, GovernanceTask, survivor,
            [row for row in cluster if row['id'] != survivor['id']],
            summary, using, connection_obj,
        )
    _clean_names(DataPerson, summary, using)
    summary['conflicts'] = (
        summary['identity_conflicts'] + summary['name_conflicts']
    )
    return summary


def forwards(apps, schema_editor):
    DataPerson = apps.get_model('catalog', 'DataPerson')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
    connection_obj = schema_editor.connection
    summary = _dedupe_data_persons_v0056(
        DataPerson,
        ItemGroup,
        GovernanceTask,
        using=connection_obj.alias,
        connection_obj=connection_obj,
    )
    print(f'[0056] DataPerson dedupe: {summary}')


def backwards(apps, schema_editor):
    """Irreversible in substance — merged rows can't be un-merged. A no-op so
    the constraints in 0057 can still be rolled back."""


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0055_mcpapikey'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
