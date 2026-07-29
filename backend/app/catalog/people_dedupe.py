"""Merge duplicate ``DataPerson`` rows.

Why this exists
---------------
``DataPerson`` shipped without any uniqueness rule, and the member-save upsert
matched on ``user`` alone. A login-less row for a person (added via the Django
admin or a bulk import before they had an account) was therefore never found
when that person later got a login, and a SECOND row with the identical name
appeared. Both rows were owners, both in the same org — so every Owner /
Steward dropdown listed the name twice, while Org Settings looked clean because
it indexes members by user id.

This module both cleans up the existing rows and is called from the migration
that adds the constraints, so the constraints can't fail on legacy data.

Model classes are passed in rather than imported, so the same code runs against
the *historical* models inside a migration and the real ones from the
management command.

Safety rule
-----------
Two rows with the same name but two DIFFERENT logins are very likely two
different humans, not a duplicate. Those are never merged — they're renamed
(``"Jane Doe (jane@…)"``) so they stay distinct, stay identifiable in a
dropdown, and satisfy the new constraint. Everything else about the merge is
lossless: references are repointed, departments unioned, role flags OR-ed.
"""


# Governance moved from Item to ItemGroup in migration 0029, which dropped these
# columns from Django's model state but deliberately left them in the database
# (deprecated, removed later). Their FK constraints to catalog_dataperson are
# therefore still live and still enforced — so deleting a merged-away person
# fails with a foreign key violation unless these are repointed too. Django
# cannot see them, so this is raw SQL by necessity, and defensive about the
# columns having finally been dropped.
_LEGACY_ITEM_PERSON_COLUMNS = ('ownership_person_id', 'steward_id')


def _existing_legacy_columns(cursor):
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'catalog_item' AND column_name = ANY(%s)
        """,
        [list(_LEGACY_ITEM_PERSON_COLUMNS)],
    )
    return [row[0] for row in cursor.fetchall()]


def _repoint_legacy_item_columns(loser_ids, survivor_id, apply=False):
    """Repoint the deprecated catalog_item person columns. Returns rows moved."""
    from django.db import connection

    if not loser_ids:
        return 0
    moved = 0
    with connection.cursor() as cursor:
        for column in _existing_legacy_columns(cursor):
            if apply:
                cursor.execute(
                    f'UPDATE catalog_item SET {column} = %s WHERE {column} = ANY(%s)',
                    [survivor_id, list(loser_ids)],
                )
                moved += cursor.rowcount
            else:
                cursor.execute(
                    f'SELECT count(*) FROM catalog_item WHERE {column} = ANY(%s)',
                    [list(loser_ids)],
                )
                moved += cursor.fetchone()[0]
    return moved


class _UnionFind:
    """Tiny union-find so a row linked by user AND by name joins one cluster."""

    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _norm(name):
    return (name or '').strip().lower()


def find_clusters(DataPerson):
    """All groups of rows that refer to the same person.

    Two rows are linked when they share a login, or share a
    case-insensitive name within the same organization. Returns a list of
    lists of row dicts, each with 2+ entries, ordered by pk.
    """
    rows = list(DataPerson.objects.all().values(
        'id', 'name', 'organization_id', 'user_id',
        'is_owner', 'is_steward', 'is_other', 'slack_handle',
    ))
    uf = _UnionFind()
    by_user = {}
    by_name = {}
    for r in rows:
        uf.find(r['id'])
        if r['user_id'] is not None:
            by_user.setdefault(r['user_id'], []).append(r['id'])
        key = (r['organization_id'], _norm(r['name']))
        if key[1]:
            by_name.setdefault(key, []).append(r['id'])

    for ids in list(by_user.values()) + list(by_name.values()):
        for other in ids[1:]:
            uf.union(ids[0], other)

    grouped = {}
    for r in rows:
        grouped.setdefault(uf.find(r['id']), []).append(r)
    return [sorted(v, key=lambda r: r['id']) for v in grouped.values() if len(v) > 1]


def _ref_counts(cluster, ItemGroup, GovernanceTask):
    ids = [r['id'] for r in cluster]
    owned = dict()
    for pid in ids:
        owned[pid] = (
            ItemGroup.objects.filter(ownership_person_id=pid).count()
            + ItemGroup.objects.filter(steward_id=pid).count()
            + GovernanceTask.objects.filter(assignee_id=pid).count()
        )
    return owned


def _pick_survivor(cluster, refs):
    """A row with a login wins (it's the one the app upserts against), then the
    most-referenced, then the oldest pk — so the id that most of the catalog
    already points at is the one that stays."""
    return sorted(
        cluster,
        key=lambda r: (0 if r['user_id'] is not None else 1, -refs.get(r['id'], 0), r['id']),
    )[0]


def dedupe_data_persons(DataPerson, ItemGroup, GovernanceTask, apply=False, log=None):
    """Report (and optionally perform) the merge. Returns a summary dict."""
    log = log or (lambda *a, **k: None)
    summary = {
        'clusters': 0, 'merged_rows': 0, 'renamed_rows': 0,
        'repointed_groups': 0, 'repointed_tasks': 0, 'repointed_legacy_items': 0,
        'conflicts': 0,
    }

    taken_names = _taken_names(DataPerson)

    for cluster in find_clusters(DataPerson):
        summary['clusters'] += 1
        refs = _ref_counts(cluster, ItemGroup, GovernanceTask)
        distinct_users = {r['user_id'] for r in cluster if r['user_id'] is not None}

        if len(distinct_users) > 1:
            # Two different logins sharing a name — almost certainly two people,
            # so they must both survive. But "don't merge" can't mean "do
            # nothing": the cluster may still contain several rows per login,
            # which uniq_dataperson_user would reject. So merge WITHIN each
            # login first, then rename what's left apart.
            summary['conflicts'] += 1
            log(f"  CONFLICT same name, {len(distinct_users)} different logins: "
                f"{[r['id'] for r in cluster]} — merging per login, then renaming")

            survivors = []
            for user_id in sorted(distinct_users):
                rows = [r for r in cluster if r['user_id'] == user_id]
                keeper = _pick_survivor(rows, refs)
                survivors.append(keeper)
                if len(rows) > 1:
                    _merge(DataPerson, ItemGroup, GovernanceTask, keeper,
                           [r for r in rows if r['id'] != keeper['id']],
                           summary, apply, log)
            # Login-less rows in a conflicted cluster are genuinely ambiguous —
            # we can't tell which of the two humans they are — so they're left
            # standing and renamed rather than guessed at.
            survivors.extend(r for r in cluster if r['user_id'] is None)

            survivors.sort(key=lambda r: r['id'])
            for r in survivors[1:]:
                person = DataPerson.objects.filter(id=r['id']).first()
                if person is None:
                    continue
                new_name = _disambiguate(person, taken_names)
                log(f"    rename #{person.id} -> {new_name!r}")
                summary['renamed_rows'] += 1
                if apply:
                    person.name = new_name
                    person.save(update_fields=['name'])
            continue

        survivor_row = _pick_survivor(cluster, refs)
        losers = [r for r in cluster if r['id'] != survivor_row['id']]
        log(f"  MERGE {[r['id'] for r in cluster]} -> keep #{survivor_row['id']} "
            f"({survivor_row['name']!r}, refs={refs.get(survivor_row['id'], 0)})")
        _merge(DataPerson, ItemGroup, GovernanceTask, survivor_row, losers,
               summary, apply, log)

    return summary


def _taken_names(DataPerson):
    """``{(organization_id, lower(name))}`` — every name already in use.

    The disambiguating rename has to land on a name nothing else holds, or it
    just swaps one uniqueness violation for another and the migration still
    aborts.
    """
    return {
        (org_id, _norm(name))
        for org_id, name in DataPerson.objects.values_list('organization_id', 'name')
    }


def _disambiguate(person, taken_names, max_length=255):
    """A name for ``person`` that is unique within its org and actually fits.

    Truncates the BASE, never the finished string: chopping the result to
    ``max_length`` can cut the suffix off and hand back the very name we were
    trying to differ from.
    """
    base = (person.name or '').strip()
    user = getattr(person, 'user', None) if person.user_id else None
    hint = getattr(user, 'email', None) or getattr(user, 'username', None) or str(person.id)

    for attempt, suffix_value in enumerate([hint, f'{hint} #{person.id}']
                                           + [f'{hint} #{person.id}.{n}' for n in range(2, 50)]):
        suffix = f' ({suffix_value})'
        trimmed = base[:max(1, max_length - len(suffix))]
        candidate = f'{trimmed}{suffix}'[:max_length]
        key = (person.organization_id, _norm(candidate))
        if key not in taken_names:
            taken_names.add(key)
            return candidate
    # Unreachable in practice; fall back to something guaranteed unique.
    return f'{base[:max_length - 24]} (dataperson {person.id})'[:max_length]


def _merge(DataPerson, ItemGroup, GovernanceTask, survivor_row, losers,
           summary, apply, log):
    """Fold ``losers`` into ``survivor_row``, repointing everything that refers
    to them. Lossless by design: a merge must never shrink someone's reach."""
    loser_ids = [r['id'] for r in losers]
    if not loser_ids:
        return

    summary['repointed_groups'] += (
        ItemGroup.objects.filter(ownership_person_id__in=loser_ids).count()
        + ItemGroup.objects.filter(steward_id__in=loser_ids).count()
    )
    summary['repointed_tasks'] += GovernanceTask.objects.filter(
        assignee_id__in=loser_ids).count()
    summary['merged_rows'] += len(loser_ids)

    if not apply:
        summary['repointed_legacy_items'] += _repoint_legacy_item_columns(
            loser_ids, survivor_row['id'], apply=False,
        )
        return

    survivor = DataPerson.objects.get(id=survivor_row['id'])
    ItemGroup.objects.filter(ownership_person_id__in=loser_ids).update(
        ownership_person_id=survivor.id)
    ItemGroup.objects.filter(steward_id__in=loser_ids).update(steward_id=survivor.id)
    GovernanceTask.objects.filter(assignee_id__in=loser_ids).update(assignee_id=survivor.id)
    summary['repointed_legacy_items'] += _repoint_legacy_item_columns(
        loser_ids, survivor.id, apply=True,
    )

    # Union the departments and OR the role flags: a merge must never take away
    # a person's reach, or they'd vanish from a dropdown they belonged in. Same
    # for the slack handle and the login link.
    dept_ids = set(survivor.departments.values_list('id', flat=True))
    for loser in DataPerson.objects.filter(id__in=loser_ids):
        dept_ids |= set(loser.departments.values_list('id', flat=True))
        survivor.is_owner = survivor.is_owner or loser.is_owner
        survivor.is_steward = survivor.is_steward or loser.is_steward
        survivor.is_other = survivor.is_other or loser.is_other
        if not survivor.slack_handle and loser.slack_handle:
            survivor.slack_handle = loser.slack_handle
        if survivor.user_id is None and loser.user_id is not None:
            survivor.user_id = loser.user_id
        if survivor.organization_id is None and loser.organization_id is not None:
            survivor.organization_id = loser.organization_id
    survivor.save()
    survivor.departments.set(list(dept_ids))
    DataPerson.objects.filter(id__in=loser_ids).delete()
