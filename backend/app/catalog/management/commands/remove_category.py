"""Safely remove a category, with lifecycle conversion for the legacy marker.

``To Be Deleted`` was historically used as a category even though deletion is
now represented by ``ItemGroup.status == "DELETED"``. Removing that marker
without converting its groups first erases intent and creates incorrect
NO_CATEGORY work. This command previews the consequences and, on ``--apply``,
performs the conversion, audit logging, item mirroring, task reconciliation,
detach, and delete atomically.

Other category names retain ordinary removal behavior: their groups become
uncategorized and governance Category tasks are reconciled immediately.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from catalog.governance_tasks import (
    REASON_NO_CATEGORY,
    REASON_POLICY,
    sync_group_metadata_tasks,
)
from catalog.models import (
    Category,
    Item,
    ItemGroup,
    Organization,
    StatusChangeLog,
)


_NO_CATEGORY_KINDS = REASON_POLICY[REASON_NO_CATEGORY]['kinds']
_DELETION_MARKER = 'to be deleted'
_CHUNK = 900


def _chunks(values):
    values = list(values)
    for index in range(0, len(values), _CHUNK):
        yield values[index:index + _CHUNK]

# Governance moved off Item in migration 0029, but the deprecated physical
# column and FK can still exist. It must be cleared before Category deletion.
_LEGACY_ITEM_TABLE = 'catalog_item'
_LEGACY_ITEM_COLUMN = 'category_id'


class Command(BaseCommand):
    help = (
        'Delete a Category by name and detach it from ItemGroups. The legacy '
        '"To Be Deleted" marker is converted to DELETED status first. Dry run '
        'unless --apply is passed.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'name',
            type=str,
            help='Category name, matched case-insensitively.',
        )
        parser.add_argument(
            '--org',
            type=int,
            default=None,
            help='Organization id. Omit to match the name in every org.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist changes (default: dry run).',
        )

    @staticmethod
    def _category_queryset(name, deletion_marker):
        if deletion_marker:
            return Category.objects.filter(
                name__iregex=r'^\s*to be deleted\s*$',
            )
        return Category.objects.filter(name__iexact=name)

    @staticmethod
    def _trusted_marker_filter(categories):
        """Exact-tenant category links eligible to carry deletion intent."""
        trusted = Q(pk__in=[])
        for category in categories:
            trusted |= Q(
                category_id=category.id,
                organization_id=category.organization_id,
            )
        return trusted

    def handle(self, *args, **options):
        name = options['name']
        org_id = options['org']
        apply = options['apply']
        deletion_marker = name.strip().casefold() == _DELETION_MARKER

        org = None
        if org_id is not None:
            org = Organization.objects.filter(pk=org_id).first()
            if org is None:
                self.stderr.write(f'Organization id={org_id} not found.')
                return

        category_qs = self._category_queryset(name, deletion_marker)
        if org is not None:
            category_qs = category_qs.filter(organization=org)
        categories = list(
            category_qs.select_related('organization')
            .order_by('organization_id', 'id')
        )

        if not categories:
            scope = f' in org {org.name} (id={org.id})' if org else ''
            self.stdout.write(self.style.WARNING(
                f'No category named {name!r}{scope}. Nothing to do.',
            ))
            return

        # The same display name in another tenant is not authorization to
        # mutate it. Force an explicit organization whenever several tenants
        # match.
        org_ids = {category.organization_id for category in categories}
        if org is None and len(org_ids) > 1:
            self.stderr.write(
                f'{name!r} exists in {len(org_ids)} organizations:',
            )
            for category in categories:
                owner = (
                    category.organization.name
                    if category.organization else '(no organization)'
                )
                self.stderr.write(
                    f'  id={category.id}  {category.name!r} -> {owner}',
                )
            self.stderr.write('Re-run with --org <id> to pick one.')
            return

        self.stdout.write(
            f'Matched {len(categories)} category row(s) for {name!r}:',
        )
        for category in categories:
            owner = (
                category.organization.name
                if category.organization else '(no organization)'
            )
            self.stdout.write(
                f'  id={category.id}  {category.name!r} -> {owner}',
            )

        category_ids = [category.id for category in categories]
        groups = ItemGroup.objects.filter(category_id__in=category_ids)
        total = groups.count()
        legacy_items = self._legacy_item_rows(category_ids)

        self.stdout.write(f'\nItemGroups currently using it: {total}')
        for label in (
            groups.order_by('group_key')
            .values_list('group_key', flat=True)[:15]
        ):
            self.stdout.write(f'  {label}')
        if total > 15:
            self.stdout.write(f'  ... +{total - 15} more')
        if legacy_items:
            self.stdout.write(
                'Deprecated catalog_item.category_id rows pointing at it: '
                f'{legacy_items} (cleared in the same transaction).',
            )

        if deletion_marker:
            trusted_groups = groups.filter(
                self._trusted_marker_filter(categories),
            )
            trusted_total = trusted_groups.count()
            cross_tenant = total - trusted_total
            status_counts = {
                row['status']: row['count']
                for row in (
                    trusted_groups.values('status')
                    .annotate(count=Count('id'))
                    .order_by('status')
                )
            }
            will_promote = trusted_total - status_counts.get('DELETED', 0)
            breakdown = ', '.join(
                f'{status}={count}'
                for status, count in status_counts.items()
            ) or 'none'
            message = (
                f'\nDeletion-marker conversion: {will_promote} non-DELETED '
                f'ItemGroup(s) will move to DELETED; '
                f'{status_counts.get("DELETED", 0)} are already DELETED. '
                f'Current trusted status breakdown: {breakdown}. '
                f'The transaction will append {will_promote} status-history '
                f'row(s), stamp lifecycle time, mirror Item.status, detach '
                f'the category, and reconcile status/category tasks. '
                f'No trusted converted group will receive NO_CATEGORY work.'
            )
            if cross_tenant:
                message += (
                    f' {cross_tenant} cross-tenant category link(s) are '
                    f'untrusted: they will be detached but not promoted.'
                )
            self.stdout.write(self.style.WARNING(message))
        else:
            task_targets = (
                groups
                .annotate(
                    has_active_items=Exists(
                        Item.objects.filter(
                            item_group_id=OuterRef('pk'),
                            deleted=False,
                        )
                    )
                )
                .filter(
                    deleted=False,
                    has_active_items=True,
                )
                .exclude(status='DELETED')
            )
            if _NO_CATEGORY_KINDS:
                task_targets = task_targets.filter(
                    kind__in=_NO_CATEGORY_KINDS,
                )
            will_get_task = task_targets.count()
            self.stdout.write(self.style.WARNING(
                f'\nAll {total} group(s) become uncategorized. '
                f'{will_get_task} qualify for the NO_CATEGORY rule and task '
                f'reconciliation can open "Set a category" work for them.',
            ))

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDry run - no changes written. Re-run with --apply.',
            ))
            return

        with transaction.atomic():
            locked_category_qs = self._category_queryset(
                name,
                deletion_marker,
            ).select_for_update().filter(pk__in=category_ids)
            if org is not None:
                locked_category_qs = locked_category_qs.filter(
                    organization=org,
                )
            locked_categories = list(
                locked_category_qs.order_by('organization_id', 'id'),
            )
            locked_category_ids = [
                category.id for category in locked_categories
            ]
            locked_groups = list(
                ItemGroup.objects.select_for_update()
                .filter(category_id__in=locked_category_ids)
                .order_by('id')
            )
            group_ids = [group.id for group in locked_groups]

            promoted = logs_created = items_mirrored = timestamps_stamped = 0
            if deletion_marker:
                trusted_identities = {
                    (category.id, category.organization_id)
                    for category in locked_categories
                }
                trusted_groups = [
                    group
                    for group in locked_groups
                    if (
                        group.category_id,
                        group.organization_id,
                    ) in trusted_identities
                ]
                transitioning = [
                    group
                    for group in trusted_groups
                    if group.status != 'DELETED'
                ]
                now = timezone.now()
                if transitioning:
                    StatusChangeLog.objects.bulk_create(
                        [
                            StatusChangeLog(
                                organization_id=group.organization_id,
                                item_group_id=group.id,
                                group_key=group.group_key,
                                old_status=group.status,
                                new_status='DELETED',
                                changed_by=None,
                            )
                            for group in transitioning
                        ],
                        batch_size=1000,
                    )
                    logs_created = len(transitioning)
                    for id_chunk in _chunks(
                            group.id for group in transitioning):
                        promoted += ItemGroup.objects.filter(
                            id__in=id_chunk,
                        ).update(status='DELETED')

                trusted_ids = [group.id for group in trusted_groups]
                if trusted_ids:
                    for id_chunk in _chunks(trusted_ids):
                        timestamps_stamped += ItemGroup.objects.filter(
                            id__in=id_chunk,
                            status='DELETED',
                            deleted_at__isnull=True,
                        ).update(deleted_at=now)
                        items_mirrored += (
                            Item.objects.filter(item_group_id__in=id_chunk)
                            .exclude(status='DELETED')
                            .update(status='DELETED')
                        )

            detached = 0
            for category_chunk in _chunks(locked_category_ids):
                detached += ItemGroup.objects.filter(
                    category_id__in=category_chunk,
                ).update(category=None)
            self._clear_legacy_item_rows(locked_category_ids)
            for category_chunk in _chunks(locked_category_ids):
                Category.objects.filter(pk__in=category_chunk).delete()

            task_summary = sync_group_metadata_tasks(
                group_ids,
                create_missing_category=True,
                create_missing_status=deletion_marker,
            )

        if deletion_marker:
            self.stdout.write(self.style.SUCCESS(
                f'\nConverted {promoted} ItemGroup(s) to DELETED, wrote '
                f'{logs_created} status-history row(s), stamped '
                f'{timestamps_stamped} existing DELETED group(s), mirrored '
                f'{items_mirrored} Item status row(s), detached {detached} '
                f'group(s), and deleted {len(locked_category_ids)} category '
                f'row(s). Tasks: {task_summary}.',
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\nDetached {detached} ItemGroup(s), deleted '
                f'{len(locked_category_ids)} category row(s), and reconciled '
                f'tasks: {task_summary}.',
            ))

    def _has_legacy_column(self):
        """Return whether the deprecated Item.category_id column still exists."""
        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(
                cursor,
                _LEGACY_ITEM_TABLE,
            )
        return any(
            column.name == _LEGACY_ITEM_COLUMN
            for column in description
        )

    def _legacy_item_rows(self, category_ids):
        if not category_ids or not self._has_legacy_column():
            return 0
        quoted_table = connection.ops.quote_name(_LEGACY_ITEM_TABLE)
        quoted_column = connection.ops.quote_name(_LEGACY_ITEM_COLUMN)
        total = 0
        with connection.cursor() as cursor:
            for category_chunk in _chunks(category_ids):
                placeholders = ', '.join(['%s'] * len(category_chunk))
                cursor.execute(
                    f'SELECT COUNT(*) FROM {quoted_table} '
                    f'WHERE {quoted_column} IN ({placeholders})',
                    category_chunk,
                )
                total += cursor.fetchone()[0]
        return total

    def _clear_legacy_item_rows(self, category_ids):
        if not category_ids or not self._has_legacy_column():
            return
        quoted_table = connection.ops.quote_name(_LEGACY_ITEM_TABLE)
        quoted_column = connection.ops.quote_name(_LEGACY_ITEM_COLUMN)
        with connection.cursor() as cursor:
            for category_chunk in _chunks(category_ids):
                placeholders = ', '.join(['%s'] * len(category_chunk))
                cursor.execute(
                    f'UPDATE {quoted_table} SET {quoted_column} = NULL '
                    f'WHERE {quoted_column} IN ({placeholders})',
                    category_chunk,
                )
