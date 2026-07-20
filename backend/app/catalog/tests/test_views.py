import json
from datetime import date
import pytest
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client
from django.contrib.auth.models import Group
from catalog.models import (
    CustomUser, Organization, OrganizationMembership, Summary, Item,
    PowerBIReportUsage,
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
            total_reports=3
        )
        self.item = Item.objects.create(
            item_id="123",
            item_name="Sales",
            item_type="SemanticModel"
        )

    # NOTE: the classic server-rendered pages (dashboard, dictionary, …) were
    # removed — the React app is the only frontend now. Their page-render tests
    # were dropped; the API tests below are what back those screens.

    def test_api_summary(self):
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
class TestItemStatusSlackAlert:
    """Tests that PATCH status/deleted fires Slack alerts."""

    @patch('etl.hooks.slack.slack_alerts.send_slack_item_alert')
    def test_patch_status_fires_slack(self, mock_alert, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
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
    def test_patch_deleted_fires_slack(self, mock_alert, client, rw_user, item_with_org):
        client.login(username='writer@example.com', password='testpass')
        resp = client.patch(
            f'/api/items/{item_with_org.item_id}/',
            data=json.dumps({'deleted': True}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        # Setting deleted=True also auto-changes status → DELETED, so the alert
        # fires twice (once for status, once for deleted).  Assert at least the
        # deleted call was made; call_args is always the most-recent call.
        mock_alert.assert_called()
        call_args = mock_alert.call_args
        assert call_args[0][2] == 'deleted'

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


@pytest.mark.django_db
class TestDraftReportFiltering:
    """Draft / test / sandbox reports are excluded from every report stat and
    list, but the heuristic must not catch production reports or non-report
    items that merely contain a stem as a substring (e.g. 'test_staging')."""

    # (name, is_draft) pairs — the boundary cases that keep the regex honest.
    DRAFT_NAMES = ['Test Dashboard', 'QA Report', 'My Draft', 'Sandbox v2',
                   'WIP Finance', 'Demo - Sales', 'POC pricing', "John's Playground"]
    KEEP_NAMES = ['Finance VAT Chain', 'Commercial Overview', 'Contest Winners',
                  'Qatar Market', 'Testament of Sales', 'Draftsman Tools']

    @pytest.fixture
    def reports(self, org):
        objs = []
        for i, n in enumerate(self.DRAFT_NAMES + self.KEEP_NAMES):
            objs.append(Item.objects.create(
                item_id=f'rep_{i}', item_name=n, item_type='PB_REPORT',
                service='powerbi', workspace_name='WS One', organization=org))
        # A non-report whose name contains 'test' — must survive draft_report_q.
        Item.objects.create(
            item_id='tbl_test', item_name='test_staging', item_type='PB_TABLE',
            service='powerbi', workspace_name='WS One', organization=org)
        return objs

    def test_is_draft_report_name_boundaries(self):
        from catalog.report_filters import is_draft_report_name
        for n in self.DRAFT_NAMES:
            assert is_draft_report_name(n), f'{n!r} should be a draft'
        for n in self.KEEP_NAMES:
            assert not is_draft_report_name(n), f'{n!r} should NOT be a draft'
        assert not is_draft_report_name('')
        assert not is_draft_report_name(None)

    def test_items_list_hides_draft_reports_but_keeps_tables(self, client, rw_user, reports):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/items/?item_type=PB_REPORT')
        assert resp.status_code == 200
        names = {r['item_name'] for r in resp.json()['results']}
        assert names == set(self.KEEP_NAMES)
        # The PB_TABLE named 'test_staging' is not name-tested by draft_report_q.
        resp2 = client.get('/api/items/?item_name=test_staging')
        assert any(r['item_name'] == 'test_staging' for r in resp2.json()['results'])

    def test_dashboard_report_count_excludes_drafts(self, client, rw_user, reports):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/dashboard/')
        assert resp.status_code == 200
        ws = resp.json()['ws_stats']['WS One']
        assert ws['r'] == len(self.KEEP_NAMES)

    def test_powerbi_usage_excludes_draft_report_names(self, client, rw_user, org):
        for i, n in enumerate(self.DRAFT_NAMES + self.KEEP_NAMES):
            PowerBIReportUsage.objects.create(
                month=date(2026, 5, 1), workspace_name='WS One',
                report_id=f'u_{i}', report_name=n, user_email='a@x',
                view_count=1, organization=org)
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/powerbi-usage/')
        names = {r['report_name'] for r in resp.json()['results']}
        assert names == set(self.KEEP_NAMES)


def test_strip_draft_reports_from_graph_prunes_subtree():
    """A draft report and its exclusive page/visual subtree are removed from a
    lineage payload; a shared upstream measure and a sibling real report stay."""
    from catalog.report_filters import strip_draft_reports_from_graph
    nodes = [
        {'id': 'PB_MEASURE::m', 'label': 'GMV', 'group': 'PB_MEASURE'},
        {'id': 'PB_VISUAL::v1', 'label': 'Trend', 'group': 'PB_VISUAL'},
        {'id': 'PB_PAGE::p1', 'label': 'Exec', 'group': 'PB_PAGE'},
        {'id': 'PB_REPORT::r1', 'label': 'QA Sandbox', 'group': 'PB_REPORT'},
        {'id': 'PB_VISUAL::v2', 'label': 'Card', 'group': 'PB_VISUAL'},
        {'id': 'PB_PAGE::p2', 'label': 'Main', 'group': 'PB_PAGE'},
        {'id': 'PB_REPORT::r2', 'label': 'Finance VAT Chain', 'group': 'PB_REPORT'},
    ]
    links = [
        {'source': 'PB_MEASURE::m', 'target': 'PB_VISUAL::v1'},
        {'source': 'PB_VISUAL::v1', 'target': 'PB_PAGE::p1'},
        {'source': 'PB_PAGE::p1', 'target': 'PB_REPORT::r1'},
        {'source': 'PB_MEASURE::m', 'target': 'PB_VISUAL::v2'},
        {'source': 'PB_VISUAL::v2', 'target': 'PB_PAGE::p2'},
        {'source': 'PB_PAGE::p2', 'target': 'PB_REPORT::r2'},
    ]
    out_nodes, out_links = strip_draft_reports_from_graph(nodes, links)
    kept = {n['id'] for n in out_nodes}
    assert kept == {'PB_MEASURE::m', 'PB_VISUAL::v2', 'PB_PAGE::p2', 'PB_REPORT::r2'}
    # No surviving link touches a removed node.
    removed = {'PB_REPORT::r1', 'PB_PAGE::p1', 'PB_VISUAL::v1'}
    assert all(l['source'] not in removed and l['target'] not in removed for l in out_links)


def test_strip_draft_reports_from_graph_noop_without_drafts():
    from catalog.report_filters import strip_draft_reports_from_graph
    nodes = [{'id': 'PB_REPORT::r', 'label': 'Finance VAT Chain', 'group': 'PB_REPORT'}]
    out_nodes, out_links = strip_draft_reports_from_graph(nodes, [])
    assert out_nodes == nodes and out_links == []


@pytest.mark.django_db
class TestNetworkGraphDraftFiltering:
    """End-to-end: the /api/network/ endpoint drops draft report nodes (and
    their page/visual subtree) from the whole-graph payload."""

    @pytest.fixture
    def graph(self, org):
        from catalog.models import NetworkNode, NetworkEdge
        nodes = [
            ('PB_MEASURE::m', 'GMV', 'PB_MEASURE'),
            ('PB_VISUAL::v1', 'Trend', 'PB_VISUAL'),
            ('PB_PAGE::p1', 'Exec', 'PB_PAGE'),
            ('PB_REPORT::r1', 'QA Sandbox', 'PB_REPORT'),   # draft
            ('PB_VISUAL::v2', 'Card', 'PB_VISUAL'),
            ('PB_PAGE::p2', 'Main', 'PB_PAGE'),
            ('PB_REPORT::r2', 'Finance VAT Chain', 'PB_REPORT'),  # real
        ]
        for nid, name, grp in nodes:
            NetworkNode.objects.create(node_id=nid, name=name, group=grp, organization=org)
        edges = [
            ('PB_MEASURE::m', 'PB_VISUAL::v1'), ('PB_VISUAL::v1', 'PB_PAGE::p1'),
            ('PB_PAGE::p1', 'PB_REPORT::r1'),
            ('PB_MEASURE::m', 'PB_VISUAL::v2'), ('PB_VISUAL::v2', 'PB_PAGE::p2'),
            ('PB_PAGE::p2', 'PB_REPORT::r2'),
        ]
        for s, t in edges:
            NetworkEdge.objects.create(source=s, target=t, organization=org)

    def test_network_all_hides_draft_report_and_subtree(self, client, rw_user, graph):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/network/?node_id=ALL')
        assert resp.status_code == 200
        ids = {n['id'] for n in resp.json()['nodes']}
        assert 'PB_REPORT::r1' not in ids
        assert 'PB_PAGE::p1' not in ids and 'PB_VISUAL::v1' not in ids
        # Real report + shared measure survive.
        assert {'PB_REPORT::r2', 'PB_MEASURE::m', 'PB_VISUAL::v2'} <= ids
