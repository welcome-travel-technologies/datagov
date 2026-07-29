"""End-to-end coverage for the 0062 cleanup through 0064 episode repair."""

import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


MIGRATE_FROM = [('catalog', '0061_definition_layer')]
MIGRATE_TO = [('catalog', '0064_governancetask_condition_cleared_at')]
PRODUCTION_MIGRATE_FROM = [('catalog', '0055_mcpapikey')]
PRODUCTION_MIGRATE_TO = [('catalog', '0064_governancetask_condition_cleared_at')]


def test_0056_dedupe_routes_every_operation_to_schema_editor_connection(
        monkeypatch):
    migration = importlib.import_module(
        'catalog.migrations.0056_dataperson_dedupe',
    )
    historical_models = {
        name: object()
        for name in ('DataPerson', 'ItemGroup', 'GovernanceTask')
    }

    class Apps:
        @staticmethod
        def get_model(app_label, model_name):
            assert app_label == 'catalog'
            return historical_models[model_name]

    class Connection:
        alias = 'non_default_catalog'

    class SchemaEditor:
        connection = Connection()

    captured = {}

    def fake_dedupe(*models, **kwargs):
        captured['models'] = models
        captured['kwargs'] = kwargs
        return {'merged_rows': 0}

    monkeypatch.setattr(
        migration, '_dedupe_data_persons_v0056', fake_dedupe,
    )

    migration.forwards(Apps(), SchemaEditor())

    assert captured['models'] == (
        historical_models['DataPerson'],
        historical_models['ItemGroup'],
        historical_models['GovernanceTask'],
    )
    assert captured['kwargs'] == {
        'using': 'non_default_catalog',
        'connection_obj': SchemaEditor.connection,
    }


def test_0059_backfill_routes_queries_to_schema_editor_connection():
    migration = importlib.import_module(
        'catalog.migrations.0059_governancetask_backfill_reason',
    )
    aliases = []

    class Query:
        def filter(self, **kwargs):
            return self

        def update(self, **kwargs):
            return 0

        def order_by(self, *fields):
            return self

        def values_list(self, *fields):
            return self

        def iterator(self):
            return iter(())

    class Manager:
        @staticmethod
        def using(alias):
            aliases.append(alias)
            return Query()

    class GovernanceTask:
        objects = Manager()

    class Apps:
        @staticmethod
        def get_model(app_label, model_name):
            assert (app_label, model_name) == (
                'catalog', 'GovernanceTask',
            )
            return GovernanceTask

    class Connection:
        alias = 'non_default_catalog'

    class SchemaEditor:
        connection = Connection()

    migration.backfill(Apps(), SchemaEditor())

    assert aliases == ['non_default_catalog']


def test_0064_backfill_routes_reads_and_bulk_updates_to_schema_editor_connection():
    migration = importlib.import_module(
        'catalog.migrations.0064_governancetask_condition_cleared_at',
    )
    completed_at = timezone.now()
    task = SimpleNamespace(
        organization_id=1,
        item_group_id=None,
        item_group=None,
        reason='ATTENTION',
        completed_at=completed_at,
        updated_at=completed_at,
        created_at=completed_at,
        condition_cleared_at=None,
    )
    calls = []

    class ItemQuery:
        def __init__(self, alias):
            self.alias = alias

        def filter(self, **kwargs):
            calls.append(('item_filter', self.alias, kwargs))
            return self

        def values_list(self, *fields, **kwargs):
            calls.append(('item_values_list', self.alias, fields, kwargs))
            return self

        def distinct(self):
            calls.append(('item_distinct', self.alias))
            return self

        def iterator(self, **kwargs):
            calls.append(('item_iterator', self.alias, kwargs))
            return iter(())

    class ItemManager:
        def using(self, alias):
            calls.append(('using', alias))
            return ItemQuery(alias)

    class StatusQuery:
        def __init__(self, alias):
            self.alias = alias

        def filter(self, **kwargs):
            calls.append(('status_filter', self.alias, kwargs))
            return self

        def values(self, *fields):
            calls.append(('status_values', self.alias, fields))
            return self

        def annotate(self, **kwargs):
            calls.append(('status_annotate', self.alias, kwargs))
            return self

        def values_list(self, *fields, **kwargs):
            calls.append((
                'status_values_list', self.alias, fields, kwargs,
            ))
            return self

        def iterator(self, **kwargs):
            calls.append(('status_iterator', self.alias, kwargs))
            return iter(())

    class StatusManager:
        def using(self, alias):
            calls.append(('using', alias))
            return StatusQuery(alias)

    class BoundManager:
        def __init__(self, alias):
            self.alias = alias
            self.projected = False

        def filter(self, **kwargs):
            calls.append(('filter', self.alias, kwargs))
            return self

        def update(self, **kwargs):
            calls.append(('update', self.alias, kwargs))
            return 0

        def values_list(self, *fields, **kwargs):
            calls.append(('values_list', self.alias, fields, kwargs))
            self.projected = True
            return self

        def select_related(self, *fields):
            calls.append(('select_related', self.alias, fields))
            return self

        def order_by(self, *fields):
            calls.append(('order_by', self.alias, fields))
            return self

        def iterator(self, **kwargs):
            calls.append(('iterator', self.alias, kwargs))
            return iter(()) if self.projected else iter([task])

        def bulk_update(self, rows, fields, **kwargs):
            calls.append((
                'bulk_update', self.alias, list(rows), fields, kwargs,
            ))

    class Manager:
        def using(self, alias):
            calls.append(('using', alias))
            return BoundManager(alias)

    GovernanceTask = SimpleNamespace(objects=Manager())
    Item = SimpleNamespace(objects=ItemManager())
    StatusChangeLog = SimpleNamespace(objects=StatusManager())

    class Apps:
        @staticmethod
        def get_model(app_label, model_name):
            assert app_label == 'catalog'
            return {
                'GovernanceTask': GovernanceTask,
                'Item': Item,
                'StatusChangeLog': StatusChangeLog,
            }[model_name]

    class Connection:
        alias = 'non_default_catalog'

    class SchemaEditor:
        connection = Connection()

    migration.backfill_condition_episodes(Apps(), SchemaEditor())

    assert task.condition_cleared_at == completed_at
    assert [call for call in calls if call[0] == 'using'] == [
        ('using', 'non_default_catalog'),
        ('using', 'non_default_catalog'),
        ('using', 'non_default_catalog'),
        ('using', 'non_default_catalog'),
        ('using', 'non_default_catalog'),
        ('using', 'non_default_catalog'),
        ('using', 'non_default_catalog'),
        ('using', 'non_default_catalog'),
    ]
    bulk_call = next(call for call in calls if call[0] == 'bulk_update')
    assert bulk_call[1:] == (
        'non_default_catalog',
        [task],
        ['condition_cleared_at'],
        {'batch_size': 1000},
    )


@pytest.mark.django_db(transaction=True)
def test_production_0055_upgrade_with_legacy_rows_reaches_0064_safely():
    """Exercise the exact deployed 0055→0064 path with representative data."""
    executor = MigrationExecutor(connection)
    executor.migrate(PRODUCTION_MIGRATE_FROM)
    old_apps = executor.loader.project_state(PRODUCTION_MIGRATE_FROM).apps

    try:
        Organization = old_apps.get_model('catalog', 'Organization')
        CustomUser = old_apps.get_model('catalog', 'CustomUser')
        DataPerson = old_apps.get_model('catalog', 'DataPerson')
        Category = old_apps.get_model('catalog', 'Category')
        Item = old_apps.get_model('catalog', 'Item')
        ItemGroup = old_apps.get_model('catalog', 'ItemGroup')
        GovernanceTask = old_apps.get_model('catalog', 'GovernanceTask')
        StatusChangeLog = old_apps.get_model('catalog', 'StatusChangeLog')

        org = Organization.objects.create(name='Production-shaped Org')
        login = CustomUser.objects.create(
            username='legacy-owner', email='legacy-owner@example.com',
        )
        survivor = DataPerson.objects.create(
            name='Alex Owner', organization=org, user=login,
            is_owner=True, is_steward=False,
        )
        duplicate = DataPerson.objects.create(
            name='Alexander Owner', organization=org, user=login,
            is_owner=False, is_steward=True,
        )
        namesake_one = DataPerson.objects.create(
            name='Twin', organization=org,
        )
        namesake_two = DataPerson.objects.create(
            name='Twin', organization=org,
        )

        obsolete = Category.objects.create(
            name='  TO BE DELETED  ', organization=org,
        )
        first_group = ItemGroup.objects.create(
            group_key='legacy::attention', kind='measure_name',
            organization=org, ownership_person=survivor, steward=survivor,
            status='ATTENTION',
        )
        second_group = ItemGroup.objects.create(
            group_key='legacy::deleted', kind='measure_name',
            organization=org, ownership_person=survivor, steward=survivor,
            category=obsolete, status='DELETED', deleted=True,
        )
        manual_attention_group = ItemGroup.objects.create(
            group_key='legacy::manual-attention', kind='measure_name',
            organization=org, ownership_person=survivor, steward=survivor,
            status='ATTENTION',
        )
        manual_deleted_group = ItemGroup.objects.create(
            group_key='legacy::manual-deleted', kind='measure_name',
            organization=org, ownership_person=survivor, steward=survivor,
            status='DELETED',
        )
        cleared_attention_group = ItemGroup.objects.create(
            group_key='legacy::cleared-attention', kind='measure_name',
            organization=org, ownership_person=survivor, steward=survivor,
            status='VERIFIED',
        )
        empty_attention_group = ItemGroup.objects.create(
            group_key='legacy::empty-attention', kind='measure_name',
            organization=org, ownership_person=survivor, steward=survivor,
            status='ATTENTION',
        )
        promoted_marker_group = ItemGroup.objects.create(
            group_key='legacy::promoted-marker', kind='measure_name',
            organization=org, ownership_person=survivor, steward=survivor,
            category=obsolete, status='ATTENTION',
        )
        first_item = Item.objects.create(
            item_id='legacy-attention-item', item_name='Legacy Attention',
            item_type='PB_MEASURE', group_id=first_group.group_key,
            item_group=first_group, organization=org, status='ATTENTION',
        )
        second_item = Item.objects.create(
            item_id='legacy-deleted-item', item_name='Legacy Deleted',
            item_type='PB_MEASURE', group_id=second_group.group_key,
            item_group=second_group, organization=org, status='DELETED',
            deleted=True,
        )
        ItemGroup.objects.filter(pk=first_group.pk).update(primary_item=first_item)
        ItemGroup.objects.filter(pk=second_group.pk).update(primary_item=second_item)
        promoted_marker_item = Item.objects.create(
            item_id='legacy-promoted-marker-item',
            item_name='Legacy promoted marker',
            item_type='PB_MEASURE',
            group_id=promoted_marker_group.group_key,
            item_group=promoted_marker_group,
            organization=org,
            status='ATTENTION',
        )
        ItemGroup.objects.filter(pk=promoted_marker_group.pk).update(
            primary_item=promoted_marker_item,
        )
        for group, status in (
            (manual_attention_group, 'ATTENTION'),
            (manual_deleted_group, 'DELETED'),
            (cleared_attention_group, 'VERIFIED'),
        ):
            item = Item.objects.create(
                item_id=f'{group.group_key}-item',
                item_name=group.group_key,
                item_type='PB_MEASURE',
                group_id=group.group_key,
                item_group=group,
                organization=org,
                status=status,
            )
            ItemGroup.objects.filter(pk=group.pk).update(primary_item=item)

        legacy_completed_at = timezone.now() - timedelta(days=40)
        superseded_manual_attention = GovernanceTask.objects.create(
            organization=org,
            item_group=first_group,
            assignee=duplicate,
            trigger_status='ATTENTION',
            title='Older dismissed attention episode',
            state='done',
            completed_at=legacy_completed_at,
            completed_by=login,
        )
        older_attention = GovernanceTask.objects.create(
            organization=org, item_group=first_group, assignee=duplicate,
            trigger_status='ATTENTION', title='Older attention',
        )
        newer_attention = GovernanceTask.objects.create(
            organization=org, item_group=first_group, assignee=duplicate,
            trigger_status='ATTENTION', title='Newer attention',
        )
        deleted_task = GovernanceTask.objects.create(
            organization=org, item_group=second_group, assignee=duplicate,
            trigger_status='DELETED', title='Confirm deletion',
        )
        manual_attention_task = GovernanceTask.objects.create(
            organization=org,
            item_group=manual_attention_group,
            assignee=duplicate,
            trigger_status='ATTENTION',
            title='Dismissed attention',
            state='done',
            completed_at=legacy_completed_at,
            completed_by=login,
        )
        manual_deleted_task = GovernanceTask.objects.create(
            organization=org,
            item_group=manual_deleted_group,
            assignee=duplicate,
            trigger_status='DELETED',
            title='Dismissed deletion',
            state='done',
            completed_at=legacy_completed_at,
            completed_by=login,
        )
        promoted_marker_manual_task = GovernanceTask.objects.create(
            organization=org,
            item_group=promoted_marker_group,
            assignee=duplicate,
            trigger_status='DELETED',
            title='Dismissed deletion before marker promotion',
            state='done',
            completed_at=legacy_completed_at,
            completed_by=login,
        )
        cleared_attention_task = GovernanceTask.objects.create(
            organization=org,
            item_group=cleared_attention_group,
            assignee=duplicate,
            trigger_status='ATTENTION',
            title='Dismissed and later cleared attention',
            state='done',
            completed_at=legacy_completed_at,
            completed_by=login,
        )
        empty_attention_task = GovernanceTask.objects.create(
            organization=org,
            item_group=empty_attention_group,
            assignee=duplicate,
            trigger_status='ATTENTION',
            title='Dismissed attention on an empty retained group',
            state='done',
            completed_at=legacy_completed_at,
            completed_by=login,
        )
        current_deleted_entry = StatusChangeLog.objects.create(
            organization=org,
            item_group=manual_deleted_group,
            group_key=manual_deleted_group.group_key,
            old_status='ATTENTION',
            new_status='DELETED',
            changed_by=login,
        )
        current_deleted_entry_at = (
            legacy_completed_at - timedelta(days=1)
        )
        StatusChangeLog.objects.filter(pk=current_deleted_entry.pk).update(
            changed_at=current_deleted_entry_at,
        )

        # The state-only governance move in 0029 left these legacy columns in
        # the physical table. 0056 must repoint them before deleting a person.
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE catalog_item '
                'SET ownership_person_id = %s, category_id = %s '
                'WHERE item_id = %s',
                [duplicate.id, obsolete.id, second_item.item_id],
            )

        ids = {
            'org': org.id,
            'survivor': survivor.id,
            'duplicate': duplicate.id,
            'namesakes': [namesake_one.id, namesake_two.id],
            'first_group': first_group.id,
            'second_group': second_group.id,
            'second_item': second_item.item_id,
            'older_attention': older_attention.id,
            'newer_attention': newer_attention.id,
            'deleted_task': deleted_task.id,
            'superseded_manual_attention': superseded_manual_attention.id,
            'manual_attention_group': manual_attention_group.id,
            'manual_deleted_group': manual_deleted_group.id,
            'cleared_attention_group': cleared_attention_group.id,
            'empty_attention_group': empty_attention_group.id,
            'promoted_marker_group': promoted_marker_group.id,
            'manual_attention_task': manual_attention_task.id,
            'manual_deleted_task': manual_deleted_task.id,
            'cleared_attention_task': cleared_attention_task.id,
            'empty_attention_task': empty_attention_task.id,
            'promoted_marker_manual_task': promoted_marker_manual_task.id,
            'legacy_completed_at': legacy_completed_at,
            'current_deleted_entry_at': current_deleted_entry_at,
            'obsolete_category': obsolete.id,
        }

        executor = MigrationExecutor(connection)
        executor.migrate(PRODUCTION_MIGRATE_TO)
        apps = executor.loader.project_state(PRODUCTION_MIGRATE_TO).apps
        Organization = apps.get_model('catalog', 'Organization')
        DataPerson = apps.get_model('catalog', 'DataPerson')
        Category = apps.get_model('catalog', 'Category')
        ItemGroup = apps.get_model('catalog', 'ItemGroup')
        GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
        StatusChangeLog = apps.get_model('catalog', 'StatusChangeLog')

        assert DataPerson.objects.filter(pk=ids['survivor']).exists()
        assert not DataPerson.objects.filter(pk=ids['duplicate']).exists()
        survivor = DataPerson.objects.get(pk=ids['survivor'])
        assert survivor.is_owner is True
        assert survivor.is_steward is True
        assert DataPerson.objects.filter(user_id=login.id).count() == 1

        # The corrected identity constraint is per organization, so one login
        # can have a distinct governance profile in another tenant.
        other_org = Organization.objects.create(name='Second tenant')
        other_profile = DataPerson.objects.create(
            name='Alex in Second Tenant',
            organization=other_org,
            user_id=login.id,
        )
        assert other_profile.organization_id == other_org.id
        assert DataPerson.objects.filter(user_id=login.id).count() == 2

        # Same-name login-less people are not identity matches. Both survive
        # with unique display labels for the 0057/0063 constraints.
        namesakes = list(
            DataPerson.objects.filter(pk__in=ids['namesakes'])
            .order_by('id')
            .values_list('name', flat=True)
        )
        assert len(namesakes) == 2
        assert len({name.strip().lower() for name in namesakes}) == 2

        for group_id in (ids['first_group'], ids['second_group']):
            group = ItemGroup.objects.get(pk=group_id)
            assert group.ownership_person_id == ids['survivor']
            assert group.steward_id == ids['survivor']

        older_attention = GovernanceTask.objects.get(pk=ids['older_attention'])
        newer_attention = GovernanceTask.objects.get(pk=ids['newer_attention'])
        deleted_task = GovernanceTask.objects.get(pk=ids['deleted_task'])
        assert older_attention.reason == 'ATTENTION'
        assert older_attention.state == 'done'
        assert older_attention.closed_reason == 'resolved'
        assert newer_attention.reason == 'ATTENTION'
        assert newer_attention.state == 'open'
        assert deleted_task.reason == 'DELETED'
        assert deleted_task.state == 'open'
        for task in (older_attention, newer_attention, deleted_task):
            assert task.assignee_id == ids['survivor']
            assert task.organization_id == ids['org']
            assert task.condition_cleared_at is None

        superseded_manual_attention = GovernanceTask.objects.get(
            pk=ids['superseded_manual_attention'],
        )
        assert superseded_manual_attention.state == 'done'
        assert superseded_manual_attention.closed_reason == 'manual'
        assert (
            superseded_manual_attention.condition_cleared_at
            == ids['legacy_completed_at']
        )

        manual_attention_task = GovernanceTask.objects.get(
            pk=ids['manual_attention_task'],
        )
        manual_deleted_task = GovernanceTask.objects.get(
            pk=ids['manual_deleted_task'],
        )
        cleared_attention_task = GovernanceTask.objects.get(
            pk=ids['cleared_attention_task'],
        )
        empty_attention_task = GovernanceTask.objects.get(
            pk=ids['empty_attention_task'],
        )
        for task in (manual_attention_task, manual_deleted_task):
            assert task.state == 'done'
            assert task.closed_reason == 'manual'
            assert task.completed_at == ids['legacy_completed_at']
            assert task.completed_by_id == login.id
            assert task.condition_cleared_at is None
        current_deleted_log = StatusChangeLog.objects.get(
            item_group_id=ids['manual_deleted_group'],
            new_status='DELETED',
        )
        assert current_deleted_log.changed_at == ids['current_deleted_entry_at']
        assert current_deleted_log.changed_at < manual_deleted_task.completed_at

        promoted_marker_manual_task = GovernanceTask.objects.get(
            pk=ids['promoted_marker_manual_task'],
        )
        assert promoted_marker_manual_task.state == 'done'
        assert promoted_marker_manual_task.closed_reason == 'manual'
        assert (
            promoted_marker_manual_task.condition_cleared_at
            == ids['legacy_completed_at']
        )
        promoted_entry = StatusChangeLog.objects.get(
            item_group_id=ids['promoted_marker_group'],
            old_status='ATTENTION',
            new_status='DELETED',
        )
        assert (
            promoted_entry.changed_at
            > promoted_marker_manual_task.completed_at
        )
        assert cleared_attention_task.state == 'done'
        assert cleared_attention_task.closed_reason == 'manual'
        assert (
            cleared_attention_task.condition_cleared_at
            == ids['legacy_completed_at']
        )
        assert empty_attention_task.state == 'done'
        assert empty_attention_task.closed_reason == 'manual'
        assert (
            empty_attention_task.condition_cleared_at
            == ids['legacy_completed_at']
        )
        empty_attention_group = ItemGroup.objects.get(
            pk=ids['empty_attention_group'],
        )
        assert empty_attention_group.ownership_person_id == ids['survivor']
        assert empty_attention_group.steward_id == ids['survivor']
        assert not empty_attention_group.items.exists()

        # The first all-kind status sweep honors current dismissals but opens
        # fresh work for the marker promotion that happened after its old Done.
        from catalog.governance_tasks import generate_tasks
        from catalog.models import (
            GovernanceTask as RuntimeGovernanceTask,
            Organization as RuntimeOrganization,
        )

        sweep = generate_tasks(
            RuntimeOrganization.objects.get(pk=ids['org']),
            reasons=['ATTENTION', 'DELETED'],
            kind_scope='all',
        )
        assert sweep['totals']['created'] == 1
        assert not RuntimeGovernanceTask.objects.filter(
            item_group_id__in=[
                ids['manual_attention_group'],
                ids['manual_deleted_group'],
            ],
            state='open',
        ).exists()
        assert RuntimeGovernanceTask.objects.filter(
            item_group_id=ids['promoted_marker_group'],
            reason='DELETED',
            state='open',
        ).exists()

        assert not Category.objects.filter(pk=ids['obsolete_category']).exists()
        assert ItemGroup.objects.get(pk=ids['second_group']).category_id is None
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT ownership_person_id, category_id '
                'FROM catalog_item WHERE item_id = %s',
                [ids['second_item']],
            )
            legacy_owner_id, legacy_category_id = cursor.fetchone()
        assert legacy_owner_id == ids['survivor']
        assert legacy_category_id is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_0064_clears_hygiene_dismissals_when_only_deleted_items_remain():
    migrate_from = [('catalog', '0063_name_integrity_constraints')]
    migrate_to = [('catalog', '0064_governancetask_condition_cleared_at')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps

    try:
        Organization = old_apps.get_model('catalog', 'Organization')
        Item = old_apps.get_model('catalog', 'Item')
        ItemGroup = old_apps.get_model('catalog', 'ItemGroup')
        GovernanceTask = old_apps.get_model('catalog', 'GovernanceTask')

        org = Organization.objects.create(name='Episode migration org')
        completed_at = timezone.now() - timedelta(days=5)
        task_ids = []
        for reason, status in (
            ('UNVERIFIED', 'UNVERIFIED'),
            ('NO_CATEGORY', 'VERIFIED'),
        ):
            group = ItemGroup.objects.create(
                group_key=f'episode::{reason.lower()}',
                kind='measure_name',
                organization=org,
                status=status,
                deleted=False,
            )
            Item.objects.create(
                item_id=f'episode-{reason.lower()}-item',
                item_name=reason,
                item_type='PB_MEASURE',
                group_id=group.group_key,
                item_group=group,
                organization=org,
                status=status,
                deleted=True,
                deleted_at=completed_at,
            )
            task = GovernanceTask.objects.create(
                organization=org,
                item_group=group,
                reason=reason,
                trigger_status=(
                    'UNVERIFIED' if reason == 'UNVERIFIED' else None
                ),
                title=f'Dismissed {reason}',
                state='done',
                closed_reason='manual',
                completed_at=completed_at,
            )
            task_ids.append(task.pk)

        active_singleton = ItemGroup.objects.create(
            group_key='episode::singleton-no-category',
            kind='singleton',
            organization=org,
            status='VERIFIED',
            deleted=False,
        )
        Item.objects.create(
            item_id='episode-singleton-no-category-item',
            item_name='Uncategorized report',
            item_type='PB_REPORT',
            item_group=active_singleton,
            organization=org,
            status='VERIFIED',
            deleted=False,
        )
        active_singleton_task = GovernanceTask.objects.create(
            organization=org,
            item_group=active_singleton,
            reason='NO_CATEGORY',
            title='Dismissed singleton category hygiene',
            state='done',
            closed_reason='manual',
            completed_at=completed_at,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        apps = executor.loader.project_state(migrate_to).apps
        GovernanceTask = apps.get_model('catalog', 'GovernanceTask')

        assert set(
            GovernanceTask.objects.filter(pk__in=task_ids)
            .values_list('condition_cleared_at', flat=True)
        ) == {completed_at}
        assert GovernanceTask.objects.get(
            pk=active_singleton_task.pk,
        ).condition_cleared_at is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_integrity_cleanup_and_constraints_migrate_real_legacy_state(
        monkeypatch):
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    old_apps = executor.loader.project_state(MIGRATE_FROM).apps

    try:
        Organization = old_apps.get_model('catalog', 'Organization')
        CustomUser = old_apps.get_model('catalog', 'CustomUser')
        Department = old_apps.get_model('catalog', 'Department')
        DataPerson = old_apps.get_model('catalog', 'DataPerson')
        Category = old_apps.get_model('catalog', 'Category')
        Definition = old_apps.get_model('catalog', 'Definition')
        Item = old_apps.get_model('catalog', 'Item')
        ItemGroup = old_apps.get_model('catalog', 'ItemGroup')
        GovernanceTask = old_apps.get_model('catalog', 'GovernanceTask')

        org_a = Organization.objects.create(name='Org A')
        org_b = Organization.objects.create(name='Org B')
        login = CustomUser.objects.create(
            username='legacy-multi-org',
            email='legacy-multi-org@example.com',
        )
        department_a = Department.objects.create(name='Finance A', organization=org_a)
        department_b = Department.objects.create(name='Finance B', organization=org_b)
        person_a = DataPerson.objects.create(
            name='Alice', organization=org_a, user=login,
        )
        person_b = DataPerson.objects.create(name='Bob', organization=org_b)
        person_a.departments.add(department_a, department_b)
        person_b.departments.add(department_b)

        # Simulate a database that had already recorded the old 0057 migration:
        # it physically enforced a global user-only identity even though the
        # corrected migration state now describes an org-scoped identity.
        with connection.cursor() as cursor:
            cursor.execute(
                'DROP INDEX IF EXISTS "uniq_dataperson_user_org"'
            )
            cursor.execute(
                'DROP INDEX IF EXISTS "uniq_dataperson_user"'
            )
            cursor.execute(
                'CREATE UNIQUE INDEX "uniq_dataperson_user" '
                'ON "catalog_dataperson" ("user_id") '
                'WHERE "user_id" IS NOT NULL'
            )
        category_a = Category.objects.create(name='Finance A', organization=org_a)
        category_b = Category.objects.create(name='Finance B', organization=org_b)

        # 0061 permits trim-equivalent names when organization is NULL, and
        # permits whitespace-only names. 0062 must preserve every row while
        # making the names safe for 0063.
        null_person_one = DataPerson.objects.create(name=' Jane ')
        null_person_two = DataPerson.objects.create(name='jane')
        null_person_one.departments.add(department_a)
        blank_person = DataPerson.objects.create(name='   ', organization=org_a)
        padded_person = DataPerson.objects.create(
            name=' Legacy Person ', organization=org_a,
        )
        plain_person = DataPerson.objects.create(
            name='Legacy Person', organization=org_a,
        )
        null_definition_one = Definition.objects.create(name=' Shared ')
        null_definition_two = Definition.objects.create(name='shared')
        blank_definition = Definition.objects.create(
            name='\t', organization=org_a,
        )
        padded_definition = Definition.objects.create(
            name=' Legacy Definition ', organization=org_a,
        )
        plain_definition = Definition.objects.create(
            name='Legacy Definition', organization=org_a,
        )

        good_definition = Definition.objects.create(
            name='Good', organization=org_a,
            ownership_person=person_a, ownership_department=department_a,
        )
        foreign_definition = Definition.objects.create(
            name='Foreign', organization=org_b,
        )
        cross_metadata_definition = Definition.objects.create(
            name='Cross metadata', organization=org_a,
            ownership_person=person_b, ownership_department=department_b,
        )
        inferred_definition = Definition.objects.create(
            name='Inferred', ownership_person=person_b,
        )
        ambiguous_definition = Definition.objects.create(name='Ambiguous')
        collision_definition = Definition.objects.create(
            name='Collision', organization=org_a,
        )
        inferred_collision_definition = Definition.objects.create(
            name=' collision ',
        )

        def group(key, organization=None, **kwargs):
            return ItemGroup.objects.create(
                group_key=key, kind='measure_name',
                organization=organization, **kwargs,
            )

        def attach(group_obj, item_id, organization):
            item = Item.objects.create(
                item_id=item_id, item_name=item_id, item_type='PB_MEASURE',
                group_id=group_obj.group_key, item_group=group_obj,
                organization=organization,
            )
            ItemGroup.objects.filter(pk=group_obj.pk).update(primary_item=item)
            return item

        good_group = group(
            'good', org_a, definition=good_definition,
            ownership_person=person_a, steward=person_a,
            ownership_department=department_a, category=category_a,
        )
        attach(good_group, 'good-item', org_a)
        non_measure_definition_group = ItemGroup.objects.create(
            group_key='item::legacy-report',
            kind='singleton',
            organization=org_a,
            definition=good_definition,
        )
        non_measure_item = Item.objects.create(
            item_id='legacy-report',
            item_name='Legacy Report',
            item_type='PB_REPORT',
            item_group=non_measure_definition_group,
            organization=org_a,
        )
        ItemGroup.objects.filter(
            pk=non_measure_definition_group.pk,
        ).update(primary_item=non_measure_item)
        foreign_item = Item.objects.create(
            item_id='foreign-item', item_name='foreign-item',
            item_type='PB_MEASURE', group_id=good_group.group_key,
            item_group=good_group, organization=org_b,
        )
        unscoped_item = Item.objects.create(
            item_id='unscoped-item', item_name='unscoped-item',
            item_type='PB_MEASURE', group_id=good_group.group_key,
            item_group=good_group,
        )
        ItemGroup.objects.filter(pk=good_group.pk).update(
            primary_item=foreign_item,
        )

        cross_group = group(
            'cross', org_a, definition=foreign_definition,
            ownership_person=person_b, steward=person_b,
            ownership_department=department_b, category=category_b,
        )
        attach(cross_group, 'cross-item', org_a)

        inferred_group = group(
            'inferred', definition=inferred_definition,
        )
        attach(inferred_group, 'inferred-item', org_a)
        inferred_collision_group = group(
            'inferred-collision', org_a,
            definition=inferred_collision_definition,
        )
        attach(inferred_collision_group, 'inferred-collision-item', org_a)

        ambiguous_group = group(
            'ambiguous', definition=ambiguous_definition,
        )
        ambiguous_a_item = attach(ambiguous_group, 'ambiguous-a', org_a)
        ambiguous_b_item = attach(ambiguous_group, 'ambiguous-b', org_b)
        ambiguous_b_group = group(
            'ambiguous-b', org_b, definition=ambiguous_definition,
        )
        attach(ambiguous_b_group, 'ambiguous-b-only', org_b)

        nonmember_group = group('nonmember-primary', org_a)
        nonmember_item = attach(nonmember_group, 'nonmember-member', org_a)
        other_group = group('other-primary', org_a)
        other_item = attach(other_group, 'other-primary-item', org_a)
        ItemGroup.objects.filter(pk=nonmember_group.pk).update(
            primary_item=other_item,
        )
        synthetic_duplicate_group = group(
            'old-0059-synthetic-duplicate',
            org_a,
            status='ATTENTION',
        )
        attach(
            synthetic_duplicate_group,
            'old-0059-synthetic-duplicate-item',
            org_a,
        )
        synthetic_duplicate_task = GovernanceTask.objects.create(
            organization=org_a,
            item_group=synthetic_duplicate_group,
            assignee=person_a,
            assignee_role='steward',
            reason='ATTENTION',
            title='Old 0059 synthetic duplicate closure',
            state='done',
            completed_at=None,
            closed_reason=None,
        )

        staging_completed_at = timezone.now() - timedelta(days=10)
        staging_legacy_done_task = GovernanceTask.objects.create(
            organization=org_a,
            item_group=nonmember_group,
            assignee=person_a,
            assignee_role='owner',
            reason='ATTENTION',
            title='Staging-era unlabeled Done',
            state='done',
            completed_at=staging_completed_at,
            closed_reason=None,
        )
        wrong_org_task = GovernanceTask.objects.create(
            organization=org_b, item_group=good_group,
            assignee=person_b, assignee_role='steward',
            reason='ATTENTION', title='Wrong tenant task',
        )
        null_org_task = GovernanceTask.objects.create(
            item_group=good_group, assignee=person_a, assignee_role='owner',
            reason='DELETED', title='Null tenant task',
        )
        orphan_task = GovernanceTask.objects.create(
            organization=org_a, reason='NO_CATEGORY', title='Orphan task',
        )

        empty_group = group('empty', org_a, definition=good_definition)
        empty_task = GovernanceTask.objects.create(
            organization=org_a, item_group=empty_group,
            reason='UNVERIFIED', title='Ghost task',
        )

        source_obsolete_at = timezone.now() - timedelta(days=40)
        source_obsolete_group = group(
            'source-obsolete', org_a, status='VERIFIED', deleted=False,
        )
        source_obsolete_item = attach(
            source_obsolete_group, 'source-obsolete-item', org_a,
        )
        Item.objects.filter(pk=source_obsolete_item.pk).update(
            status='UNVERIFIED',
            deleted=True,
            deleted_at=source_obsolete_at,
        )

        preserved_deleted_at = timezone.now() - timedelta(days=30)
        corrupt_deleted_group = group(
            'corrupt-deleted', org_a, status='ATTENTION', deleted=True,
            deleted_at=preserved_deleted_at,
        )
        corrupt_deleted_item = attach(
            corrupt_deleted_group, 'corrupt-deleted-item', org_a,
        )
        Item.objects.filter(pk=corrupt_deleted_item.pk).update(
            status='VERIFIED', deleted=False, deleted_at=None,
        )
        corrupt_deleted_source_at = timezone.now() - timedelta(days=35)
        corrupt_deleted_source_item = attach(
            corrupt_deleted_group, 'corrupt-deleted-source-item', org_a,
        )
        Item.objects.filter(pk=corrupt_deleted_source_item.pk).update(
            status='VERIFIED',
            deleted=True,
            deleted_at=corrupt_deleted_source_at,
        )

        promoted_deleted_at = timezone.now() - timedelta(days=20)
        corrupt_status_group = group(
            'corrupt-status-deleted', org_a, status='DELETED',
            deleted=False, deleted_at=None,
        )
        corrupt_status_item = attach(
            corrupt_status_group, 'corrupt-status-item', org_a,
        )
        Item.objects.filter(pk=corrupt_status_item.pk).update(
            status='UNVERIFIED', deleted=True,
            deleted_at=promoted_deleted_at,
        )

        legacy_category_a = Category.objects.create(
            name='  to be deleted  ', organization=org_a,
        )
        legacy_category_b = Category.objects.create(
            name='\tTO BE DELETED\t', organization=org_b,
        )
        retained_category = Category.objects.create(
            name='To Be Deleted Later', organization=org_a,
        )
        legacy_group = group('legacy-category', org_a, category=legacy_category_a)
        legacy_item = attach(legacy_group, 'legacy-item', org_a)
        legacy_attention_group = group(
            'legacy-category-attention',
            org_a,
            category=legacy_category_a,
            status='ATTENTION',
        )
        legacy_attention_item = attach(
            legacy_attention_group,
            'legacy-attention-item',
            org_a,
        )
        legacy_deleted_group = group(
            'legacy-category-already-deleted',
            org_a,
            category=legacy_category_a,
            status='DELETED',
        )
        legacy_deleted_item = attach(
            legacy_deleted_group,
            'legacy-already-deleted-item',
            org_a,
        )
        cross_tenant_legacy_group = group(
            'cross-tenant-legacy-category',
            org_b,
            category=legacy_category_a,
            status='UNVERIFIED',
        )
        cross_tenant_legacy_item = attach(
            cross_tenant_legacy_group,
            'cross-tenant-legacy-item',
            org_b,
        )
        legacy_no_category_task = GovernanceTask.objects.create(
            organization=org_a,
            item_group=legacy_group,
            reason='NO_CATEGORY',
            title='Stale category work',
        )
        legacy_attention_task = GovernanceTask.objects.create(
            organization=org_a,
            item_group=legacy_attention_group,
            reason='ATTENTION',
            title='Stale attention work',
        )
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE catalog_item SET category_id = %s WHERE item_id = %s',
                [legacy_category_b.id, legacy_item.item_id],
            )

        ids = {
            'good_group': good_group.id,
            'non_measure_definition_group': non_measure_definition_group.id,
            'foreign_item': foreign_item.item_id,
            'unscoped_item': unscoped_item.item_id,
            'cross_group': cross_group.id,
            'inferred_group': inferred_group.id,
            'ambiguous_group': ambiguous_group.id,
            'ambiguous_items': [
                ambiguous_a_item.item_id, ambiguous_b_item.item_id,
            ],
            'ambiguous_b_group': ambiguous_b_group.id,
            'nonmember_group': nonmember_group.id,
            'nonmember_item': nonmember_item.item_id,
            'other_group': other_group.id,
            'other_item': other_item.item_id,
            'synthetic_duplicate_group': synthetic_duplicate_group.id,
            'synthetic_duplicate_task': synthetic_duplicate_task.id,
            'staging_legacy_done_task': staging_legacy_done_task.id,
            'staging_completed_at': staging_completed_at,
            'good_definition': good_definition.id,
            'cross_metadata_definition': cross_metadata_definition.id,
            'inferred_definition': inferred_definition.id,
            'ambiguous_definition': ambiguous_definition.id,
            'collision_definition': collision_definition.id,
            'inferred_collision_definition': inferred_collision_definition.id,
            'wrong_org_task': wrong_org_task.id,
            'null_org_task': null_org_task.id,
            'orphan_task': orphan_task.id,
            'empty_group': empty_group.id,
            'empty_task': empty_task.id,
            'source_obsolete_group': source_obsolete_group.id,
            'source_obsolete_item': source_obsolete_item.item_id,
            'source_obsolete_at': source_obsolete_at,
            'corrupt_deleted_group': corrupt_deleted_group.id,
            'corrupt_deleted_item': corrupt_deleted_item.item_id,
            'corrupt_deleted_source_item': (
                corrupt_deleted_source_item.item_id
            ),
            'corrupt_deleted_source_at': corrupt_deleted_source_at,
            'corrupt_status_group': corrupt_status_group.id,
            'corrupt_status_item': corrupt_status_item.item_id,
            'preserved_deleted_at': preserved_deleted_at,
            'promoted_deleted_at': promoted_deleted_at,
            'legacy_group': legacy_group.id,
            'legacy_item': legacy_item.item_id,
            'legacy_attention_group': legacy_attention_group.id,
            'legacy_attention_item': legacy_attention_item.item_id,
            'legacy_deleted_group': legacy_deleted_group.id,
            'legacy_deleted_item': legacy_deleted_item.item_id,
            'cross_tenant_legacy_group': cross_tenant_legacy_group.id,
            'cross_tenant_legacy_item': cross_tenant_legacy_item.item_id,
            'legacy_no_category_task': legacy_no_category_task.id,
            'legacy_attention_task': legacy_attention_task.id,
            'legacy_categories': [
                legacy_category_a.id, legacy_category_b.id,
            ],
            'retained_category': retained_category.id,
            'null_people': [null_person_one.id, null_person_two.id, blank_person.id],
            'trim_collision_people': [padded_person.id, plain_person.id],
            'null_definitions': [
                null_definition_one.id, null_definition_two.id, blank_definition.id,
            ],
            'trim_collision_definitions': [
                padded_definition.id, plain_definition.id,
            ],
        }

        # Force every migration helper chunk to contain one row. The legacy
        # category fixtures below span several groups/tasks, so this proves the
        # promotion and stale-task updates do not regress to one unbounded
        # ``__in`` query when production has many affected groups.
        integrity_migration = importlib.import_module(
            'catalog.migrations.0062_integrity_cleanup',
        )

        def one_row_chunks(values, size=2000):
            del size
            for value in values:
                yield [value]

        monkeypatch.setattr(
            integrity_migration, '_chunks', one_row_chunks,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        apps = executor.loader.project_state(MIGRATE_TO).apps
        DataPerson = apps.get_model('catalog', 'DataPerson')
        Definition = apps.get_model('catalog', 'Definition')
        Category = apps.get_model('catalog', 'Category')
        Item = apps.get_model('catalog', 'Item')
        ItemGroup = apps.get_model('catalog', 'ItemGroup')
        GovernanceTask = apps.get_model('catalog', 'GovernanceTask')
        StatusChangeLog = apps.get_model('catalog', 'StatusChangeLog')

        # 0063 replaces the legacy global index even on an already-applied
        # database, permitting the same login's separate profile in Org B.
        second_profile = DataPerson.objects.create(
            name='Alice in Org B',
            organization_id=org_b.id,
            user_id=login.id,
        )
        assert second_profile.organization_id == org_b.id
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND tablename = 'catalog_dataperson' "
                "AND indexname IN "
                "('uniq_dataperson_user', 'uniq_dataperson_user_org')"
            )
            assert {row[0] for row in cursor.fetchall()} == {
                'uniq_dataperson_user_org',
            }

        good_group = ItemGroup.objects.get(pk=ids['good_group'])
        assert good_group.organization_id == org_a.id
        assert good_group.definition_id == ids['good_definition']
        assert good_group.ownership_person_id == person_a.id
        assert good_group.steward_id == person_a.id
        assert good_group.ownership_department_id == department_a.id
        assert good_group.category_id == category_a.id
        assert good_group.primary_item_id is None
        assert (
            ItemGroup.objects.get(
                pk=ids['non_measure_definition_group'],
            ).definition_id
            is None
        )
        assert Item.objects.get(pk=ids['foreign_item']).item_group_id is None
        assert Item.objects.get(pk=ids['unscoped_item']).item_group_id is None
        assert not ItemGroup.objects.filter(
            pk=ids['good_group'],
            items__pk=ids['foreign_item'],
            ownership_person_id=person_a.id,
        ).exists()

        cross_group = ItemGroup.objects.get(pk=ids['cross_group'])
        assert cross_group.definition_id is None
        assert cross_group.ownership_person_id is None
        assert cross_group.steward_id is None
        assert cross_group.ownership_department_id is None
        assert cross_group.category_id is None

        assert (
            ItemGroup.objects.get(pk=ids['inferred_group']).organization_id
            == org_a.id
        )
        assert (
            Definition.objects.get(pk=ids['inferred_definition']).organization_id
            == org_a.id
        )
        inferred_definition = Definition.objects.get(pk=ids['inferred_definition'])
        assert inferred_definition.ownership_person_id is None
        inferred_collision_definition = Definition.objects.get(
            pk=ids['inferred_collision_definition'],
        )
        assert inferred_collision_definition.organization_id == org_a.id
        assert (
            inferred_collision_definition.name.strip().lower()
            != Definition.objects.get(
                pk=ids['collision_definition'],
            ).name.strip().lower()
        )
        ambiguous_group = ItemGroup.objects.get(pk=ids['ambiguous_group'])
        assert ambiguous_group.organization_id is None
        assert ambiguous_group.primary_item_id is None
        assert ambiguous_group.definition_id == ids['ambiguous_definition']
        assert not Item.objects.filter(
            pk__in=ids['ambiguous_items'], item_group__isnull=False,
        ).exists()
        assert (
            Definition.objects.get(pk=ids['ambiguous_definition']).organization_id
            is None
        )
        assert (
            ItemGroup.objects.get(pk=ids['ambiguous_b_group']).definition_id
            is None
        )

        nonmember_group = ItemGroup.objects.get(pk=ids['nonmember_group'])
        assert nonmember_group.primary_item_id is None
        assert (
            Item.objects.get(pk=ids['nonmember_item']).item_group_id
            == nonmember_group.id
        )
        assert (
            ItemGroup.objects.get(pk=ids['other_group']).primary_item_id
            == ids['other_item']
        )
        synthetic_duplicate_task = GovernanceTask.objects.get(
            pk=ids['synthetic_duplicate_task'],
        )
        assert synthetic_duplicate_task.state == 'done'
        assert synthetic_duplicate_task.completed_at is None
        assert synthetic_duplicate_task.closed_reason == 'resolved'
        assert synthetic_duplicate_task.condition_cleared_at is None
        staging_legacy_done_task = GovernanceTask.objects.get(
            pk=ids['staging_legacy_done_task'],
        )
        assert staging_legacy_done_task.closed_reason == 'manual'
        assert (
            staging_legacy_done_task.condition_cleared_at
            == ids['staging_completed_at']
        )

        cross_definition = Definition.objects.get(
            pk=ids['cross_metadata_definition'],
        )
        assert cross_definition.ownership_person_id is None
        assert cross_definition.ownership_department_id is None

        wrong_org_task = GovernanceTask.objects.get(pk=ids['wrong_org_task'])
        assert wrong_org_task.organization_id == org_a.id
        assert wrong_org_task.assignee_id is None
        assert wrong_org_task.assignee_role is None
        null_org_task = GovernanceTask.objects.get(pk=ids['null_org_task'])
        assert null_org_task.organization_id == org_a.id
        assert null_org_task.assignee_id == person_a.id
        assert null_org_task.assignee_role == 'owner'
        orphan_task = GovernanceTask.objects.get(pk=ids['orphan_task'])
        assert orphan_task.state == 'done'
        assert orphan_task.closed_reason == 'resolved'
        assert orphan_task.completed_at is not None
        empty_group = ItemGroup.objects.get(pk=ids['empty_group'])
        assert empty_group.definition_id == ids['good_definition']
        assert not empty_group.items.exists()
        empty_task = GovernanceTask.objects.get(pk=ids['empty_task'])
        assert empty_task.item_group_id == empty_group.id
        assert empty_task.state == 'done'
        assert empty_task.closed_reason == 'resolved'
        assert empty_task.completed_at is not None

        source_obsolete_group = ItemGroup.objects.get(
            pk=ids['source_obsolete_group'],
        )
        source_obsolete_item = Item.objects.get(
            pk=ids['source_obsolete_item'],
        )
        assert source_obsolete_group.status == 'VERIFIED'
        assert source_obsolete_group.deleted is False
        assert source_obsolete_item.status == 'VERIFIED'
        assert source_obsolete_item.deleted is True
        assert source_obsolete_item.deleted_at == ids['source_obsolete_at']

        corrupt_deleted_group = ItemGroup.objects.get(
            pk=ids['corrupt_deleted_group'],
        )
        corrupt_deleted_item = Item.objects.get(
            pk=ids['corrupt_deleted_item'],
        )
        corrupt_deleted_source_item = Item.objects.get(
            pk=ids['corrupt_deleted_source_item'],
        )
        assert corrupt_deleted_group.status == 'DELETED'
        assert corrupt_deleted_group.deleted is True
        assert corrupt_deleted_group.deleted_at == ids['preserved_deleted_at']
        assert corrupt_deleted_item.status == 'DELETED'
        assert corrupt_deleted_item.deleted is True
        assert corrupt_deleted_item.deleted_at == ids['preserved_deleted_at']
        assert corrupt_deleted_source_item.status == 'DELETED'
        assert corrupt_deleted_source_item.deleted is True
        assert (
            corrupt_deleted_source_item.deleted_at
            == ids['corrupt_deleted_source_at']
        )

        corrupt_status_group = ItemGroup.objects.get(
            pk=ids['corrupt_status_group'],
        )
        corrupt_status_item = Item.objects.get(
            pk=ids['corrupt_status_item'],
        )
        assert corrupt_status_group.status == 'DELETED'
        assert corrupt_status_group.deleted is False
        assert corrupt_status_group.deleted_at is not None
        assert corrupt_status_group.deleted_at != ids['promoted_deleted_at']
        assert corrupt_status_item.status == 'DELETED'
        assert corrupt_status_item.deleted is True
        assert corrupt_status_item.deleted_at == ids['promoted_deleted_at']

        assert not Category.objects.filter(
            pk__in=ids['legacy_categories'],
        ).exists()
        assert Category.objects.filter(pk=ids['retained_category']).exists()
        for group_key, item_key, old_status in (
            ('legacy_group', 'legacy_item', 'UNVERIFIED'),
            (
                'legacy_attention_group',
                'legacy_attention_item',
                'ATTENTION',
            ),
        ):
            converted_group = ItemGroup.objects.get(pk=ids[group_key])
            converted_item = Item.objects.get(pk=ids[item_key])
            assert converted_group.category_id is None
            assert converted_group.status == 'DELETED'
            assert converted_group.deleted is False
            assert converted_group.deleted_at is not None
            assert converted_item.status == 'DELETED'
            assert converted_item.deleted is False
            assert converted_item.deleted_at is None
            transition = StatusChangeLog.objects.get(
                item_group_id=converted_group.id,
                new_status='DELETED',
            )
            assert transition.organization_id == org_a.id
            assert transition.group_key == converted_group.group_key
            assert transition.old_status == old_status
            assert transition.changed_by_id is None

        legacy_deleted_group = ItemGroup.objects.get(
            pk=ids['legacy_deleted_group'],
        )
        legacy_deleted_item = Item.objects.get(
            pk=ids['legacy_deleted_item'],
        )
        assert legacy_deleted_group.category_id is None
        assert legacy_deleted_group.status == 'DELETED'
        assert legacy_deleted_group.deleted is False
        assert legacy_deleted_group.deleted_at is not None
        assert legacy_deleted_item.status == 'DELETED'
        assert not StatusChangeLog.objects.filter(
            item_group_id=legacy_deleted_group.id,
            new_status='DELETED',
        ).exists()

        # Tenant quarantine runs before marker promotion. A corrupt foreign
        # category link is detached, never interpreted as deletion intent.
        cross_tenant_legacy_group = ItemGroup.objects.get(
            pk=ids['cross_tenant_legacy_group'],
        )
        cross_tenant_legacy_item = Item.objects.get(
            pk=ids['cross_tenant_legacy_item'],
        )
        assert cross_tenant_legacy_group.category_id is None
        assert cross_tenant_legacy_group.status == 'UNVERIFIED'
        assert cross_tenant_legacy_group.deleted_at is None
        assert cross_tenant_legacy_item.status == 'UNVERIFIED'
        assert not StatusChangeLog.objects.filter(
            item_group_id=cross_tenant_legacy_group.id,
            new_status='DELETED',
        ).exists()

        for task_key in (
            'legacy_no_category_task',
            'legacy_attention_task',
        ):
            stale_task = GovernanceTask.objects.get(pk=ids[task_key])
            assert stale_task.state == 'done'
            assert stale_task.closed_reason == 'resolved'
            assert stale_task.completed_at is not None
        assert not GovernanceTask.objects.filter(
            item_group_id__in=[
                ids['legacy_group'],
                ids['legacy_attention_group'],
                ids['legacy_deleted_group'],
            ],
            reason='NO_CATEGORY',
            state='open',
        ).exists()
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT category_id FROM catalog_item WHERE item_id = %s',
                [ids['legacy_item']],
            )
            assert cursor.fetchone()[0] is None

        people_names = list(
            DataPerson.objects.filter(pk__in=ids['null_people'])
            .values_list('name', flat=True)
        )
        definition_names = list(
            Definition.objects.filter(pk__in=ids['null_definitions'])
            .values_list('name', flat=True)
        )
        assert len({name.strip().lower() for name in people_names}) == 3
        assert len({name.strip().lower() for name in definition_names}) == 3
        assert all(name.strip() for name in people_names + definition_names)
        assert set(
            DataPerson.objects.get(pk=person_a.id)
            .departments.values_list('id', flat=True)
        ) == {department_a.id}
        assert not (
            DataPerson.objects.get(pk=null_person_one.id)
            .departments.exists()
        )
        for Model, row_ids in (
                (DataPerson, ids['trim_collision_people']),
                (Definition, ids['trim_collision_definitions'])):
            names = list(
                Model.objects.filter(pk__in=row_ids)
                .order_by('id')
                .values_list('name', flat=True)
            )
            assert names[0] in ('Legacy Person', 'Legacy Definition')
            assert len({name.strip().lower() for name in names}) == 2

        with pytest.raises(IntegrityError), transaction.atomic():
            DataPerson.objects.create(name='jane')
        with pytest.raises(IntegrityError), transaction.atomic():
            DataPerson.objects.create(name='  ')
        with pytest.raises(IntegrityError), transaction.atomic():
            Definition.objects.create(name='SHARED')
        with pytest.raises(IntegrityError), transaction.atomic():
            Definition.objects.create(name='\t')
        with pytest.raises(IntegrityError), transaction.atomic():
            Category.objects.create(
                name='  TO BE DELETED  ', organization_id=org_a.id,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            ItemGroup.objects.filter(
                pk=ids['non_measure_definition_group'],
            ).update(definition_id=ids['good_definition'])

        # Deployment retries are harmless: rerunning the data operation after
        # the executor reached 0063 changes no identities and raises no error.
        migration = importlib.import_module(
            'catalog.migrations.0062_integrity_cleanup',
        )
        migration.cleanup_integrity(apps, connection.schema_editor())
        assert not Category.objects.filter(
            pk__in=ids['legacy_categories'],
        ).exists()
        assert DataPerson.objects.filter(pk__in=ids['null_people']).count() == 3
        assert Definition.objects.filter(pk__in=ids['null_definitions']).count() == 3
        assert StatusChangeLog.objects.filter(
            item_group_id__in=[
                ids['legacy_group'],
                ids['legacy_attention_group'],
            ],
            new_status='DELETED',
        ).count() == 2
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
