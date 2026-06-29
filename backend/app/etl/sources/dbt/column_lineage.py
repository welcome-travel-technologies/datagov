"""
Column-level lineage extraction for dbt models using sqlglot.

The dbt manifest already carries each model's *compiled* SQL (``compiled_code``
— refs resolved to real ``project.dataset.table`` relations). For every model
output column we ask sqlglot which upstream relation column(s) it derives from,
then emit ``DBT_COLUMN -> DBT_COLUMN`` edges (producer column -> consumer
column) using the *same* item-ids ``transform_dbt`` assigns to dbt columns.

Because each model's compiled SQL only references its *direct* upstream
relations, the per-model edges chain together across the whole project into a
full column DAG once every model has been processed.

SQL source (see ``resolve_sql``): dbt's manifest ``compiled_code`` is primary;
when it's absent we fall back to resolving ``{{ ref() }}`` / ``{{ source() }}``
from ``raw_code`` ourselves, so lineage survives manifests that weren't fully
compiled.

Design notes:
  - Pure-python, no Django — unit-testable in isolation.
  - Defensive: a single unparseable model (or column) is skipped and counted,
    never fatal to the ETL run.
  - ``catalog.json`` column types feed a sqlglot schema so ``SELECT *`` models
    expand to real columns instead of a single ``*`` leaf.

Limitations (logged, not fatal): dbt Python models have no SQL; models whose SQL
neither compiled nor cleanly resolves (heavy macros, ``{% %}`` control flow) are
skipped; exotic BigQuery constructs may yield partial lineage.
"""
import re

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage

_DIALECT = 'bigquery'
_RE_NORM = re.compile(r"[\s'\"`]+")


def _norm(value):
    """Lower-case and strip whitespace / quotes / backticks. '' on None."""
    if value is None:
        return ''
    return _RE_NORM.sub('', str(value).strip().lower())


def _col_key(name):
    """Normalized key for matching a column name across SQL / manifest / catalog."""
    return _norm(name)


def _classify_lineage_type(expr):
    """Classify how an output column is derived from its single projection.

    - ``pass-through``: a plain column, or an alias whose name equals the column
    - ``rename``:       an alias over a plain column with a different name
    - ``transformation``: any expression (function, arithmetic, CASE, …)
    - ``unknown``:      projection not available

    A column fed by 2+ upstream columns is forced to ``transformation`` by the
    caller, so this only needs to judge the single-input shape.
    """
    if expr is None:
        return 'unknown'
    if isinstance(expr, exp.Alias):
        inner = expr.this
        if isinstance(inner, exp.Column):
            return 'pass-through' if (inner.name or '').lower() == (expr.alias or '').lower() else 'rename'
        return 'transformation'
    if isinstance(expr, exp.Column):
        return 'pass-through'
    return 'transformation'


# ── Jinja ref/source resolution (compiler-lite) ────────────────────────────────
# Backup for when the manifest carries no ``compiled_code`` (e.g. it was produced
# by ``dbt parse`` rather than a full ``dbt docs generate``, or a future
# dbt/Fusion changes what it emits). We resolve ``{{ ref() }}`` / ``{{ source() }}``
# from ``raw_code`` using the manifest's own relation names — a minimal "compiler"
# so column lineage still works when dbt's compiled SQL is unavailable.
_RE_REF = re.compile(r"\{\{[-\s]*ref\((.*?)\)[-\s]*\}\}", re.DOTALL)
_RE_SOURCE = re.compile(r"\{\{[-\s]*source\((.*?)\)[-\s]*\}\}", re.DOTALL)
_RE_THIS = re.compile(r"\{\{[-\s]*this[-\s]*\}\}")
_RE_CONFIG = re.compile(r"\{\{[-\s]*config\(.*?\)[-\s]*\}\}", re.DOTALL)
_RE_LITERAL = re.compile(r"""^\s*['"]([^'"]*)['"]\s*$""")


def _relation_name(node):
    """Real, dialect-quoted relation for a model/source node.

    Prefers the manifest's ``relation_name``; falls back to building
    ``\\`db\\`.\\`schema\\`.\\`name\\``` from the split fields.
    """
    rel = node.get('relation_name')
    if rel:
        return rel
    name = node.get('alias') or node.get('identifier') or node.get('name')
    parts = [p for p in (node.get('database'), node.get('schema'), name) if p]
    return '.'.join(f'`{p}`' for p in parts)


def _literal_args(inner):
    """Parse ``'a', 'b'`` into ``['a', 'b']``; None if any arg isn't a plain literal.

    Returning None on a non-literal arg (e.g. ``ref(var('x'))``) means we leave
    the Jinja untouched and the model is skipped rather than mis-resolved.
    """
    args = []
    for part in inner.split(','):
        m = _RE_LITERAL.match(part)
        if not m:
            return None
        args.append(m.group(1))
    return args


def _resolve_from_raw(node, model_nodes, sources, glob_model_rel, glob_source_rel):
    """Resolve ``raw_code``'s Jinja refs to real relations, or None if we can't.

    Resolves ``{{ ref() }}`` / ``{{ source() }}`` / ``{{ this }}`` against the
    manifest relation maps (scoped first to this node's ``depends_on``, then
    global) and strips ``{{ config() }}``. Returns None when the SQL is empty or
    still contains unresolved Jinja (macros, ``{% %}`` blocks, ``var()`` …) — we
    never feed half-templated SQL to sqlglot.
    """
    raw = node.get('raw_code') or node.get('raw_sql')
    if not raw or not raw.strip():
        return None

    dep_uids = (node.get('depends_on') or {}).get('nodes') or []
    dep_model_rel = {}
    dep_source_rel = {}
    for uid in dep_uids:
        if uid in model_nodes:
            dep_model_rel[(model_nodes[uid].get('name') or '').lower()] = _relation_name(model_nodes[uid])
        elif uid in sources:
            s = sources[uid]
            dep_source_rel[((s.get('source_name') or '').lower(),
                            (s.get('name') or '').lower())] = _relation_name(s)
    own_rel = _relation_name(node)

    def _ref(m):
        args = _literal_args(m.group(1))
        if not args:
            return m.group(0)
        name = args[-1].lower()  # ref('pkg', 'model') -> model is the last arg
        return dep_model_rel.get(name) or glob_model_rel.get(name) or m.group(0)

    def _source(m):
        args = _literal_args(m.group(1))
        if not args or len(args) < 2:
            return m.group(0)
        key = (args[0].lower(), args[1].lower())
        return dep_source_rel.get(key) or glob_source_rel.get(key) or m.group(0)

    sql = _RE_REF.sub(_ref, raw)
    sql = _RE_SOURCE.sub(_source, sql)
    sql = _RE_THIS.sub(own_rel, sql)
    sql = _RE_CONFIG.sub('', sql)

    if '{{' in sql or '{%' in sql:
        return None  # unresolved Jinja — skip rather than mis-parse
    return sql


def resolve_sql(node, model_nodes, sources, glob_model_rel, glob_source_rel):
    """Return ``(sql, origin)`` for a model, or ``(None, None)`` if unusable.

    dbt's own artifacts are the source of truth: the manifest's ``compiled_code``
    is tried **first** (macros already expanded by dbt). Our raw-code resolver is
    the **backup** for models dbt didn't compile. The two produce equivalent SQL
    for ref-only models, so precedence only matters for macro-heavy models —
    where compiled wins.

    To change precedence or add a source later (e.g. a warehouse
    INFORMATION_SCHEMA provider), reorder / extend ``providers`` below — nothing
    else changes.
    """
    providers = (
        ('compiled', lambda: node.get('compiled_code') or node.get('compiled_sql')),
        ('raw', lambda: _resolve_from_raw(node, model_nodes, sources,
                                          glob_model_rel, glob_source_rel)),
    )
    for origin, provider in providers:
        sql = provider()
        if sql and sql.strip():
            return sql, origin
    return None, None


def build_relation_index(model_nodes, sources):
    """Map a normalized relation name -> dbt ``unique_id``.

    Indexes the 3-part (``db.schema.name``) form for every model and source, and
    the 2-part (``schema.name``) form when it is unambiguous, so a bare
    ``schema.table`` reference still resolves without risking a wrong
    cross-schema match.

    Returns ``(three_part_index, two_part_index)``.
    """
    three = {}
    two = {}
    two_counts = {}

    def add(database, schema, name, uid):
        if not name:
            return
        k3 = f"{_norm(database)}.{_norm(schema)}.{_norm(name)}"
        three[k3] = uid
        k2 = f"{_norm(schema)}.{_norm(name)}"
        two[k2] = uid
        two_counts[k2] = two_counts.get(k2, 0) + 1

    for uid, node in model_nodes.items():
        add(node.get('database'), node.get('schema'),
            node.get('alias') or node.get('name'), uid)
    for uid, src in sources.items():
        add(src.get('database'), src.get('schema'),
            src.get('identifier') or src.get('name'), uid)

    two = {k: v for k, v in two.items() if two_counts.get(k) == 1}
    return three, two


def _resolve_uid(table_name, three, two):
    """Resolve sqlglot's ``exp.table_name()`` string to a dbt ``unique_id``."""
    parts = [_norm(p) for p in str(table_name).split('.')]
    if len(parts) >= 3:
        uid = three.get('.'.join(parts[-3:]))
        if uid:
            return uid
    if len(parts) >= 2:
        uid = two.get('.'.join(parts[-2:]))
        if uid:
            return uid
    return None


def build_schema(model_nodes, sources, catalog_lookup):
    """Build a nested sqlglot schema ``{db: {schema: {table: {col: type}}}}``.

    Sourced from ``catalog.json`` column types (real DB types). Tables without
    catalog columns are omitted — sqlglot simply won't expand ``*`` for them.
    """
    schema = {}

    def add(database, schema_name, table, uid):
        if not table:
            return
        cat = catalog_lookup.get(uid) or {}
        cols = {}
        for info in (cat.get('columns') or {}).values():
            name = info.get('name')
            if name:
                cols[name] = info.get('type') or 'UNKNOWN'
        if cols:
            schema.setdefault(database or '', {}).setdefault(schema_name or '', {})[table] = cols

    for uid, node in model_nodes.items():
        add(node.get('database'), node.get('schema'),
            node.get('alias') or node.get('name'), uid)
    for uid, src in sources.items():
        add(src.get('database'), src.get('schema'),
            src.get('identifier') or src.get('name'), uid)
    return schema


def extract_column_edges(model_nodes, sources, catalog_lookup, col_index,
                         project_name, log=print):
    """Extract ``DBT_COLUMN -> DBT_COLUMN`` lineage edges.

    Args:
        model_nodes: ``{unique_id: manifest_node}`` for models/seeds/snapshots.
        sources: ``{unique_id: manifest_source}``.
        catalog_lookup: output of ``transform_dbt._build_catalog_lookup``.
        col_index: ``{unique_id: {col_key: (display_name, dbt_column_item_id)}}``
            built by ``transform_dbt`` while it creates the column items, so the
            ids here are guaranteed to match existing ``DBT_COLUMN`` nodes.
        project_name: dbt project name (kept for symmetry / future use).
        log: callable for progress output (defaults to ``print``).

    Returns:
        List of ``(producer_col_id, producer_col_name, consumer_col_id,
        consumer_col_name, lineage_type)`` tuples — producer is the upstream
        column; ``lineage_type`` is how the consumer column is derived
        ('pass-through' | 'rename' | 'transformation' | 'unknown').
    """
    three, two = build_relation_index(model_nodes, sources)
    schema = build_schema(model_nodes, sources, catalog_lookup)

    # Global relation maps for resolving {{ ref() }} / {{ source() }} ourselves.
    glob_model_rel = {
        (n.get('name') or '').lower(): _relation_name(n) for n in model_nodes.values()
    }
    glob_source_rel = {
        ((s.get('source_name') or '').lower(), (s.get('name') or '').lower()): _relation_name(s)
        for s in sources.values()
    }

    edges = []
    seen = set()
    total = len(model_nodes)
    with_sql = 0
    from_raw = 0
    from_compiled = 0
    models_with_edges = 0
    parse_failures = 0

    # Heartbeat: this per-model sqlglot pass is the slowest part of the dbt
    # transform (minutes on large projects). Emit ~25 progress lines so the run
    # log shows forward motion instead of going silent for the whole pass.
    processed = 0
    heartbeat = max(1, total // 25)

    for uid, node in model_nodes.items():
        processed += 1
        if processed % heartbeat == 0 or processed == total:
            log(f"[CLL] progress: {processed}/{total} models "
                f"({len(edges)} edges so far)")
        sql, origin = resolve_sql(node, model_nodes, sources, glob_model_rel, glob_source_rel)
        if not sql or not sql.strip():
            continue
        out_cols = col_index.get(uid)
        if not out_cols:
            continue

        # Validate the SQL parses once so a bad model is skipped cheaply
        # instead of failing once per column.
        try:
            sqlglot.parse_one(sql, dialect=_DIALECT)
        except Exception:
            parse_failures += 1
            continue
        with_sql += 1
        if origin == 'raw':
            from_raw += 1
        else:
            from_compiled += 1

        model_had_edge = False
        for (display_name, consumer_id) in out_cols.values():
            try:
                root = lineage(display_name, sql, schema=schema, dialect=_DIALECT)
            except Exception:
                try:
                    # Retry without schema: loses SELECT * expansion but still
                    # resolves explicit column references.
                    root = lineage(display_name, sql, dialect=_DIALECT)
                except Exception:
                    continue

            base_type = _classify_lineage_type(getattr(root, 'expression', None))
            producers = []  # unique (prod_id, prod_name) feeding this column
            for n in root.walk():
                src = n.source
                if not isinstance(src, exp.Table):
                    continue
                prod_uid = _resolve_uid(exp.table_name(src), three, two)
                if not prod_uid or prod_uid == uid:
                    continue
                prod_col = col_index.get(prod_uid, {}).get(_col_key(n.name.split('.')[-1]))
                if not prod_col:
                    continue
                prod_name, prod_id = prod_col
                if prod_id == consumer_id:
                    continue
                key = (prod_id, consumer_id)
                if key in seen:
                    continue
                seen.add(key)
                producers.append((prod_id, prod_name))

            if not producers:
                continue
            # 2+ upstream columns => the output column combines them => transformation.
            ltype = 'transformation' if len(producers) >= 2 else base_type
            for prod_id, prod_name in producers:
                edges.append((prod_id, prod_name, consumer_id, display_name, ltype))
            model_had_edge = True

        if model_had_edge:
            models_with_edges += 1

    log(f"[CLL] models={total} parseable_with_sql={with_sql} "
        f"(raw={from_raw} compiled={from_compiled}) parse_failures={parse_failures} "
        f"models_with_lineage={models_with_edges} -> {len(edges)} column-level edges")
    return edges
