import json
from datetime import date
import pytest
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client
from django.contrib.auth.models import Group
from catalog.models import (
    Category, CustomUser, DataPerson, Department, IntegrationSource, Item,
    ItemGroup, Organization, OrganizationMembership, PowerBIReportUsage,
    SourceRunLog, Summary,
)


class ViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name="Test Org")
        self.user = CustomUser.objects.create_user(
            username="jdoe",
            email="jdoe@example.com",
            password="testpass"
        )
        self.membership = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
        )
        
        # Give access groups so they can view pages (Company unlocks Data Dictionary).
        # get_or_create: the access-groups data migration may have created it already.
        self.company_group, _ = Group.objects.get_or_create(name="Company")
        self.user.groups.add(self.company_group)

        self.summary = Summary.objects.create(
            total_measures=10, unused_measures=2,
            total_columns=20, unused_columns=5,
            total_reports=3,
            organization=self.org,
        )
        self.item = Item.objects.create(
            item_id="123",
            item_name="Sales",
            item_type="SemanticModel",
            organization=self.org,
        )

    # NOTE: the classic server-rendered pages (dashboard, dictionary, …) were
    # removed — the React app is the only frontend now. Their page-render tests
    # were dropped; the API tests below are what back those screens.

    def test_api_summary(self):
        self.client.login(username="jdoe@example.com", password="testpass")
        response = self.client.get('/api/summary/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total_measures'], 10)

    def test_api_items(self):
        self.client.login(username="jdoe@example.com", password="testpass")
        response = self.client.get('/api/items/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 1)
        self.assertEqual(response.json()['results'][0]['item_name'], "Sales")


# =============================================
# New API tests for plan.md features
# =============================================

@pytest.mark.django_db
class TestItemAPINewFields:
    """Tests that the API returns the new fields from plan.md."""

    def test_api_returns_connected_reports_json(self, client, rw_user, item_with_reports):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get(f'/api/items/{item_with_reports.item_id}/')
        assert resp.status_code == 200
        data = resp.json()
        assert 'connected_reports_json' in data
        assert len(data['connected_reports_json']) == 2
        assert data['connected_reports_json'][0]['name'] == 'Sales Report'

    def test_api_returns_organization_name(self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get(f'/api/items/{item_with_org.item_id}/')
        assert resp.status_code == 200
        data = resp.json()
        assert data['organization_name'] == 'Test Org'

    def test_api_returns_item_id_field(self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get(f'/api/items/{item_with_org.item_id}/')
        assert resp.status_code == 200
        assert resp.json()['item_id'] == item_with_org.item_id


@pytest.mark.django_db
class TestItemTenantIntegrity:

    def test_corrupt_cross_tenant_group_link_is_hidden_and_cannot_be_mutated(
            self, client, rw_user, item_with_org):
        other = Organization.objects.create(name='Neighbour')
        foreign_group = ItemGroup.objects.create(
            group_key='foreign::group',
            kind=ItemGroup.KIND_MEASURE_NAME,
            organization=other,
        )
        Item.objects.filter(pk=item_with_org.pk).update(item_group=foreign_group)
        client.login(username='writer@example.com', password='testpass')

        assert client.get(
            f'/api/items/{item_with_org.pk}/',
        ).status_code == 404
        listed_ids = {
            row['item_id'] for row in client.get('/api/items/').json()['results']
        }
        assert item_with_org.pk not in listed_ids
        assert client.patch(
            f'/api/items/{item_with_org.pk}/',
            data=json.dumps({'status': 'ATTENTION'}),
            content_type='application/json',
        ).status_code == 404
        assert client.post(
            f'/api/items/{item_with_org.pk}/set_primary/',
        ).status_code == 404

        foreign_group.refresh_from_db()
        assert foreign_group.status == 'UNVERIFIED'
        assert foreign_group.primary_item_id is None


@pytest.mark.django_db
class TestCatalogMutationSurfaceAndRbac:

    @staticmethod
    def _member(org, email, *, tier=None, admin=False):
        user = CustomUser.objects.create_user(
            username=email, email=email, password='testpass',
        )
        OrganizationMembership.objects.create(
            user=user, organization=org, is_admin=admin,
        )
        if tier:
            user.groups.add(Group.objects.get_or_create(name=tier)[0])
        return user

    def test_items_have_no_generic_create_put_or_destroy_and_patch_only_status(
            self, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')

        assert client.post(
            '/api/items/',
            data=json.dumps({'item_id': 'injected'}),
            content_type='application/json',
        ).status_code == 405
        assert client.put(
            f'/api/items/{item_with_org.pk}/',
            data=json.dumps({'status': 'VERIFIED'}),
            content_type='application/json',
        ).status_code == 405
        assert client.delete(
            f'/api/items/{item_with_org.pk}/',
        ).status_code == 405

        rejected = client.patch(
            f'/api/items/{item_with_org.pk}/',
            data=json.dumps({'item_name': 'Tampered'}),
            content_type='application/json',
        )
        assert rejected.status_code == 400
        item_with_org.refresh_from_db()
        assert item_with_org.item_name == 'Revenue'

    @patch('catalog.views.async_task')
    def test_run_source_rejects_inactive_and_ambiguous_load_scope_before_queue(
            self, async_task_mock, client, org):
        admin = self._member(org, 'admin-source@example.com', admin=True)
        inactive = IntegrationSource.objects.create(
            organization=org,
            name='Inactive Fabric',
            source_type='powerbi_fabric',
            is_active=False,
        )
        client.login(username=admin.email, password='testpass')

        inactive_response = client.post(
            f'/api/integrations/sources/{inactive.pk}/run/',
        )

        assert inactive_response.status_code == 400
        assert 'Activate' in inactive_response.json()['error']
        assert not SourceRunLog.objects.filter(source=inactive).exists()
        async_task_mock.assert_not_called()

        inactive.is_active = True
        inactive.save(update_fields=['is_active'])
        IntegrationSource.objects.create(
            organization=org,
            name='Duplicate Fabric',
            source_type='powerbi_fabric',
            is_active=True,
        )

        ambiguous_response = client.post(
            f'/api/integrations/sources/{inactive.pk}/run/',
        )

        assert ambiguous_response.status_code == 400
        assert 'multiple active sources' in ambiguous_response.json()['error']
        assert not SourceRunLog.objects.filter(source=inactive).exists()
        async_task_mock.assert_not_called()

    def test_item_status_patch_needs_a_catalog_tier_and_primary_is_company_only(
            self, client, org, item_with_org):
        no_tier = self._member(org, 'none@example.com')
        client.login(username=no_tier.email, password='testpass')
        assert client.patch(
            f'/api/items/{item_with_org.pk}/',
            data=json.dumps({'status': 'ATTENTION'}),
            content_type='application/json',
        ).status_code == 403
        client.logout()

        analytics = self._member(
            org, 'analytics@example.com', tier='Analytics',
        )
        client.login(username=analytics.email, password='testpass')
        assert client.patch(
            f'/api/items/{item_with_org.pk}/',
            data=json.dumps({'status': 'ATTENTION'}),
            content_type='application/json',
        ).status_code == 200
        assert client.post(
            f'/api/items/{item_with_org.pk}/set_primary/',
        ).status_code == 403
        client.logout()

        company = self._member(org, 'company@example.com', tier='Company')
        client.login(username=company.email, password='testpass')
        assert client.post(
            f'/api/items/{item_with_org.pk}/set_primary/',
        ).status_code == 200

    def test_item_group_patch_is_payload_aware_by_tier(
            self, client, org, item_with_org):
        group = item_with_org.item_group
        category = Category.objects.create(name='Finance', organization=org)

        no_tier = self._member(org, 'none-group@example.com')
        client.login(username=no_tier.email, password='testpass')
        assert client.get(f'/api/item-groups/{group.pk}/').status_code == 403
        assert client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps({'status': 'ATTENTION'}),
            content_type='application/json',
        ).status_code == 403
        client.logout()

        analytics = self._member(
            org, 'analytics-group@example.com', tier='Analytics',
        )
        client.login(username=analytics.email, password='testpass')
        assert client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps({'status': 'ATTENTION'}),
            content_type='application/json',
        ).status_code == 200
        assert client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps({'category': category.pk}),
            content_type='application/json',
        ).status_code == 403
        client.logout()

        company = self._member(
            org, 'company-group@example.com', tier='Company',
        )
        client.login(username=company.email, password='testpass')
        assert client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps({'category': category.pk}),
            content_type='application/json',
        ).status_code == 200
        group.refresh_from_db()
        assert group.category_id == category.pk

    def test_reference_crud_requires_company_and_people_writes_stay_admin_only(
            self, client, org):
        no_tier = self._member(org, 'none-reference@example.com')
        client.login(username=no_tier.email, password='testpass')
        for endpoint in ('departments', 'data-persons', 'categories'):
            assert client.get(f'/api/{endpoint}/').status_code == 403
        client.logout()

        analytics = self._member(
            org, 'analytics-reference@example.com', tier='Analytics',
        )
        client.login(username=analytics.email, password='testpass')
        for endpoint in ('departments', 'data-persons', 'categories'):
            assert client.get(f'/api/{endpoint}/').status_code == 403
        client.logout()

        company = self._member(
            org, 'company-reference@example.com', tier='Company',
        )
        client.login(username=company.email, password='testpass')
        for endpoint in ('departments', 'data-persons', 'categories'):
            assert client.get(f'/api/{endpoint}/').status_code == 200
        assert client.post(
            '/api/departments/',
            data=json.dumps({'name': 'Finance'}),
            content_type='application/json',
        ).status_code == 201
        category = client.post(
            '/api/categories/',
            data=json.dumps({'name': 'Core'}),
            content_type='application/json',
        )
        assert category.status_code == 201
        assert client.post(
            '/api/categories/',
            data=json.dumps({'name': '  to BE deleted  '}),
            content_type='application/json',
        ).status_code == 400
        assert client.patch(
            f"/api/categories/{category.json()['id']}/",
            data=json.dumps({'name': ' TO BE DELETED '}),
            content_type='application/json',
        ).status_code == 400
        assert client.post(
            '/api/data-persons/',
            data=json.dumps({'name': 'Alice'}),
            content_type='application/json',
        ).status_code == 403

    def test_data_person_normalized_name_errors_are_api_400s(
            self, client, org):
        admin = self._member(org, 'admin-people@example.com', admin=True)
        client.login(username=admin.email, password='testpass')

        created = client.post(
            '/api/data-persons/',
            data=json.dumps({'name': ' Alice '}),
            content_type='application/json',
        )
        assert created.status_code == 201
        alice = DataPerson.objects.get(pk=created.json()['id'])
        assert alice.name == 'Alice'

        duplicate = client.post(
            '/api/data-persons/',
            data=json.dumps({'name': '  aLiCe  '}),
            content_type='application/json',
        )
        blank = client.post(
            '/api/data-persons/',
            data=json.dumps({'name': '   '}),
            content_type='application/json',
        )
        bob = DataPerson.objects.create(name='Bob', organization=org)
        rename = client.patch(
            f'/api/data-persons/{bob.pk}/',
            data=json.dumps({'name': ' ALICE '}),
            content_type='application/json',
        )

        assert duplicate.status_code == 400
        assert blank.status_code == 400
        assert rename.status_code == 400
        bob.refresh_from_db()
        assert bob.name == 'Bob'

    def test_data_person_duplicate_login_link_is_an_api_400(
            self, client, org):
        admin = self._member(org, 'admin-links@example.com', admin=True)
        linked_user = self._member(org, 'linked@example.com')
        first = DataPerson.objects.create(
            name='First', organization=org, user=linked_user,
        )
        second = DataPerson.objects.create(name='Second', organization=org)
        client.login(username=admin.email, password='testpass')

        create = client.post(
            '/api/data-persons/',
            data=json.dumps({
                'name': 'Duplicate link',
                'user': linked_user.pk,
            }),
            content_type='application/json',
        )
        update = client.patch(
            f'/api/data-persons/{second.pk}/',
            data=json.dumps({'user': linked_user.pk}),
            content_type='application/json',
        )

        assert create.status_code == 400
        assert update.status_code == 400
        assert 'user' in create.json()
        assert 'user' in update.json()
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.user_id == linked_user.pk
        assert second.user_id is None

    def test_data_person_representation_hides_cross_tenant_departments(
            self, client, org):
        admin = self._member(org, 'admin-m2m@example.com', admin=True)
        local = Department.objects.create(name='Local', organization=org)
        other = Organization.objects.create(name='Neighbour')
        foreign = Department.objects.create(
            name='Secret Department', organization=other,
        )
        person = DataPerson.objects.create(name='Alice', organization=org)
        person.departments.add(local, foreign)
        client.login(username=admin.email, password='testpass')

        response = client.get(f'/api/data-persons/{person.pk}/')

        assert response.status_code == 200
        assert response.json()['departments'] == [local.pk]
        assert response.json()['department_names'] == ['Local']
        assert foreign.pk not in response.json()['departments']
        assert 'Secret Department' not in response.content.decode()


@pytest.mark.django_db
class TestItemStatusSlackAlert:
    """Tests that PATCH status/deleted fires Slack alerts."""

    @patch('etl.hooks.slack.slack_alerts.send_slack_item_alert')
    def test_patch_status_fires_slack(
            self, mock_alert, client, rw_user, item_with_org,
            django_capture_on_commit_callbacks):
        client.login(username='writer@example.com', password='testpass')
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.patch(
                f'/api/items/{item_with_org.item_id}/',
                data=json.dumps({'status': 'VERIFIED'}),
                content_type='application/json',
            )
        assert resp.status_code == 200
        mock_alert.assert_called_once()
        call_args = mock_alert.call_args
        assert call_args[0][2] == 'status'  # change_type
        assert call_args[0][3] == 'UNVERIFIED'  # old_value
        assert call_args[0][4] == 'VERIFIED'  # new_value

    @patch('etl.hooks.slack.slack_alerts.send_slack_item_alert')
    def test_item_level_deleted_is_rejected_without_side_effects(
            self, mock_alert, client, rw_user, item_with_org,
            django_capture_on_commit_callbacks):
        client.login(username='writer@example.com', password='testpass')
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.patch(
                f'/api/items/{item_with_org.item_id}/',
                data=json.dumps({'deleted': True}),
                content_type='application/json',
            )
        assert resp.status_code == 400
        # Setting deleted=True also auto-changes status → DELETED, so the alert
        # fires twice (once for status, once for deleted).  Assert at least the
        # deleted call was made; call_args is always the most-recent call.
        item_with_org.refresh_from_db()
        assert item_with_org.deleted is False
        assert item_with_org.status == 'UNVERIFIED'
        mock_alert.assert_not_called()

    @patch('etl.hooks.slack.slack_alerts.send_slack_item_alert')
    def test_no_alert_when_status_unchanged(self, mock_alert, client, rw_user, item_with_org):
        """No Slack alert if the status value didn't actually change."""
        client.login(username='writer@example.com', password='testpass')
        resp = client.patch(
            f'/api/items/{item_with_org.item_id}/',
            data=json.dumps({'status': 'UNVERIFIED'}),  # same as default
            content_type='application/json',
        )
        assert resp.status_code == 200
        mock_alert.assert_not_called()

    def test_legacy_item_status_cannot_bypass_a_soft_deleted_group(
            self, client, rw_user, item_with_org):
        group = item_with_org.item_group
        client.login(username='writer@example.com', password='testpass')
        assert client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        ).status_code == 200

        rejected = client.patch(
            f'/api/items/{item_with_org.pk}/?include_deleted=true',
            data=json.dumps({'status': 'VERIFIED'}),
            content_type='application/json',
        )

        assert rejected.status_code == 400
        group.refresh_from_db()
        item_with_org.refresh_from_db()
        assert group.deleted is True
        assert group.status == 'DELETED'
        assert item_with_org.deleted is True
        assert item_with_org.status == 'DELETED'

    def test_item_status_cannot_restore_a_soft_deleted_group(
            self, client, rw_user, item_with_org):
        group = item_with_org.item_group
        client.login(username='writer@example.com', password='testpass')
        assert client.patch(
            f'/api/item-groups/{group.pk}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        ).status_code == 200

        rejected = client.patch(
            f'/api/items/{item_with_org.pk}/?include_deleted=true',
            data=json.dumps({'status': 'VERIFIED'}),
            content_type='application/json',
        )

        assert rejected.status_code == 400
        group.refresh_from_db()
        item_with_org.refresh_from_db()
        assert group.deleted is True
        assert group.status == 'DELETED'
        assert item_with_org.deleted is True
        assert item_with_org.status == 'DELETED'


@pytest.mark.django_db
class TestPowerBIUsageAPI:
    """Tests for the /api/powerbi-usage/ endpoint backing the Reports Usage tab."""

    @pytest.fixture
    def usage_rows(self, org):
        """Three rows in May, two in April. Same workspace, two reports, two users.
        Asserts exercise: monthly aggregation, unique-users counting, filters."""
        rows = [
            # May: report A — Alice 5 + Bob 3 = 8 views, 2 unique users
            dict(month=date(2026, 5, 1), workspace_id='ws-1', workspace_name='WS One',
                 report_id='rpt-A', report_name='Report A',
                 user_email='alice@example.com', view_count=5, organization=org),
            dict(month=date(2026, 5, 1), workspace_id='ws-1', workspace_name='WS One',
                 report_id='rpt-A', report_name='Report A',
                 user_email='bob@example.com', view_count=3, organization=org),
            # May: report B — Alice 2 views, 1 user
            dict(month=date(2026, 5, 1), workspace_id='ws-1', workspace_name='WS One',
                 report_id='rpt-B', report_name='Report B',
                 user_email='alice@example.com', view_count=2, organization=org),
            # April: report A — Alice 4 views, 1 user
            dict(month=date(2026, 4, 1), workspace_id='ws-1', workspace_name='WS One',
                 report_id='rpt-A', report_name='Report A',
                 user_email='alice@example.com', view_count=4, organization=org),
        ]
        for r in rows:
            PowerBIReportUsage.objects.create(**r)

    def test_aggregates_by_month_and_report(self, client, rw_user, usage_rows):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/')
        assert resp.status_code == 200
        data = resp.json()
        # 3 distinct (month, report) buckets: May/A, May/B, April/A
        assert len(data['results']) == 3

        by_key = {(r['month'], r['report_id']): r for r in data['results']}
        may_a = by_key[('2026-05-01', 'rpt-A')]
        assert may_a['view_count'] == 8
        assert may_a['unique_users'] == 2

        may_b = by_key[('2026-05-01', 'rpt-B')]
        assert may_b['view_count'] == 2
        assert may_b['unique_users'] == 1

    def test_returns_distinct_months_sorted_desc(self, client, rw_user, usage_rows):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/')
        assert resp.json()['months'] == ['2026-05-01', '2026-04-01']

    def test_workspace_filter(self, client, rw_user, org):
        PowerBIReportUsage.objects.create(
            month=date(2026, 5, 1), workspace_name='WS One',
            report_id='rpt-A', user_email='a@x', view_count=1, organization=org,
        )
        PowerBIReportUsage.objects.create(
            month=date(2026, 5, 1), workspace_name='WS Two',
            report_id='rpt-X', user_email='b@x', view_count=99, organization=org,
        )
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/?workspace_name=WS Two')
        results = resp.json()['results']
        assert len(results) == 1
        assert results[0]['workspace_name'] == 'WS Two'
        assert results[0]['view_count'] == 99

    def test_month_filter(self, client, rw_user, usage_rows):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/?month=2026-04-01')
        results = resp.json()['results']
        assert len(results) == 1
        assert results[0]['month'] == '2026-04-01'

    def test_org_scoping_excludes_other_orgs(self, client, rw_user, usage_rows):
        """Rows attached to a different org must NOT appear in another org's response."""
        other_org = Organization.objects.create(name='Other Org')
        PowerBIReportUsage.objects.create(
            month=date(2026, 5, 1), workspace_name='Other WS',
            report_id='rpt-other', user_email='ext@x', view_count=999,
            organization=other_org,
        )
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/')
        results = resp.json()['results']
        assert all(r['workspace_name'] != 'Other WS' for r in results)
        # Other org's view count is huge — confirm it didn't leak into the first org's totals
        assert all(r['view_count'] < 999 for r in results)

    def test_pivot_group_by_user_collapses_across_reports(self, client, rw_user, usage_rows):
        """`group_by=user_email` aggregates the user's views across every report."""
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/?group_by=user_email')
        assert resp.status_code == 200
        data = resp.json()
        assert data['group_by'] == ['user_email']
        by_email = {r['user_email']: r for r in data['results']}
        # Alice: 5 (May/A) + 2 (May/B) + 4 (Apr/A) = 11
        assert by_email['alice@example.com']['view_count'] == 11
        # Bob: 3 (May/A only)
        assert by_email['bob@example.com']['view_count'] == 3
        # Each row should carry only the requested dim plus the metrics.
        keys = set(by_email['alice@example.com'].keys())
        assert keys == {'user_email', 'view_count', 'unique_users'}

    def test_pivot_unknown_dims_are_silently_dropped(self, client, rw_user, usage_rows):
        """Unknown dim names get filtered out of the projection."""
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/?group_by=user_email,DROP_TABLE,workspace_name')
        assert resp.status_code == 200
        assert resp.json()['group_by'] == ['user_email', 'workspace_name']

    def test_pivot_empty_group_by_falls_back_to_default(self, client, rw_user, usage_rows):
        """If every requested dim is unknown, the endpoint reverts to the default grain."""
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/?group_by=NOPE,STILL_NOPE')
        assert resp.status_code == 200
        gb = resp.json()['group_by']
        assert 'month' in gb and 'workspace_id' in gb and 'report_id' in gb
