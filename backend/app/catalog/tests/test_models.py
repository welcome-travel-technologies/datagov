import pytest
from datetime import date
from django.test import TestCase
from catalog.models import (
    Department, DataPerson, CustomUser, Summary, Item,
    Organization, OrganizationMembership, IntegrationSource,
    PowerBIReportUsage,
)


class ModelTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Engineering")
        self.owner = DataPerson.objects.create(name="Jane Doe")
        self.owner.departments.add(self.dept)
        self.org = Organization.objects.create(name="Test Org")
        self.user = CustomUser.objects.create_user(
            username="jdoe", 
            email="jdoe@example.com", 
            password="testpass",
            department=self.dept
        )
        self.membership = OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
        )
        self.item = Item.objects.create(
            item_id="item_1",
            item_name="Sales Model",
            item_type="SemanticModel"
        )
        self.source = IntegrationSource.objects.create(
            organization=self.org,
            name="PowerBI Source",
            source_type="powerbi_fabric"
        )

    def test_department_str(self):
        self.assertEqual(str(self.dept), "Engineering")

    def test_data_person_str(self):
        self.assertEqual(str(self.owner), "Jane Doe")

    def test_data_person_role_defaults(self):
        """New DataPerson rows default to is_owner=True, is_steward=False, is_other=False."""
        self.assertTrue(self.owner.is_owner)
        self.assertFalse(self.owner.is_steward)
        self.assertFalse(self.owner.is_other)

    def test_data_person_slack_handle_validation(self):
        from django.core.exceptions import ValidationError
        p = DataPerson(name="Bad", slack_handle="missing-at")
        with self.assertRaises(ValidationError):
            p.full_clean()
        p.slack_handle = "@ok"
        p.full_clean()  # should not raise

    def test_data_person_multi_department(self):
        """A DataPerson can belong to multiple departments via M2M."""
        ops = Department.objects.create(name="Operations")
        self.owner.departments.add(ops)
        names = list(self.owner.departments.values_list('name', flat=True).order_by('name'))
        self.assertEqual(names, ["Engineering", "Operations"])
        # Reverse access works too.
        self.assertIn(self.owner, ops.data_persons.all())

    def test_user_creation(self):
        self.assertEqual(self.user.username, "jdoe")
        self.assertEqual(self.user.department, self.dept)

    def test_organization_str(self):
        self.assertEqual(str(self.org), "Test Org")

    def test_membership_str(self):
        self.assertIn("jdoe", str(self.membership))
        self.assertIn("Test Org", str(self.membership))

    def test_integration_source_str(self):
        self.assertEqual(str(self.source), "PowerBI Source (Test Org)")

    def test_item_creation(self):
        self.assertEqual(self.item.item_id, "item_1")
        self.assertEqual(self.item.item_name, "Sales Model")


# =============================================
# New tests for plan.md features
# =============================================

@pytest.mark.django_db
class TestItemOrganizationFK:
    def test_item_organization_fk(self, item_with_org, org):
        """Item can be linked to an Organization via FK."""
        assert item_with_org.organization == org
        assert item_with_org.organization.name == 'Test Org'

    def test_item_organization_nullable(self, item):
        """Item.organization defaults to None."""
        assert item.organization is None

    def test_item_org_cascade_set_null(self, item_with_org, org):
        """Deleting the org sets item.organization to NULL (SET_NULL)."""
        org.delete()
        item_with_org.refresh_from_db()
        assert item_with_org.organization is None

    def test_item_org_reverse_relation(self, item_with_org, org):
        """Organization.items reverse manager works."""
        assert item_with_org in org.items.all()


@pytest.mark.django_db
class TestItemConnectedReportsJSON:
    def test_connected_reports_json_default(self, item):
        """Default value for connected_reports_json is an empty list."""
        assert item.connected_reports_json == []

    def test_connected_reports_json_stores_data(self, item_with_reports):
        """connected_reports_json stores and retrieves list of dicts."""
        rpts = item_with_reports.connected_reports_json
        assert isinstance(rpts, list)
        assert len(rpts) == 2
        assert rpts[0]['name'] == 'Sales Report'
        assert rpts[1]['id'] == 'rpt2'

    def test_connected_reports_json_roundtrip(self, db):
        """Write and re-read from DB preserves JSON structure."""
        data = [{'id': 'x', 'name': 'Test', 'url': 'https://example.com'}]
        item = Item.objects.create(
            item_id='json_rt_1', item_name='RT Test', item_type='PB_COLUMN',
            connected_reports_json=data,
        )
        item.refresh_from_db()
        assert item.connected_reports_json == data


@pytest.mark.django_db
class TestPowerBIReportUsage:
    """Tests for the PowerBIReportUsage model (per ws × report × user × month)."""

    def _row(self, **overrides):
        defaults = dict(
            month=date(2026, 5, 1),
            workspace_id='ws-1', workspace_name='WS One',
            report_id='rpt-1', report_name='Report A',
            user_email='alice@example.com', user_display_name='Alice',
            platform='Web', distribution_method='Workspace', report_page='Page 1',
            view_count=7,
        )
        defaults.update(overrides)
        return PowerBIReportUsage.objects.create(**defaults)

    def test_create_minimal_row(self, db):
        row = self._row()
        assert row.pk is not None
        assert row.view_count == 7
        assert row.month == date(2026, 5, 1)

    def test_org_and_source_fk_nullable(self, db):
        row = self._row()
        assert row.organization is None
        assert row.integration_source is None

    def test_org_set_null_on_delete(self, org, source):
        row = self._row(organization=org, integration_source=source)
        org.delete()
        row.refresh_from_db()
        assert row.organization is None

    def test_reverse_relations(self, org, source):
        row = self._row(organization=org, integration_source=source)
        assert row in org.powerbi_usage.all()
        assert row in source.powerbi_usage.all()
