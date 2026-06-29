"""
Tests for Django admin configuration changes.
"""
import pytest
from django.contrib.admin.sites import AdminSite
from catalog.admin import ItemAdmin
from catalog.models import Item


@pytest.mark.django_db
class TestItemAdmin:
    def test_list_display(self):
        """Item admin has the expected list_display fields."""
        assert 'item_name' in ItemAdmin.list_display
        assert 'item_type' in ItemAdmin.list_display
        assert 'organization' in ItemAdmin.list_display
        assert 'status' in ItemAdmin.list_display
        assert 'deleted' in ItemAdmin.list_display

    def test_list_filter(self):
        """Item admin has the expected list_filter fields."""
        assert 'organization' in ItemAdmin.list_filter
        assert 'item_type' in ItemAdmin.list_filter
        assert 'status' in ItemAdmin.list_filter
        assert 'deleted' in ItemAdmin.list_filter

    def test_search_fields(self):
        """Item admin supports searching by name and id."""
        assert 'item_name' in ItemAdmin.search_fields
        assert 'item_id' in ItemAdmin.search_fields

    def test_admin_registered(self):
        """Item model is registered with the custom ItemAdmin."""
        from django.contrib import admin
        assert Item in admin.site._registry
        registered_admin = admin.site._registry[Item]
        assert isinstance(registered_admin, ItemAdmin)
