"""
Workflow Final Step — runs after ALL sources have loaded.

This command handles cross-tool operations that require data from multiple
sources to be present in the database:

1. Cross-tool bridge edges (dbt ↔ PowerBI) at table and column level. The
   matching is delegated to ``catalog.services.bridge_builder`` which
   prefers the BigQuery FQN as the join key and falls back to display-name
   matching when the FQN is unavailable.
2. Backfill dbt usage stats (``is_unused``, ``connected_reports``) — only
   computable after the bridge is built, since "unused" means no consumer in
   either dbt or PowerBI.
3. Summary statistics recalculation.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from catalog.models import Item, NetworkEdge, Organization, Summary
from catalog.services.bridge_builder import build_cross_tool_bridges
from catalog.services.load_scope import acquire_catalog_load_lock


# Item types that count as "real" downstream consumers of a dbt asset. A
# DBT_TEST or own-DBT_COLUMN child does not make a model "used" — those are
# the model's own structure / quality checks, not consumers.
DBT_CONSUMER_PREFIXES = ('DBT_MODEL::', 'DBT_SEED::', 'PB_TABLE::',
                         'PB_COLUMN::', 'PB_MEASURE::', 'PB_REPORT::')

# dbt asset types we backfill stats for.
DBT_PRODUCER_TYPES = ('DBT_MODEL', 'DBT_SEED', 'DBT_SOURCE')


class Command(BaseCommand):
    help = 'Run the workflow final step: cross-tool bridges + summary calculation'

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', type=int, required=True,
                            help='Organization PK to scope the final step')

    @transaction.atomic
    def handle(self, *args, **kwargs):
        self.organization_id = kwargs.get('organization_id')
        if (
            isinstance(self.organization_id, bool)
            or not isinstance(self.organization_id, int)
            or self.organization_id <= 0
        ):
            raise CommandError('--organization-id must be a positive integer.')
        if not Organization.objects.filter(pk=self.organization_id).exists():
            raise CommandError(
                f'Organization {self.organization_id} does not exist.'
            )

        # Hold the same global catalog-write lock as both loaders through the
        # bridge rebuild, dbt usage backfill, and Summary replacement. Otherwise
        # a loader could change Items/edges between these three dependent steps.
        acquire_catalog_load_lock()
        with connection.cursor() as cursor:
            build_cross_tool_bridges(
                cursor,
                self.organization_id,
                write=self.stdout.write,
            )

        self._backfill_dbt_usage_stats()
        self._calculate_summary()
        self.stdout.write(self.style.SUCCESS('Workflow final step complete.'))

    def _backfill_dbt_usage_stats(self):
        """Populate ``is_unused`` and ``connected_reports`` for dbt items.

        Walks the merged NetworkEdge graph (which now includes the cross-tool
        bridges built one step above) and, for each DBT_MODEL / DBT_SEED /
        DBT_SOURCE, counts how many distinct PB_REPORT nodes are reachable
        downstream. ``is_unused`` is set when the dbt asset has zero outgoing
        edges to a "real" consumer type (other dbt models or PowerBI assets).
        """
        self.stdout.write('Backfilling dbt usage stats from graph...')

        edge_qs = NetworkEdge.objects.filter(
            organization_id=self.organization_id,
        )

        # Build adjacency list. node_id strings are the canonical keys.
        adjacency = defaultdict(set)
        for source, target in edge_qs.values_list('source', 'target'):
            if source and target:
                adjacency[source].add(target)

        # PB_REPORT::<id> reachability via BFS from each dbt producer.
        # Memoize per-node downstream report sets to avoid re-walking shared
        # subgraphs (a single PB_TABLE often feeds many DBT consumers).
        reports_cache: dict[str, set] = {}

        def downstream_reports(node_id: str, visiting: set) -> set:
            if node_id in reports_cache:
                return reports_cache[node_id]
            if node_id in visiting:  # cycle guard
                return set()
            visiting.add(node_id)
            collected = set()
            if node_id.startswith('PB_REPORT::'):
                collected.add(node_id)
            for child in adjacency.get(node_id, ()):
                collected |= downstream_reports(child, visiting)
            visiting.remove(node_id)
            reports_cache[node_id] = collected
            return collected

        item_qs = Item.objects.filter(
            organization_id=self.organization_id,
            item_type__in=DBT_PRODUCER_TYPES,
            deleted=False,
        )

        updates = []
        for item in item_qs.only('item_id', 'item_type', 'is_unused', 'connected_reports'):
            node_id = f'{item.item_type}::{item.item_id}'

            # is_unused: no outgoing edge to a real consumer type.
            children = adjacency.get(node_id, set())
            has_consumer = any(
                child.startswith(DBT_CONSUMER_PREFIXES) for child in children
            )
            new_is_unused = not has_consumer

            # connected_reports: distinct PB_REPORT descendants.
            new_connected = len(downstream_reports(node_id, set()))

            if item.is_unused != new_is_unused or item.connected_reports != new_connected:
                item.is_unused = new_is_unused
                item.connected_reports = new_connected
                updates.append(item)

        if updates:
            Item.objects.bulk_update(updates, ['is_unused', 'connected_reports'], batch_size=500)
        self.stdout.write(
            f'  → dbt usage backfill: {len(updates)} items updated '
            f'(scanned {item_qs.count()} dbt producers).'
        )

    def _calculate_summary(self):
        """Recalculate this organization's summary across all its sources."""
        self.stdout.write('Calculating summary statistics...')
        items = Item.objects.filter(
            organization_id=self.organization_id,
            deleted=False,
        )
        total_measures = items.filter(item_type='PB_MEASURE').count()
        unused_measures = items.filter(
            item_type='PB_MEASURE', is_unused=True,
        ).count()
        total_columns = items.filter(item_type='PB_COLUMN').count()
        unused_columns = items.filter(
            item_type='PB_COLUMN', is_unused=True,
        ).count()
        total_reports = items.filter(item_type='PB_REPORT').count()

        with transaction.atomic():
            Summary.objects.filter(
                organization_id=self.organization_id,
            ).delete()
            Summary.objects.create(
                total_measures=total_measures,
                unused_measures=unused_measures,
                total_columns=total_columns,
                unused_columns=unused_columns,
                total_reports=total_reports,
                organization_id=self.organization_id,
            )
        self.stdout.write(f'  → Summary: {total_measures} measures, {total_columns} columns, {total_reports} reports')
