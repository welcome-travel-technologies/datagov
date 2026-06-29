"""
ETL Destination Registry
========================
Mirror of etl/sources/registry.py but for destinations.

Each destination type is a self-contained class with two methods:

    test()   -> dict  {status: 'ok'|'fail', lines: [...]}
                      Lightweight connectivity check — no data written.

    push(dest_model, log)
                      Full push (delegates to the existing push_* functions).

To add a new destination type:
  1. Subclass BaseDestination below.
  2. Add it to DESTINATION_REGISTRY.
  3. Views, tasks, and management commands pick it up automatically.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Callable


# ─────────────────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class BaseDestination(ABC):

    @classmethod
    @abstractmethod
    def from_model(cls, dest) -> "BaseDestination":
        """Construct an instance from an IntegrationDestination Django model."""

    @abstractmethod
    def test(self) -> dict:
        """
        Lightweight connectivity check (no data written).
        Returns::
            {"status": "ok"|"fail", "lines": ["line1", ...]}
        """

    @abstractmethod
    def push(self, dest_model, log: Callable[[str], None]) -> dict:
        """Full push.  *log* accepts a single string message."""


# ─────────────────────────────────────────────────────────────────────────────
# BigQuery
# ─────────────────────────────────────────────────────────────────────────────

class BigQueryDestination(BaseDestination):
    """Google BigQuery destination."""

    def __init__(self, bq_service_account_json: str, bq_dataset_id: str,
                 bq_project_id: str):
        self.bq_service_account_json = bq_service_account_json or ''
        self.bq_dataset_id           = bq_dataset_id or ''
        self.bq_project_id           = bq_project_id or ''

    @classmethod
    def from_model(cls, dest) -> "BigQueryDestination":
        return cls(
            bq_service_account_json = dest.bq_service_account_json or '',
            bq_dataset_id           = dest.bq_dataset_id or '',
            bq_project_id           = dest.bq_project_id or '',
        )

    # ── interface ─────────────────────────────────────────────────────────────

    def test(self) -> dict:
        lines = []
        try:
            if not self.bq_service_account_json:
                raise ValueError('No service account JSON saved — paste it and click Save first')
            if not self.bq_dataset_id:
                raise ValueError('Missing dataset ID')

            # Parse JSON
            try:
                sa_info = json.loads(self.bq_service_account_json)
            except json.JSONDecodeError as e:
                raise ValueError(f'Invalid service account JSON: {e}')

            project_id = sa_info.get('project_id') or self.bq_project_id
            if not project_id:
                raise ValueError('Cannot determine project_id from service account JSON')

            client_email = sa_info.get('client_email', '(unknown)')
            lines.append(f'Project  : {project_id}')
            lines.append(f'Dataset  : {self.bq_dataset_id}')
            lines.append(f'Account  : {client_email}')
            lines.append('Building credentials...')

            from google.oauth2 import service_account
            from google.cloud import bigquery

            credentials = service_account.Credentials.from_service_account_info(sa_info)
            lines.append('Credentials OK')

            lines.append('Connecting to BigQuery...')
            client = bigquery.Client(
                project=project_id,
                credentials=credentials,
                location='EU',
            )

            # Lightweight check: list datasets (max 1)
            lines.append(f'Checking project {project_id}...')
            datasets = list(client.list_datasets(max_results=1))
            lines.append(f'Connection OK — project accessible ({len(datasets)} dataset(s) visible)')

            # Check if the target dataset exists
            dataset_ref = f'{project_id}.{self.bq_dataset_id}'
            lines.append(f'Checking dataset {dataset_ref}...')
            try:
                client.get_dataset(dataset_ref)
                lines.append(f'Dataset {self.bq_dataset_id} exists')
            except Exception:
                lines.append(
                    f'Dataset {self.bq_dataset_id} does not exist yet '
                    f'(will be created on first push)'
                )

        except Exception as e:
            lines.append(f'Error: {e}')
            return {'status': 'fail', 'lines': lines}

        return {'status': 'ok', 'lines': lines}

    def push(self, dest_model, log: Callable[[str], None]) -> dict:
        from etl.destinations.bigquery.push_to_bigquery import push_to_bigquery
        return push_to_bigquery(dest_model, None, log)


# ─────────────────────────────────────────────────────────────────────────────
# Registry & factory
# ─────────────────────────────────────────────────────────────────────────────

DESTINATION_REGISTRY: dict[str, type[BaseDestination]] = {
    'bigquery': BigQueryDestination,
}


def get_destination(dest_model) -> BaseDestination:
    """
    Factory: given an IntegrationDestination Django model instance,
    return the appropriate BaseDestination subclass, fully initialised.
    """
    cls = DESTINATION_REGISTRY.get(dest_model.destination_type)
    if cls is None:
        raise ValueError(
            f'Unknown destination type: {dest_model.destination_type!r}. '
            f'Registered: {list(DESTINATION_REGISTRY)}'
        )
    return cls.from_model(dest_model)
