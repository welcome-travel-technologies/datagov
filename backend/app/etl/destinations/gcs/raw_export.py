"""GCS raw-export uploader.

Zips a source's raw extracted files in-memory and uploads the archive to
``gs://{bucket}/{org_slug}/{source_name}/{run_id}/{source_name}_{datetime}.zip``.

The configuration lives on ``WorkflowRawExport`` (one per organization). When
``is_active`` is False or credentials are missing, callers should skip the
upload — this module raises on any actual misconfiguration so the caller can
log the error without failing the whole workflow.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Callable


def _slugify(value: str) -> str:
    return re.sub(r'[^a-z0-9_.-]+', '_', (value or '').strip().lower()) or 'unnamed'


def _zip_dirs(raw_dirs: list[str]) -> tuple[bytes, int]:
    """Zip the contents of each directory in raw_dirs into a single in-memory
    archive. Each directory's basename becomes the top-level folder inside the
    zip, so the archive can be extracted directly back into the source's
    etl_dir to replay the transform.

    Returns (zip_bytes, file_count). Missing directories are skipped silently."""
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for raw_dir in raw_dirs:
            if not raw_dir or not os.path.isdir(raw_dir):
                continue
            top = os.path.basename(os.path.normpath(raw_dir))
            for dirpath, _, filenames in os.walk(raw_dir):
                for fn in filenames:
                    abs_path = os.path.join(dirpath, fn)
                    rel = os.path.relpath(abs_path, raw_dir).replace(os.sep, '/')
                    zf.write(abs_path, arcname=f'{top}/{rel}')
                    count += 1
    return buf.getvalue(), count


def upload_source_raw_to_gcs(
    raw_export,
    org,
    source,
    raw_dirs: list[str],
    log: Callable[[str], None],
) -> None:
    """Zip the source's pre-transform raw folders and upload to GCS.

    Path layout: raw/{org}/{source}/{source}_{yyyymmdd_hhmmss}.zip (all lowercase).
    The zip preserves each raw folder's name as a top-level directory so it
    can be extracted directly back into etl_dir/ for re-running the transform.
    """
    if not raw_export or not raw_export.is_active:
        return
    if not raw_export.gcs_bucket_name or not raw_export.gcs_service_account_json:
        log('  [Raw Export] Skipped — bucket or service account not configured.')
        return
    if not raw_dirs:
        log(f'  [Raw Export] Skipped — source {source.name} declares no raw dirs.')
        return

    try:
        sa_info = json.loads(raw_export.gcs_service_account_json)
    except json.JSONDecodeError as e:
        log(f'  [Raw Export] Invalid service account JSON: {e}')
        return

    try:
        from google.oauth2 import service_account
        from google.cloud import storage
    except ImportError:
        log('  [Raw Export] google-cloud-storage is not installed.')
        return

    zip_bytes, file_count = _zip_dirs(raw_dirs)
    if file_count == 0:
        log(f'  [Raw Export] Skipped — no files in raw dirs for {source.name}.')
        return

    org_slug = _slugify(getattr(org, 'slug', None) or org.name)
    source_slug = _slugify(source.source_type)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    object_name = f'raw/{org_slug}/{source_slug}/{source_slug}_{timestamp}.zip'

    try:
        credentials = service_account.Credentials.from_service_account_info(sa_info)
        client = storage.Client(project=sa_info.get('project_id'), credentials=credentials)
        bucket = client.bucket(raw_export.gcs_bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_string(zip_bytes, content_type='application/zip')
    except Exception as e:
        log(f'  [Raw Export] Upload failed for {source.name}: {e}')
        return

    log(
        f'  [Raw Export] Uploaded {file_count} file(s) → '
        f'gs://{raw_export.gcs_bucket_name}/{object_name} '
        f'({len(zip_bytes) / 1024:.1f} KB)'
    )


def test_gcs_connection(bucket_name: str, service_account_json: str) -> dict:
    """Lightweight connectivity test for the configured GCS bucket.
    Verifies credentials parse, bucket is reachable, and a probe object can be
    written then deleted."""
    lines: list[str] = []
    try:
        if not service_account_json:
            raise ValueError('No service account JSON saved — paste it and click Save first')
        if not bucket_name:
            raise ValueError('Missing bucket name')

        try:
            sa_info = json.loads(service_account_json)
        except json.JSONDecodeError as e:
            raise ValueError(f'Invalid service account JSON: {e}')

        client_email = sa_info.get('client_email', '(unknown)')
        project_id = sa_info.get('project_id', '(unknown)')
        lines.append(f'Project : {project_id}')
        lines.append(f'Bucket  : {bucket_name}')
        lines.append(f'Account : {client_email}')

        from google.oauth2 import service_account
        from google.cloud import storage

        credentials = service_account.Credentials.from_service_account_info(sa_info)
        lines.append('Credentials OK')

        client = storage.Client(project=sa_info.get('project_id'), credentials=credentials)
        lines.append('Connecting to Cloud Storage...')

        bucket = client.bucket(bucket_name)
        if not bucket.exists():
            raise ValueError(f'Bucket {bucket_name} does not exist or is not visible to this account')
        lines.append(f'Bucket {bucket_name} reachable')

        probe_name = f'_welcome_data_catalog_probe_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}.txt'
        blob = bucket.blob(probe_name)
        blob.upload_from_string(b'probe', content_type='text/plain')
        lines.append('Write OK')
        try:
            blob.delete()
            lines.append('Delete OK')
        except Exception as e:
            lines.append(f'Delete failed (non-fatal): {e}')

    except Exception as e:
        lines.append(f'Error: {e}')
        return {'status': 'fail', 'lines': lines}

    return {'status': 'ok', 'lines': lines}
