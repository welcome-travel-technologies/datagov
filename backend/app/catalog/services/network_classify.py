"""Single source of truth for classifying a ``NetworkEdge``.

Historically the graph derived "is this asset- or column-level lineage?" on
every read by string-splitting the ``TYPE::hash`` node-id prefixes
(``views._edge_kind``). That made the classification impossible to index or
filter in SQL, which is why asset-mode traversal couldn't cheaply exclude
column edges and ended up polluted.

We now *persist* the classification on each edge as two columns:

* ``kind``  — the semantic edge type, one of:
    - ``contains``  container -> member  (DBT_MODEL/SEED/SOURCE/PB_TABLE -> column/measure)
    - ``column``    column<->column / measure->column lineage (incl. the cross-tool bridge)
    - ``model``     model<->model, source->model, report hierarchy, table-level bridge
* ``level`` — which lineage *view* the edge belongs to: ``asset`` or ``column``.

This module is the ONE place the rules live. It is consumed by:
  * the ETL loaders (via the SQL ``CASE`` builders, computed from the
    ``source_type``/``target_type`` columns the graph CSVs already carry),
  * ``bridge_builder`` (via :func:`classify_edge` on the known bridge types),
  * the backfill migration and ``views`` (via :func:`classify_node_ids`).

The ``kind`` values intentionally match the strings the graph frontend already
styles on (``column``/``contains``), so nothing in the UI has to change.
"""

# The report/usage hierarchy — report → page → visual, and anything a visual
# references. This is the *structural* world and lives in the asset view.
PB_STRUCT_TYPES = ('PB_VISUAL', 'PB_PAGE', 'PB_REPORT')
# Node types that act as containers (boxes that hold columns/measures/fields).
CONTAINER_TYPES = ('DBT_MODEL', 'DBT_SEED', 'DBT_SOURCE', 'PB_TABLE')
# Pure column types.
COLUMN_TYPES = ('DBT_COLUMN', 'PB_COLUMN')
# Field-level lineage members: columns + PowerBI report fields. These are the
# nodes the *column* view traces through.
FIELD_TYPES = COLUMN_TYPES + ('PB_FIELD',)
# Everything that lives *inside* a container / participates in field-level
# lineage (drawn as children in the column view). Measures are members too.
MEMBER_TYPES = FIELD_TYPES + ('PB_MEASURE',)

# kind values (semantic edge type — also what the graph frontend styles on)
KIND_CONTAINS = 'contains'
KIND_COLUMN = 'column'
KIND_MODEL = 'model'

# level values (which view an edge belongs to). ``both`` lets an edge appear in
# both views — measures and field↔measure links are hinges between the two.
LEVEL_ASSET = 'asset'
LEVEL_COLUMN = 'column'
LEVEL_BOTH = 'both'


def classify_edge(source_type, target_type):
    """Return ``(kind, level)`` for an edge between two node *types*.

    ``kind`` is the semantic edge type (``contains``/``column``/``model``);
    ``level`` is which lineage view the edge belongs to — ``asset``, ``column``
    or ``both``. The views overlap rather than partition: a measure is a hinge
    that appears in the structural (asset) view AND the derivation (column) view.
    """
    st = (source_type or '').strip()
    tt = (target_type or '').strip()

    # 1. Anything touching the report/page/visual hierarchy is structural usage
    #    → asset view (keeps visuals out of the column-derivation view).
    if st in PB_STRUCT_TYPES or tt in PB_STRUCT_TYPES:
        return KIND_MODEL, LEVEL_ASSET
    # 2. Container → member (the box a member sits in). A measure's box is
    #    meaningful in both views; a column/field box only in the column view.
    if st in CONTAINER_TYPES and tt in MEMBER_TYPES:
        return KIND_CONTAINS, (LEVEL_BOTH if tt == 'PB_MEASURE' else LEVEL_COLUMN)
    # 3. Field-/measure-level relationships: derivation, DAX, the cross-tool
    #    bridge, and report-field references. A field↔measure link bridges the
    #    report-field world to a measure, so it shows in both views.
    if st in MEMBER_TYPES or tt in MEMBER_TYPES:
        if {st, tt} == {'PB_FIELD', 'PB_MEASURE'}:
            return KIND_COLUMN, LEVEL_BOTH
        return KIND_COLUMN, LEVEL_COLUMN
    # 4. Structural model graph (model↔model, source→model, →test) + table bridge.
    return KIND_MODEL, LEVEL_ASSET


def _node_type(node_id):
    nid = node_id or ''
    return nid.split('::', 1)[0] if '::' in nid else ''


def classify_node_ids(source_id, target_id):
    """Like :func:`classify_edge` but derives types from ``TYPE::hash`` ids."""
    return classify_edge(_node_type(source_id), _node_type(target_id))


# ── SQL CASE builders (for the COPY-based loaders) ──────────────────────────
# Values are module constants (never user input) so direct interpolation is
# safe. ``st``/``tt`` name the type columns in the loader's temp table.
def _in_sql(col, vals):
    quoted = ', '.join("'%s'" % v for v in vals)
    return '%s IN (%s)' % (col, quoted)


def kind_case_sql(st='source_type', tt='target_type'):
    """SQL expression yielding the ``kind`` string from two type columns.
    Mirrors :func:`classify_edge` exactly."""
    return (
        'CASE '
        "WHEN %s OR %s THEN '%s' "                # PB struct/usage -> model
        "WHEN %s AND %s THEN '%s' "               # container -> member -> contains
        "WHEN %s OR %s THEN '%s' "                # field/measure level -> column
        "ELSE '%s' END"
    ) % (
        _in_sql(st, PB_STRUCT_TYPES), _in_sql(tt, PB_STRUCT_TYPES), KIND_MODEL,
        _in_sql(st, CONTAINER_TYPES), _in_sql(tt, MEMBER_TYPES), KIND_CONTAINS,
        _in_sql(st, MEMBER_TYPES), _in_sql(tt, MEMBER_TYPES), KIND_COLUMN,
        KIND_MODEL,
    )


def level_case_sql(st='source_type', tt='target_type'):
    """SQL expression yielding the ``level`` string from two type columns.
    Mirrors :func:`classify_edge` exactly (``asset``/``column``/``both``)."""
    field_measure = (
        "((%s = 'PB_FIELD' AND %s = 'PB_MEASURE') OR (%s = 'PB_MEASURE' AND %s = 'PB_FIELD'))"
    ) % (st, tt, st, tt)
    return (
        'CASE '
        "WHEN %s OR %s THEN '%s' "                       # PB struct/usage -> asset
        "WHEN %s AND %s = 'PB_MEASURE' THEN '%s' "       # container -> measure -> both
        "WHEN %s AND %s THEN '%s' "                      # container -> column/field -> column
        "WHEN %s THEN '%s' "                             # field<->measure -> both
        "WHEN %s OR %s THEN '%s' "                       # other field/measure level -> column
        "ELSE '%s' END"
    ) % (
        _in_sql(st, PB_STRUCT_TYPES), _in_sql(tt, PB_STRUCT_TYPES), LEVEL_ASSET,
        _in_sql(st, CONTAINER_TYPES), tt, LEVEL_BOTH,
        _in_sql(st, CONTAINER_TYPES), _in_sql(tt, MEMBER_TYPES), LEVEL_COLUMN,
        field_measure, LEVEL_BOTH,
        _in_sql(st, MEMBER_TYPES), _in_sql(tt, MEMBER_TYPES), LEVEL_COLUMN,
        LEVEL_ASSET,
    )
