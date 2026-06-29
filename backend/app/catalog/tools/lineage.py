"""
Lineage / dependency tools — query the materialized network graph
(``NetworkNode``, ``NetworkEdge``) for upstream / downstream / cross-system
relationships. Pure read-only catalog operations.
"""
from django.db.models import Q

from ..models import Item, NetworkNode, NetworkEdge


# Resolution preference: a real measure / table / report / workspace should win
# over a column / field / page / visual that merely contains the same words.
_PB_TYPE_RANK = {
    'PB_MEASURE': 0, 'PB_TABLE': 1, 'PB_REPORT': 2, 'PB_WORKSPACE': 3,
    'PB_COLUMN': 7, 'PB_FIELD': 8, 'PB_PAGE': 9, 'PB_VISUAL': 9,
}


def get_lineage(node_name: str) -> str:
    """
    Returns the upstream dependencies and downstream dependants for a single
    node in the lineage graph.

    Use this ONLY for a quick ONE-HOP neighbour check ("what directly feeds X?",
    "what directly depends on X?"). For a full "where is X used?" / impact answer
    (the WHOLE downstream subtree: measure → visual → report) call the item
    profiler instead — ``get_pb_item_details(name)`` for PowerBI or
    ``get_dbt_item_details(name)`` for dbt — its "Used by" section already walks
    every hop in one call. NEVER call this tool hop-by-hop to traverse the graph
    yourself: that loops without converging. Call it at most once or twice.

    This tool does NOT search — it resolves an EXACT name or id only. Pass ONE of:
      • An asset name copied VERBATIM from the catalog in your prompt (e.g.
        "Driver Availability"). Partial guesses ("Ops", "Ops Reports") will NOT
        match — read the catalog for the exact name rather than guessing here.
        If one exact name maps to several nodes (same name across datasets) the
        tool returns a short disambiguation list to pick from.
      • A composite graph ID (e.g. "PB_MEASURE::<hash>") for an exact lookup.

    Do NOT use this tool to FIND or RESOLVE an item, and do NOT pass an item_id —
    pass the *name* (verbatim) or the *node_id*. To list the measures a REPORT
    uses, call ``get_pb_item_details(report_name)`` (its "Uses" section), not this.
    """
    if not node_name:
        return 'Please provide a node name to look up.'

    # 1. Try exact composite id match (fastest, unambiguous)
    matches = list(NetworkNode.objects.filter(node_id=node_name))

    # 2. Otherwise match by EXACT display name only. No `icontains` fallback:
    #    a partial/contains match turned this one-hop tool into a de-facto
    #    catalog SEARCH — the agent looped it on name fragments ("Ops", "Ops
    #    Reports", "Operations") to hunt for an item, which the context-first
    #    design deliberately omits. The agent must pass a name straight from the
    #    front-loaded catalog.
    if not matches:
        matches = list(NetworkNode.objects.filter(name__iexact=node_name)[:25])

    if not matches:
        return (
            f"No node is named exactly '{node_name}'. This tool does not "
            f"search, so do NOT retry with name variations. The full measure / "
            f"report / model inventory is already in the catalog in your prompt "
            f"— read it for the exact name. For an item's measures or where it "
            f"is used, call get_pb_item_details(name) (PowerBI) or "
            f"get_dbt_item_details(name) (dbt) instead of this one-hop tool."
        )

    if len(matches) > 1:
        rows = '\n'.join(
            f'- {n.name or n.node_id} ({n.group or "UNKNOWN"}) [id={n.node_id}]'
            for n in matches
        )
        return (
            f"'{node_name}' matches {len(matches)} nodes. Please pick one and "
            f're-run the lookup with its full id:\n{rows}'
        )

    node = matches[0]
    edges = NetworkEdge.objects.filter(Q(source=node.node_id) | Q(target=node.node_id))
    if not edges.exists():
        return f"Node '{node.name or node.node_id}' ({node.group}) has no connections."

    upstream = []
    downstream = []
    for edge in edges:
        other = edge.source if edge.target == node.node_id else edge.target
        other_node = NetworkNode.objects.filter(node_id=other).only('name', 'group').first()
        label = (other_node.name if other_node and other_node.name else other)
        grp = other_node.group if other_node else 'UNKNOWN'
        entry = f'{label} ({grp})'
        if edge.target == node.node_id:
            upstream.append(entry)
        else:
            downstream.append(entry)

    return (
        f"Lineage for '{node.name or node.node_id}' ({node.group}):\n"
        f"Upstream dependencies (what it relies on): {', '.join(upstream) if upstream else 'None'}\n"
        f"Downstream impacts (what relies on it): {', '.join(downstream) if downstream else 'None'}"
    )


def where_is_used(node_name: str, direction: str = 'downstream', workspace_id: str = '') -> str:
    """
    One-shot "where is X used?" / impact analysis. Returns ALL consumers of an
    asset — the reports and measures that depend on it — by walking the WHOLE
    downstream lineage subtree (measure -> visual -> report) in a SINGLE call.

    USE THIS (not repeated ``get_lineage`` calls) for:
      • "where is X used?" / "which reports use measure/column/table Y?"
      • "is Z used anywhere?" / "is this table referenced in any report?"
      • impact analysis — "what breaks if I change / delete X?"

    Do NOT walk ``get_lineage`` hop-by-hop to answer these — this tool already
    traverses every hop at once and is far cheaper.

    ``node_name``   — measure / column / table / model name, or a composite
                      ``PB_*::<id>`` node id. Ambiguous names return a short
                      candidate list — present it and ask the user to pick one.
    ``direction``   — ``'downstream'`` (default) = consumers / where it is used;
                      ``'upstream'`` = sources it relies on; ``'both'``.
    ``workspace_id``— optional PowerBI workspace constraint.

    Reports and consumer measures are listed by name. Visuals (and other node
    types) are summarised as COUNTS only — they are too numerous to list and
    each one rolls up into a report.
    """
    from ..services.network_path import find_reachable_nodes, resolve_node_id_by_name

    if not node_name:
        return 'Please provide an asset name to look up.'
    if direction not in ('downstream', 'upstream', 'both'):
        direction = 'downstream'

    candidates = resolve_node_id_by_name(node_name)
    if not candidates:
        return f"Node '{node_name}' not found in the lineage graph."
    if len(candidates) > 1 and not any(c.node_id == node_name for c in candidates):
        rows = '\n'.join(
            f'- {c.name or c.node_id} ({c.group or "UNKNOWN"}) [id={c.node_id}]'
            for c in candidates[:10]
        )
        more = '' if len(candidates) <= 10 else f"\n…and {len(candidates) - 10} more."
        return (
            f"'{node_name}' matches {len(candidates)} nodes. Present this list and "
            f"ask the user to pick one, then re-run with its id:\n{rows}{more}"
        )

    node = candidates[0]

    # 'both' = union of the upstream and downstream reachable sets.
    dirs = ['upstream', 'downstream'] if direction == 'both' else [direction]
    by_group: dict = {}          # group -> {node_id: (label, distance)}
    truncated = False
    for d in dirs:
        res = find_reachable_nodes(node.node_id, direction=d, workspace_id=workspace_id)
        truncated = truncated or res.truncated
        for rn in res.nodes:
            g = (rn.group or 'UNKNOWN').upper()
            bucket = by_group.setdefault(g, {})
            prev = bucket.get(rn.id)
            if prev is None or rn.distance < prev[1]:
                bucket[rn.id] = (rn.label, rn.distance)

    if not by_group:
        verb = {'downstream': 'consumers', 'upstream': 'sources',
                'both': 'connections'}[direction]
        return (f"**{node.name or node.node_id}** ({node.group}) has no {verb} "
                f"in the lineage graph.")

    # Headline counts (human-friendly singular/plural).
    label_map = [('PB_REPORT', 'report'), ('PB_MEASURE', 'measure'),
                 ('PB_VISUAL', 'visual'), ('PB_TABLE', 'table'),
                 ('PB_COLUMN', 'column'), ('DBT_MODEL', 'dbt model')]
    headline = []
    for g, lbl in label_map:
        c = len(by_group.get(g, {}))
        if c:
            headline.append(f"{c} {lbl}{'' if c == 1 else 's'}")

    arrow = {'downstream': 'is used by', 'upstream': 'depends on',
             'both': 'is connected to'}[direction]
    lines = [f"**{node.name or node.node_id}** ({node.group}) {arrow}: "
             + (', '.join(headline) if headline else 'nothing') + '.']

    # List the meaningful consumers (reports, measures, dbt models). Visuals and
    # every other node type are summarised as counts only — not listed.
    LIST_GROUPS = [('PB_REPORT', 'Reports'), ('PB_MEASURE', 'Measures'),
                   ('DBT_MODEL', 'dbt models')]
    CAP = 60
    for g, title in LIST_GROUPS:
        vals = sorted(by_group.get(g, {}).values(), key=lambda lv: (lv[1], (lv[0] or '').lower()))
        if not vals:
            continue
        # Unique display labels, preserving the distance/name ordering.
        labels = list(dict.fromkeys(lbl for lbl, _ in vals))
        names = ', '.join(labels[:CAP])
        extra = '' if len(labels) <= CAP else f" …(+{len(labels) - CAP} more)"
        lines.append(f"\n**{title} ({len(vals)}):** {names}{extra}")

    listed = {g for g, _ in LIST_GROUPS}
    others = [f"{g}: {len(by_group[g])}" for g in sorted(by_group) if g not in listed]
    if others:
        lines.append("\n**Other connected nodes (counts only):** " + ', '.join(others))

    if truncated:
        lines.append("\n_(Traversal hit the graph node cap — counts may be "
                     "partial; narrow by workspace to be exhaustive.)_")

    return '\n'.join(lines)


def _person_name(p):
    if not p:
        return None
    return getattr(p, 'name', None) or getattr(p, 'full_name', None) or str(p)


def _governance_and_usage_block(item, node_id=None, include_used_by=True,
                                include_uses=True):
    """Ownership + description + precomputed usage stats + the upstream ``Uses``
    graph and the downstream ``Used by`` graph for any Item. Reused across every
    element type so every profile carries the same governance/usage footer.
    ``include_used_by`` / ``include_uses`` are set False when the type-specific
    section already lists that direction (e.g. the dbt model schema lists both
    upstream lineage and downstream consumers), to avoid duplicating a block."""
    lines = ['\n## Ownership, description & usage\n']

    owner = _person_name(getattr(item, 'ownership_person', None))
    steward = _person_name(getattr(item, 'steward', None))
    dept = getattr(getattr(item, 'ownership_department', None), 'name', None)
    lines.append(f"- **Owner:** {owner or '—'} &nbsp; **Steward:** {steward or '—'} "
                 f"&nbsp; **Department:** {dept or '—'}")

    desc = (getattr(item, 'custom_description', None) or item.description or '').strip()
    if desc:
        lines.append(f"- **Description:** {desc[:600]}")

    tags = getattr(item, 'tags', None) or []
    if tags:
        lines.append(f"- **Tags:** {', '.join(str(t) for t in tags)}")

    stat_bits = []
    for field, label in (('connected_reports', 'reports'),
                         ('connected_visuals', 'visuals'),
                         ('connected_measures', 'measures'),
                         ('connected_columns', 'columns'),
                         ('connected_tables', 'tables')):
        v = getattr(item, field, 0) or 0
        if v:
            stat_bits.append(f"{v} {label}")
    usage = ', '.join(stat_bits) if stat_bits else 'no connections recorded'
    if getattr(item, 'is_unused', False):
        usage += '  ⚠️ flagged UNUSED'
    lines.append(f"- **Usage stats:** {usage}")

    nid = node_id or f"{item.item_type}::{item.item_id}"

    # Upstream sources — what this item is built FROM. For a REPORT this is the
    # measures & tables it displays, which is the most useful direction (a report
    # has essentially no downstream); without it, "what measures are in report X?"
    # could not be answered. For a measure it is the tables/measures it depends on.
    if include_uses:
        uses = where_is_used(nid, direction='upstream')
        if 'not found in the lineage graph' in uses:
            uses = where_is_used(item.item_name or '', direction='upstream')
        lines.append('\n### Uses (upstream sources)\n' + uses)

    # Full downstream consumer graph (reports listed, visuals counted).
    if include_used_by:
        used = where_is_used(nid, direction='downstream')
        if 'not found in the lineage graph' in used:
            used = where_is_used(item.item_name or '', direction='downstream')
        lines.append('\n### Used by (downstream consumers)\n' + used)

    return '\n'.join(lines)


def _table_columns_block(item):
    """List a table/model's columns from the catalog."""
    cols = (
        list(Item.objects.filter(deleted=False, item_type='PB_COLUMN',
                                 dataset_id=item.dataset_id, table_name=item.item_name)
             .order_by('item_name').values_list('item_name', 'datatype')[:200])
        or list(Item.objects.filter(deleted=False, item_type='PB_COLUMN',
                                    table_name=item.item_name)
                .order_by('item_name').values_list('item_name', 'datatype')[:200])
    )
    if not cols:
        return ''
    rows = ', '.join(f"{n}{f' ({d})' if d else ''}" for n, d in cols)
    return f"\n## Columns ({len(cols)})\n{rows}\n"


def _workspace_profile(name):
    """Summary of a whole PowerBI workspace: contents, datasets, owners."""
    items = Item.objects.filter(deleted=False, workspace_name__iexact=name)
    if not items.exists():
        items = Item.objects.filter(deleted=False, workspace_name__icontains=name)
    if not items.exists():
        return None

    from collections import Counter
    wsname = items.first().workspace_name
    by_type = Counter(items.values_list('item_type', flat=True))
    datasets = sorted({d for d in items.values_list('dataset_name', flat=True) if d})

    lines = [f"# Workspace: **{wsname}**\n"]
    lines.append('**Contents:** ' + ', '.join(f"{n} {t}" for t, n in by_type.most_common()))
    if datasets:
        lines.append(f"\n**Datasets ({len(datasets)}):** " + ', '.join(datasets[:50]))

    owners = Counter()
    for it in (items.select_related('item_group', 'item_group__ownership_person')[:1500]):
        o = _person_name(getattr(it, 'ownership_person', None))
        if o:
            owners[o] += 1
    if owners:
        lines.append('\n**Owners:** ' + ', '.join(f"{o} ({c})" for o, c in owners.most_common(10)))
    return '\n'.join(lines)


def get_pb_item_details(name: str, workspace_id: str = '', dataset_id: str = '') -> str:
    """THE PowerBI item profiler — call this for ANY PowerBI item to get
    one complete profile in ONE call: a measure, table, column, report, or a
    whole workspace. (For dbt models use ``get_dbt_item_details``.)

    Returns, in a single bundle:
      • Identity — name, type, service, workspace, dataset.
      • Definition (FULL detail) — a measure's DAX expression + home/related
        tables + columns + relationships + dataset/workspace ids (everything you
        need to then WRITE a live DAX query); a table's columns; a dbt model's
        SQL / YAML.
      • Ownership & description — owner, steward, department, description, tags.
      • Usage statistics — precomputed counts of connected reports / visuals /
        measures / columns / tables (instant; flags UNUSED items).
      • Uses (upstream sources) — what the item is built FROM. For a REPORT this
        is the measures & tables it displays, so "what / which measures are used
        in report X?" is answered by ONE call here (no need to walk the graph);
        for a measure it is the tables/measures it depends on.
      • Used by — the downstream consumer graph: which reports use it (listed)
        and how many visuals (counted).

    This is the ONLY lookup tool you need for "what is X / how is X built / who
    owns X / where is X used / is X used anywhere / give me X's value". Do NOT
    chain other tools or walk get_lineage to assemble any of this — one call has
    it all. Pass the name straight from the front-loaded catalog listing; if it
    matches several items the tool returns a short candidate list to disambiguate.

    ``name``        — item name, item_id, or a workspace name.
    ``workspace_id`` / ``dataset_id`` — optional; pass after the user picks one
    when a name spans multiple workspaces/datasets.
    """
    q = (name or '').strip()
    if not q:
        return 'Please provide an item, table, measure, report, or workspace name.'

    # PowerBI items only — this is the PowerBI profiler (dbt has its own).
    qs = Item.objects.filter(deleted=False, service='powerbi')
    # Accept a composite lineage id (e.g. "PB_MEASURE::<hash>"); Item.item_id is
    # the bare hash, so strip the "TYPE::" prefix before the id lookup.
    lookup_id = q.split('::', 1)[1] if '::' in q else q
    exact_id = list(qs.filter(item_id=lookup_id)[:1])
    if exact_id:
        matches = exact_id
    else:
        # Rank so a real measure / table / report / workspace wins over a
        # column / field / page / visual that merely contains the same words.
        cands = (list(qs.filter(item_name__iexact=q)[:25])
                 or list(qs.filter(item_name__icontains=q)[:25]))
        cands.sort(key=lambda it: (
            _PB_TYPE_RANK.get((it.item_type or '').upper(), 5),
            -(it.connected_reports or 0),
            -(it.connected_visuals or 0),
        ))
        matches = cands[:10]

    if not matches:
        ws = _workspace_profile(q)
        if ws:
            return ws
        return (f"No catalog item, table, measure, report, or workspace matches "
                f"'{q}'. Check the spelling against the front-loaded catalog listing.")

    # Several matches. Governance lives on the ItemGroup, so the SAME measure
    # recurs across datasets as several Items that all share ONE item_group_id
    # (one owner / definition). Those are NOT a real ambiguity — collapse them:
    # profile the highest-usage instance (matches are pre-sorted by usage) and
    # note the sibling datasets below. Only genuinely different items/groups
    # (distinct item_group_ids) get the ask-which-one disambiguation list.
    if len(matches) > 1:
        group_ids = {m.item_group_id for m in matches}
        same_group = len(group_ids) == 1 and None not in group_ids
        if not same_group:
            rows = '\n'.join(
                f"- **{m.item_name}** ({m.item_type}) — dataset: {m.dataset_name or '?'}, "
                f"workspace: {m.workspace_name or '?'} [id={m.item_id}]"
                for m in matches[:10]
            )
            return (f"'{q}' matches {len(matches)} items:\n{rows}\n"
                    f"If the user named a SINGLE item, present this list and ask which "
                    f"one they mean, then re-run with its id. If they used a plural / "
                    f"named a SET (e.g. 'the Ops reports'), do NOT ask — call this "
                    f"profiler again on EACH id above and aggregate the results.")
        # else: all one measure group → fall through and profile matches[0].

    item = matches[0]
    itype = (item.item_type or '').upper()

    # Group-first: governance lives on the ItemGroup and we KNOW every Item in
    # the group (the shared item_group_id). For a multi-dataset measure group,
    # list ALL sibling instances — queried by group, not just whatever the name
    # happened to match — so a follow-up live DAX query can target a specific
    # dataset. The profile above (definition, owner, where-used) already applies
    # to the whole group, so we never ask the user to pick.
    group_note = ''
    if item.item_group_id:
        members = list(
            Item.objects.filter(deleted=False, item_group_id=item.item_group_id)
            .order_by('-connected_reports', '-connected_visuals', 'dataset_name')
        )
        if len(members) > 1:
            sib_rows = '\n'.join(
                f"- dataset **{m.dataset_name or '?'}** / workspace "
                f"**{m.workspace_name or '?'}** [id={m.item_id}]"
                for m in members
            )
            group_note = (
                f"\n\n## Same measure across {len(members)} datasets (one measure "
                f"group)\nThis name is ONE measure group with shared "
                f"owner/governance (above), present in {len(members)} datasets. The "
                f"profile above is the representative instance and answers any "
                f"definition / ownership / where-used question for the whole group "
                f"— do NOT ask the user to pick. Choose a specific dataset ONLY "
                f"when running a live DAX value:\n{sib_rows}"
            )

    # Workspaces are items too — route them to the workspace summary.
    if itype == 'PB_WORKSPACE':
        ws = _workspace_profile(item.workspace_name or item.item_name)
        if ws:
            return ws

    # Measures: keep the FULL schema bundle (DAX + tables + columns +
    # relationships + ids) verbatim, then add the governance/usage footer.
    if itype == 'PB_MEASURE':
        from .pb_schema_bundle import get_pb_measure_schema
        definition = get_pb_measure_schema(item.item_id, workspace_id, dataset_id)
        return definition + _governance_and_usage_block(item) + group_note

    # All other item types: identity + definition + columns + relationships,
    # then the same governance/usage footer.
    header = [f"# {item.item_name} ({item.item_type})\n"]
    if itype in ('PB_COLUMN', 'PB_FIELD'):
        parent = item.table_name or (item.item_name or '').split('.', 1)[0]
        header.append(
            f"_Note: this is a {itype.replace('PB_', '').lower()} of table "
            f"**{parent}**. If the user actually meant the table itself or a "
            f"specific measure, ask them to confirm rather than guessing._")
    loc = []
    for label, val in (('service', item.service), ('workspace', item.workspace_name),
                       ('dataset', item.dataset_name), ('table', item.table_name),
                       ('datatype', item.datatype)):
        if val:
            loc.append(f"{label}: {val}")
    if loc:
        header.append('**Location:** ' + ', '.join(loc))

    defn = ''
    if item.expression:
        lang = 'sql' if itype.startswith('DBT') else 'dax'
        defn += f"\n## Definition\n```{lang}\n{item.expression}\n```\n"
    if item.compiled_expression and item.compiled_expression != item.expression:
        defn += f"\n## Compiled SQL\n```sql\n{item.compiled_expression}\n```\n"
    if item.properties_yaml:
        defn += f"\n## Properties (YAML)\n```yaml\n{item.properties_yaml}\n```\n"

    cols = _table_columns_block(item) if itype in ('PB_TABLE', 'DBT_MODEL', 'DBT_SOURCE') else ''

    rel = ''
    if getattr(item, 'relationships_json', None):
        try:
            rel = '\n## Relationships\n' + '\n'.join(
                f"- {r}" for r in item.relationships_json[:30]) + '\n'
        except Exception:
            rel = ''

    return '\n'.join(header) + defn + cols + rel + _governance_and_usage_block(item) + group_note


def get_dbt_item_details(name: str) -> str:
    """THE dbt item profiler — call this for ANY dbt model / seed / snapshot
    to get one complete profile in ONE call: its materialization + BigQuery FQN,
    description, columns (with tests), the SQL definition, the upstream lineage
    tree, the downstream consumers, plus owner / steward / usage statistics.

    Pass the model name straight from the listing; ambiguous names return the
    candidates to disambiguate. Use the BigQuery FQN verbatim if you go on to
    run live BigQuery SQL. (For PowerBI items use ``get_pb_item_details``.)
    """
    from .dbt import get_dbt_model_schema

    schema = get_dbt_model_schema(name)
    if 'matches multiple' in schema or 'No dbt model' in schema:
        return schema  # disambiguation / not-found message — return as-is

    q = (name or '').strip()
    lookup_id = q.split('::', 1)[1] if '::' in q else q
    item = (
        Item.objects.filter(deleted=False, service='dbt', item_id=lookup_id).first()
        or Item.objects.filter(
            deleted=False, service='dbt',
            item_type__in=['DBT_MODEL', 'DBT_SEED', 'DBT_SNAPSHOT'],
            item_name__iexact=q,
        ).first()
    )
    if item is None:
        return schema
    # dbt schema already lists BOTH upstream lineage and downstream consumers,
    # so skip both graph blocks here to avoid duplicating them.
    return schema + _governance_and_usage_block(
        item, include_used_by=False, include_uses=False)


def get_pb_measure_dependencies(measure_name_or_id: str) -> str:
    """
    Returns a measure's home table, full DAX expression, and the directly
    upstream / downstream measures connected via PB_MEASURE → PB_MEASURE
    DAX-dependency edges.

    Use this in STEP 2 of MEASURE FLOW, AFTER you have resolved a single
    measure via ``get_pb_item_details``. The DAX expression in the output lets
    you see which columns the measure references; pass each grouping
    dimension the user asked for to ``verify_pb_measure_dimension_link`` to
    confirm a valid relationship path before generating live DAX.

    Does NOT include column-level dependencies — the caller should read the
    Expression block to identify referenced columns and verify them.
    """
    query = (measure_name_or_id or '').strip()
    if not query:
        return 'Please provide a measure name or item_id.'

    qs = Item.objects.filter(deleted=False, item_type='PB_MEASURE', service='powerbi')
    matches = (
        list(qs.filter(item_id=query)[:2])
        or list(qs.filter(item_name__iexact=query)[:10])
        or list(qs.filter(item_name__icontains=query)[:10])
    )
    if not matches:
        return f"No measure found for '{query}'."
    if len(matches) > 1:
        rows = '\n'.join(
            f'- **{m.item_name}** — dataset: {m.dataset_name}, workspace: '
            f'{m.workspace_name} [id={m.item_id}]'
            for m in matches[:10]
        )
        return f"'{query}' matches multiple measures. Re-run with the exact item_id:\n{rows}"

    measure = matches[0]
    measure_node_id = f'PB_MEASURE::{measure.item_id}'

    home_edge = NetworkEdge.objects.filter(
        target=measure_node_id, source__startswith='PB_TABLE::',
    ).first()
    home_label = '?'
    if home_edge:
        home_node = NetworkNode.objects.filter(node_id=home_edge.source).only('name').first()
        home_label = (home_node.name if home_node and home_node.name else home_edge.source)

    upstream_ids = list(NetworkEdge.objects.filter(
        target=measure_node_id, source__startswith='PB_MEASURE::',
    ).values_list('source', flat=True))
    downstream_ids = list(NetworkEdge.objects.filter(
        source=measure_node_id, target__startswith='PB_MEASURE::',
    ).values_list('target', flat=True))

    def _label(node_ids):
        if not node_ids:
            return []
        nodes = NetworkNode.objects.filter(node_id__in=node_ids).only('node_id', 'name')
        by_id = {n.node_id: (n.name or n.node_id) for n in nodes}
        return [by_id.get(nid, nid) for nid in node_ids]

    upstream = _label(upstream_ids)
    downstream = _label(downstream_ids)

    return (
        f'Measure: **{measure.item_name}**\n'
        f'ID: {measure_node_id}\n'
        f'Home Table: {home_label}\n'
        f'Dataset: {measure.dataset_name or "?"} ({measure.dataset_id or "?"})\n'
        f'Workspace: {measure.workspace_name or "?"} ({measure.workspace_id or "?"})\n'
        f'Expression:\n```dax\n{measure.expression or "(none)"}\n```\n'
        f'Upstream measures (this measure depends on): '
        f'{", ".join(upstream) if upstream else "None"}\n'
        f'Downstream measures (depend on this measure): '
        f'{", ".join(downstream) if downstream else "None"}'
    )


def get_dbt_bigquery_lineage(asset_name_or_fqn: str, max_results: int = 20) -> str:
    """
    Returns focused lineage between dbt assets and BigQuery/BI tables.

    Use this WHEN the user asks how dbt models relate to BigQuery tables,
    PowerBI tables, or cross-system lineage between transformations and BI.
    Accepts a dbt model/source name, item_id, graph node id, table name, or a
    BigQuery-style fully-qualified table name.
    """
    query = (asset_name_or_fqn or '').strip().strip('`')
    if not query:
        return 'Please provide a dbt asset name, BigQuery table FQN, or graph node id.'
    max_results = max(1, min(int(max_results or 20), 50))

    node_matches = list(NetworkNode.objects.filter(node_id=query)[:2])
    if not node_matches:
        node_matches = list(NetworkNode.objects.filter(
            Q(name__iexact=query) | Q(name__icontains=query) | Q(node_id__icontains=query)
        )[:25])

    item_matches = list(Item.objects.filter(deleted=False).filter(
        Q(service='dbt') | Q(service__icontains='powerbi') | Q(service__icontains='bigquery') | Q(service__isnull=True)
    ).filter(
        Q(item_id=query) |
        Q(item_name__iexact=query) |
        Q(item_name__icontains=query) |
        Q(table_name__icontains=query) |
        Q(database_name__icontains=query)
    )[:25])

    candidate_ids = {n.node_id for n in node_matches}
    for item in item_matches:
        if item.item_type:
            node_type = item.item_type if item.item_type.startswith('DBT_') else item.item_type
            if node_type == 'DBT_SEED':
                candidate_ids.add(f'DBT_SEED::{item.item_id}')
            elif node_type == 'DBT_MODEL':
                candidate_ids.add(f'DBT_MODEL::{item.item_id}')
            elif node_type == 'DBT_SOURCE':
                candidate_ids.add(f'DBT_SOURCE::{item.item_id}')
            elif node_type == 'DBT_TEST':
                candidate_ids.add(f'DBT_TEST::{item.item_id}')
            else:
                candidate_ids.add(f'{node_type}::{item.item_id}')

    existing_nodes = list(NetworkNode.objects.filter(node_id__in=list(candidate_ids)[:50]))
    if not existing_nodes:
        return f"No dbt/BigQuery lineage nodes found for '{query}'."
    if len(existing_nodes) > 1:
        rows = '\n'.join(
            f'- {n.name or n.node_id} ({n.group or "UNKNOWN"}) [id={n.node_id}]'
            for n in existing_nodes[:10]
        )
        return f"'{query}' matches multiple lineage nodes. Re-run with a full id:\n{rows}"

    node = existing_nodes[0]
    first_edges = list(NetworkEdge.objects.filter(Q(source=node.node_id) | Q(target=node.node_id))[:200])
    neighbor_ids = {e.source if e.target == node.node_id else e.target for e in first_edges}
    second_edges = list(NetworkEdge.objects.filter(Q(source__in=neighbor_ids) | Q(target__in=neighbor_ids))[:300])
    all_edges = first_edges + second_edges
    all_node_ids = {node.node_id}
    for edge in all_edges:
        all_node_ids.add(edge.source)
        all_node_ids.add(edge.target)
    nodes_by_id = {
        n.node_id: n for n in NetworkNode.objects.filter(node_id__in=list(all_node_ids)[:500]).only('node_id', 'name', 'group')
    }

    upstream = []
    downstream = []
    cross_system = []
    for edge in all_edges:
        if edge.target == node.node_id or edge.source == node.node_id:
            other_id = edge.source if edge.target == node.node_id else edge.target
            other = nodes_by_id.get(other_id)
            label = f'{(other.name if other and other.name else other_id)} ({other.group if other else "UNKNOWN"}) [id={other_id}]'
            if edge.target == node.node_id:
                upstream.append(label)
            else:
                downstream.append(label)
        src = nodes_by_id.get(edge.source)
        tgt = nodes_by_id.get(edge.target)
        src_group = src.group if src else ''
        tgt_group = tgt.group if tgt else ''
        if (src_group or '').startswith('DBT_') != (tgt_group or '').startswith('DBT_'):
            src_label = src.name if src and src.name else edge.source
            tgt_label = tgt.name if tgt and tgt.name else edge.target
            cross_system.append(f'{src_label} ({src_group or "UNKNOWN"}) → {tgt_label} ({tgt_group or "UNKNOWN"})')

    # Order-preserving dedupe, capped to max_results.
    upstream = list(dict.fromkeys(upstream))[:max_results]
    downstream = list(dict.fromkeys(downstream))[:max_results]
    cross_system = list(dict.fromkeys(cross_system))[:max_results]
    return (
        f'DBT ↔ BigQuery/BI lineage for {node.name or node.node_id} ({node.group}):\n'
        f'Upstream: {"; ".join(upstream) if upstream else "None"}\n'
        f'Downstream: {"; ".join(downstream) if downstream else "None"}\n'
        f'Cross-system edges: {"; ".join(cross_system) if cross_system else "None found nearby"}'
    )


def preview_pb_dbt_bridge(pbi_table_name_or_id: str, max_results: int = 10) -> str:
    """
    Explains why (or why not) a PowerBI table is bridged to a dbt model.

    Use this WHEN the user asks "how is this PBI table connected to dbt?",
    "why didn't this match?", or wants to audit a specific cross-tool link.
    Accepts a PowerBI TABLE item name or item_id and returns the candidate
    dbt matches across all matching passes (BQ FQN, full name, tail name).
    """
    from ..services.bridge_matching import (
        DbtModelKey, PbiTableKey, preview_matches,
    )

    query = (pbi_table_name_or_id or '').strip()
    if not query:
        return 'Please provide a PowerBI table name or item_id.'
    max_results = max(1, min(int(max_results or 10), 50))

    pbi_qs = Item.objects.filter(
        item_type='PB_TABLE', deleted=False,
    ).filter(
        Q(item_id=query) | Q(item_name__iexact=query) | Q(item_name__icontains=query)
    ).exclude(service='dbt')[:5]
    pbi_items = list(pbi_qs)
    if not pbi_items:
        return f"No PowerBI TABLE matches '{query}'."
    if len(pbi_items) > 1:
        rows = '\n'.join(
            f'- **{p.item_name}** (workspace: {p.workspace_name or "?"}) [id={p.item_id}]'
            for p in pbi_items
        )
        return f"'{query}' matches multiple PowerBI tables. Re-run with item_id:\n{rows}"

    pbi = pbi_items[0]
    dbt_qs = Item.objects.filter(
        service='dbt', item_type='DBT_MODEL', deleted=False, table_name__isnull=False,
    ).only('item_id', 'item_name', 'database_name', 'schema_name', 'alias', 'table_name')

    pbi_key = PbiTableKey(
        item_id=pbi.item_id,
        item_name=pbi.item_name or '',
        bq_project=pbi.bq_project,
        bq_schema=pbi.bq_schema,
        bq_source_name=pbi.bq_source_name,
    )
    dbt_keys = [
        DbtModelKey(
            item_id=d.item_id, item_name=d.item_name or '',
            database=d.database_name, schema=d.schema_name, alias=d.alias,
            table_name=d.table_name,
        )
        for d in dbt_qs
    ]

    matches = preview_matches(pbi_key, dbt_keys)
    fqn = '.'.join(filter(None, [pbi.bq_project, pbi.bq_schema, pbi.bq_source_name])) or '(no BQ source)'
    if not matches:
        return (
            f"No bridge candidates for **{pbi.item_name}** (BQ FQN: `{fqn}`).\n"
            f"Considered {len(dbt_keys)} dbt models — none matched on FQN or name."
        )

    by_id: dict = {}
    dbt_lookup = {d.item_id: d for d in dbt_qs}
    for m in matches[:max_results]:
        by_id.setdefault(m.dbt_item_id, []).append(m.reason)
    rows = []
    for dbt_id, reasons in by_id.items():
        d = dbt_lookup.get(dbt_id)
        if not d:
            continue
        rows.append(f'- **{d.item_name}** ({d.table_name}) — matched by: {", ".join(reasons)}')
    return (
        f"Bridge candidates for **{pbi.item_name}** (BQ FQN: `{fqn}`):\n"
        + '\n'.join(rows)
    )
