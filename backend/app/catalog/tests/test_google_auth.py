"""Regression tests for Google login and registration, with no provider traffic."""
import base64
import hashlib
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from google.auth.exceptions import GoogleAuthError

from catalog import google_auth
from catalog.models import CustomUser, Organization, OrganizationMembership
from catalog.spa_auth import _me_payload

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def google_settings(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = 'test-client.apps.googleusercontent.com'
    settings.GOOGLE_OAUTH_CLIENT_SECRET = 'test-secret'
    settings.GOOGLE_OAUTH_REDIRECT_URI = 'https://catalog.example/api/auth/google/callback/'
    settings.GOOGLE_SIGNUP_ORGANIZATION_ID = ''


@pytest.fixture
def provider(monkeypatch):
    """Mock both outbound HTTP and identity verification."""
    http = Mock()
    http.__enter__ = Mock(return_value=http)
    http.__exit__ = Mock(return_value=False)
    http.post.return_value.json.return_value = {'id_token': 'signed-id-token'}
    monkeypatch.setattr(google_auth.requests, 'Session', Mock(return_value=http))
    verify = Mock()
    monkeypatch.setattr(google_auth.id_token, 'verify_oauth2_token', verify)
    return http, verify


def start(client, next_path='/dashboard'):
    response = client.post(reverse('api-google-start'), {'next': next_path}, content_type='application/json')
    assert response.status_code == 200
    return client.session['google_oauth'], response


def callback(client, provider, claims=None, next_path='/dashboard'):
    flow, _ = start(client, next_path)
    provider[1].return_value = {
        'sub': 'google-subject-1', 'email': 'Jane.Doe@gmail.com',
        'email_verified': True, 'nonce': flow['nonce'],
        'given_name': 'Jane', 'family_name': 'Doe', **(claims or {}),
    }
    return client.get(reverse('api-google-callback'), {'state': flow['state'], 'code': 'one-use-code'})


def assert_error(response, code):
    assert response.status_code == 302
    location = urlsplit(response['Location'])
    assert location.path == '/login'
    assert parse_qs(location.query)['google_error'] == [code]
    assert response['Referrer-Policy'] == 'no-referrer'


def test_config_exposes_only_availability_and_sets_csrf_cookie(client, settings):
    response = client.get(reverse('api-google-config'))
    assert response.json() == {'enabled': True, 'pending_link': None}
    assert settings.CSRF_COOKIE_NAME in response.cookies
    assert 'no-store' in response['Cache-Control']
    assert settings.GOOGLE_OAUTH_CLIENT_SECRET not in response.content.decode()


@pytest.mark.parametrize('missing', ['GOOGLE_OAUTH_CLIENT_ID', 'GOOGLE_OAUTH_CLIENT_SECRET', 'GOOGLE_OAUTH_REDIRECT_URI'])
def test_missing_configuration_disables_config_and_start(client, settings, missing):
    setattr(settings, missing, '')
    assert client.get(reverse('api-google-config')).json() == {'enabled': False, 'pending_link': None}
    assert client.post(reverse('api-google-start'), '{}', content_type='application/json').status_code == 503
    assert 'google_oauth' not in client.session


def test_start_requires_csrf_and_post(settings):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse('api-google-start')).status_code == 405
    assert client.post(reverse('api-google-start'), '{}', content_type='application/json').status_code == 403
    response = client.get(reverse('api-google-config'))
    token = response.cookies[settings.CSRF_COOKIE_NAME].value
    response = client.post(reverse('api-google-start'), '{}', content_type='application/json', HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 200


@pytest.mark.parametrize('body', ['{', '[]', 'null', '"text"'])
def test_start_rejects_non_object_json(client, body):
    assert client.post(reverse('api-google-start'), body, content_type='application/json').status_code == 400
    assert 'google_oauth' not in client.session


def test_start_uses_session_bound_state_nonce_and_pkce(client, settings):
    flow, response = start(client, '/dictionary?search=partner')
    authorization = urlsplit(response.json()['url'])
    params = parse_qs(authorization.query)
    assert (authorization.scheme, authorization.netloc) == ('https', 'accounts.google.com')
    assert params['client_id'] == [settings.GOOGLE_OAUTH_CLIENT_ID]
    assert params['redirect_uri'] == [settings.GOOGLE_OAUTH_REDIRECT_URI]
    assert params['scope'] == ['openid email profile']
    assert params['response_type'] == ['code']
    assert params['state'] == [flow['state']]
    assert params['nonce'] == [flow['nonce']]
    expected = base64.urlsafe_b64encode(hashlib.sha256(flow['verifier'].encode()).digest()).rstrip(b'=').decode()
    assert params['code_challenge'] == [expected]
    assert params['code_challenge_method'] == ['S256']
    assert flow['verifier'] not in response.content.decode()
    assert settings.GOOGLE_OAUTH_CLIENT_SECRET not in response.content.decode()
    new_flow, _ = start(client)
    assert all(new_flow[key] != flow[key] for key in ['state', 'nonce', 'verifier'])


@pytest.mark.parametrize('next_path', [
    'https://evil.example/', '//evil.example/', '///evil.example/',
    '/\\evil.example/', '/dashboard\nLocation: https://evil.example/',
    'javascript:alert(1)', 'dashboard', None, {'path': '/dictionary'},
])
def test_start_rejects_unsafe_redirects(client, next_path):
    flow, _ = start(client, next_path)
    assert flow['next'] == '/dashboard'


def test_signup_creates_basic_user_and_authenticates_session(client, provider, org, settings):
    start(client)
    old_session_key = client.session.session_key
    response = callback(client, provider, next_path='/dictionary?search=partner')
    assert response.status_code == 302
    assert response['Location'] == '/dictionary?search=partner'
    user = CustomUser.objects.get(google_subject='google-subject-1')
    assert (user.username, user.email, user.first_name, user.last_name) == ('jane.doe', 'jane.doe@gmail.com', 'Jane', 'Doe')
    assert not user.is_staff and not user.is_superuser
    assert user.is_active and not user.has_usable_password()
    assert list(user.groups.values_list('name', flat=True)) == ['Company']
    membership = user.memberships.get()
    assert membership.organization == org and not membership.is_admin
    payload = _me_payload(user)
    assert payload['role'] == 'member' and payload['is_authenticated']
    assert payload['organization']['name'] == org.name
    assert {key for key, allowed in payload['perms'].items() if allowed} == {
        'can_view_dictionary', 'can_view_tasks', 'can_view_champions',
        'can_view_chat', 'can_view_powerbi', 'can_view_reports',
    }
    assert client.session['_auth_user_id'] == str(user.pk)
    assert client.session.session_key != old_session_key
    assert 'google_oauth' not in client.session
    http, verify = provider
    http.post.assert_called_once()
    request_args = http.post.call_args
    assert request_args.args == ('https://oauth2.googleapis.com/token',)
    assert request_args.kwargs['timeout'] == 10
    assert request_args.kwargs['data']['code'] == 'one-use-code'
    assert request_args.kwargs['data']['grant_type'] == 'authorization_code'
    assert request_args.kwargs['data']['redirect_uri'] == settings.GOOGLE_OAUTH_REDIRECT_URI
    assert verify.call_args.args[0] == 'signed-id-token'
    assert verify.call_args.args[2] == settings.GOOGLE_OAUTH_CLIENT_ID
    assert not any(key in client.session for key in ['id_token', 'access_token', 'refresh_token'])


def test_signup_suffixes_case_insensitive_username_collision(client, provider, org):
    CustomUser.objects.create_user(username='JANE.DOE', email='other@example.com')
    CustomUser.objects.create_user(username='jane.doe_1', email='another@example.com')
    assert callback(client, provider)['Location'] == '/dashboard'
    assert CustomUser.objects.get(google_subject='google-subject-1').username == 'jane.doe_2'


def test_username_collision_keeps_suffix_within_field_limit():
    limit = CustomUser._meta.get_field('username').max_length
    CustomUser.objects.create_user(username='a' * limit, email='other@example.com')
    candidate = google_auth._username('a' * (limit + 20) + '@gmail.com')
    assert candidate == 'a' * (limit - 2) + '_1'
    assert len(candidate) == limit


def test_linked_subject_reuses_account_and_preserves_roles_despite_email_change(client, provider, org):
    user = CustomUser.objects.create_user(username='existing', email='old@gmail.com', google_subject='google-subject-1', is_staff=True)
    group, _ = Group.objects.get_or_create(name='Analytics')
    user.groups.add(group)
    OrganizationMembership.objects.create(user=user, organization=org, is_admin=True)
    assert callback(client, provider, {'email': 'new@gmail.com'})['Location'] == '/dashboard'
    user.refresh_from_db()
    assert CustomUser.objects.count() == 1
    assert user.email == 'old@gmail.com' and user.username == 'existing' and user.is_staff
    assert list(user.groups.values_list('name', flat=True)) == ['Analytics']
    assert user.memberships.get().is_admin
    assert client.session['_auth_user_id'] == str(user.pk)


@pytest.mark.parametrize('email,claims', [('Jane.Doe@gmail.com', {}), ('Jane.Doe@company.example', {'hd': 'company.example'})])
def test_authoritative_email_links_existing_account_without_changing_password_or_permissions(client, provider, org, email, claims):
    user = CustomUser.objects.create_user(username='old-username', email=email, password='local-password', is_staff=True)
    OrganizationMembership.objects.create(user=user, organization=org, is_admin=True)
    before_password = user.password
    assert callback(client, provider, {'email': email.lower(), **claims})['Location'] == '/dashboard'
    user.refresh_from_db()
    assert CustomUser.objects.count() == 1 and user.google_subject == 'google-subject-1'
    assert user.password == before_password and user.username == 'old-username'
    assert user.is_staff and user.memberships.get().is_admin
    assert not user.groups.exists()
    # Google is a second method: the exact existing password still logs in.
    client.post(reverse('api-auth-logout'))
    password_login = client.post(reverse('api-auth-login'), {
        'username': user.username, 'password': 'local-password',
    }, content_type='application/json')
    assert password_login.status_code == 200
    assert password_login.json()['id'] == user.pk
    assert CustomUser.objects.count() == 1


@pytest.mark.parametrize('email,subject,active,error', [
    ('jane.doe@gmail.com', 'different-subject', True, 'account_conflict'),
    ('jane.doe@gmail.com', None, False, 'inactive'),
    ('jane.doe@gmail.com', 'google-subject-1', False, 'inactive'),
])
def test_conflicting_or_inactive_identity_is_refused(client, provider, org, email, subject, active, error):
    user = CustomUser.objects.create_user(username='existing', email=email, google_subject=subject, is_active=active)
    assert_error(callback(client, provider, {'email': email}), error)
    user.refresh_from_db()
    assert user.google_subject == subject and CustomUser.objects.count() == 1
    assert '_auth_user_id' not in client.session


@pytest.mark.parametrize('claims', [
    {'nonce': 'another-browser'}, {'nonce': None}, {'email_verified': False},
    {'email_verified': 'true'}, {'sub': ''}, {'sub': None}, {'sub': 123},
    {'sub': 'x' * 256}, {'email': 'not-an-email'}, {'email': None},
])
def test_invalid_identity_claims_cannot_create_user(client, provider, org, claims):
    assert_error(callback(client, provider, claims), 'failed')
    assert not CustomUser.objects.exists()
    assert '_auth_user_id' not in client.session


@pytest.mark.parametrize('failure', [ValueError('bad audience or signature'), GoogleAuthError('expired token')])
def test_verification_errors_are_rejected_without_exposing_details(client, provider, org, failure):
    provider[1].side_effect = failure
    response = callback(client, provider)
    assert_error(response, 'failed')
    assert str(failure) not in response['Location']
    assert not CustomUser.objects.exists()


def test_token_exchange_network_failure_does_not_create_user(client, provider, org):
    provider[0].post.side_effect = requests.Timeout('contains-secret-provider-details')
    response = callback(client, provider)
    assert_error(response, 'failed')
    assert 'secret' not in response['Location']
    provider[1].assert_not_called()
    assert not CustomUser.objects.exists()


@pytest.mark.parametrize('age', [-1, 601])
def test_expired_or_future_flow_cannot_authenticate(client, provider, monkeypatch, age):
    flow, _ = start(client)
    monkeypatch.setattr(google_auth.time, 'time', lambda: flow['started_at'] + age)
    response = client.get(reverse('api-google-callback'), {'state': flow['state'], 'code': 'code'})
    assert_error(response, 'expired')
    assert 'google_oauth' not in client.session
    provider[0].post.assert_not_called()


def test_callback_is_session_bound_and_failure_consumes_state(client, provider):
    flow, _ = start(client)
    other_browser = Client()
    assert_error(other_browser.get(reverse('api-google-callback'), {'state': flow['state'], 'code': 'code'}), 'expired')
    assert_error(client.get(reverse('api-google-callback'), {'state': 'wrong-state', 'code': 'code'}), 'expired')
    assert_error(client.get(reverse('api-google-callback'), {'state': flow['state'], 'code': 'code'}), 'expired')
    provider[0].post.assert_not_called()


def test_successful_callback_cannot_be_replayed(client, provider, org):
    assert callback(client, provider)['Location'] == '/dashboard'
    assert_error(client.get(reverse('api-google-callback'), {'state': 'old-state', 'code': 'one-use-code'}), 'expired')
    assert provider[0].post.call_count == 1 and CustomUser.objects.count() == 1


@pytest.mark.parametrize('organization_count,configured,error', [(0, '', 'unavailable'), (2, '', 'unavailable'), (1, 'invalid', 'unavailable'), (1, '9' * 100, 'unavailable'), (2, 'selected', None)])
def test_signup_requires_an_unambiguous_valid_organization(client, provider, settings, organization_count, configured, error):
    orgs = [Organization.objects.create(name=f'Tenant {i}') for i in range(organization_count)]
    settings.GOOGLE_SIGNUP_ORGANIZATION_ID = str(orgs[-1].pk) if configured == 'selected' else configured
    response = callback(client, provider)
    if error:
        assert_error(response, error)
        assert not CustomUser.objects.exists()
        assert not OrganizationMembership.objects.exists()
    else:
        assert response['Location'] == '/dashboard'
        assert OrganizationMembership.objects.get().organization == orgs[-1]


@pytest.fixture
def pending_link(client, provider, org):
    user = CustomUser.objects.create_user(
        username='established-user', email='Jane.Doe@other.example',
        password='existing-password', first_name='Original', is_staff=True,
    )
    analytics, _ = Group.objects.get_or_create(name='Analytics')
    user.groups.add(analytics)
    OrganizationMembership.objects.create(user=user, organization=org, is_admin=True)
    response = callback(client, provider, {'email': user.email.lower()}, next_path='/dictionary?tab=mine')
    assert response.status_code == 302
    location = urlsplit(response['Location'])
    assert location.path == '/login'
    assert parse_qs(location.query)['google_link'] == ['1']
    user.refresh_from_db()
    assert user.google_subject is None
    assert '_auth_user_id' not in client.session
    return user


def confirm_link(client, password='existing-password'):
    return client.post(reverse('api-google-link'), {'password': password}, content_type='application/json')


def test_third_party_email_links_after_password_confirmation_and_preserves_both_methods(client, provider, pending_link):
    user = pending_link
    before_password = user.password
    config = client.get(reverse('api-google-config')).json()
    assert config['pending_link'] == {'email': user.email.lower()}
    assert 'google-subject-1' not in str(config)
    old_session_key = client.session.session_key
    response = confirm_link(client)
    assert response.status_code == 200
    assert response.json() == {'url': '/dictionary?tab=mine'}
    user.refresh_from_db()
    assert user.google_subject == 'google-subject-1'
    assert (user.username, user.first_name, user.password) == ('established-user', 'Original', before_password)
    assert user.is_staff and user.memberships.get().is_admin
    assert list(user.groups.values_list('name', flat=True)) == ['Analytics']
    assert CustomUser.objects.count() == 1
    assert client.session['_auth_user_id'] == str(user.pk)
    assert client.session.session_key != old_session_key
    assert 'google_pending_link' not in client.session
    assert client.get(reverse('api-google-config')).json()['pending_link'] is None
    assert confirm_link(client).json()['code'] == 'expired'
    client.post(reverse('api-auth-logout'))
    response = client.post(reverse('api-auth-login'), {
        'username': user.email, 'password': 'existing-password',
    }, content_type='application/json')
    assert response.status_code == 200 and response.json()['id'] == user.pk
    client.post(reverse('api-auth-logout'))
    assert callback(client, provider, {'email': user.email.lower()})['Location'] == '/dashboard'
    assert client.session['_auth_user_id'] == str(user.pk)
    assert CustomUser.objects.count() == 1


def test_pending_link_is_session_bound(client, pending_link):
    other_browser = Client()
    response = confirm_link(other_browser)
    assert response.status_code == 400 and response.json()['code'] == 'expired'
    assert other_browser.get(reverse('api-google-config')).json()['pending_link'] is None
    pending_link.refresh_from_db()
    assert pending_link.google_subject is None
    assert 'google_pending_link' in client.session


def test_confirmation_requires_post_and_csrf(client, pending_link, settings):
    protected = Client(enforce_csrf_checks=True)
    protected.cookies = client.cookies
    assert protected.get(reverse('api-google-link')).status_code == 405
    assert confirm_link(protected).status_code == 403
    assert 'google_pending_link' in protected.session
    config = protected.get(reverse('api-google-config'))
    token = config.cookies[settings.CSRF_COOKIE_NAME].value
    response = protected.post(reverse('api-google-link'), {'password': 'existing-password'}, content_type='application/json', HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 200


def test_wrong_password_keeps_pending_link_until_correct_confirmation(client, pending_link):
    for _ in range(2):
        response = confirm_link(client, 'wrong-password')
        assert response.status_code == 400
        assert response.json()['code'] == 'invalid_password'
        pending_link.refresh_from_db()
        assert pending_link.google_subject is None
        assert '_auth_user_id' not in client.session
        assert 'google_pending_link' in client.session
    assert confirm_link(client).status_code == 200
    pending_link.refresh_from_db()
    assert pending_link.google_subject == 'google-subject-1'
    assert client.session['_auth_user_id'] == str(pending_link.pk)
    assert 'google_pending_link' not in client.session


@pytest.mark.parametrize('body', ['{', '[]', '{}', '{"password": 123}'])
def test_invalid_confirmation_body_does_not_consume_pending_link(client, pending_link, body):
    response = client.post(reverse('api-google-link'), body, content_type='application/json')
    assert response.status_code == 400
    assert 'google_pending_link' in client.session
    assert confirm_link(client).status_code == 200


def test_expired_pending_link_cannot_confirm_or_appear_in_config(client, pending_link, monkeypatch):
    flow = client.session['google_pending_link']
    monkeypatch.setattr(google_auth.time, 'time', lambda: flow['started_at'] + 601)
    assert client.get(reverse('api-google-config')).json()['pending_link'] is None
    response = confirm_link(client)
    assert response.status_code == 400 and response.json()['code'] == 'expired'
    pending_link.refresh_from_db()
    assert pending_link.google_subject is None
    assert 'google_pending_link' not in client.session


@pytest.mark.parametrize('mutation,status,code', [
    ('email', 409, 'account_conflict'), ('subject', 409, 'account_conflict'),
    ('inactive', 403, 'inactive'), ('claimed_subject', 409, 'account_conflict'),
    ('duplicate_email', 409, 'account_conflict'),
])
def test_confirmation_rechecks_target_and_identity_conflicts(client, pending_link, mutation, status, code):
    user = pending_link
    if mutation == 'email':
        user.email = 'changed@other.example'
        user.save(update_fields=['email'])
    elif mutation == 'subject':
        user.google_subject = 'another-google-subject'
        user.save(update_fields=['google_subject'])
    elif mutation == 'inactive':
        user.is_active = False
        user.save(update_fields=['is_active'])
    elif mutation == 'claimed_subject':
        CustomUser.objects.create_user(username='another', email='another@example.com', google_subject='google-subject-1')
    else:
        CustomUser.objects.create_user(username='duplicate', email=user.email.lower())
    response = confirm_link(client)
    assert response.status_code == status and response.json()['code'] == code
    user.refresh_from_db()
    assert user.google_subject != 'google-subject-1'
    assert '_auth_user_id' not in client.session
    assert 'google_pending_link' not in client.session


@pytest.mark.parametrize('action', ['google_start', 'disabled_google_start', 'logout'])
def test_starting_new_flow_or_logging_out_clears_pending_link(client, pending_link, settings, action):
    configured_client_id = settings.GOOGLE_OAUTH_CLIENT_ID
    if action == 'logout':
        client.post(reverse('api-auth-logout'))
    else:
        if action == 'disabled_google_start':
            settings.GOOGLE_OAUTH_CLIENT_ID = ''
        client.post(reverse('api-google-start'), '{}', content_type='application/json')
    assert 'google_pending_link' not in client.session
    settings.GOOGLE_OAUTH_CLIENT_ID = configured_client_id
    assert confirm_link(client).json()['code'] == 'expired'


def test_disabled_confirmation_revokes_link_after_configuration_returns(client, pending_link, settings):
    configured_client_id = settings.GOOGLE_OAUTH_CLIENT_ID
    settings.GOOGLE_OAUTH_CLIENT_ID = ''
    response = confirm_link(client)
    assert response.status_code == 503 and response.json()['code'] == 'unavailable'
    assert 'google_pending_link' not in client.session
    settings.GOOGLE_OAUTH_CLIENT_ID = configured_client_id
    assert client.get(reverse('api-google-config')).json()['pending_link'] is None
    assert confirm_link(client).json()['code'] == 'expired'
    pending_link.refresh_from_db()
    assert pending_link.google_subject is None
