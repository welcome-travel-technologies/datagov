"""
Load dbt CSVs into the database.

Unlike load_data (which wipes all network data), this command is *additive*:
it only touches items/nodes/edges with service='dbt' and leaves existing
PowerBI data untouched. It also builds cross-tool bridge edges between dbt
models and PowerBI tables by matching materialized table names.
"""
import os
from django.core.management.base import BaseCommand
from catalog.models import Item, NetworkNode, NetworkEdge
from django.conf import settings
from django.db import connection, transaction


class Command(BaseCommand):
    help = 'Load dbt data from CSVs into the database (additive — preserves PowerBI data)'

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', type=int, default=None,
                            help='Organization PK to assign to loaded items')
        parser.add_argument('--source-id', type=int, default=None,
                            help='IntegrationSource PK that produced this data')

    def handle(self, *args, **kwargs):
        self.organization_id = kwargs.get('organization_id')
        self.source_id = kwargs.get('source_id')
        org_id_literal = 'NULL'
        if self.organization_id is not None:
            org_id_literal = str(int(self.organization_id))
        source_id_literal = 'NULL'
        if self.source_id is not None:
            source_id_literal = str(int(self.source_id))

        data_dir = os.path.join(settings.BASE_DIR, 'etl', 'sources', 'dbt', 'data')
        items_csv = os.path.join(data_dir, 'dbt_info_items.csv')
        graph_csv = os.path.join(data_dir, 'dbt_info_graph.csv')

        if not os.path.exists(items_csv):
            self.stdout.write(self.style.ERROR(f'dbt items CSV not found at {items_csv}'))
            return

        # ── Clean up old dbt network data ─────────────────────────────────
        self.stdout.write('Cleaning up old dbt network data...')

        # Delete DBT_* typed nodes (DBT_MODEL, DBT_SOURCE, DBT_SEED, DBT_TEST, DBT_COLUMN)
        # plus their edges. DBT_COLUMN node_ids start with 'DBT_COLUMN::' so the
        # source/target startswith filters cover them along with the others.
        NetworkNode.objects.filter(group__startswith='DBT_').delete()
        NetworkEdge.objects.filter(source__startswith='DBT_').delete()
        NetworkEdge.objects.filter(target__startswith='DBT_').delete()

        with transaction.atomic(), connection.cursor() as cursor:
            # ── Load items CSV ─────────────────────────────────────────────
            with open(items_csv, 'r', encoding='utf-8-sig') as f:
                header_line = f.readline().rstrip('\n').rstrip('\r')
            csv_cols = [c.strip().lower() for c in header_line.split(',')]
            safe_cols = [f'"{c}"' if c else '"col_blank"' for c in csv_cols]
            cols_ddl = ",\n                    ".join(f"{sc} text" for sc in safe_cols)

            cursor.execute("DROP TABLE IF EXISTS temp_dbt_items;")
            cursor.execute(f"""
                CREATE TEMP TABLE temp_dbt_items (
                    {cols_ddl}
                );
            """)

            with open(items_csv, 'r', encoding='utf-8-sig') as f:
                if hasattr(cursor, 'copy_expert'):
                    cursor.copy_expert("COPY temp_dbt_items FROM STDIN WITH CSV HEADER", f)
                else:
                    with cursor.copy("COPY temp_dbt_items FROM STDIN WITH (FORMAT csv, HEADER true)") as copy:
                        while data := f.read(8192):
                            copy.write(data)

            has_crj = 'connected_reports_json' in csv_cols
            crj_select = "COALESCE(NULLIF(TRIM(connected_reports_json), ''), '[]')::jsonb" if has_crj else "'[]'::jsonb"

            # New metadata columns — use them if present in CSV, otherwise default
            has_database_name = 'database_name' in csv_cols
            has_schema_name = 'schema_name' in csv_cols
            has_alias = 'alias' in csv_cols
            has_tags = 'tags' in csv_cols
            has_meta = 'meta' in csv_cols

            db_name_select = "NULLIF(TRIM(database_name), '')" if has_database_name else "NULL"
            schema_name_select = "NULLIF(TRIM(schema_name), '')" if has_schema_name else "NULL"
            alias_select = "NULLIF(TRIM(alias), '')" if has_alias else "NULL"
            tags_select = "COALESCE(NULLIF(TRIM(tags), ''), '[]')::jsonb" if has_tags else "'[]'::jsonb"
            meta_select = "COALESCE(NULLIF(TRIM(meta), ''), '{}')::jsonb" if has_meta else "'{}'::jsonb"

            cursor.execute(f"""
                INSERT INTO catalog_item (
                    item_id, lineage_tag, item_name, item_type, service, description,
                    workspace_id, workspace_name, dataset_id, dataset_name, table_name,
                    datatype, column_type, expression, formatstring,
                    is_unused, connected_reports, connected_report_pages, connected_visuals,
                    connected_measures, connected_columns, connected_tables, deleted, web_url,
                    ownership_department_id, ownership_person_id, steward_id,
                    category_id, custom_description, status,
                    organization_id, connected_reports_json,
                    database_name, schema_name, alias, tags, meta,
                    integration_source_id,
                    is_related, relationships_json
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
                    NULL, NULL, 'UNVERIFIED',
                    {org_id_literal},
                    {crj_select},
                    {db_name_select},
                    {schema_name_select},
                    {alias_select},
                    {tags_select},
                    {meta_select},
                    {source_id_literal},
                    FALSE,
                    '[]'::jsonb
                FROM temp_dbt_items
                WHERE NULLIF(TRIM(item_id), '') IS NOT NULL
                ON CONFLICT(item_id) DO UPDATE SET
                    lineage_tag = EXCLUDED.lineage_tag,
                    -- Preserve existing item_name/expression if the new run provides nothing
                    item_name = COALESCE(EXCLUDED.item_name, catalog_item.item_name),
                    item_type = EXCLUDED.item_type,
                    service = EXCLUDED.service,
                    -- Keep existing description if the new run provides nothing (YAML had no description)
                    description = COALESCE(EXCLUDED.description, catalog_item.description),
                    workspace_id = EXCLUDED.workspace_id,
                    workspace_name = EXCLUDED.workspace_name,
                    dataset_id = EXCLUDED.dataset_id,
                    dataset_name = EXCLUDED.dataset_name,
                    table_name = EXCLUDED.table_name,
                    datatype = EXCLUDED.datatype,
                    column_type = EXCLUDED.column_type,
                    -- Keep existing SQL if the new run provides nothing (e.g. raw file missing)
                    expression = COALESCE(EXCLUDED.expression, catalog_item.expression),
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
                    -- New metadata fields
                    database_name = EXCLUDED.database_name,
                    schema_name = EXCLUDED.schema_name,
                    alias = EXCLUDED.alias,
                    tags = EXCLUDED.tags,
                    meta = EXCLUDED.meta,
                    integration_source_id = EXCLUDED.integration_source_id;
            """)
            items_upserted = cursor.rowcount
            self.stdout.write(f'  → {items_upserted} dbt items inserted/updated.')

            # Mark obsolete dbt items as deleted (only service='dbt')
            cursor.execute("""
                UPDATE catalog_item
                SET deleted = TRUE
                WHERE service = 'dbt'
                  AND item_id NOT IN (SELECT NULLIF(TRIM(item_id), '') FROM temp_dbt_items);
            """)

            # ── Load graph CSV ─────────────────────────────────────────────
            if os.path.exists(graph_csv):
                # Build the temp table from the CSV header so newer columns
                # (edge_kind, lineage_type) load positionally and older CSVs
                # without them still work.
                with open(graph_csv, 'r', encoding='utf-8-sig') as f:
                    g_header = f.readline().rstrip('\n').rstrip('\r')
                g_cols = [c.strip().lower() for c in g_header.split(',')]
                g_safe = [f'"{c}"' if c else '"col_blank"' for c in g_cols]
                g_ddl = ",\n                        ".join(f"{sc} text" for sc in g_safe)

                cursor.execute("DROP TABLE IF EXISTS temp_dbt_nodes;")
                cursor.execute(f"""
                    CREATE TEMP TABLE temp_dbt_nodes (
                        {g_ddl}
                    );
                """)
                with open(graph_csv, 'r', encoding='utf-8-sig') as f:
                    if hasattr(cursor, 'copy_expert'):
                        cursor.copy_expert("COPY temp_dbt_nodes FROM STDIN WITH CSV HEADER", f)
                    else:
                        with cursor.copy("COPY temp_dbt_nodes FROM STDIN WITH (FORMAT csv, HEADER true)") as copy:
                            while data := f.read(8192):
                                copy.write(data)

                cursor.execute(f"""
                    INSERT INTO catalog_networknode (node_id, name, "group", organization_id)
                    SELECT source_id, source, source_type, {org_id_literal} FROM temp_dbt_nodes
                    WHERE source_id IS NOT NULL AND TRIM(source_id) != ''
                    UNION
                    SELECT target_id, target, target_type, {org_id_literal} FROM temp_dbt_nodes
                    WHERE target_id IS NOT NULL AND TRIM(target_id) != ''
                    ON CONFLICT (node_id) DO NOTHING;
                """)
                # Classify each edge (kind + level) from its endpoint types so
                # reads filter on indexed columns. The CASE logic is generated
                # from catalog.services.network_classify — the single source of
                # truth — over the temp table's source_type/target_type columns.
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
                    FROM temp_dbt_nodes
                    WHERE source_id IS NOT NULL AND TRIM(source_id) != ''
                      AND target_id IS NOT NULL AND TRIM(target_id) != ''
                    ON CONFLICT (source, target) DO UPDATE SET
                        kind = EXCLUDED.kind, level = EXCLUDED.level,
                        lineage_type = EXCLUDED.lineage_type;
                """)
                self.stdout.write('  → dbt graph nodes and edges loaded.')
            else:
                self.stdout.write(self.style.WARNING('No dbt graph CSV found — skipping graph load.'))

            # NOTE: Cross-tool bridges (dbt → PowerBI) are no longer built here.
            # They are built by the workflow "final step" (run_workflow_final command)
            # which runs after ALL sources have loaded, ensuring both dbt and PowerBI
            # data are available regardless of source execution order.

        # Every dbt item gets its own singleton ItemGroup (idempotent;
        # existing groups' curated governance is preserved).
        self.stdout.write('Syncing ItemGroups...')
        from catalog.services.item_groups import ensure_item_groups
        linked = ensure_item_groups(self.organization_id)
        self.stdout.write(f'  → {linked} items linked to groups.')

        self.stdout.write(self.style.SUCCESS('Successfully loaded dbt data.'))
