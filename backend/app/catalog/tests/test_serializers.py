import pytest
from catalog.serializers import ItemSerializer
from catalog.models import Item, Organization


@pytest.mark.django_db
class TestItemSerializer:
    def test_includes_organization_name(self, item_with_org):
        """Serializer outputs organization_name from the related org."""
        data = ItemSerializer(item_with_org).data
        assert 'organization_name' in data
        assert data['organization_name'] == 'Test Org'

    def test_organization_name_null_when_no_org(self, item):
        """organization_name is None when item has no org."""
        data = ItemSerializer(item).data
        assert data['organization_name'] is None

    def test_includes_connected_reports_json(self, item_with_reports):
        """connected_reports_json field is present and correct."""
        data = ItemSerializer(item_with_reports).data
        assert 'connected_reports_json' in data
        assert len(data['connected_reports_json']) == 2
        assert data['connected_reports_json'][0]['name'] == 'Sales Report'

    def test_connected_reports_json_empty_list(self, item):
        """connected_reports_json defaults to empty list."""
        data = ItemSerializer(item).data
        assert data['connected_reports_json'] == []

    def test_type_alias_field(self, item):
        """The 'type' alias field maps to item_type."""
        data = ItemSerializer(item).data
        assert data['type'] == item.item_type

    def test_all_core_fields_present(self, item_with_org):
        """Spot-check that key fields are in the serializer output."""
        data = ItemSerializer(item_with_org).data
        for field in ['item_id', 'item_name', 'item_type', 'workspace_name',
                      'dataset_name', 'table_name', 'organization', 'organization_name',
                      'connected_reports_json', 'status']:
            assert field in data, f'Missing field: {field}'
