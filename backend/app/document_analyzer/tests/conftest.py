"""Shared fixtures: luccy is always a ``httpx.MockTransport``; nothing leaves the process."""
import httpx
import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache

from catalog.models import CustomUser, Organization, OrganizationMembership
from document_analyzer import client

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                      'LOCATION': 'document-analyzer-tests'}}


@pytest.fixture
def setup(settings, db):
    settings.CACHES = LOCMEM
    cache.clear()
    org = Organization.objects.create(name='Luccy company', powerbi_tools_enabled=False)
    other = Organization.objects.create(name='Another company')
    settings.LUCCY_ENABLED = True
    settings.LUCCY_CHAT_ENABLED = True
    settings.LUCCY_API_URL = 'http://luccy:8787/'
    settings.LUCCY_API_KEY = 'test-luccy-key-with-no-real-access'
    settings.LUCCY_ORGANIZATION_ID = str(org.pk)
    settings.LUCCY_MAX_ASKS_PER_HOUR = 60
    company, _ = Group.objects.get_or_create(name='Company')

    def make(name, organization=None, admin=False, in_company=False, **extra):
        user = CustomUser.objects.create_user(username=name, email=f'{name}@analyzer.test', password='pw', **extra)
        if organization is not None:
            OrganizationMembership.objects.create(organization=organization, user=user, is_admin=admin)
        if in_company:
            user.groups.add(company)
        return user

    users = {
        'reader': make('reader', org, in_company=True),
        'admin': make('orgadmin', org, admin=True),
        'plain': make('plain', org),
        'outsider': make('outsider', other, admin=True, in_company=True),
        'superuser': make('root', is_superuser=True, is_staff=True),
    }
    yield {'org': org, 'other': other, **users}
    cache.clear()


@pytest.fixture
def luccy(monkeypatch):
    """Route every luccy call to ``state['handler']`` and keep the requests it saw."""
    state = {'handler': lambda request: httpx.Response(500), 'requests': []}

    def handle(request):
        state['requests'].append(request)
        return state['handler'](request)

    def sync_client(read_timeout, connect=client.CONNECT_TIMEOUT):
        return httpx.Client(**client.options(read_timeout, connect), transport=httpx.MockTransport(handle))

    def async_client(read_timeout):
        return httpx.AsyncClient(**client.options(read_timeout), transport=httpx.MockTransport(handle))

    monkeypatch.setattr(client, 'sync_client', sync_client)
    monkeypatch.setattr(client, 'async_client', async_client)
    return state
