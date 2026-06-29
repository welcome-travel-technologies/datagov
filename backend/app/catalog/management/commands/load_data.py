import os
import pandas as pd
from django.core.management.base import BaseCommand
from catalog.models import Summary, Item, NetworkNode, NetworkEdge
from django.conf import settings
from django.db import connection, transaction

class Command(BaseCommand):
    help = 'Load data from CSVs into the database'

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', type=int, default=None,
                            help='Organization PK to assign to loaded items')
        parser.add_argument('--source-id', type=int, default=None,
                            help='IntegrationSource PK that produced this data')

    def handle(self, *args, **kwargs):
        self.organization_id = kwargs.get('organization_id')
        self.source_id = kwargs.get('source_id')
        # Build the organization_id literal for SQL injection-safe usage (used throughout)
        org_id_literal = 'NULL'
        if self.organization_id is not None:
            org_id_literal = str(int(self.organization_id))
        source_id_literal = 'NULL'
        if self.source_id is not None:
            source_id_literal = str(int(self.source_id))
        data_dir = os.path.join(settings.BASE_DIR, 'etl', 'sources', 'fabric', 'data')
        items_csv = os.path.join(data_dir, 'fabric_info_items.csv')
        # The transform stage writes `fabric_info_graph.csv` with the schema:
        #   source_id, source, source_type, target_id, target, target_type, workspace_id
        # where *_id columns carry composite unique ids of the form "{TYPE}::{item_id_hash}".
        graph_csv = os.path.join(data_dir, 'fabric_info_graph.csv')
        # Per-workspace × report × user × month usage (from each workspace's
        # 'Report Usage Metrics Model' v2 dataset). Optional — present only when
        # extract_usage.run_usage_extraction succeeded.
        usage_csv = os.path.join(data_dir, 'fabric_info_usage.csv')

        if not os.path.exists(items_csv):
            self.stdout.write(self.style.ERROR(f'items CSV not found at {items_csv}'))
            return
            
        if not os.path.exists(graph_csv):
            self.stdout.write(self.style.ERROR(f'graph CSV not found at {graph_csv}'))
            return

        self.stdout.write('Cleaning up non-persistent Fabric/PowerBI network data...')
        # Only delete non-dbt network data (preserve dbt graph when PowerBI re-runs).
        # All dbt-side nodes have group/node_id starting with 'DBT_' (DBT_MODEL,
        # DBT_SOURCE, DBT_SEED, DBT_TEST, DBT_COLUMN) so a single startswith
        # filter is sufficient.
        from django.db.models import Q
        NetworkNode.objects.exclude(group__startswith='DBT_').delete()
        # Delete non-dbt edges
        NetworkEdge.objects.exclude(
            Q(source__startswith='DBT_') | Q(target__startswith='DBT_')
        ).delete()
        # Also delete cross-tool bridge edges (they will be rebuilt by the final step)
        NetworkEdge.objects.filter(
            Q(source__startswith='DBT_') & ~Q(target__startswith='DBT_')
        ).delete()

        # ==========================================
        # FAST POSTGRESQL PATH (Using COPY)
        # ==========================================
        if True:  # Always PostgreSQL in production
            self.stdout.write('Using blazing fast PostgreSQL COPY for bulk loading...')
            
            with transaction.atomic(), connection.cursor() as cursor:
                # 1. Network Nodes & Edges
                # The temp table mirrors the new graph CSV schema, which carries
                # both composite unique ids (source_id/target_id of the form
                # "{TYPE}::{hash}") and human-readable names (source/target).
                # Build the temp table from the CSV header so newer columns
                # (edge_kind, lineage_type) load positionally and older CSVs
                # without them still work.
                with open(graph_csv, 'r', encoding='utf-8-sig') as f:
                    g_header = f.readline().rstrip('\n').rstrip('\r')
                g_cols = [c.strip().lower() for c in g_header.split(',')]
                g_safe = [f'"{c}"' if c else '"col_blank"' for c in g_cols]
                g_ddl = ",\n                        ".join(f"{sc} text" for sc in g_safe)

                cursor.execute("DROP TABLE IF EXISTS temp_nodes;")
                cursor.execute(f"""
                    CREATE TEMP TABLE temp_nodes (
                        {g_ddl}
                    );
                """)
                with open(graph_csv, 'r', encoding='utf-8-sig') as f:
                    # psycopg2 handles file-like objects nicely with copy_expert
                    # for psycopg3 we use the copy manager
                    if hasattr(cursor, 'copy_expert'):
                        cursor.copy_expert("COPY temp_nodes FROM STDIN WITH CSV HEADER", f)
                    else:
                        with cursor.copy("COPY temp_nodes FROM STDIN WITH (FORMAT csv, HEADER true)") as copy:
                            while data := f.read(8192):
                                copy.write(data)

                # Insert unique nodes using UNION. node_id is the composite id
                # (TYPE::hash) so duplicates across SOURCE/TARCode are collapsed
                # by the primary-key conflict handler.
                cursor.execute(f"""
                    INSERT INTO catalog_networknode (node_id, name, "group", organization_id)
                    SELECT source_id, source, source_type, {org_id_literal} FROM temp_nodes
                    WHERE source_id IS NOT NULL AND TRIM(source_id) != ''
                    UNION
                    SELECT target_id, target, target_type, {org_id_literal} FROM temp_nodes
                    WHERE target_id IS NOT NULL AND TRIM(target_id) != ''
                    ON CONFLICT (node_id) DO NOTHING;
                """)
                # Insert edges using composite ids for stable cross-references.
                # kind/level are classified from the endpoint types via the
                # shared network_classify CASE builders (single source of truth)
                # so reads filter on indexed columns rather than parsing prefixes.
                # An explicit ``edge_kind`` (when present) overrides the classifier
                # for structural edges (e.g. 'join'); ``lineage_type`` records how
                # a column edge's target column was derived.
                from catalog.services.network_classify import kind_case_sql, level_case_sql
                kind_expr = kind_case_sql()
                if 'edge_kind' in g_cols:
                    kind_expr = f"COALESCE(NULLIF(TRIM(edge_kind), ''), {kind_expr})"
                lineage_type_expr = "NULLIF(TRIM(lineage_type), '')" if 'lineage_type' in g_cols else "NULL"
                cursor.execute(f"""
                    INSERT INTO catalog_networkedge (source, target, organization_id, kind, level, lineage_type)
                    SELECT DISTINCT source_id, target_id, {org_id_literal},
                           {kind_expr}, {level_case_sql()}, {lineage_type_expr}
                    FROM temp_nodes
                    WHERE source_id IS NOT NULL AND TRIM(source_id) != ''
                      AND target_id IS NOT NULL AND TRIM(target_id) != ''
                    ON CONFLICT (source, target) DO UPDATE SET
                        kind = EXCLUDED.kind, level = EXCLUDED.level,
                        lineage_type = EXCLUDED.lineage_type;
                """)
                
                # 2. Catalog Items
                # The `temp_items` column list must match the column ORDER in
                # fabric_info_items.csv, NOT a fixed order. The transform step
                # writes some rows (workspaces) with fewer trailing usage-stat
                # columns; `web_url` comes AFTER formatstring in the CSV header.
                # We read the real header to build the DDL dynamically so
                # Postgres COPY never complains about missing trailing columns.
                with open(items_csv, 'r', encoding='utf-8-sig') as f:
                    header_line = f.readline().rstrip('\n').rstrip('\r')
                csv_cols = [c.strip().lower() for c in header_line.split(',')]
                # Defensive: rename any reserved / awkward names.
                safe_cols = [f'"{c}"' if c else '"col_blank"' for c in csv_cols]
                cols_ddl = ",\n                        ".join(
                    f"{sc} text" for sc in safe_cols
                )
                cursor.execute("DROP TABLE IF EXISTS temp_items;")
                cursor.execute(f"""
                    CREATE TEMP TABLE temp_items (
                        {cols_ddl}
                    );
                """)

                with open(items_csv, 'r', encoding='utf-8-sig') as f:
                    if hasattr(cursor, 'copy_expert'):
                        cursor.copy_expert("COPY temp_items FROM STDIN WITH CSV HEADER", f)
                    else:
                        with cursor.copy("COPY temp_items FROM STDIN WITH (FORMAT csv, HEADER true)") as copy:
                            while data := f.read(8192):
                                copy.write(data)
                
                # Upsert into catalog_item
                # NOTE: user-managed fields (status, ownership_department_id, ownership_person_id,
                #       steward_id, category, custom_description) are intentionally excluded from
                #       ON CONFLICT DO UPDATE so they are never overwritten on re-import.
                #       They are only set on INSERT (new items get defaults).

                # Check if temp_items has connected_reports_json column
                has_crj = '"connected_reports_json"' in safe_cols or 'connected_reports_json' in csv_cols

                crj_select = "COALESCE(NULLIF(TRIM(connected_reports_json), ''), '[]')::jsonb" if has_crj else "'[]'::jsonb"

                # BigQuery FQN columns (added in migration 0009). Older CSVs
                # may not have them yet — fall back to NULL in that case.
                has_bq_project = 'bq_project' in csv_cols
                has_bq_schema = 'bq_schema' in csv_cols
                has_bq_source = 'bq_source_name' in csv_cols
                # Strip both empty strings AND the legacy 'N/A' literal that older
                # parser revisions emitted on miss. 'N/A' is truthy in the Python
                # bridge matcher and would create false-positive triple matches.
                bq_project_select = "NULLIF(NULLIF(TRIM(bq_project), ''), 'N/A')" if has_bq_project else "NULL"
                bq_schema_select = "NULLIF(NULLIF(TRIM(bq_schema), ''), 'N/A')" if has_bq_schema else "NULL"
                bq_source_select = "NULLIF(NULLIF(TRIM(bq_source_name), ''), 'N/A')" if has_bq_source else "NULL"

                # Relationship fields (added in migration 0010). Older CSVs
                # produced by previous transform versions don't carry these
                # columns; default to FALSE / [] in that case.
                has_is_related = 'is_related' in csv_cols
                has_rel_json = 'relationships_json' in csv_cols
                is_related_select = (
                    "COALESCE(LOWER(TRIM(is_related)) IN ('true', '1', 't'), FALSE)"
                    if has_is_related else "FALSE"
                )
                rel_json_select = (
                    "COALESCE(NULLIF(TRIM(relationships_json), ''), '[]')::jsonb"
                    if has_rel_json else "'[]'::jsonb"
                )

                # Compiled SQL (added in migration 0049). Older CSVs won't carry
                # it — fall back to NULL so the upsert still works.
                has_compiled_expression = 'compiled_expression' in csv_cols
                compiled_expression_select = (
                    "NULLIF(TRIM(compiled_expression), '')"
                    if has_compiled_expression else "NULL"
                )

                # schema.yml properties (added in migration 0050). Older CSVs
                # won't carry it — fall back to NULL.
                has_properties_yaml = 'properties_yaml' in csv_cols
                properties_yaml_select = (
                    "NULLIF(TRIM(properties_yaml), '')"
                    if has_properties_yaml else "NULL"
                )

                cursor.execute(f"""
                    INSERT INTO catalog_item (
                        item_id, lineage_tag, item_name, item_type, service, description,
                        workspace_id, workspace_name, dataset_id, dataset_name, table_name,
                        datatype, column_type, expression, compiled_expression, properties_yaml, formatstring,
                        is_unused, connected_reports, connected_report_pages, connected_visuals,
                        connected_measures, connected_columns, connected_tables, deleted, web_url,
                        ownership_department_id, ownership_person_id, steward_id,
                        category_id, custom_description,
                        organization_id, connected_reports_json,
                        database_name, tags, meta,
                        integration_source_id,
                        bq_project, bq_schema, bq_source_name,
                        is_related, relationships_json,
                        group_id
                    )
                    SELECT 
                        NULLIF(TRIM(item_id), ''),
                        NULLIF(TRIM(lineage_tag), ''),
                        NULLIF(TRIM(item_name), ''),
                        NULLIF(TRIM(item_type), ''),
                        NULLIF(TRIM(item_service), ''),
                        NULLIF(TRIM(description), ''),
                        NULLIF(TRIM(workspace_id), ''),
                        NULLIF(TRIM(workspace_name), ''),
                        NULLIF(TRIM(dataset_id), ''),
                        NULLIF(TRIM(dataset_name), ''),
                        NULLIF(TRIM(table_name), ''),
                        NULLIF(TRIM(datatype), ''),
                        NULLIF(TRIM(column_type), ''),
                        NULLIF(TRIM(expression), ''),
                        {compiled_expression_select},
                        {properties_yaml_select},
                        NULLIF(TRIM(formatstring), ''),
                        COALESCE(LOWER(TRIM(is_unused)) IN ('true', '1', 't'), FALSE),
                        COALESCE(NULLIF(TRIM(connected_reports), '')::numeric::integer, 0),
                        COALESCE(NULLIF(TRIM(connected_report_pages), '')::numeric::integer, 0),
                        COALESCE(NULLIF(TRIM(connected_visuals), '')::numeric::integer, 0),
                        COALESCE(NULLIF(TRIM(connected_measures), '')::numeric::integer, 0),
                        COALESCE(NULLIF(TRIM(connected_columns), '')::numeric::integer, 0),
                        COALESCE(NULLIF(TRIM(connected_tables), '')::numeric::integer, 0),
                        FALSE,
                        NULLIF(TRIM(web_url), ''),
                        NULL, NULL, NULL,
                        NULL, NULL,
                        {org_id_literal},
                        {crj_select},
                        NULL,
                        '[]'::jsonb,
                        '{{}}'::jsonb,
                        {source_id_literal},
                        {bq_project_select},
                        {bq_schema_select},
                        {bq_source_select},
                        {is_related_select},
                        {rel_json_select},
                        -- Measure grouping key: only PB_MEASURE rows get one.
                        -- "{{org_id or 0}}::{{lower(trim(item_name))}}" so the
                        -- same measure name across datasets/workspaces collapses
                        -- into one governance-consistent Data Dictionary row.
                        CASE
                            WHEN NULLIF(TRIM(item_type), '') = 'PB_MEASURE'
                                 AND NULLIF(TRIM(item_name), '') IS NOT NULL
                            THEN COALESCE({org_id_literal}::text, '0') || '::' || LOWER(TRIM(item_name))
                            ELSE NULL
                        END
                    FROM temp_items
                    WHERE NULLIF(TRIM(item_id), '') IS NOT NULL
                    ON CONFLICT(item_id) DO UPDATE SET
                        -- Source-managed fields: always refresh from CSV
                        lineage_tag = EXCLUDED.lineage_tag,
                        item_name = EXCLUDED.item_name,
                        item_type = EXCLUDED.item_type,
                        service = EXCLUDED.service,
                        description = EXCLUDED.description,
                        workspace_id = EXCLUDED.workspace_id,
                        workspace_name = EXCLUDED.workspace_name,
                        dataset_id = EXCLUDED.dataset_id,
                        dataset_name = EXCLUDED.dataset_name,
                        table_name = EXCLUDED.table_name,
                        datatype = EXCLUDED.datatype,
                        column_type = EXCLUDED.column_type,
                        expression = EXCLUDED.expression,
                        compiled_expression = EXCLUDED.compiled_expression,
                        properties_yaml = EXCLUDED.properties_yaml,
                        formatstring = EXCLUDED.formatstring,
                        is_unused = EXCLUDED.is_unused,
                        connected_reports = EXCLUDED.connected_reports,
                        connected_report_pages = EXCLUDED.connected_report_pages,
                        connected_visuals = EXCLUDED.connected_visuals,
                        connected_measures = EXCLUDED.connected_measures,
                        connected_columns = EXCLUDED.connected_columns,
                        connected_tables = EXCLUDED.connected_tables,
                        deleted = EXCLUDED.deleted,
                        web_url = EXCLUDED.web_url,
                        organization_id = EXCLUDED.organization_id,
                        connected_reports_json = EXCLUDED.connected_reports_json,
                        bq_project = EXCLUDED.bq_project,
                        bq_schema = EXCLUDED.bq_schema,
                        bq_source_name = EXCLUDED.bq_source_name,
                        is_related = EXCLUDED.is_related,
                        relationships_json = EXCLUDED.relationships_json,
                        group_id = EXCLUDED.group_id;
                        -- Governance (owner / steward / status / category /
                        -- annotation / primary) lives on catalog_itemgroup
                        -- now and is NEVER touched by ETL — see
                        -- catalog.services.item_groups.ensure_item_groups,
                        -- which only links new items to (or creates) groups.
                        -- The legacy ownership_*/category_id/custom_description
                        -- columns here are deprecated and left untouched.
                        -- Extended metadata NOT overwritten by Fabric (preserves dbt data):
                        -- database_name, tags, meta, integration_source_id
                """)
                items_upserted = cursor.rowcount
                self.stdout.write(f'  → {items_upserted} items inserted/updated.')
                
                # Mark obsolete Fabric/PowerBI items as deleted (preserve dbt items)
                cursor.execute("""
                    UPDATE catalog_item
                    SET deleted = TRUE
                    WHERE (service IS NULL OR service != 'dbt')
                      AND item_id NOT IN (SELECT NULLIF(TRIM(item_id), '') FROM temp_items);
                """)

                # 3. PowerBI Report Usage (per workspace × report × user × month)
                # Optional CSV — present only when extract_usage succeeded.
                # Windowed-replace scoped to (organization_id, integration_source_id):
                # delete only the months present in this run, then re-insert them,
                # leaving older months untouched so history accumulates (see below).
                if os.path.exists(usage_csv):
                    self.stdout.write('Loading PowerBI usage from CSV...')
                    cursor.execute("DROP TABLE IF EXISTS temp_usage;")
                    cursor.execute("""
                        CREATE TEMP TABLE temp_usage (
                            month text,
                            workspace_id text,
                            workspace_name text,
                            report_id text,
                            report_name text,
                            user_email text,
                            user_display_name text,
                            platform text,
                            distribution_method text,
                            report_page text,
                            view_count text
                        );
                    """)
                    with open(usage_csv, 'r', encoding='utf-8-sig') as f:
                        if hasattr(cursor, 'copy_expert'):
                            cursor.copy_expert("COPY temp_usage FROM STDIN WITH CSV HEADER", f)
                        else:
                            with cursor.copy("COPY temp_usage FROM STDIN WITH (FORMAT csv, HEADER true)") as copy:
                                while data := f.read(8192):
                                    copy.write(data)

                    # Windowed replace — refresh only the months in this CSV and
                    # leave older months untouched, so usage history accumulates
                    # across runs (we only ever re-extract the most recent N
                    # months; Power BI's usage models retain ~30-90 days). A
                    # re-pulled month is fully swapped to the latest rows, so a
                    # legacy→modern schema transition can't double-count a month.
                    #
                    # Empty-safe by construction: if extraction failed the CSV is
                    # header-only, the month set is empty, and `month IN (...)`
                    # matches nothing — so a failed run deletes nothing.
                    cursor.execute(f"""
                        DELETE FROM catalog_powerbireportusage
                        WHERE (organization_id IS NOT DISTINCT FROM {org_id_literal})
                          AND (integration_source_id IS NOT DISTINCT FROM {source_id_literal})
                          AND month IN (
                              SELECT DISTINCT NULLIF(TRIM(month), '')::date
                              FROM temp_usage
                              WHERE NULLIF(TRIM(month), '') IS NOT NULL
                          );
                    """)
                    cursor.execute(f"""
                        INSERT INTO catalog_powerbireportusage (
                            month, workspace_id, workspace_name,
                            report_id, report_name,
                            user_email, user_display_name,
                            platform, distribution_method, report_page,
                            view_count, organization_id, integration_source_id
                        )
                        SELECT
                            NULLIF(TRIM(month), '')::date,
                            NULLIF(TRIM(workspace_id), ''),
                            NULLIF(TRIM(workspace_name), ''),
                            NULLIF(TRIM(report_id), ''),
                            NULLIF(TRIM(report_name), ''),
                            NULLIF(TRIM(user_email), ''),
                            NULLIF(TRIM(user_display_name), ''),
                            NULLIF(TRIM(platform), ''),
                            NULLIF(TRIM(distribution_method), ''),
                            NULLIF(TRIM(report_page), ''),
                            COALESCE(NULLIF(TRIM(view_count), '')::numeric::integer, 0),
                            {org_id_literal},
                            {source_id_literal}
                        FROM temp_usage
                        WHERE NULLIF(TRIM(month), '') IS NOT NULL;
                    """)
                    usage_rows = cursor.rowcount
                    self.stdout.write(f'  → {usage_rows} usage rows loaded.')

        # ==========================================
        # FALLBACK PATH (SQLite or others)
        # ==========================================
        else:
            self.stdout.write('Loading Items from CSV via standard chunked processing...')
            df_items = pd.read_csv(items_csv, dtype=str)
            df_items = df_items.where(pd.notnull(df_items), None)

            def safe_int(val):
                try:
                    if pd.isna(val):
                        return 0
                    return int(float(val))
                except (ValueError, TypeError):
                    return 0

            def safe_str(val):
                if val is None:
                    return None
                try:
                    import math
                    if isinstance(val, float) and math.isnan(val):
                        return None
                except Exception:
                    pass
                s = str(val).strip()
                if s.lower() in ('nan', 'none', ''):
                    return None
                return s

            records = df_items.to_dict('records')
            items_data = []
            seen_item_ids = set()

            for row in records:
                item_id = row.get('item_id')
                if not item_id or item_id in seen_item_ids:
                    continue
                    
                seen_item_ids.add(item_id)

                _name = safe_str(row.get('item_name'))
                _type = safe_str(row.get('item_type'))
                # PB_MEASURE rows share a group_id across datasets/workspaces.
                group_id = (
                    f"{self.organization_id or 0}::{_name.strip().lower()}"
                    if _type == 'PB_MEASURE' and _name else None
                )

                items_data.append((
                    item_id,
                    safe_str(row.get('lineage_tag')),
                    safe_str(row.get('item_name')),
                    safe_str(row.get('item_type')),
                    safe_str(row.get('description')),
                    safe_str(row.get('workspace_id')),
                    safe_str(row.get('workspace_name')),
                    safe_str(row.get('dataset_id')),
                    safe_str(row.get('dataset_name')),
                    safe_str(row.get('table_name')),
                    safe_str(row.get('datatype')),
                    safe_str(row.get('column_type')),
                    safe_str(row.get('expression')),
                    safe_str(row.get('compiled_expression')),
                    safe_str(row.get('properties_yaml')),
                    safe_str(row.get('formatstring')),
                    str(row.get('is_unused', 'False')).lower() in ['true', '1', 't'],
                    safe_int(row.get('connected_reports')),
                    safe_int(row.get('connected_report_pages')),
                    safe_int(row.get('connected_visuals')),
                    safe_int(row.get('connected_measures')),
                    safe_int(row.get('connected_columns')),
                    safe_int(row.get('connected_tables')),
                    False, # deleted
                    safe_str(row.get('web_url')),
                    safe_str(row.get('item_service')),
                    group_id,
                ))

            self.stdout.write(f'Upserting {len(items_data)} items via raw SQL for maximum speed...')
            
            upsert_query = """
            INSERT INTO catalog_item (
                item_id, lineage_tag, item_name, item_type, description,
                workspace_id, workspace_name, dataset_id, dataset_name, table_name,
                datatype, column_type, expression, compiled_expression, properties_yaml, formatstring,
                is_unused, connected_reports, connected_report_pages, connected_visuals,
                connected_measures, connected_columns, connected_tables, deleted, web_url, service,
                group_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(item_id) DO UPDATE SET
                lineage_tag = EXCLUDED.lineage_tag,
                item_name = EXCLUDED.item_name,
                item_type = EXCLUDED.item_type,
                description = EXCLUDED.description,
                workspace_id = EXCLUDED.workspace_id,
                workspace_name = EXCLUDED.workspace_name,
                dataset_id = EXCLUDED.dataset_id,
                dataset_name = EXCLUDED.dataset_name,
                table_name = EXCLUDED.table_name,
                datatype = EXCLUDED.datatype,
                column_type = EXCLUDED.column_type,
                expression = EXCLUDED.expression,
                compiled_expression = EXCLUDED.compiled_expression,
                properties_yaml = EXCLUDED.properties_yaml,
                formatstring = EXCLUDED.formatstring,
                is_unused = EXCLUDED.is_unused,
                connected_reports = EXCLUDED.connected_reports,
                connected_report_pages = EXCLUDED.connected_report_pages,
                connected_visuals = EXCLUDED.connected_visuals,
                connected_measures = EXCLUDED.connected_measures,
                connected_columns = EXCLUDED.connected_columns,
                connected_tables = EXCLUDED.connected_tables,
                deleted = EXCLUDED.deleted,
                web_url = EXCLUDED.web_url,
                service = EXCLUDED.service,
                group_id = EXCLUDED.group_id
            """
            
            with connection.cursor() as cursor:
                # Execute in batches of 2000
                chunk_size = 2000
                for i in range(0, len(items_data), chunk_size):
                    cursor.executemany(upsert_query, items_data[i:i + chunk_size])

            self.stdout.write('Marking obsolete items as deleted...')
            Item.objects.exclude(item_id__in=seen_item_ids).update(deleted=True)

            self.stdout.write('Loading Network Graph...')
            df_graph = pd.read_csv(graph_csv)
            df_graph = df_graph.where(pd.notnull(df_graph), None)

            nodes_by_id = {}
            edges_set = set()

            graph_records = df_graph.to_dict('records')
            for row in graph_records:
                src_id = str(row.get('source_id', '') or '').strip()
                src_name = str(row.get('source', '') or '').strip()
                src_type = str(row.get('source_type', '') or '').strip()
                tgt_id = str(row.get('target_id', '') or '').strip()
                tgt_name = str(row.get('target', '') or '').strip()
                tgt_type = str(row.get('target_type', '') or '').strip()

                if src_id and src_id not in nodes_by_id:
                    nodes_by_id[src_id] = (src_name, src_type)
                if tgt_id and tgt_id not in nodes_by_id:
                    nodes_by_id[tgt_id] = (tgt_name, tgt_type)
                if src_id and tgt_id:
                    edges_set.add((src_id, tgt_id))

            from catalog.services.network_classify import classify_edge
            nodes_data = [(nid, name, grp) for nid, (name, grp) in nodes_by_id.items()]
            # Tag each edge with kind/level from its endpoint types (single
            # source of truth) so the SQLite path matches the Postgres COPY path.
            edges_data = []
            for src, tgt in edges_set:
                s_type = (nodes_by_id.get(src) or ('', ''))[1]
                t_type = (nodes_by_id.get(tgt) or ('', ''))[1]
                kind, level = classify_edge(s_type, t_type)
                edges_data.append((src, tgt, kind, level))

            self.stdout.write(f'Inserting {len(nodes_data)} nodes and {len(edges_data)} edges via raw SQL...')

            with connection.cursor() as cursor:
                chunk_size = 2000
                for i in range(0, len(nodes_data), chunk_size):
                    cursor.executemany(
                        'INSERT INTO catalog_networknode (node_id, name, "group") VALUES (%s, %s, %s)'
                        ' ON CONFLICT (node_id) DO NOTHING',
                        nodes_data[i:i + chunk_size],
                    )
                for i in range(0, len(edges_data), chunk_size):
                    cursor.executemany(
                        'INSERT INTO catalog_networkedge (source, target, kind, level) VALUES (%s, %s, %s, %s)'
                        ' ON CONFLICT (source, target) DO UPDATE SET kind = excluded.kind, level = excluded.level',
                        edges_data[i:i + chunk_size],
                    )

        # Attach every item to an ItemGroup (creates measure/singleton groups
        # for new items; never touches existing groups' curated governance).
        self.stdout.write('Syncing ItemGroups...')
        from catalog.services.item_groups import ensure_item_groups
        linked = ensure_item_groups(self.organization_id)
        self.stdout.write(f'  → {linked} items linked to groups.')

        # Calculate Summary
        self.stdout.write('Calculating Summary...')
        total_measures = Item.objects.filter(item_type='PB_MEASURE', deleted=False).count()
        unused_measures = Item.objects.filter(item_type='PB_MEASURE', is_unused=True, deleted=False).count()
        total_columns = Item.objects.filter(item_type='PB_COLUMN', deleted=False).count()
        unused_columns = Item.objects.filter(item_type='PB_COLUMN', is_unused=True, deleted=False).count()
        total_reports = Item.objects.filter(item_type='PB_REPORT', deleted=False).count()

        Summary.objects.all().delete()
        Summary.objects.create(
            total_measures=total_measures,
            unused_measures=unused_measures,
            total_columns=total_columns,
            unused_columns=unused_columns,
            total_reports=total_reports,
            organization_id=self.organization_id,
        )

        self.stdout.write(self.style.SUCCESS('Successfully loaded all data from CSVs'))
