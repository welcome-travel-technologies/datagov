"""
Synchronous PowerBI REST API client for the chatbot agent.

Reuses the same IntegrationSource credentials already configured in the
Integrations page. Token caching prevents redundant auth calls within a
single agent run.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import time

import requests

logger = logging.getLogger(__name__)

_BASE_URL = 'https://api.powerbi.com/v1.0/myorg'


class PowerBIAuthError(Exception):
    """Raised when OAuth2 authentication fails."""


class PowerBIRequestError(Exception):
    """Raised when a PowerBI API call returns a non-2xx response."""


class PowerBIClient:
    """
    Synchronous client for the PowerBI REST API.

    Instantiate once per agent run; the token is cached for the lifetime of
    the object and refreshed automatically when it expires.
    """

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        if not all([tenant_id, client_id, client_secret]):
            raise PowerBIAuthError('tenant_id, client_id, and client_secret are all required.')
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: Optional[str] = None
        self._token_expires: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _ensure_token(self) -> None:
        """Obtain or refresh the OAuth2 bearer token if necessary."""
        if self._access_token and self._token_expires and datetime.now() < self._token_expires:
            return

        auth_url = (
            f'https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token'
        )
        payload = {
            'client_id': self._client_id,
            'client_secret': self._client_secret,
            'scope': 'https://analysis.windows.net/powerbi/api/.default',
            'grant_type': 'client_credentials',
        }
        try:
            resp = requests.post(auth_url, data=payload, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise PowerBIAuthError(f'Authentication request failed: {exc}') from exc

        data = resp.json()
        token = data.get('access_token')
        if not token:
            raise PowerBIAuthError(f'No access_token in response: {data}')

        expires_in = int(data.get('expires_in', 3600))
        self._access_token = token
        # Subtract 60 s as a safety buffer
        self._token_expires = datetime.now() + timedelta(seconds=expires_in - 60)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        self._ensure_token()
        url = f'{_BASE_URL}{endpoint}'
        headers = {'Authorization': f'Bearer {self._access_token}'}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise PowerBIRequestError(f'GET {endpoint} failed: {exc}') from exc
        return resp.json()

    def _post(self, endpoint: str, body: Dict) -> Any:
        self._ensure_token()
        url = f'{_BASE_URL}{endpoint}'
        headers = {
            'Authorization': f'Bearer {self._access_token}',
            'Content-Type': 'application/json',
        }
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as exc:
            # The response body on a 400 carries the real reason (missing
            # column, bad table reference, syntax error). Surface it so
            # the DAX-gen retry has something useful to learn from.
            body_excerpt = ''
            resp = getattr(exc, 'response', None)
            if resp is not None:
                try:
                    body_excerpt = resp.text or ''
                except Exception:
                    body_excerpt = ''
                if len(body_excerpt) > 1000:
                    body_excerpt = body_excerpt[:1000] + '…'
            suffix = f' | body: {body_excerpt}' if body_excerpt else ''
            raise PowerBIRequestError(
                f'POST {endpoint} failed: {exc}{suffix}'
            ) from exc
        # Some endpoints (e.g. refresh) return 202 with no body
        try:
            return resp.json()
        except ValueError:
            return {'status_code': resp.status_code}

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_workspaces(self) -> list:
        """Return all workspace objects the service principal can access."""
        data = self._get('/groups')
        return data.get('value', [])

    def get_datasets(self, workspace_id: Optional[str] = None) -> list:
        """Return datasets for a workspace (or all accessible datasets)."""
        endpoint = f'/groups/{workspace_id}/datasets' if workspace_id else '/datasets'
        data = self._get(endpoint)
        return data.get('value', [])

    def get_dataset_tables(self, dataset_id: str, workspace_id: Optional[str] = None) -> list:
        """Return table definitions (with columns and measures) for a dataset."""
        if workspace_id:
            endpoint = f'/groups/{workspace_id}/datasets/{dataset_id}/tables'
        else:
            endpoint = f'/datasets/{dataset_id}/tables'
        data = self._get(endpoint)
        return data.get('value', [])

    def execute_dax_query(
        self,
        dataset_id: str,
        dax_query: str,
        workspace_id: Optional[str] = None,
    ) -> Dict:
        """Execute a DAX query and return the raw result dict."""
        if workspace_id:
            endpoint = f'/groups/{workspace_id}/datasets/{dataset_id}/executeQueries'
        else:
            endpoint = f'/datasets/{dataset_id}/executeQueries'

        body = {
            'queries': [{'query': dax_query}],
            'serializerSettings': {'includeNulls': True},
        }
        return self._post(endpoint, body)

    def refresh_dataset(
        self,
        dataset_id: str,
        workspace_id: Optional[str] = None,
    ) -> Dict:
        """Trigger an on-demand refresh for a dataset. Returns the API response."""
        if workspace_id:
            endpoint = f'/groups/{workspace_id}/datasets/{dataset_id}/refreshes'
        else:
            endpoint = f'/datasets/{dataset_id}/refreshes'
        return self._post(endpoint, {})

    def get_refresh_history(
        self,
        dataset_id: str,
        workspace_id: Optional[str] = None,
        top: int = 5,
    ) -> list:
        """Return the most recent refresh history entries for a dataset."""
        if workspace_id:
            endpoint = f'/groups/{workspace_id}/datasets/{dataset_id}/refreshes'
        else:
            endpoint = f'/datasets/{dataset_id}/refreshes'
        data = self._get(endpoint, params={'$top': top})
        return data.get('value', [])

    def get_reports(self, workspace_id: Optional[str] = None) -> list:
        """Return reports for a workspace (or all accessible reports)."""
        endpoint = f'/groups/{workspace_id}/reports' if workspace_id else '/reports'
        data = self._get(endpoint)
        return data.get('value', [])


# ------------------------------------------------------------------
# Factory — build from an IntegrationSource ORM object
# ------------------------------------------------------------------

def build_powerbi_client_for_org(organization) -> Optional['PowerBIClient']:
    """
    Given an Organization instance, look up its active powerbi_fabric
    IntegrationSource and return a ready PowerBIClient, or None if
    credentials are not configured.
    """
    from catalog.models import IntegrationSource  # local import avoids circular deps

    source = (
        IntegrationSource.objects
        .filter(
            organization=organization,
            source_type='powerbi_fabric',
            is_active=True,
        )
        .first()
    )
    if not source:
        return None
    if not all([source.tenant_id, source.client_id, source.client_secret]):
        return None

    return PowerBIClient(
        tenant_id=source.tenant_id,
        client_id=source.client_id,
        client_secret=source.client_secret,
    )
