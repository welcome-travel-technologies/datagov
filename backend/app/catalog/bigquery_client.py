"""
Small BigQuery client wrapper for chatbot tools.

The wrapper is intentionally read-oriented: it builds a Google BigQuery client
from the active BigQuery IntegrationDestination for an organization, exposes
schema/listing helpers, and lets tools execute only guarded SELECT/WITH queries.
"""
import json
from typing import Optional


class BigQueryConfigError(Exception):
    """Raised when BigQuery integration credentials/config are incomplete."""


class BigQueryClient:
    """Thin synchronous wrapper around ``google.cloud.bigquery.Client``."""

    def __init__(self, google_client, project_id: str, default_dataset_id: str = '') -> None:
        self.client = google_client
        self.project_id = project_id
        self.default_dataset_id = default_dataset_id or ''

    def list_datasets(self, max_results: int = 50) -> list:
        return list(self.client.list_datasets(project=self.project_id, max_results=max_results))

    def list_tables(self, dataset_id: str, max_results: int = 100) -> list:
        dataset_ref = self._normalize_dataset_ref(dataset_id)
        return list(self.client.list_tables(dataset_ref, max_results=max_results))

    def get_table(self, table_fqn: str):
        return self.client.get_table(self._normalize_table_ref(table_fqn))

    def dry_run_query(self, sql: str, maximum_bytes_billed: int):
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            maximum_bytes_billed=maximum_bytes_billed,
        )
        return self.client.query(sql, job_config=job_config)

    def execute_query(self, sql: str, maximum_bytes_billed: int, timeout: int = 60) -> list[dict]:
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            use_query_cache=True,
            maximum_bytes_billed=maximum_bytes_billed,
        )
        job = self.client.query(sql, job_config=job_config)
        rows = job.result(timeout=timeout)
        return [dict(row.items()) for row in rows]

    def _normalize_dataset_ref(self, dataset_id: str) -> str:
        value = (dataset_id or self.default_dataset_id or '').strip().strip('`')
        if not value:
            raise BigQueryConfigError('dataset_id is required.')
        if '.' in value:
            return value
        return f'{self.project_id}.{value}'

    def _normalize_table_ref(self, table_fqn: str) -> str:
        value = (table_fqn or '').strip().strip('`')
        if not value:
            raise BigQueryConfigError('table_fqn is required.')
        parts = value.split('.')
        if len(parts) == 3:
            return value
        if len(parts) == 2:
            return f'{self.project_id}.{value}'
        if len(parts) == 1 and self.default_dataset_id:
            return f'{self.project_id}.{self.default_dataset_id}.{value}'
        raise BigQueryConfigError('table_fqn must be table, dataset.table, or project.dataset.table.')


def build_bigquery_client_for_org(organization) -> Optional[BigQueryClient]:
    """
    Build a BigQueryClient from the active BigQuery destination for an org.

    Returns ``None`` when the destination is inactive or credentials are missing.
    """
    from google.cloud import bigquery
    from google.oauth2 import service_account
    from catalog.models import IntegrationDestination

    dest = (
        IntegrationDestination.objects
        .filter(organization=organization, destination_type='bigquery', is_active=True)
        .first()
    )
    if not dest:
        return None
    if not all([dest.bq_project_id, dest.bq_service_account_json]):
        return None

    try:
        sa_info = json.loads(dest.bq_service_account_json)
    except json.JSONDecodeError as exc:
        raise BigQueryConfigError('BigQuery service account JSON is invalid.') from exc

    credentials = service_account.Credentials.from_service_account_info(sa_info)
    google_client = bigquery.Client(project=dest.bq_project_id, credentials=credentials, location='EU')
    return BigQueryClient(
        google_client=google_client,
        project_id=dest.bq_project_id,
        default_dataset_id=dest.bq_dataset_id or '',
    )