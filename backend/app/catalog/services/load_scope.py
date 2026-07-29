"""Fail-closed tenant/source guards for catalog loader commands.

Catalog Item and lineage graph identities are still globally unique in the
database. Until those legacy keys become tenant-composite, loaders must reject
an incoming identity already owned by another organization/source instead of
silently adopting or overwriting it.
"""

import csv
from contextlib import contextmanager

from django.core.management.base import CommandError
from django.db import connection

from ..models import (
    IntegrationSource,
    Item,
    NetworkEdge,
    NetworkNode,
    Organization,
)

_CHUNK = 900
_EDGE_CHUNK = 300
_LOAD_LOCK_NAMESPACE = 0x574443  # "WDC"
_LOAD_LOCK_KEY = 1


def _chunks(values):
    values = sorted(set(values))
    for index in range(0, len(values), _CHUNK):
        yield values[index:index + _CHUNK]


def acquire_catalog_load_lock():
    """Serialize global-key catalog loaders for the current transaction."""
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_advisory_xact_lock(%s, %s)',
            [_LOAD_LOCK_NAMESPACE, _LOAD_LOCK_KEY],
        )


@contextmanager
def catalog_load_session_lock():
    """Serialize one whole loader without extending its row-lock transaction.

    The CSV mutation transaction must commit before ``ensure_item_groups``
    acquires Group->Item locks. A transaction-level advisory lock would either
    release at that boundary or force the loader to retain Item locks and
    invert the curation path's order. PostgreSQL session advisory locks provide
    the required cross-loader serialization independently of those commits.
    """
    if connection.vendor != 'postgresql':
        yield
        return

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_advisory_lock(%s, %s)',
            [_LOAD_LOCK_NAMESPACE, _LOAD_LOCK_KEY],
        )
    try:
        yield
    finally:
        # A broken/closed connection has already released its session locks.
        # Do not mask the loader's original exception while attempting cleanup.
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_advisory_unlock(%s, %s)',
                    [_LOAD_LOCK_NAMESPACE, _LOAD_LOCK_KEY],
                )
        except Exception:
            connection.close()


def require_grouping_commit_boundary():
    """Fail closed if grouping would inherit row locks from a load phase."""
    if connection.in_atomic_block:
        raise RuntimeError(
            'Item loading must commit before ItemGroup reconciliation starts.'
        )


def read_incoming_identities(items_csv, graph_csv=None):
    """Read normalized Item/node/edge identities before any loader mutation."""
    item_ids = set()
    with open(items_csv, newline='', encoding='utf-8-sig') as handle:
        for raw_row in csv.DictReader(handle):
            row = {
                (key or '').strip().lower(): value
                for key, value in raw_row.items()
            }
            item_id = (row.get('item_id') or '').strip()
            if item_id:
                item_ids.add(item_id)

    node_ids = set()
    edges = set()
    if graph_csv:
        try:
            handle = open(graph_csv, newline='', encoding='utf-8-sig')
        except FileNotFoundError:
            handle = None
        if handle is not None:
            with handle:
                for raw_row in csv.DictReader(handle):
                    row = {
                        (key or '').strip().lower(): value
                        for key, value in raw_row.items()
                    }
                    source = (row.get('source_id') or '').strip()
                    target = (row.get('target_id') or '').strip()
                    if source:
                        node_ids.add(source)
                    if target:
                        node_ids.add(target)
                    if source and target:
                        edges.add((source, target))
    return item_ids, node_ids, edges


def require_exact_load_scope(
        organization_id, source_id, *, expected_source_types=None):
    """Validate and return the mandatory exact organization/source pair."""
    if organization_id is None or source_id is None:
        raise CommandError(
            '--organization-id and --source-id are required for catalog loads.'
        )
    if isinstance(organization_id, bool) or isinstance(source_id, bool):
        raise CommandError(
            'Organization and source identifiers must be positive integers.'
        )
    try:
        organization_id = int(organization_id)
        source_id = int(source_id)
    except (TypeError, ValueError) as exc:
        raise CommandError(
            'Organization and source identifiers must be integers.'
        ) from exc
    if organization_id <= 0 or source_id <= 0:
        raise CommandError(
            'Organization and source identifiers must be positive integers.'
        )

    if not Organization.objects.filter(pk=organization_id).exists():
        raise CommandError(f'Organization {organization_id} does not exist.')
    source = IntegrationSource.objects.filter(
        pk=source_id, organization_id=organization_id,
    ).first()
    if source is None:
        raise CommandError(
            f'Integration source {source_id} does not belong to '
            f'organization {organization_id}.'
        )
    if (
        expected_source_types is not None
        and source.source_type not in set(expected_source_types)
    ):
        raise CommandError(
            f'Integration source {source_id} has type {source.source_type!r}; '
            f'expected one of {sorted(set(expected_source_types))}.'
        )
    if not source.is_active:
        raise CommandError(
            f'Integration source {source_id} is inactive.'
        )
    sibling = (
        IntegrationSource.objects.filter(
            organization_id=organization_id,
            source_type=source.source_type,
            is_active=True,
        )
        .exclude(pk=source_id)
        .order_by('pk')
        .values_list('pk', flat=True)
        .first()
    )
    if sibling is not None:
        raise CommandError(
            f'Organization {organization_id} has multiple active '
            f'{source.source_type!r} sources ({source_id} and {sibling}). '
            'The legacy network schema has no source ownership, so the load '
            'was refused before mutation.'
        )
    return organization_id, source_id


def network_domain_is_exclusive(organization_id, source_id):
    """Whether this is the org's only source of its integration type.

    Network rows lack source ownership. Destructive replacement is therefore
    safe only when no sibling source could own rows in the same graph domain.
    """
    source = IntegrationSource.objects.get(
        pk=source_id, organization_id=organization_id,
    )
    return not IntegrationSource.objects.filter(
        organization_id=organization_id,
        source_type=source.source_type,
        is_active=True,
    ).exclude(pk=source_id).exists()


def assert_incoming_identities_available(
        *, organization_id, source_id, item_ids=(), node_ids=(), edges=()):
    """Reject global-key collisions before a loader changes any catalog row.

    Item ownership is exact on both organization and integration source.
    Same-organization legacy rows with ``integration_source_id=NULL`` are not
    auto-adopted: operators must associate them explicitly before retrying.
    Network tables do not yet carry source identity, so their strongest
    enforceable boundary is exact organization.
    """
    for chunk in _chunks(item_ids):
        collision = (
            Item.objects.filter(pk__in=chunk)
            .exclude(
                organization_id=organization_id,
                integration_source_id=source_id,
            )
            .order_by('pk')
            .values_list('pk', flat=True)
            .first()
        )
        if collision is not None:
            raise CommandError(
                f'Incoming item identity {collision!r} is already owned by '
                'another organization/source; no load changes were applied.'
            )

    for chunk in _chunks(node_ids):
        collision = (
            NetworkNode.objects.filter(pk__in=chunk)
            .exclude(organization_id=organization_id)
            .order_by('pk')
            .values_list('pk', flat=True)
            .first()
        )
        if collision is not None:
            raise CommandError(
                f'Incoming network node {collision!r} is already owned by '
                'another organization; no load changes were applied.'
            )

    edge_values = sorted(set(edges))
    for index in range(0, len(edge_values), _EDGE_CHUNK):
        chunk = edge_values[index:index + _EDGE_CHUNK]
        predicate = None
        from django.db.models import Q

        for source, target in chunk:
            pair = Q(source=source, target=target)
            predicate = pair if predicate is None else predicate | pair
        if predicate is None:
            continue
        collision = (
            NetworkEdge.objects.filter(predicate)
            .exclude(organization_id=organization_id)
            .order_by('source', 'target')
            .values_list('source', 'target')
            .first()
        )
        if collision is not None:
            raise CommandError(
                f'Incoming network edge {collision!r} is already owned by '
                'another organization; no load changes were applied.'
            )
