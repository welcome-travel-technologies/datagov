"""
Transform dbt artifacts into catalog CSVs.

Parses manifest.json and optionally catalog.json, producing dbt_info_items.csv
and dbt_info_graph.csv that follow the same column schema as the Fabric ETL
output (plus extra metadata columns) so the shared load_dbt_data command can
upsert them into catalog_item / catalog_networknode / catalog_networkedge.

When catalog.json is available (produced by ``dbt docs generate``), the
transform merges its richer metadata:
  - Complete column inventory (not just YAML-documented ones)
  - Actual database data types (e.g. VARCHAR(256) instead of what's in YAML)
  - Table-level statistics (row count, size)

New metadata columns (populated from manifest + catalog):
  - database_name — 3rd part of FQN (database.schema.table)
  - tags          — JSON list of dbt tags
  - meta          — JSON dict of dbt meta + constraints + loader + access level

Uses ``dbt-artifacts-parser`` (already in requirements.txt) to validate the
manifest / catalog schema and detect the dbt version before the raw-dict
extraction logic runs.  If the parser cannot handle an artifact version it
gracefully falls back to the raw JSON so we never break on future dbt
releases.
"""
import os
import re
import sys
import json
import hashlib
import functools

import pandas as pd

# ── Optional dbt-artifacts-parser integration ─────────────────────────────────
try:
    from dbt_artifacts_parser.parser import parse_manifest as _dbt_parse_manifest  # noqa: F401
    from dbt_artifacts_parser.parser import parse_catalog as _dbt_parse_catalog    # noqa: F401
    _HAS_PARSER = True
except ImportError:
    _HAS_PARSER = False

# ── Column-level lineage (sqlglot) ─────────────────────────────────────────────
# transform_dbt is imported top-level (extract_dbt appends its dir to sys.path),
# so prefer the plain import; fall back to package-relative, then to a no-op if
# sqlglot itself is unavailable so the rest of the ETL never hard-crashes.
try:
    from column_lineage import extract_column_edges, _col_key as _cll_key
except ImportError:  # pragma: no cover
    try:
        from .column_lineage import extract_column_edges, _col_key as _cll_key
    except ImportError:
        extract_column_edges = None

        def _cll_key(s):
            return (s or '').strip().lower()

# PyYAML powers the per-model schema.yml extraction (see _build_properties_yaml).
# Guarded so the ETL never hard-crashes if it's somehow unavailable.
try:
    import yaml

    class _YamlDumper(yaml.SafeDumper):
        """Dumper that renders multi-line strings as readable `|` block scalars."""

    def _yaml_repr_str(dumper, data):
        style = '|' if '\n' in data else None
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style=style)

    _YamlDumper.add_representer(str, _yaml_repr_str)
except ImportError:  # pragma: no cover
    yaml = None
    _YamlDumper = None


def _clean_yaml_value(v):
    """Strip trailing whitespace from string lines so `|` block style is usable
    (PyYAML falls back to ugly quoted scalars when lines have trailing spaces)."""
    if isinstance(v, str):
        return '\n'.join(line.rstrip() for line in v.split('\n')) if '\n' in v else v
    if isinstance(v, list):
        return [_clean_yaml_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean_yaml_value(x) for k, x in v.items()}
    return v

_RE_CLEAN_ID = re.compile(r"[\s'\"`]+")


@functools.lru_cache(maxsize=None)
def generate_custom_id(*args):
    combined = "_".join(str(arg) for arg in args if arg is not None and str(arg).strip())
    cleaned = _RE_CLEAN_ID.sub("", combined.lower())
    return hashlib.md5(cleaned.encode('utf-8')).hexdigest()


def _read_sql_file(repo_dir, original_file_path):
    """Try to read the raw SQL file from the repo."""
    if not original_file_path:
        return None
    full_path = os.path.join(repo_dir, original_file_path)
    if os.path.exists(full_path):
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            pass
    return None


def _build_properties_yaml(node, repo_dir):
    """Authored dbt properties (schema.yml block) for one node, as YAML text.

    Reads the node's patch file (manifest ``patch_path`` -> ``pkg://path``),
    finds this node's entry under models/seeds/snapshots, and re-serializes just
    that entry. Returns '' when the node has no properties file (no schema.yml
    entry) or it can't be read/parsed — the UI then shows an empty state.
    """
    if yaml is None:
        return ''
    patch_path = node.get('patch_path')
    if not patch_path:
        return ''
    rel = patch_path.split('://', 1)[-1] if '://' in patch_path else patch_path
    content = _read_sql_file(repo_dir, rel)  # generic text-file reader
    if not content:
        return ''
    try:
        doc = yaml.safe_load(content)
    except Exception:
        return ''
    if not isinstance(doc, dict):
        return ''
    name = node.get('name')
    for section in ('models', 'seeds', 'snapshots', 'sources'):
        entries = doc.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get('name') == name:
                try:
                    return yaml.dump(
                        {'version': doc.get('version', 2), section: [_clean_yaml_value(entry)]},
                        Dumper=_YamlDumper, sort_keys=False, default_flow_style=False,
                        allow_unicode=True, width=100,
                    )
                except Exception:
                    return ''
    return ''


def _parse_json(path):
    """Load a JSON file and return the parsed dict."""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _extract_repo_name(repo_dir):
    """Extract a human-readable repo name from the repo directory."""
    return os.path.basename(repo_dir.rstrip('/\\')) or 'dbt-project'


def _build_catalog_lookup(catalog):
    """
    Build a lookup dict from catalog.json for fast column/stats access.

    Returns:
        {
            unique_id: {
                'columns': {col_name_lower: {'name': str, 'type': str, 'index': int, 'comment': str}},
                'stats': {stat_label: stat_value},
            }
        }

    Notes:
        - Nested struct sub-columns (containing '.') are skipped — only
          top-level columns are included.
        - Stats with ``include: false`` (e.g. ``has_stats``) are excluded.
    """
    lookup = {}

    for section_key in ('nodes', 'sources'):
        section = catalog.get(section_key, {})
        for unique_id, entry in section.items():
            columns = {}
            for col_name, col_info in (entry.get('columns') or {}).items():
                # Skip nested struct sub-fields (e.g. "struct_col.sub_field")
                if '.' in col_name:
                    continue
                columns[col_name.lower()] = {
                    'name': col_info.get('name', col_name),
                    'type': col_info.get('type', ''),
                    'index': col_info.get('index', 0),
                    'comment': col_info.get('comment', ''),
                }

            stats = {}
            raw_stats = entry.get('stats') or {}
            stat_items = raw_stats.values() if isinstance(raw_stats, dict) else raw_stats
            for stat_info in stat_items:
                if not isinstance(stat_info, dict):
                    continue
                # Respect the 'include' flag — skip meta-stats like has_stats
                if not stat_info.get('include', True):
                    continue
                label = stat_info.get('label', stat_info.get('id', ''))
                value = stat_info.get('value')
                if label and value is not None:
                    stats[label] = value

            metadata = entry.get('metadata', {})
            lookup[unique_id] = {
                'columns': columns,
                'stats': stats,
                'owner': metadata.get('owner', ''),
                'db_schema': metadata.get('schema', ''),
                'database': metadata.get('database', ''),
            }

    return lookup


def _make_item_row(**kwargs):
    """Create an item dict with all expected columns, using defaults."""
    defaults = {
        'item_id': '',
        'lineage_tag': None,
        'item_name': '',
        'item_type': '',
        'item_service': 'dbt',
        'description': '',
        'workspace_id': '',
        'workspace_name': '',
        'dataset_id': None,
        'dataset_name': '',
        'table_name': None,
        'datatype': None,
        'column_type': None,
        'expression': None,
        'compiled_expression': None,
        'properties_yaml': None,
        'formatstring': None,
        'web_url': None,
        'is_unused': False,
        'connected_reports': 0,
        'connected_report_pages': 0,
        'connected_visuals': 0,
        'connected_measures': 0,
        'connected_columns': 0,
        'connected_tables': 0,
        'connected_reports_json': '[]',
        # New metadata columns
        'database_name': None,
        'schema_name': None,
        'alias': None,
        'tags': '[]',
        'meta': '{}',
    }
    defaults.update(kwargs)
    return defaults


def _make_edge(**kwargs):
    """Create a graph edge dict.

    ``edge_kind`` optionally overrides the type-based kind classifier at load
    time (used for structural 'join'/'filter' edges the classifier can't infer);
    ``lineage_type`` records how a column edge's target column was derived.
    Both are empty for ordinary edges.
    """
    return {
        'source_id': kwargs.get('source_id', ''),
        'source': kwargs.get('source', ''),
        'source_type': kwargs.get('source_type', ''),
        'target_id': kwargs.get('target_id', ''),
        'target': kwargs.get('target', ''),
        'target_type': kwargs.get('target_type', ''),
        'workspace_id': kwargs.get('workspace_id', ''),
        'edge_kind': kwargs.get('edge_kind', ''),
        'lineage_type': kwargs.get('lineage_type', ''),
    }


def _validate_with_parser(raw: dict, kind: str) -> None:
    """
    Validate a dbt artifact dict using dbt-artifacts-parser.

    Logs the detected schema version.  Failures are non-fatal — the caller
    continues with the raw dict.

    Args:
        raw:  Parsed JSON dict (manifest or catalog).
        kind: ``"manifest"`` or ``"catalog"``.
    """
    if not _HAS_PARSER:
        return
    try:
        if kind == "manifest":
            obj = _dbt_parse_manifest(manifest=raw)
            schema_ver = getattr(getattr(obj, 'metadata', None), 'dbt_schema_version', None)
            print(f"[PARSER] Manifest validated: {type(obj).__name__}"
                  f"{' — schema: ' + schema_ver if schema_ver else ''}")
        else:
            obj = _dbt_parse_catalog(catalog=raw)
            print(f"[PARSER] Catalog validated: {type(obj).__name__}")
    except Exception as exc:
        # Non-fatal: log and let the raw-dict path continue
        print(f"[PARSER] Warning: could not validate {kind} with dbt-artifacts-parser: {exc}. "
              f"Continuing with raw JSON.")


def _build_meta_dict(node, is_source=False):
    """Build the combined meta JSON dict for an item.

    Merges dbt ``meta``, ``loader``, ``access``, and any other niche fields
    into a single dict that is stored in the Item.meta JSONField.
    """
    meta = dict(node.get('meta', {}) or {})
    if is_source:
        loader = node.get('loader', '')
        if loader:
            meta['loader'] = loader
    access = node.get('access')
    if access:
        meta['access'] = access
    return meta


def _build_column_meta(manifest_col):
    """Build combined meta for a column item: meta + constraints."""
    meta = dict(manifest_col.get('meta', {}) or {})
    constraints = []
    for c in manifest_col.get('constraints', []):
        if isinstance(c, dict):
            constraints.append(c.get('type', str(c)))
        else:
            constraints.append(str(c))
    if constraints:
        meta['constraints'] = constraints
    return meta


def main(manifest_path, repo_dir, output_dir, catalog_path=None):
    """
    Main transform entry point.

    Args:
        manifest_path: Absolute path to manifest.json
        repo_dir: Absolute path to the cloned repo root
        output_dir: Directory to write CSV output files
        catalog_path: Optional absolute path to catalog.json
    """
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    print("[START] Starting dbt Transform Phase...")

    manifest = _parse_json(manifest_path)
    _validate_with_parser(manifest, 'manifest')
    metadata = manifest.get('metadata', {})
    project_name = metadata.get('project_name') or metadata.get('project_id') or 'dbt_project'
    repo_name = _extract_repo_name(repo_dir)

    # Use repo_dir hash as workspace_id for scoping
    workspace_id = generate_custom_id('dbt', repo_name)
    workspace_name = repo_name

    # Load catalog.json if available
    catalog_lookup = {}
    if catalog_path and os.path.exists(catalog_path):
        print(f"[CATALOG] Loading catalog.json from {catalog_path}...")
        catalog = _parse_json(catalog_path)
        _validate_with_parser(catalog, 'catalog')
        catalog_lookup = _build_catalog_lookup(catalog)
        print(f"[CATALOG] Loaded metadata for {len(catalog_lookup)} nodes/sources.")
    else:
        print("[CATALOG] No catalog.json available — using manifest.json only.")

    nodes = manifest.get('nodes', {})
    sources = manifest.get('sources', {})

    item_rows = []
    graph_edges = []
    # {unique_id: {col_key: (display_name, dbt_column_item_id)}} — built while we
    # create DBT_COLUMN items so the column-lineage extractor reuses the exact
    # same ids (no dangling edges).
    col_index = {}

    # ──────────────────────────────────────────────
    # 1. WORKSPACE item (one per dbt project)
    # ──────────────────────────────────────────────
    item_rows.append(_make_item_row(
        item_id=workspace_id,
        item_name=f'{project_name} (dbt)',
        item_type='DBT_WORKSPACE',
        description=f'dbt project: {project_name}',
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        dataset_name=project_name,
    ))

    # ──────────────────────────────────────────────
    # 2. SOURCES
    # ──────────────────────────────────────────────
    print(f"[SOURCES] Parsing {len(sources)} dbt sources...")
    source_id_map = {}  # unique_id -> item_id

    for unique_id, src in sources.items():
        src_name = src.get('name', unique_id.split('.')[-1])
        source_name = src.get('source_name', '')
        item_id = generate_custom_id(project_name, unique_id)
        source_id_map[unique_id] = item_id

        schema = src.get('schema', '')
        identifier = src.get('identifier') or src_name
        table_fqn = f'{schema}.{identifier}' if schema else identifier

        # Catalog stats for this source
        cat_entry = catalog_lookup.get(unique_id, {})
        stats = cat_entry.get('stats', {})
        row_count_str = ''
        if stats:
            parts = [f'{k}: {v}' for k, v in stats.items()]
            row_count_str = ' | '.join(parts)

        # Extract database from manifest or catalog
        database = src.get('database') or cat_entry.get('database') or None

        item_rows.append(_make_item_row(
            item_id=item_id,
            lineage_tag=unique_id,
            item_name=f'{source_name}.{src_name}' if source_name else src_name,
            item_type='DBT_SOURCE',
            description=src.get('description', ''),
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            dataset_id=unique_id,
            dataset_name=project_name,
            table_name=table_fqn,
            formatstring=row_count_str or None,
            # New metadata
            database_name=database,
            schema_name=schema or None,
            alias=identifier or None,
            tags=json.dumps(src.get('tags', [])),
            meta=json.dumps(_build_meta_dict(src, is_source=True)),
        ))

        # Source columns — merge manifest + catalog
        manifest_cols = src.get('columns') or {}
        catalog_cols = cat_entry.get('columns', {})
        all_col_names = _merge_column_names(manifest_cols, catalog_cols)

        for col_name in all_col_names:
            manifest_col = manifest_cols.get(col_name, {})
            catalog_col = catalog_cols.get(col_name.lower(), {})

            col_item_id = generate_custom_id(project_name, unique_id, col_name)
            # Prefer catalog data type (actual DB type), fall back to manifest
            data_type = catalog_col.get('type') or manifest_col.get('data_type', '')
            # Prefer manifest description (user-authored), fall back to catalog comment
            description = manifest_col.get('description', '') or catalog_col.get('comment', '')
            display_name = catalog_col.get('name', col_name) if catalog_col else col_name
            col_index.setdefault(unique_id, {})[_cll_key(col_name)] = (display_name, col_item_id)

            item_rows.append(_make_item_row(
                item_id=col_item_id,
                item_name=display_name,
                item_type='DBT_COLUMN',
                description=description,
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                dataset_id=unique_id,
                dataset_name=project_name,
                table_name=src_name,
                datatype=data_type,
                column_type='source',
                # Column-level metadata
                meta=json.dumps(_build_column_meta(manifest_col)),
                tags=json.dumps(manifest_col.get('tags', [])),
            ))
            graph_edges.append(_make_edge(
                source_id=f'DBT_SOURCE::{item_id}',
                source=f'{source_name}.{src_name}' if source_name else src_name,
                source_type='DBT_SOURCE',
                target_id=f'DBT_COLUMN::{col_item_id}',
                target=display_name,
                target_type='DBT_COLUMN',
                workspace_id=workspace_id,
            ))

    # ──────────────────────────────────────────────
    # 3. MODELS, SEEDS, SNAPSHOTS
    # ──────────────────────────────────────────────
    model_id_map = {}  # unique_id -> item_id

    model_nodes = {k: v for k, v in nodes.items()
                   if v.get('resource_type') in ('model', 'seed', 'snapshot')}
    test_nodes = {k: v for k, v in nodes.items()
                  if v.get('resource_type') == 'test'}

    print(f"[MODELS] Parsing {len(model_nodes)} dbt models/seeds/snapshots...")

    for unique_id, node in model_nodes.items():
        node_name = node.get('name', unique_id.split('.')[-1])
        resource_type = node.get('resource_type', 'model')
        item_id = generate_custom_id(project_name, unique_id)
        model_id_map[unique_id] = item_id

        item_type = 'DBT_SEED' if resource_type == 'seed' else 'DBT_MODEL'

        schema = node.get('schema', '')
        alias = node.get('alias') or node_name
        materialized_table = f'{schema}.{alias}' if schema else alias
        materialization = (node.get('config') or {}).get('materialized', '')

        # Read SQL from the original file, fallback to manifest.
        # Skip for seeds: the "expression" would be the full CSV data, which is
        # too large and breaks the CSV export downstream.
        if resource_type == 'seed':
            raw_sql = ''
            compiled_sql = ''
        else:
            original_file_path = node.get('original_file_path') or node.get('path', '')
            raw_sql = _read_sql_file(repo_dir, original_file_path)
            if not raw_sql:
                raw_sql = node.get('raw_code') or node.get('raw_sql') or ''
            # Compiled SQL straight from dbt's manifest (refs/macros expanded) — the
            # same source colibri shows in its "Compiled" toggle. Empty for manifests
            # produced by `dbt parse` (no compile step); the UI falls back to raw.
            compiled_sql = node.get('compiled_code') or node.get('compiled_sql') or ''

        # Authored schema.yml properties (description/columns/tests/meta) for this
        # node, serialized as YAML for the detail panel's YAML tab.
        properties_yaml = _build_properties_yaml(node, repo_dir)

        # Catalog stats
        cat_entry = catalog_lookup.get(unique_id, {})
        stats = cat_entry.get('stats', {})
        row_count_str = ''
        if stats:
            parts = [f'{k}: {v}' for k, v in stats.items()]
            row_count_str = ' | '.join(parts)

        # Extract database from manifest or catalog
        database = node.get('database') or cat_entry.get('database') or None

        item_rows.append(_make_item_row(
            item_id=item_id,
            lineage_tag=unique_id,
            item_name=node_name,
            item_type=item_type,
            description=node.get('description', ''),
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            dataset_id=unique_id,
            dataset_name=project_name,
            table_name=materialized_table,
            column_type=materialization,
            expression=raw_sql,
            compiled_expression=compiled_sql or None,
            properties_yaml=properties_yaml or None,
            formatstring=row_count_str or None,
            # New metadata
            database_name=database,
            schema_name=schema or None,
            alias=alias or None,
            tags=json.dumps(node.get('tags', [])),
            meta=json.dumps(_build_meta_dict(node)),
        ))

        # Model columns — merge manifest + catalog
        manifest_cols = node.get('columns') or {}
        catalog_cols = cat_entry.get('columns', {})
        all_col_names = _merge_column_names(manifest_cols, catalog_cols)

        for col_name in all_col_names:
            manifest_col = manifest_cols.get(col_name, {})
            catalog_col = catalog_cols.get(col_name.lower(), {})

            col_item_id = generate_custom_id(project_name, unique_id, col_name)
            data_type = catalog_col.get('type') or manifest_col.get('data_type', '')
            description = manifest_col.get('description', '') or catalog_col.get('comment', '')
            display_name = catalog_col.get('name', col_name) if catalog_col else col_name
            col_index.setdefault(unique_id, {})[_cll_key(col_name)] = (display_name, col_item_id)

            item_rows.append(_make_item_row(
                item_id=col_item_id,
                item_name=display_name,
                item_type='DBT_COLUMN',
                description=description,
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                dataset_id=unique_id,
                dataset_name=project_name,
                table_name=node_name,
                datatype=data_type,
                column_type='model',
                # Column-level metadata
                meta=json.dumps(_build_column_meta(manifest_col)),
                tags=json.dumps(manifest_col.get('tags', [])),
            ))
            graph_edges.append(_make_edge(
                source_id=f'{item_type}::{item_id}',
                source=node_name,
                source_type=item_type,
                target_id=f'DBT_COLUMN::{col_item_id}',
                target=display_name,
                target_type='DBT_COLUMN',
                workspace_id=workspace_id,
            ))

    # ──────────────────────────────────────────────
    # 4. TESTS
    # ──────────────────────────────────────────────
    print(f"[TESTS] Parsing {len(test_nodes)} dbt tests...")

    for unique_id, node in test_nodes.items():
        node_name = node.get('name', unique_id.split('.')[-1])
        item_id = generate_custom_id(project_name, unique_id)

        item_rows.append(_make_item_row(
            item_id=item_id,
            lineage_tag=unique_id,
            item_name=node_name,
            item_type='DBT_TEST',
            description=node.get('description', ''),
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            dataset_id=unique_id,
            dataset_name=project_name,
            column_type=node.get('test_metadata', {}).get('name', ''),
            expression=node.get('raw_code') or node.get('raw_sql') or '',
            # New metadata
            tags=json.dumps(node.get('tags', [])),
            meta=json.dumps(_build_meta_dict(node)),
        ))

        # Edges: tested model/source -> DBT_TEST
        for dep_id in (node.get('depends_on') or {}).get('nodes', []):
            dep_item_id = model_id_map.get(dep_id) or source_id_map.get(dep_id)
            if dep_item_id:
                dep_type = 'DBT_SOURCE' if dep_id in source_id_map else 'DBT_MODEL'
                dep_name = (sources.get(dep_id) or nodes.get(dep_id, {})).get('name', dep_id.split('.')[-1])
                graph_edges.append(_make_edge(
                    source_id=f'{dep_type}::{dep_item_id}',
                    source=dep_name,
                    source_type=dep_type,
                    target_id=f'DBT_TEST::{item_id}',
                    target=node_name,
                    target_type='DBT_TEST',
                    workspace_id=workspace_id,
                ))

    # ──────────────────────────────────────────────
    # 5. DEPENDENCY EDGES (ref / source)
    # ──────────────────────────────────────────────
    print("[GRAPH] Building dependency edges...")

    for unique_id, node in model_nodes.items():
        consumer_id = model_id_map.get(unique_id)
        if not consumer_id:
            continue
        consumer_name = node.get('name', unique_id.split('.')[-1])
        resource_type = node.get('resource_type', 'model')
        consumer_type = 'DBT_SEED' if resource_type == 'seed' else 'DBT_MODEL'

        for dep_id in (node.get('depends_on') or {}).get('nodes', []):
            producer_id = model_id_map.get(dep_id) or source_id_map.get(dep_id)
            if not producer_id or producer_id == consumer_id:
                continue
            producer_type = 'DBT_SOURCE' if dep_id in source_id_map else 'DBT_MODEL'
            dep_node = sources.get(dep_id) or nodes.get(dep_id, {})
            producer_name = dep_node.get('name', dep_id.split('.')[-1])
            if producer_type == 'DBT_SOURCE':
                src_name = dep_node.get('source_name', '')
                if src_name:
                    producer_name = f'{src_name}.{producer_name}'

            graph_edges.append(_make_edge(
                source_id=f'{producer_type}::{producer_id}',
                source=producer_name,
                source_type=producer_type,
                target_id=f'{consumer_type}::{consumer_id}',
                target=consumer_name,
                target_type=consumer_type,
                workspace_id=workspace_id,
            ))

    # ──────────────────────────────────────────────
    # 5b. COLUMN-LEVEL LINEAGE (sqlglot)
    # ──────────────────────────────────────────────
    if extract_column_edges is not None:
        print("[CLL] Extracting column-level lineage from compiled SQL...")
        try:
            col_edges = extract_column_edges(
                model_nodes, sources, catalog_lookup, col_index, project_name,
            )
        except Exception as exc:  # never let CLL break the run
            print(f"[CLL] Column lineage extraction failed, skipping: {exc}")
            col_edges = []
        for src_id, src_name, tgt_id, tgt_name, lineage_type in col_edges:
            graph_edges.append(_make_edge(
                source_id=f'DBT_COLUMN::{src_id}',
                source=src_name,
                source_type='DBT_COLUMN',
                target_id=f'DBT_COLUMN::{tgt_id}',
                target=tgt_name,
                target_type='DBT_COLUMN',
                workspace_id=workspace_id,
                lineage_type=lineage_type,
            ))
    else:
        print("[CLL] sqlglot not available — skipping column-level lineage.")

    # ──────────────────────────────────────────────
    # 6. DEDUPLICATE & SAVE
    # ──────────────────────────────────────────────
    print("[DEDUP] Deduplicating edges...")
    seen_edges = set()
    unique_edges = []
    for e in graph_edges:
        key = (e['source_id'], e['target_id'])
        if key not in seen_edges:
            seen_edges.add(key)
            unique_edges.append(e)

    os.makedirs(output_dir, exist_ok=True)

    # Save items CSV
    if item_rows:
        items_df = pd.DataFrame(item_rows)
        items_df = items_df.fillna('')
        items_df = items_df.replace({'nan': '', 'NaN': '', 'None': ''})

        # Deduplicate items on item_id
        dup_mask = items_df.duplicated(subset=['item_id'], keep=False)
        if dup_mask.any():
            dup_ids = items_df[dup_mask]['item_id'].unique()
            print(f"[WARNING] Found {len(dup_ids)} duplicate item_ids. Keeping last.")
            items_df = items_df.drop_duplicates(subset=['item_id'], keep='last')

        items_path = os.path.join(output_dir, 'dbt_info_items.csv')
        items_df.to_csv(items_path, index=False, encoding='utf-8-sig')
        print(f"[SUCCESS] Saved: {items_path} ({len(items_df)} rows)")

    # Save graph CSV
    if unique_edges:
        graph_df = pd.DataFrame(unique_edges)
        graph_path = os.path.join(output_dir, 'dbt_info_graph.csv')
        graph_df.to_csv(graph_path, index=False, encoding='utf-8-sig')
        print(f"[SUCCESS] Saved: {graph_path} ({len(graph_df)} rows)")
    else:
        print("[WARNING] No graph edges to save.")

    print("[DONE] dbt transform complete.")


def _merge_column_names(manifest_cols, catalog_cols):
    """
    Merge column names from manifest and catalog, preserving order.

    Manifest columns come first (user-documented), then any additional
    columns discovered only in catalog are appended.
    """
    seen = set()
    ordered = []

    # Manifest columns first (preserves author's ordering)
    for name in manifest_cols:
        lower = name.lower()
        if lower not in seen:
            seen.add(lower)
            ordered.append(name)

    # Catalog-only columns appended (sorted by index if available)
    catalog_only = [
        (info.get('index', 9999), info.get('name', name))
        for name, info in catalog_cols.items()
        if name.lower() not in seen
    ]
    catalog_only.sort(key=lambda x: x[0])
    for _, name in catalog_only:
        ordered.append(name)

    return ordered


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--repo-dir', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--catalog', default=None, help='Path to catalog.json (optional)')
    args = parser.parse_args()
    main(args.manifest, args.repo_dir, args.output_dir, catalog_path=args.catalog)
