"""
Shared pytest fixtures for the DataGov test suite.
"""
import pytest
from django.contrib.auth.models import Group
from catalog.models import (
    Organization, OrganizationMembership, CustomUser, Department, DataPerson,
    Item, Summary, IntegrationSource, IntegrationHook,
)


@pytest.fixture
def org(db):
    return Organization.objects.create(name='Test Org')


@pytest.fixture
def dept(db):
    return Department.objects.create(name='Engineering')


@pytest.fixture
def owner(db, dept):
    p = DataPerson.objects.create(name='Jane Doe', is_owner=True)
    p.departments.add(dept)
    return p


@pytest.fixture
def user(db):
    """A basic user with no special permissions."""
    return CustomUser.objects.create_user(
        username='reader', email='reader@example.com', password='testpass',
    )


@pytest.fixture
def rw_user(db, org):
    """A user with org membership and broad (non-admin) page access. (Name
    kept for backwards compatibility with existing tests; the read/write
    split was removed — any org member can write.)

    Company + Analytics unlock every page except Org Settings & Integrations,
    which stay Admin-only (see test_api_integrations negative assertions)."""
    u = CustomUser.objects.create_user(
        username='writer', email='writer@example.com', password='testpass',
    )
    OrganizationMembership.objects.create(user=u, organization=org)
    for name in ['Company', 'Analytics']:
        g, _ = Group.objects.get_or_create(name=name)
        u.groups.add(g)
    return u


@pytest.fixture
def ro_user(db, org):
    """A user with org membership and Company page access (includes the Data
    Dictionary). (Name kept for backwards compatibility; the read/write split
    was removed.)"""
    u = CustomUser.objects.create_user(
        username='readonly', email='readonly@example.com', password='testpass',
    )
    OrganizationMembership.objects.create(user=u, organization=org)
    g, _ = Group.objects.get_or_create(name='Company')
    u.groups.add(g)
    return u


@pytest.fixture
def item(db):
    """A plain Item without organization."""
    return Item.objects.create(
        item_id='item_1',
        item_name='Sales Model',
        item_type='PB_MEASURE',
    )


@pytest.fixture
def item_with_org(db, org):
    """An Item linked to the test organization."""
    return Item.objects.create(
        item_id='item_org_1',
        item_name='Revenue',
        item_type='PB_MEASURE',
        organization=org,
        workspace_name='WS1',
        dataset_name='DS1',
        table_name='T1',
    )


@pytest.fixture
def item_with_reports(db, org):
    """An Item with connected_reports_json populated."""
    return Item.objects.create(
        item_id='item_rpts_1',
        item_name='Revenue KPI',
        item_type='PB_MEASURE',
        organization=org,
        connected_reports_json=[
            {'id': 'rpt1', 'name': 'Sales Report', 'url': 'https://app.powerbi.com/r1'},
            {'id': 'rpt2', 'name': 'Finance Report', 'url': ''},
        ],
    )


@pytest.fixture
def summary(db):
    return Summary.objects.create(
        total_measures=10, unused_measures=2,
        total_columns=20, unused_columns=5,
        total_reports=3,
    )


@pytest.fixture
def source(db, org):
    return IntegrationSource.objects.create(
        organization=org,
        name='PowerBI Source',
        source_type='powerbi_fabric',
    )


@pytest.fixture
def slack_hook(db, org):
    """An active slack_alerts hook for the test org."""
    return IntegrationHook.objects.create(
        organization=org,
        hook_type='slack_alerts',
        name='Slack Alerts',
        is_active=True,
        slack_bot_token='xoxb-fake-token',
        slack_alerts_channel='#alerts',
    )
