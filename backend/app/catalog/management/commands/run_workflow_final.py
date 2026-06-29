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

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from catalog.models import Item, NetworkEdge, Summary
from catalog.services.bridge_builder import build_cross_tool_bridges


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
        parser.add_argument('--organization-id', type=int, default=None,
                            help='Organization PK to scope the final step')

    def handle(self, *args, **kwargs):
        self.organization_id = kwargs.get('organization_id')
        org_id_literal = 'NULL'
        if self.organization_id is not None:
            org_id_literal = str(int(self.organization_id))

        with transaction.atomic(), connection.cursor() as cursor:
            build_cross_tool_bridges(
                cursor,
                org_id_literal,
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

        edge_qs = NetworkEdge.objects.all()
        if self.organization_id is not None:
            edge_qs = edge_qs.filter(organization_id=self.organization_id)

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

        item_qs = Item.objects.filter(item_type__in=DBT_PRODUCER_TYPES, deleted=False)
        if self.organization_id is not None:
            item_qs = item_qs.filter(organization_id=self.organization_id)

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
        """Recalculate summary statistics across all sources."""
        self.stdout.write('Calculating summary statistics...')
        total_measures = Item.objects.filter(item_type='PB_MEASURE', deleted=False).count()
        unused_measures = Item.objects.filter(item_type='PB_MEASURE', is_unused=True, deleted=False).count()
        total_columns = Item.objects.filter(item_type='PB_COLUMN', deleted=False).count()
        unused_columns = Item.objects.filter(item_type='PB_COLUMN', is_unused=True, deleted=False).count()
        total_reports = Item.objects.filter(item_type='PB_REPORT', deleted=False).count()

        Summary.objects.all().delete()
        Summary.objects.create(
            total_measures=total_measures,
            unused_measures=unused_measures,
            total_columns=total_columns,
            unused_columns=unused_columns,
            total_reports=total_reports,
            organization_id=self.organization_id,
        )
        self.stdout.write(f'  → Summary: {total_measures} measures, {total_columns} columns, {total_reports} reports')
