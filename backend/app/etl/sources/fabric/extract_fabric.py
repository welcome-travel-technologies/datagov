import requests
import json
import os
import sys
import base64
from time import sleep

def run_fabric_extraction(tenant_id, client_id, client_secret, workspace_ids, etl_dir, log):
    """
    Connects to the Power BI / Fabric REST API using the provided credentials.
    Extracts Semantic Models and Reports for all specified workspaces.
    Then runs the transform script to generate the final CSVs.
    """
    if not all([tenant_id, client_id, client_secret]):
        raise ValueError('Missing PowerBI credentials (tenant_id, client_id, client_secret)')
    if not workspace_ids:
        raise ValueError('No workspace IDs configured for this source')

    log(f'Authenticating with Azure AD (tenant: {tenant_id})...')
    authority_url = f'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token'
    data = {
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': 'https://analysis.windows.net/powerbi/api/.default',
        'grant_type': 'client_credentials'
    }
    
    resp = requests.post(authority_url, data=data)
    if resp.status_code != 200:
        raise ValueError(f'Authentication failed: {resp.text}')
    
    token = resp.json().get('access_token')
    if not token:
        raise ValueError('Authentication failed: No access_token in response')
        
    log('✅ Authentication successful.')

    headers = {'Authorization': f'Bearer {token}'}
    IGNORE_ITEMS = ['Usage Metrics Report', 'Report Usage Metrics Model']

    for ws_id in workspace_ids:
        log(f'\nScanning workspace: {ws_id}')

        # Semantic Models
        model_resp = requests.get(
            f'https://api.fabric.microsoft.com/v1/workspaces/{ws_id}/items?type=SemanticModel',
            headers=headers
        )
        if model_resp.status_code == 200:
            for ds in model_resp.json().get('value', []):
                if ds['displayName'] in IGNORE_ITEMS:
                    continue
                log(f'  [MODEL] {ds["displayName"]}')
                _fetch_fabric_definition(ws_id, ds['id'], ds['displayName'], 'SemanticModel', headers, etl_dir, ds, log)

        # Reports
        report_resp = requests.get(
            f'https://api.fabric.microsoft.com/v1/workspaces/{ws_id}/items?type=Report',
            headers=headers
        )
        if report_resp.status_code == 200:
            for rpt in report_resp.json().get('value', []):
                if rpt['displayName'] in IGNORE_ITEMS:
                    continue
                log(f'  [REPORT] {rpt["displayName"]}')
                _fetch_fabric_definition(ws_id, rpt['id'], rpt['displayName'], 'Report', headers, etl_dir, rpt, log)

    # Per-workspace report usage (last N months) via 'Report Usage Metrics Model'.
    # Independent of the catalog extract — wrap so a failure here doesn't block
    # the rest of the pipeline (e.g. workspace without the usage dataset).
    try:
        from etl.sources.fabric.extract_usage import run_usage_extraction
        run_usage_extraction(
            token=token,
            workspace_ids=workspace_ids,
            etl_dir=etl_dir,
            log=log,
            months=3,
        )
    except Exception as e:
        log(f'  [WARNING] Usage extraction failed: {e}')

    log('\nRunning transform...')

    # We must append the etl_dir to sys.path so it can find its internal dependencies if needed
    if etl_dir not in sys.path:
        sys.path.append(etl_dir)
        
    import transform_fabric
    
    # To capture print statements from the transform script
    class LogCapture:
        def write(self, text):
            if text.strip():
                log(f'    {text.strip()}')
        def flush(self):
            pass
            
    old_stdout = sys.stdout
    sys.stdout = LogCapture()
    
    try:
        transform_fabric.main()
    finally:
        sys.stdout = old_stdout
        
    log('✅ Transform complete.')


def _fetch_fabric_definition(workspace_id, item_id, item_name, item_type, headers, etl_dir, item_metadata, log):
    """
    Downloads the TMDL/JSON definitions for a Fabric item and saves them to the etl_dir.
    """
    url = f'https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{item_id}/getDefinition'
    resp = requests.post(url, headers=headers)

    if resp.status_code not in (200, 202):
        log(f'    [WARNING] GetDefinition failed (HTTP {resp.status_code})')
        return None

    if resp.status_code == 202:
        operation_url = resp.headers.get('Location')
        if not operation_url:
            return None
        while True:
            sleep(2)
            op_resp = requests.get(operation_url, headers=headers)
            if op_resp.status_code != 200:
                continue
            op_data = op_resp.json()
            status = op_data.get('status')
            if status == 'Succeeded':
                break
            elif status == 'Failed':
                return None

        if 'resourceLocation' in op_data:
            final_resp = requests.get(op_data['resourceLocation'], headers=headers)
            definition_data = final_resp.json().get('definition', {})
        elif 'definition' in op_data:
            definition_data = op_data.get('definition', {})
        else:
            result_resp = requests.get(operation_url + '/result', headers=headers)
            definition_data = result_resp.json().get('definition', {}) if result_resp.status_code == 200 else {}
    else:
        definition_data = resp.json().get('definition', {})

    parts = definition_data.get('parts', [])
    if parts:
        safe_name = ''.join([c if c.isalnum() else '_' for c in item_name])
        type_folder = 'Reports' if item_type == 'Report' else 'SemanticModels'
        dump_dir = os.path.join(etl_dir, 'raw_fabric_definitions', workspace_id, type_folder, safe_name)
        os.makedirs(dump_dir, exist_ok=True)

        for part in parts:
            path = part.get('path', 'unknown_file')
            payload = part.get('payload', '')
            full_path = os.path.join(dump_dir, path.replace('/', os.sep))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            try:
                decoded = base64.b64decode(payload).decode('utf-8')
            except Exception:
                decoded = payload
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(decoded)

        if item_metadata:
            with open(os.path.join(dump_dir, 'item_metadata.json'), 'w', encoding='utf-8') as f:
                json.dump(item_metadata, f, ensure_ascii=False, indent=2)

    return parts