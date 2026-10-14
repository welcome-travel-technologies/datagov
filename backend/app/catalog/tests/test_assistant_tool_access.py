from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import PermissionDenied
from rest_framework.test import APIClient

from catalog.models import OrganizationMembership
from catalog.services.assistant_tools import bind_tool_policy, member_policy

pytestmark = pytest.mark.django_db


def test_member_preferences_cannot_escalate_or_change_other_users(rw_user, org):
    membership = OrganizationMembership.objects.get(user=rw_user, organization=org)
    membership.assistant_tool_access = {'documents': False, 'powerbi_live': False}
    membership.save()
    client = APIClient()
    client.force_authenticate(rw_user)
    url = '/api/me/assistant-tools/'
    assert client.patch(url, {'assistant_tool_access': {'documents': True}}, format='json').status_code == 400
    assert client.patch(url, {'preferences': {'unknown': True}}, format='json').status_code == 400
    assert client.patch(url, {'preferences': {'dbt': 'false'}}, format='json').status_code == 400
    assert client.patch(url, {'preferences': {'dbt': False}, 'user_id': 999}, format='json').status_code == 400
    assert client.patch(url, {'preferences': {'documents': True, 'dbt': False}}, format='json').status_code == 200
    membership.refresh_from_db()
    assert membership.assistant_tool_access == {'documents': False, 'powerbi_live': False}
    assert member_policy(org, rw_user)['documents'] is False
    assert member_policy(org, rw_user)['dbt'] is False
    assert member_policy(org, rw_user)['powerbi'] is True


def test_tools_recheck_policy_when_executed(rw_user, org):
    calls = []
    def get_pb_item_details(name: str):
        calls.append(name)
        return name
    wrapped = bind_tool_policy(get_pb_item_details, org, rw_user)
    assert wrapped('allowed') == 'allowed'
    OrganizationMembership.objects.filter(user=rw_user, organization=org).update(assistant_tool_access={'powerbi': False})
    with pytest.raises(PermissionDenied):
        wrapped('blocked')
    assert calls == ['allowed']


def test_disabled_provider_has_no_prompt_context_or_tools(rw_user, org):
    from catalog.tools.agent import get_agent
    OrganizationMembership.objects.filter(user=rw_user, organization=org).update(assistant_tool_access={'powerbi': False, 'dbt': False, 'documents': False, 'lineage': False})
    agent = Mock()
    with patch('catalog.tools.agent.Agent', return_value=agent), patch('catalog.tools.agent.PROVIDERS') as providers:
        get_agent(org=org, user=rw_user, powerbi_tools_enabled=True, dbt_enabled=True, evidence_context=Mock())
    providers.__getitem__.assert_not_called()
    agent.tool_plain.assert_not_called()


def test_missing_membership_and_inactive_users_have_no_tools(rw_user, org):
    rw_user.is_active = False
    assert not any(member_policy(org, rw_user).values())
    rw_user.is_active = True
    OrganizationMembership.objects.filter(user=rw_user, organization=org).delete()
    assert not any(member_policy(org, rw_user).values())
