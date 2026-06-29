import pytest
from django.urls import reverse
from catalog.models import IntegrationSource, IntegrationDestination, IntegrationHook, OrganizationMembership

@pytest.mark.django_db
class TestIntegrationsAPI:

    def test_integrations_get_all_unauthorized(self, client):
        # Django is API-only now: unauthenticated requests get a clean 401 JSON
        # (api_login_required) instead of a redirect to a server-rendered login.
        resp = client.get('/api/integrations/')
        assert resp.status_code == 401

    def test_integrations_get_all_not_admin(self, client, rw_user, org):
        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/integrations/')
        assert resp.status_code == 403

    def test_integrations_get_all_admin(self, client, rw_user, org):
        # Make the user an admin
        membership = OrganizationMembership.objects.get(user=rw_user, organization=org)
        membership.is_admin = True
        membership.save()
        
        # Create some integration objects
        IntegrationSource.objects.create(organization=org, name="My Source", source_type="powerbi_fabric")
        IntegrationDestination.objects.create(organization=org, name="My Dest", destination_type="bigquery")
        IntegrationHook.objects.create(organization=org, name="My Hook", hook_type="slack")

        client.login(username='writer@example.com', password='testpass')
        resp = client.get('/api/integrations/')
        assert resp.status_code == 200
        data = resp.json()
        assert len(data['sources']) == 1
        assert data['sources'][0]['name'] == 'My Source'
        assert len(data['destinations']) == 1
        assert data['destinations'][0]['name'] == 'My Dest'
        assert len(data['hooks']) == 1
        assert data['hooks'][0]['name'] == 'My Hook'

    def test_integrations_save_source(self, client, rw_user, org):
        # Make the user an admin
        membership = OrganizationMembership.objects.get(user=rw_user, organization=org)
        membership.is_admin = True
        membership.save()
        
        client.login(username='writer@example.com', password='testpass')
        
        data = {
            "name": "New Source",
            "is_active": True,
            "tenant_id": "tenant123",
            "client_id": "client123",
            "client_secret": "secret123",
            "workspace_ids": "ws1,ws2",
            "schedule_frequency": "manual"
        }
        resp = client.post('/api/integrations/sources/save/', data=data, content_type='application/json')
        assert resp.status_code == 200
        resp_data = resp.json()
        assert resp_data['status'] == 'saved'
        
        source = IntegrationSource.objects.get(id=resp_data['id'])
        assert source.name == "New Source"
        assert source.tenant_id == "tenant123"
        assert source.workspace_ids == ["ws1", "ws2"]

    def test_integrations_save_destination(self, client, rw_user, org):
        membership = OrganizationMembership.objects.get(user=rw_user, organization=org)
        membership.is_admin = True
        membership.save()
        
        client.login(username='writer@example.com', password='testpass')
        
        sa_json = '{"project_id": "my-gcp-project", "client_email": "test@my-gcp-project.iam.gserviceaccount.com"}'
        data = {
            "name": "New Dest",
            "is_active": True,
            "bq_dataset_id": "my_dataset",
            "bq_service_account_json": sa_json,
            "schedule_frequency": "manual"
        }
        resp = client.post('/api/integrations/destinations/save/', data=data, content_type='application/json')
        assert resp.status_code == 200
        resp_data = resp.json()
        assert resp_data['status'] == 'saved'
        
        dest = IntegrationDestination.objects.get(id=resp_data['id'])
        assert dest.name == "New Dest"
        assert dest.bq_project_id == "my-gcp-project"
        assert dest.bq_dataset_id == "my_dataset"
