import json
import time
import requests
from django.core.management.base import BaseCommand
from catalog.models import Organization
from catalog.powerbi_client import build_powerbi_client_for_org

class Command(BaseCommand):
    help = "Test extracting PowerBI dataset relationships via Fabric API"

    def handle(self, *args, **kwargs):
        orgs = Organization.objects.all()
        client = None
        for org in orgs:
            client = build_powerbi_client_for_org(org)
            if client:
                self.stdout.write(self.style.SUCCESS(f"Found Power BI credentials for org: {org.name}"))
                break
        
        if not client:
            self.stdout.write(self.style.ERROR("No active PowerBI integration found."))
            return

        # Ensure token is loaded
        client._ensure_token()
        token = client._access_token

        # Get workspaces
        workspaces = client.get_workspaces()
        if not workspaces:
            self.stdout.write("No workspaces found.")
            return

        workspace_id = workspaces[0].get('id')
        self.stdout.write(self.style.SUCCESS(f"Using workspace {workspace_id}"))

        self.stdout.write("\n--- Testing Fabric API ---")
        fabric_base = "https://api.fabric.microsoft.com/v1"
        headers = {'Authorization': f'Bearer {token}'}

        # 1. Get semantic models
        models_url = f"{fabric_base}/workspaces/{workspace_id}/semanticModels"
        resp = requests.get(models_url, headers=headers)
        if not resp.ok:
            self.stdout.write(self.style.ERROR(f"Failed to get semantic models: {resp.status_code} {resp.text}"))
            return
        
        models_data = resp.json()
        models = models_data.get("value", [])
        if not models:
            self.stdout.write("No semantic models found in workspace.")
            return
            
        model_id = models[0].get('id')
        self.stdout.write(self.style.SUCCESS(f"Found semantic model: {model_id} ({models[0].get('displayName')})"))

        # 2. Try to get definition
        def_url = f"{fabric_base}/workspaces/{workspace_id}/semanticModels/{model_id}/getDefinition"
        self.stdout.write(f"Calling getDefinition: {def_url}")
        
        def_resp = requests.post(def_url, headers=headers)
        if def_resp.status_code == 202:
            # It's an async operation
            operation_id = def_resp.headers.get("Location") or def_resp.headers.get("Retry-After")
            self.stdout.write(self.style.SUCCESS(f"Async operation started. Check headers: {dict(def_resp.headers)}"))
            
            # The Location header usually contains the polling URL
            location = def_resp.headers.get("Location")
            if location:
                for _ in range(10):
                    poll_resp = requests.get(location, headers=headers)
                    if poll_resp.status_code == 200:
                        data = poll_resp.json()
                        self.stdout.write(self.style.SUCCESS(f"Got definition! Keys: {list(data.keys())}"))
                        self.stdout.write(f"Full operation data: {data}")
                        if data.get('status') == 'Running':
                            self.stdout.write("Still running...")
                            time.sleep(2)
                            continue

                        if data.get('status') == 'Succeeded':
                            self.stdout.write("Operation succeeded. Getting result...")
                            # The result of the operation is usually obtained by appending /result to the operation URL
                            result_url = location + "/result"
                            res_resp = requests.get(result_url, headers=headers)
                            if res_resp.status_code == 200:
                                res_data = res_resp.json()
                                self.stdout.write(self.style.SUCCESS(f"Got result! keys: {res_data.keys()}"))
                                if 'definition' in res_data and 'parts' in res_data['definition']:
                                    self.stdout.write(f"Parts paths: {[p.get('path') for p in res_data['definition']['parts']]}")
                                    for part in res_data['definition']['parts']:
                                        if part.get('path') == 'definition/relationships.tmdl':
                                            payload = part.get('payload', '')
                                            import base64
                                            try:
                                                decoded = base64.b64decode(payload).decode('utf-8')
                                                self.stdout.write(self.style.SUCCESS(f"Found relationships.tmdl!"))
                                                print("--- RELATIONSHIPS.TMDL ---")
                                                print(decoded)
                                                print("--------------------------")
                                            except Exception as e:
                                                self.stdout.write(self.style.ERROR(f"Failed to decode relationships.tmdl: {e}"))
                                else:
                                    self.stdout.write(f"No definition in result: {res_data.keys()}")
                            else:
                                self.stdout.write(self.style.ERROR(f"Failed to get result: {res_resp.status_code} {res_resp.text}"))
                        break
                    else:
                        self.stdout.write(f"Polling status: {poll_resp.status_code}")
                    time.sleep(2)
        elif def_resp.status_code == 200:
            data = def_resp.json()
            self.stdout.write(self.style.SUCCESS("Got definition synchronously!"))
            # handle parts
        else:
            self.stdout.write(self.style.ERROR(f"getDefinition failed: {def_resp.status_code} {def_resp.text}"))


