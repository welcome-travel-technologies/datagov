from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import PermissionDenied
from rest_framework.test import APIClient

from catalog.models import OrganizationMembership
from catalog.services.assistant_tools import bind_tool_policy, member_policy, validate_overrides

pytestmark = pytest.mark.django_db


def test_member_preferences_cannot_escalate_or_change_other_users(rw_user, org):
    membership = OrganizationMembership.objects.get(user=rw_user, organization=org)
    membership.assistant_tool_access = {'analyzer': False, 'powerbi_live': False}
    membership.save()
    client = APIClient()
    client.force_authenticate(rw_user)
    url = '/api/me/assistant-tools/'
    assert client.patch(url, {'assistant_tool_access': {'analyzer': True}}, format='json').status_code == 400
    assert client.patch(url, {'preferences': {'unknown': True}}, format='json').status_code == 400
    assert client.patch(url, {'preferences': {'dbt': 'false'}}, format='json').status_code == 400
    assert client.patch(url, {'preferences': {'dbt': False}, 'user_id': 999}, format='json').status_code == 400
    assert client.patch(url, {'preferences': {'analyzer': True, 'dbt': False}}, format='json').status_code == 200
    membership.refresh_from_db()
    assert membership.assistant_tool_access == {'analyzer': False, 'powerbi_live': False}
    assert member_policy(org, rw_user)['analyzer'] is False
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
    OrganizationMembership.objects.filter(user=rw_user, organization=org).update(assistant_tool_access={'powerbi': False, 'dbt': False, 'analyzer': False, 'lineage': False})
    agent = Mock()
    with patch('catalog.tools.agent.Agent', return_value=agent), patch('catalog.tools.agent.PROVIDERS') as providers:
        get_agent(org=org, user=rw_user, powerbi_tools_enabled=True, dbt_enabled=True, document_analyzer=Mock())
    providers.__getitem__.assert_not_called()
    agent.tool_plain.assert_not_called()


def test_missing_membership_and_inactive_users_have_no_tools(rw_user, org):
    rw_user.is_active = False
    assert not any(member_policy(org, rw_user).values())
    rw_user.is_active = True
    OrganizationMembership.objects.filter(user=rw_user, organization=org).delete()
    assert not any(member_policy(org, rw_user).values())


def test_legacy_documents_flag_is_dropped_but_never_reenables_documents(rw_user, org):
    assert validate_overrides({'documents': True, 'dbt': False}) == {'dbt': False}
    assert validate_overrides({'documents': False}) == {'analyzer': False}
    assert validate_overrides({'documents': False, 'analyzer': True}) == {'analyzer': True}
    with pytest.raises(ValueError):
        validate_overrides({'documents': 'false'})
    with pytest.raises(ValueError):
        validate_overrides({'search_evidence': False})
    client = APIClient()
    client.force_authenticate(rw_user)
    response = client.patch('/api/me/assistant-tools/', {'preferences': {'documents': False}}, format='json')
    assert response.status_code == 200
    assert 'documents' not in [tool['id'] for tool in response.json()['tools']]
    membership = OrganizationMembership.objects.get(user=rw_user, organization=org)
    assert membership.assistant_tool_preferences == {'analyzer': False}
    assert member_policy(org, rw_user)['analyzer'] is False


def test_migration_moves_a_documents_block_to_the_analyzer(rw_user, org):
    import importlib

    from django.apps import apps
    from catalog.models import CustomUser
    migration = importlib.import_module('catalog.migrations.0069_documents_tool_is_analyzer')
    cases = {
        'blocked': ({'documents': False, 'dbt': False}, {'documents': True}),
        'explicit': ({'documents': False, 'analyzer': True}, {'documents': False}),
        'untouched': ({'powerbi': False}, {}),
    }
    expected = {
        'blocked': ({'dbt': False, 'analyzer': False}, {}),
        'explicit': ({'analyzer': True}, {'analyzer': False}),
        'untouched': ({'powerbi': False}, {}),
    }
    for name, (access, preferences) in cases.items():
        user = CustomUser.objects.create_user(username=name, email=f'{name}@example.com', password='pw')
        OrganizationMembership.objects.create(user=user, organization=org, assistant_tool_access=access,
                                              assistant_tool_preferences=preferences)
    migration.documents_to_analyzer(apps, None)
    migration.documents_to_analyzer(apps, None)             # idempotent
    for name, (access, preferences) in expected.items():
        membership = OrganizationMembership.objects.get(user__username=name)
        assert (membership.assistant_tool_access, membership.assistant_tool_preferences) == (access, preferences)
    assert member_policy(org, CustomUser.objects.get(username='blocked'))['analyzer'] is False
