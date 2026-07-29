"""
Management command: remove_category

Delete a Category by name and detach it from every ItemGroup that points at it.

Written for the customer who created a category literally named "To Be Deleted"
and used it as a *status* marker — which the catalog already models properly on
``ItemGroup.status`` ('DELETED', labelled "To Be Deleted"). Two places saying the
same thing is exactly the drift the ItemGroup split was meant to end, so the
category has to go.

Detaching is not free: an ItemGroup with no category is, by definition, a
NO_CATEGORY target, so the next task sweep mints a "Set a category" task for
every measure this command touches (see catalog/governance_tasks.py). That
consequence is reported BEFORE anything is written — it is usually a surprise,
and it is the reason this command is dry-run by default.

Usage:
    python manage.py remove_category "To Be Deleted"            # dry run
    python manage.py remove_category "To Be Deleted" --org 1    # scope to an org
    python manage.py remove_category "To Be Deleted" --apply    # write changes
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from catalog.governance_tasks import REASON_NO_CATEGORY, REASON_POLICY
from catalog.models import Category, ItemGroup, Organization

# Which kinds the NO_CATEGORY sweep actually targets, read off the policy so the
# number this command quotes can never drift from the number of tasks the sweep
# goes on to create.
_NO_CATEGORY_KINDS = REASON_POLICY[REASON_NO_CATEGORY]['kinds']

# Governance moved off Item in migration 0029, but only in Django's *state* —
# catalog_item.category_id and its FK to catalog_category are still live in the
# database as a deprecated safety net. Django therefore doesn't know the column
# exists and won't clear it, and Postgres defers the FK check to COMMIT, so an
# ORM-only delete raises IntegrityError from the end of the atomic block ("Key
# (id)=(4) is still referenced from table catalog_item") with nothing written.
# On production data 11 rows still point at the 'To Be Deleted' category, so
# this is the normal case, not a corner case.
_LEGACY_ITEM_TABLE = 'catalog_item'
_LEGACY_ITEM_COLUMN = 'category_id'


class Command(BaseCommand):
    help = ('Delete a Category by name (case-insensitive) and set '
            'ItemGroup.category to NULL everywhere it was used. Dry run unless '
            '--apply is passed.')

    def add_arguments(self, parser):
        parser.add_argument('name', type=str,
                            help='Category name, matched case-insensitively.')
        parser.add_argument('--org', type=int, default=None,
                            help='Organization id. Omit to match the name in every org.')
        parser.add_argument('--apply', action='store_true',
                            help='Persist changes (default: dry run)')

    def handle(self, *args, **options):
        name = options['name']
        org_id = options['org']
        apply = options['apply']

        org = None
        if org_id is not None:
            org = Organization.objects.filter(pk=org_id).first()
            if org is None:
                self.stderr.write(f'Organization id={org_id} not found.')
                return

        qs = Category.objects.filter(name__iexact=name)
        if org is not None:
            qs = qs.filter(organization=org)
        cats = list(qs.select_related('organization').order_by('organization_id', 'id'))

        if not cats:
            scope = f' in org {org.name} (id={org.id})' if org else ''
            self.stdout.write(self.style.WARNING(f'No category named {name!r}{scope}. Nothing to do.'))
            return

        # (name, organization) is unique, so several matches mean either case
        # variants in one org or the same name in several orgs. Wiping another
        # tenant's category because they happened to pick the same name is not a
        # recoverable mistake — make them name the org instead.
        org_ids = {c.organization_id for c in cats}
        if org is None and len(org_ids) > 1:
            self.stderr.write(f'{name!r} exists in {len(org_ids)} organizations:')
            for c in cats:
                owner = c.organization.name if c.organization else '(no organization)'
                self.stderr.write(f'  id={c.id}  {c.name!r}  → {owner}')
            self.stderr.write('Re-run with --org <id> to pick one.')
            return

        self.stdout.write(f'Matched {len(cats)} category row(s) for {name!r}:')
        for c in cats:
            owner = c.organization.name if c.organization else '(no organization)'
            self.stdout.write(f'  id={c.id}  {c.name!r}  → {owner}')

        cat_ids = [c.id for c in cats]
        groups = ItemGroup.objects.filter(category__in=cats)
        total = groups.count()
        legacy = self._legacy_item_rows(cat_ids)
        # Same predicate as _target_groups_qs(REASON_NO_CATEGORY): a group that is
        # deleted, or already on its way out, is never asked to be categorised.
        will_get_task = (groups
                         .filter(kind__in=_NO_CATEGORY_KINDS, deleted=False)
                         .exclude(status='DELETED')
                         .count())

        self.stdout.write(f'\nItemGroups currently using it: {total}')
        for label in (groups.order_by('group_key')
                      .values_list('group_key', flat=True)[:15]):
            self.stdout.write(f'  {label}')
        if total > 15:
            self.stdout.write(f'  … +{total - 15} more')

        if legacy:
            self.stdout.write(
                f'Deprecated catalog_item.category_id rows pointing at it: {legacy} '
                f'(cleared as well — their FK still blocks the delete)')

        self.stdout.write(self.style.WARNING(
            f'\n⚠  All {total} of these become UNCATEGORISED. '
            f'{will_get_task} of them are measures that qualify for the NO_CATEGORY '
            f'rule, so the next task sweep will open {will_get_task} new '
            f'"Set a category" task(s), routed to each measure\'s Owner (Steward as '
            f'fallback). The sweep is the Task Manager\'s "Generate tasks" button or '
            f'`python manage.py generate_governance_tasks --dry-run` first.'))

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDry run — no changes written. Re-run with --apply.'))
            return

        with transaction.atomic():
            # Explicit detach before the delete: ItemGroup.category is SET_NULL, so
            # Django's collector would clear these anyway — doing it here is what
            # gives us a row count to report, and keeps both writes in one
            # transaction so we can never end up with a deleted category and
            # half-detached groups.
            detached = ItemGroup.objects.filter(category_id__in=cat_ids).update(category=None)
            self._clear_legacy_item_rows(cat_ids)
            Category.objects.filter(pk__in=cat_ids).delete()

        self.stdout.write(self.style.SUCCESS(
            f'\n✅ Detached {detached} ItemGroup(s) and deleted '
            f'{len(cat_ids)} category row(s) named {name!r}.'))

    # ──────────────────────────────────────────────────────────────────────
    def _has_legacy_column(self):
        """Is the deprecated ``catalog_item.category_id`` column still there?

        Introspected rather than assumed because models.py promises the column
        gets physically dropped in a later migration — when that lands this
        command keeps working instead of failing on a missing column.
        """
        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(
                cursor, _LEGACY_ITEM_TABLE)
        return any(col.name == _LEGACY_ITEM_COLUMN for col in description)

    def _legacy_item_rows(self, cat_ids):
        """How many deprecated Item rows still point at these categories."""
        if not self._has_legacy_column():
            return 0
        placeholders = ', '.join(['%s'] * len(cat_ids))
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT COUNT(*) FROM {_LEGACY_ITEM_TABLE} '
                f'WHERE {_LEGACY_ITEM_COLUMN} IN ({placeholders})', cat_ids)
            return cursor.fetchone()[0]

    def _clear_legacy_item_rows(self, cat_ids):
        """NULL the deprecated column so the FK lets the delete through."""
        if not self._has_legacy_column():
            return
        placeholders = ', '.join(['%s'] * len(cat_ids))
        with connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE {_LEGACY_ITEM_TABLE} SET {_LEGACY_ITEM_COLUMN} = NULL '
                f'WHERE {_LEGACY_ITEM_COLUMN} IN ({placeholders})', cat_ids)
