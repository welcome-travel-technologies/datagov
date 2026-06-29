import os
import json
import pandas as pd
from django.utils import timezone as tz

def push_to_bigquery(dest, data_dir, log):
    """
    Pushes CSV files generated from the ETL pipeline to the specified BigQuery destination.
    """
    if not dest.bq_service_account_json or not dest.bq_dataset_id:
        log('  [SKIP] BigQuery destination missing service account JSON or dataset ID.')
        return

    started = tz.now()
    try:
        sa_info = json.loads(dest.bq_service_account_json)
        project_id = sa_info.get('project_id') or dest.bq_project_id
        if not project_id:
            log('  [ERROR] Cannot determine BigQuery project_id.')
            return

        from google.oauth2 import service_account
        from google.cloud import bigquery
        from google.api_core.exceptions import NotFound

        credentials = service_account.Credentials.from_service_account_info(sa_info)
        client = bigquery.Client(project=project_id, credentials=credentials, location="EU")

        dataset_id_full = f"{project_id}.{dest.bq_dataset_id}"
        dataset = bigquery.Dataset(dataset_id_full)
        dataset.location = "EU"  # Defaulting to EU
        
        try:
            client.create_dataset(dataset, timeout=30, exists_ok=True)
            log(f"  [INFO] Ensured dataset {dataset_id_full} exists in EU region.")
        except Exception as e:
            log(f"  [WARNING] Could not create/verify dataset {dataset_id_full} (might lack permissions): {e}")

        # If data_dir is None, we fetch directly from Django ORM (independent destination push)
        if data_dir is None:
            from django.db.models import F
            from catalog.models import Item, NetworkNode, NetworkEdge, PowerBIReportUsage

            # Governance lives on ItemGroup (not Item), so .values() alone
            # would only expose item_group_id. Pull the human-readable values
            # through the relation so BigQuery gets owner/steward/etc.
            # `status` is now a real (synced) column on Item, so it comes
            # through .values() directly — annotating it would collide with the
            # model field. The remaining governance fields live only on the
            # group, hence the annotations.
            items_qs = Item.objects.annotate(
                ownership_department=F('item_group__ownership_department__name'),
                ownership_person=F('item_group__ownership_person__name'),
                steward=F('item_group__steward__name'),
                category=F('item_group__category__name'),
                custom_description=F('item_group__custom_description'),
            ).values()

            data_frames = {
                'catalog_items': pd.DataFrame.from_records(items_qs),
                'catalog_powerbireportusage': pd.DataFrame.from_records(PowerBIReportUsage.objects.all().values()),
                # 'network_nodes': pd.DataFrame.from_records(NetworkNode.objects.all().values()),
                # 'network_edges': pd.DataFrame.from_records(NetworkEdge.objects.all().values())
            }
            
            for table_name, df in data_frames.items():
                if df.empty:
                    log(f'  [WARNING] No data found in {table_name}, skipping.')
                    continue
                    
                for col in df.select_dtypes(include=['object']).columns:
                    df[col] = df[col].astype('string')
                    
                table_id = f'{project_id}.{dest.bq_dataset_id}.{table_name}'
                log(f'  Uploading {table_name} → {table_id} ({len(df)} rows)...')
                job_config = bigquery.LoadJobConfig(write_disposition='WRITE_TRUNCATE')
                job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
                job.result()
                log(f'  ✅ {len(df)} rows uploaded to {table_id} (Replaced existing data)')
                
        else:
            if not os.path.exists(data_dir):
                log(f'  [WARNING] Data directory not found: {data_dir}')
                return

            csv_files = [f for f in os.listdir(data_dir) if f.startswith('fabric_info_') and f.endswith('.csv')]
            if not csv_files:
                log('  [WARNING] No CSV files found to push.')
                return

            for csv_file in csv_files:
                table_name = csv_file.replace('.csv', '')
                df = pd.read_csv(os.path.join(data_dir, csv_file))
                for col in df.select_dtypes(include=['object']).columns:
                    df[col] = df[col].astype('string')

                table_id = f'{project_id}.{dest.bq_dataset_id}.{table_name}'
                log(f'  Uploading {csv_file} → {table_id} ({len(df)} rows)...')
                job_config = bigquery.LoadJobConfig(write_disposition='WRITE_TRUNCATE')
                job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
                job.result()
                log(f'  ✅ {len(df)} rows uploaded to {table_id} (Replaced existing data)')

        duration = int((tz.now() - started).total_seconds())
        return {'status': 'success', 'duration': duration}

    except Exception as e:
        log(f'  [ERROR] BigQuery push failed: {str(e)}')
        return {'status': 'failed', 'error': str(e)}
