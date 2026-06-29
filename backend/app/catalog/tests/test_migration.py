"""
Schema smoke tests — verify that key columns exist after migration.
"""
import pytest
from django.db import connection


@pytest.mark.django_db
class TestSchemaColumns:
    def test_organization_id_column_exists(self):
        """organization_id column should exist on catalog_item."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pragma_table_info('catalog_item') WHERE name='organization_id'"
                if connection.vendor == 'sqlite' else
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='catalog_item' AND column_name='organization_id'"
            )
            result = cursor.fetchone()
            assert result is not None, 'organization_id column missing from catalog_item'

    def test_connected_reports_json_column_exists(self):
        """connected_reports_json column should exist on catalog_item."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pragma_table_info('catalog_item') WHERE name='connected_reports_json'"
                if connection.vendor == 'sqlite' else
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='catalog_item' AND column_name='connected_reports_json'"
            )
            result = cursor.fetchone()
            assert result is not None, 'connected_reports_json column missing from catalog_item'
