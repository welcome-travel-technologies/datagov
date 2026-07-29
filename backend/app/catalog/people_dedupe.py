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

This module both cleans up the existing rows and is called from migrations that
add the constraints, so the constraints can't fail on legacy data.

Model classes are passed in rather than imported, so the same code runs against
the *historical* models inside a migration and the real ones from the
management command.

Safety rule
-----------
A name is display text, not identity. Two people can legitimately share one,
especially when neither has a login, so name-only matches are never merged.
Rows are merged only when they share a non-null login or the same non-empty
Slack handle within one organization. Name collisions are kept as separate
rows and deterministically disambiguated (``"Jane Doe (data person 42)"``).
Every deterministic merge is lossless: references are repointed, departments
unioned, and role flags OR-ed.
"""


# Governance moved from Item to ItemGroup in migration 0029, which dropped these
# columns from Django's model state but deliberately left them in the database
# (deprecated, removed later). Their FK constraints to catalog_dataperson are
# therefore still live and still enforced — so deleting a merged-away person
# fails with a foreign key violation unless these are repointed too. Django
# cannot see them, so this is raw SQL by necessity, and defensive about the
# columns having finally been dropped.
_LEGACY_ITEM_PERSON_COLUMNS = ('ownership_person_id', 'steward_id')


class ReviewedMergeError(ValueError):
    """A reviewed merge plan is structurally unsafe or contradicts identity."""


def _existing_legacy_columns(cursor):
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'catalog_item' AND column_name = ANY(%s)
        """,
        [list(_LEGACY_ITEM_PERSON_COLUMNS)],
    )
    return [row[0] for row in cursor.fetchall()]


def _repoint_legacy_item_columns(
        loser_ids, survivor_id, apply=False, connection_obj=None):
    """Repoint the deprecated catalog_item person columns. Returns rows moved."""
    if connection_obj is None:
        from django.db import connection as connection_obj

    if not loser_ids:
        return 0
    moved = 0
    with connection_obj.cursor() as cursor:
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
    """Tiny union-find for deterministic user/Slack identity edges."""

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


def find_clusters(DataPerson, using=None):
    """All groups of rows that refer to the same person.

    Two rows are linked only when they share a non-null login, or share a
    non-empty Slack handle within the same organization. A name by itself is
    never an identity key. Returns lists of row dicts, ordered by primary key.
    """
    rows = list(DataPerson.objects.using(using).all().values(
        'id', 'name', 'organization_id', 'user_id',
        'is_owner', 'is_steward', 'is_other', 'slack_handle',
    ))
    uf = _UnionFind()
    by_user = {}
    by_slack = {}
    for r in rows:
        uf.find(r['id'])
        if r['user_id'] is not None:
            by_user.setdefault(
                (r['organization_id'], r['user_id']), []
            ).append(r['id'])
        slack = _norm(r['slack_handle'])
        if slack and r['organization_id'] is not None:
            by_slack.setdefault((r['organization_id'], slack), []).append(r['id'])

    for ids in list(by_user.values()) + list(by_slack.values()):
        for other in ids[1:]:
            uf.union(ids[0], other)

    grouped = {}
    for r in rows:
        grouped.setdefault(uf.find(r['id']), []).append(r)
    return [sorted(v, key=lambda r: r['id']) for v in grouped.values() if len(v) > 1]


def _ref_counts(cluster, ItemGroup, GovernanceTask, Definition=None, using=None):
    ids = [r['id'] for r in cluster]
    owned = dict()
    for pid in ids:
        owned[pid] = (
            ItemGroup.objects.using(using).filter(ownership_person_id=pid).count()
            + ItemGroup.objects.using(using).filter(steward_id=pid).count()
            + GovernanceTask.objects.using(using).filter(assignee_id=pid).count()
        )
        if Definition is not None:
            owned[pid] += Definition.objects.using(using).filter(
                ownership_person_id=pid).count()
    return owned


def _pick_survivor(cluster, refs):
    """A row with a login wins (it's the one the app upserts against), then the
    most-referenced, then the oldest pk — so the id that most of the catalog
    already points at is the one that stays."""
    return sorted(
        cluster,
        key=lambda r: (0 if r['user_id'] is not None else 1, -refs.get(r['id'], 0), r['id']),
    )[0]


def dedupe_data_persons(
        DataPerson, ItemGroup, GovernanceTask, Definition=None, apply=False,
        log=None, using=None, connection_obj=None):
    """Report (and optionally perform) deterministic merges and name cleanup.

    ``Definition`` is optional because migration 0056 predates that model. The
    post-0061 command and integrity migration pass it so Definition ownership is
    repointed before a duplicate DataPerson is deleted.
    """
    log = log or (lambda *a, **k: None)
    summary = {
        'clusters': 0, 'merged_rows': 0, 'renamed_rows': 0,
        'repointed_groups': 0, 'repointed_tasks': 0,
        'repointed_definitions': 0, 'repointed_legacy_items': 0,
        'conflicts': 0, 'identity_conflicts': 0, 'name_conflicts': 0,
    }
    planned_loser_ids = set()

    for cluster in find_clusters(DataPerson, using=using):
        summary['clusters'] += 1
        refs = _ref_counts(
            cluster, ItemGroup, GovernanceTask, Definition=Definition, using=using)
        distinct_users = {r['user_id'] for r in cluster if r['user_id'] is not None}

        if len(distinct_users) > 1:
            # A reused Slack handle connected rows with different logins. The
            # logins are stronger identity, so never merge across them. We can
            # still merge duplicate rows *within* each login.
            summary['identity_conflicts'] += 1
            log(f"  IDENTITY CONFLICT, {len(distinct_users)} different logins: "
                f"{[r['id'] for r in cluster]} — merge within each login only")
            for user_id in sorted(distinct_users):
                rows = [r for r in cluster if r['user_id'] == user_id]
                keeper = _pick_survivor(rows, refs)
                if len(rows) > 1:
                    losers = [r for r in rows if r['id'] != keeper['id']]
                    planned_loser_ids.update(r['id'] for r in losers)
                    _merge(
                        DataPerson, ItemGroup, GovernanceTask, keeper, losers,
                        summary, apply, log, Definition=Definition, using=using,
                        connection_obj=connection_obj,
                    )
            # Login-less rows sharing the conflicted Slack handle are ambiguous:
            # leave them separate for an operator to resolve explicitly.
            continue

        survivor_row = _pick_survivor(cluster, refs)
        losers = [r for r in cluster if r['id'] != survivor_row['id']]
        planned_loser_ids.update(r['id'] for r in losers)
        log(f"  MERGE {[r['id'] for r in cluster]} -> keep #{survivor_row['id']} "
            f"({survivor_row['name']!r}, refs={refs.get(survivor_row['id'], 0)})")
        _merge(
            DataPerson, ItemGroup, GovernanceTask, survivor_row, losers,
            summary, apply, log, Definition=Definition, using=using,
            connection_obj=connection_obj,
        )

    _clean_names(
        DataPerson, summary, apply, log, using=using,
        exclude_ids=planned_loser_ids if not apply else (),
    )
    summary['conflicts'] = summary['identity_conflicts'] + summary['name_conflicts']
    return summary


def merge_reviewed_pairs(
        DataPerson, ItemGroup, GovernanceTask, pairs, Definition=None,
        apply=False, log=None, using=None, connection_obj=None,
        organization_id=None):
    """Merge operator-reviewed ``(survivor_id, loser_id)`` pairs.

    This is the deliberate escape hatch for confirmed twins that share only a
    display name. It never discovers pairs itself. The complete plan is
    validated before any write: rows must exist, remain within one exact
    organization, contain no chains/cycles/reused losers, and have no
    contradictory login or Slack identities. When ``organization_id`` is
    supplied, every row must belong to that exact organization. If one row has
    a login, it must be the selected survivor.
    """
    log = log or (lambda *a, **k: None)
    normalized = []
    loser_ids = set()
    survivor_ids = set()
    for index, pair in enumerate(pairs, start=1):
        try:
            survivor_id, loser_id = (int(value) for value in pair)
        except (TypeError, ValueError):
            raise ReviewedMergeError(
                f'Pair {index} must contain integer survivor_id,loser_id.'
            ) from None
        if survivor_id <= 0 or loser_id <= 0:
            raise ReviewedMergeError(f'Pair {index} contains a non-positive id.')
        if survivor_id == loser_id:
            raise ReviewedMergeError(
                f'Pair {index} uses #{survivor_id} as both survivor and loser.'
            )
        if loser_id in loser_ids:
            raise ReviewedMergeError(
                f'Loser #{loser_id} appears more than once in the merge plan.'
            )
        normalized.append((survivor_id, loser_id))
        survivor_ids.add(survivor_id)
        loser_ids.add(loser_id)

    if not normalized:
        raise ReviewedMergeError('The reviewed merge plan is empty.')
    chained = survivor_ids & loser_ids
    if chained:
        ids = ', '.join(f'#{value}' for value in sorted(chained))
        raise ReviewedMergeError(
            f'Merge chains/cycles are not allowed; these ids are both survivor '
            f'and loser: {ids}.'
        )

    all_ids = survivor_ids | loser_ids
    people_qs = DataPerson.objects.using(using)
    if apply:
        # The command wraps the reviewed apply in one transaction. Lock the
        # exact rows before validating their current org/login/Slack identity,
        # so a concurrent profile edit cannot invalidate the reviewed plan
        # between validation and _merge.
        people_qs = people_qs.select_for_update()
    people = people_qs.in_bulk(all_ids)
    missing = sorted(all_ids - set(people))
    if missing:
        raise ReviewedMergeError(
            'DataPerson id(s) not found: '
            + ', '.join(f'#{value}' for value in missing)
            + '.'
        )

    for survivor_id, loser_id in normalized:
        survivor = people[survivor_id]
        loser = people[loser_id]
        if organization_id is not None and (
            survivor.organization_id != organization_id
            or loser.organization_id != organization_id
        ):
            raise ReviewedMergeError(
                f'Cannot merge #{loser_id} into #{survivor_id}: every row in '
                f'this plan must belong to organization #{organization_id}.'
            )
        if survivor.organization_id is None or loser.organization_id is None:
            raise ReviewedMergeError(
                f'Cannot merge #{loser_id} into #{survivor_id}: both rows must '
                f'first be assigned to the same organization.'
            )
        if survivor.organization_id != loser.organization_id:
            raise ReviewedMergeError(
                f'Cannot merge #{loser_id} into #{survivor_id}: organizations '
                f'differ ({loser.organization_id!r} vs '
                f'{survivor.organization_id!r}).'
            )
        if (
            survivor.user_id is not None
            and loser.user_id is not None
            and survivor.user_id != loser.user_id
        ):
            raise ReviewedMergeError(
                f'Cannot merge #{loser_id} into #{survivor_id}: both rows have '
                f'different linked logins.'
            )
        if survivor.user_id is None and loser.user_id is not None:
            raise ReviewedMergeError(
                f'Cannot merge linked row #{loser_id} into unlinked survivor '
                f'#{survivor_id}; choose the linked row as survivor.'
            )
        survivor_slack = _norm(survivor.slack_handle)
        loser_slack = _norm(loser.slack_handle)
        if survivor_slack and loser_slack and survivor_slack != loser_slack:
            raise ReviewedMergeError(
                f'Cannot merge #{loser_id} into #{survivor_id}: both rows have '
                f'different Slack handles.'
            )

    by_survivor = {}
    for survivor_id, loser_id in normalized:
        by_survivor.setdefault(survivor_id, []).append(loser_id)

    # Validate each complete merge cluster, not only each CSV row against the
    # original survivor. Otherwise a blank survivor plus two losers carrying
    # different identities (for example @alice and @bob) looks safe one pair
    # at a time, but _merge would silently keep whichever value it encounters
    # first and discard the other.
    for survivor_id, reviewed_loser_ids in by_survivor.items():
        survivor = people[survivor_id]
        cluster = [survivor] + [
            people[loser_id] for loser_id in reviewed_loser_ids
        ]
        login_ids = {
            person.user_id for person in cluster if person.user_id is not None
        }
        if len(login_ids) > 1:
            raise ReviewedMergeError(
                f'Cannot merge into #{survivor_id}: the complete reviewed '
                f'cluster has different linked logins.'
            )
        if login_ids and survivor.user_id is None:
            raise ReviewedMergeError(
                f'Cannot merge into unlinked survivor #{survivor_id}: choose '
                f'the linked row as survivor.'
            )
        slack_handles = {
            _norm(person.slack_handle)
            for person in cluster
            if _norm(person.slack_handle)
        }
        if len(slack_handles) > 1:
            raise ReviewedMergeError(
                f'Cannot merge into #{survivor_id}: the complete reviewed '
                f'cluster has different Slack handles.'
            )

    summary = {
        'clusters': len(by_survivor), 'merged_rows': 0, 'renamed_rows': 0,
        'repointed_groups': 0, 'repointed_tasks': 0,
        'repointed_definitions': 0, 'repointed_legacy_items': 0,
        'conflicts': 0, 'identity_conflicts': 0, 'name_conflicts': 0,
    }
    for survivor_id, reviewed_loser_ids in by_survivor.items():
        survivor = people[survivor_id]
        losers = [people[loser_id] for loser_id in reviewed_loser_ids]
        canonical_name = _reviewed_canonical_name(survivor, losers)
        survivor_row = _person_row(survivor)
        loser_rows = [_person_row(loser) for loser in losers]
        log(
            f"  REVIEWED MERGE {reviewed_loser_ids} -> keep #{survivor_id} "
            f"({survivor.name!r})"
        )
        _merge(
            DataPerson, ItemGroup, GovernanceTask, survivor_row, loser_rows,
            summary, apply, log, Definition=Definition, using=using,
            connection_obj=connection_obj,
        )
        if canonical_name and canonical_name != survivor.name:
            summary['renamed_rows'] += 1
            log(
                f"    restore plain name #{survivor_id}: "
                f"{survivor.name!r} -> {canonical_name!r}"
            )
            if apply:
                # _merge has deleted the reviewed plain-name loser, so the
                # organization-scoped case-insensitive uniqueness constraint is
                # now free for the canonical name. The surrounding command
                # transaction rolls the entire merge back if that assumption
                # was invalidated by another row.
                DataPerson.objects.using(using).filter(
                    id=survivor_id,
                ).update(name=canonical_name)
    return summary


def _person_row(person):
    return {
        'id': person.id,
        'name': person.name,
        'organization_id': person.organization_id,
        'user_id': person.user_id,
        'is_owner': person.is_owner,
        'is_steward': person.is_steward,
        'is_other': person.is_other,
        'slack_handle': person.slack_handle,
    }


def _reviewed_canonical_name(survivor, losers):
    """Recover the plain name after an operator confirms a disambiguated twin.

    ``_clean_names`` appends ``(data person <id>)`` when two name-only rows
    cannot be merged automatically.  A later reviewed merge deliberately keeps
    the login-linked row as survivor, which is commonly the suffixed row.  Once
    the plain-name loser is deleted the suffix no longer serves a purpose, so
    restore that loser's exact trimmed spelling.  Do not strip email/Slack or
    arbitrary parenthetical text: only our deterministic id suffix qualifies,
    and only when a reviewed loser proves the corresponding plain name.
    """
    current = (survivor.name or '').strip()
    suffix = f' (data person {survivor.id})'
    if not current.lower().endswith(suffix.lower()):
        return None
    base = current[:-len(suffix)].strip()
    if not base:
        return None
    for loser in losers:
        loser_name = (loser.name or '').strip()
        if _norm(loser_name) == _norm(base):
            return loser_name[:255]
    return None


def _clean_names(
        DataPerson, summary, apply, log, using=None, exclude_ids=()):
    """Trim names, fill empty ones, and disambiguate collisions without merging.

    Iterating by primary key makes the outcome deterministic: the oldest row
    keeps the plain display name and later rows get an identity hint. This also
    covers organization=NULL, where PostgreSQL's default NULL-distinct behavior
    previously let exact duplicates through.
    """
    people = DataPerson.objects.using(using).exclude(id__in=exclude_ids).order_by('id')
    taken_names = set()
    collided_keys = set()
    for person in people.iterator():
        base = (person.name or '').strip() or f'Unnamed person {person.id}'
        key = (person.organization_id, _norm(base))
        if key in taken_names:
            collided_keys.add(key)
            candidate = _disambiguate(person, taken_names, base=base)
        else:
            candidate = base[:255]
            taken_names.add((person.organization_id, _norm(candidate)))

        if candidate != person.name:
            log(f"    rename #{person.id}: {person.name!r} -> {candidate!r}")
            summary['renamed_rows'] += 1
            if apply:
                person.name = candidate
                person.save(update_fields=['name'])
    summary['name_conflicts'] += len(collided_keys)


def _disambiguate(person, taken_names, base=None, max_length=255):
    """A name for ``person`` that is unique within its org and actually fits.

    Truncates the BASE, never the finished string: chopping the result to
    ``max_length`` can cut the suffix off and hand back the very name we were
    trying to differ from.
    """
    base = (base if base is not None else (person.name or '').strip())
    user = getattr(person, 'user', None) if person.user_id else None
    hint = (
        getattr(user, 'email', None)
        or getattr(user, 'username', None)
        or (person.slack_handle or '').strip()
        or f'data person {person.id}'
    )

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
    suffix = f' (data person {person.id})'
    candidate = f'{base[:max(1, max_length - len(suffix))]}{suffix}'[:max_length]
    taken_names.add((person.organization_id, _norm(candidate)))
    return candidate


def _merge(
        DataPerson, ItemGroup, GovernanceTask, survivor_row, losers,
        summary, apply, log, Definition=None, using=None, connection_obj=None):
    """Fold ``losers`` into ``survivor_row``, repointing everything that refers
    to them. Lossless by design: a merge must never shrink someone's reach."""
    loser_ids = [r['id'] for r in losers]
    if not loser_ids:
        return

    summary['repointed_groups'] += (
        ItemGroup.objects.using(using).filter(
            ownership_person_id__in=loser_ids).count()
        + ItemGroup.objects.using(using).filter(steward_id__in=loser_ids).count()
    )
    summary['repointed_tasks'] += GovernanceTask.objects.using(using).filter(
        assignee_id__in=loser_ids).count()
    if Definition is not None:
        summary['repointed_definitions'] += Definition.objects.using(using).filter(
            ownership_person_id__in=loser_ids).count()
    summary['merged_rows'] += len(loser_ids)

    if not apply:
        summary['repointed_legacy_items'] += _repoint_legacy_item_columns(
            loser_ids, survivor_row['id'], apply=False,
            connection_obj=connection_obj,
        )
        return

    survivor = DataPerson.objects.using(using).get(id=survivor_row['id'])
    ItemGroup.objects.using(using).filter(
        ownership_person_id__in=loser_ids).update(
        ownership_person_id=survivor.id)
    ItemGroup.objects.using(using).filter(
        steward_id__in=loser_ids).update(steward_id=survivor.id)
    GovernanceTask.objects.using(using).filter(
        assignee_id__in=loser_ids).update(assignee_id=survivor.id)
    if Definition is not None:
        Definition.objects.using(using).filter(
            ownership_person_id__in=loser_ids).update(
                ownership_person_id=survivor.id)
    summary['repointed_legacy_items'] += _repoint_legacy_item_columns(
        loser_ids, survivor.id, apply=True, connection_obj=connection_obj,
    )

    # Union the departments and OR the role flags: a merge must never take away
    # a person's reach, or they'd vanish from a dropdown they belonged in. Same
    # for the slack handle and the login link.
    # Legacy corruption can leave cross-tenant rows in this unconstrained M2M.
    # A merge must not carry those links onto the surviving identity.
    department_scope = {'organization_id': survivor.organization_id}
    dept_ids = set(
        survivor.departments
        .filter(**department_scope)
        .values_list('id', flat=True)
    )
    for loser in DataPerson.objects.using(using).filter(id__in=loser_ids):
        dept_ids |= set(
            loser.departments
            .filter(**department_scope)
            .values_list('id', flat=True)
        )
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
    DataPerson.objects.using(using).filter(id__in=loser_ids).delete()
