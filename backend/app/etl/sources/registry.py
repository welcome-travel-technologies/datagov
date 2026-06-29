"""
ETL Source Registry
===================
Each integration type is a self-contained class that implements two methods:

    test()     -> dict  {status: 'ok'|'fail', lines: [...]}
                        Lightweight connectivity check — no data stored.

    extract(etl_dir, log)
                        Full extraction + transform.  Calls the existing
                        extract_* functions that live next to this file.

To add a new source type:
  1. Create a new subclass of BaseSource below.
  2. Add it to SOURCE_REGISTRY at the bottom of this file.
  3. The views, tasks, and management commands pick it up automatically.
"""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from typing import Callable


# ─────────────────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class BaseSource(ABC):
    """Common interface every integration source must implement."""

    # Override in subclasses to specify the management command used to load
    # extracted data into the Django database. The workflow orchestrator calls
    # ``call_command(self.load_command, organization_id=..., stdout=...)``
    # so new sources only need to provide a command name — no workflow edits.
    load_command: str = 'load_data'

    # Pipeline layer this source belongs to. The workflow orchestrator runs
    # all 'transformation' sources to completion before starting any
    # 'visualization' source, mirroring ETL ordering (warehouse must be
    # reshaped before BI tools read it).
    category: str = 'visualization'

    @classmethod
    @abstractmethod
    def from_model(cls, source) -> "BaseSource":
        """Construct an instance from an IntegrationSource Django model."""

    @classmethod
    def get_etl_dir(cls) -> str:
        """Return the absolute path to this source's ETL directory.
        Override in subclasses if the ETL data lives elsewhere."""
        import os
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), 'fabric')
        )

    @classmethod
    def get_raw_dirs(cls, etl_dir: str) -> list[str]:
        """Return absolute paths to the folders this source produces during
        extraction. Used by the GCS raw-export feature: each folder becomes a
        top-level directory inside the archive, so both the rawest pre-transform
        inputs (for replay) and the post-transform CSVs (the data that lands
        in BigQuery) are captured together. Default: empty (nothing to archive)."""
        return []

    @abstractmethod
    def test(self) -> dict:
        """
        Lightweight connectivity check (no data downloaded).
        Returns::
            {"status": "ok"|"fail", "lines": ["line1", "line2", ...]}
        """

    @abstractmethod
    def extract(self, etl_dir: str, log: Callable[[str], None]) -> None:
        """
        Full extraction + transform.
        *log* is a callable that accepts a single string message.
        """


# ─────────────────────────────────────────────────────────────────────────────
# PowerBI / Microsoft Fabric
# ─────────────────────────────────────────────────────────────────────────────

class FabricSource(BaseSource):
    """Microsoft Fabric / Power BI REST API source."""

    category = 'visualization'

    def __init__(self, tenant_id: str, client_id: str, client_secret: str,
                 workspace_ids: list):
        self.tenant_id     = tenant_id
        self.client_id     = client_id
        self.client_secret = client_secret
        self.workspace_ids = workspace_ids or []

    @classmethod
    def from_model(cls, source) -> "FabricSource":
        return cls(
            tenant_id     = source.tenant_id or '',
            client_id     = source.client_id or '',
            client_secret = source.client_secret or '',
            workspace_ids = source.workspace_ids or [],
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        """Authenticate with Azure AD and return the access token."""
        import requests
        missing = [n for n, v in [
            ('tenant_id',     self.tenant_id),
            ('client_id',     self.client_id),
            ('client_secret', self.client_secret),
        ] if not v]
        if missing:
            raise ValueError(f'Missing credentials: {", ".join(missing)}')

        url = f'https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token'
        resp = requests.post(url, data={
            'client_id':     self.client_id,
            'client_secret': self.client_secret,
            'scope':         'https://analysis.windows.net/powerbi/api/.default',
            'grant_type':    'client_credentials',
        }, timeout=15)

        if resp.status_code != 200:
            raise ValueError(f'Auth failed (HTTP {resp.status_code}): {resp.text[:200]}')
        token = resp.json().get('access_token')
        if not token:
            raise ValueError('Auth failed: no access_token in response')
        return token

    # ── interface ─────────────────────────────────────────────────────────────

    def test(self) -> dict:
        import requests
        lines = []
        try:
            lines.append(f'Authenticating with Azure AD (tenant: {self.tenant_id})...')
            token = self._get_token()
            lines.append('Authentication OK')

            if not self.workspace_ids:
                lines.append('No workspace IDs configured — skipping workspace check')
            else:
                headers = {'Authorization': f'Bearer {token}'}
                for ws_id in self.workspace_ids:
                    lines.append(f'Checking workspace {ws_id}...')
                    r = requests.get(
                        f'https://api.fabric.microsoft.com/v1/workspaces/{ws_id}/items?type=SemanticModel',
                        headers=headers, timeout=15,
                    )
                    if r.status_code == 200:
                        count = len(r.json().get('value', []))
                        lines.append(f'OK — {count} semantic model(s) found')
                    else:
                        raise ValueError(f'Workspace {ws_id}: HTTP {r.status_code} — {r.text[:200]}')

        except Exception as e:
            lines.append(f'Error: {e}')
            return {'status': 'fail', 'lines': lines}

        return {'status': 'ok', 'lines': lines}

    def extract(self, etl_dir: str, log: Callable[[str], None]) -> None:
        from etl.sources.fabric.extract_fabric import run_fabric_extraction
        run_fabric_extraction(
            tenant_id     = self.tenant_id,
            client_id     = self.client_id,
            client_secret = self.client_secret,
            workspace_ids = self.workspace_ids,
            etl_dir       = etl_dir,
            log           = log,
        )

    @classmethod
    def get_raw_dirs(cls, etl_dir: str) -> list[str]:
        import os
        return [
            os.path.join(etl_dir, 'raw_fabric_definitions'),
            os.path.join(etl_dir, 'data'),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# dbt / GitHub
# ─────────────────────────────────────────────────────────────────────────────

class DbtSource(BaseSource):
    """dbt project via GitHub repository."""

    load_command = 'load_dbt_data'
    category = 'transformation'

    @classmethod
    def get_etl_dir(cls) -> str:
        import os
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), 'dbt')
        )

    def __init__(self, github_repo_url: str, github_token: str,
                 github_branch: str, dbt_manifest_path: str):
        self.github_repo_url    = github_repo_url or ''
        self.github_token       = github_token or ''
        self.github_branch      = github_branch or 'main'
        self.dbt_manifest_path  = dbt_manifest_path or 'target/manifest.json'

    @classmethod
    def from_model(cls, source) -> "DbtSource":
        return cls(
            github_repo_url   = source.github_repo_url or '',
            github_token      = source.github_token or '',
            github_branch     = source.github_branch or 'main',
            dbt_manifest_path = source.dbt_manifest_path or 'target/manifest.json',
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _auth_url(self) -> str:
        """Return the clone URL with the PAT embedded as username:password.
        Uses TOKEN:x-oauth-basic which is the official GitHub basic-auth PAT format,
        supported by all git versions and both classic (ghp_*) and fine-grained PATs."""
        url = self.github_repo_url.rstrip('/')
        if self.github_token and url.startswith('https://'):
            # Remove any pre-existing auth fragment
            host_path = url[len('https://'):]
            if '@' in host_path:
                host_path = host_path.split('@', 1)[1]
            url = f'https://{self.github_token}:x-oauth-basic@{host_path}'
        return url

    # ── interface ─────────────────────────────────────────────────────────────

    def test(self) -> dict:
        import os, subprocess as sp
        lines = []
        try:
            if not self.github_repo_url:
                raise ValueError('Missing github_repo_url — save a repository URL first')

            lines.append(f'Repository : {self.github_repo_url.rstrip("/")}')
            lines.append(f'Branch     : {self.github_branch}')
            lines.append('Running git ls-remote...')

            result = sp.run(
                ['git', 'ls-remote', '--heads',
                 self._auth_url(),
                 f'refs/heads/{self.github_branch}'],
                capture_output=True, text=True, timeout=30,
                stdin=sp.DEVNULL,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
            )
            stderr_clean = (result.stderr or '').replace(self.github_token, '***') \
                if self.github_token else (result.stderr or '')

            if result.returncode != 0:
                raise ValueError(f'git ls-remote failed: {stderr_clean.strip()[:300]}')
            if result.stdout.strip():
                lines.append(f'Repository accessible — branch "{self.github_branch}" found')
            else:
                raise ValueError(
                    f'Repository reachable but branch "{self.github_branch}" not found. '
                    f'Check the github_branch field.'
                )

        except Exception as e:
            lines.append(f'Error: {e}')
            return {'status': 'fail', 'lines': lines}

        return {'status': 'ok', 'lines': lines}

    def extract(self, etl_dir: str, log: Callable[[str], None]) -> None:
        from etl.sources.dbt.extract_dbt import run_dbt_extraction
        run_dbt_extraction(
            github_repo_url   = self.github_repo_url,
            github_token      = self.github_token,
            github_branch     = self.github_branch,
            dbt_manifest_path = self.dbt_manifest_path,
            etl_dir           = etl_dir,
            log               = log,
        )

    @classmethod
    def get_raw_dirs(cls, etl_dir: str) -> list[str]:
        import os
        return [
            os.path.join(etl_dir, 'dbt_artifacts'),
            os.path.join(etl_dir, 'data'),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Registry & factory
# ─────────────────────────────────────────────────────────────────────────────

SOURCE_REGISTRY: dict[str, type[BaseSource]] = {
    'powerbi_fabric': FabricSource,
    'dbt':            DbtSource,
}


def get_source(source_model) -> BaseSource:
    """
    Factory: given an IntegrationSource Django model instance,
    return the appropriate BaseSource subclass, fully initialised.

    Raises ValueError for unknown source types.
    """
    cls = SOURCE_REGISTRY.get(source_model.source_type)
    if cls is None:
        raise ValueError(f'Unknown source type: {source_model.source_type!r}. '
                         f'Registered: {list(SOURCE_REGISTRY)}')
    return cls.from_model(source_model)
