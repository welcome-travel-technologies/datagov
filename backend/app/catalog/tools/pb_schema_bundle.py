"""
``get_pb_measure_schema`` and ``verify_pb_measure_dimension_link``.

The bundle is the load-bearing artifact for live PowerBI flow: one
Markdown payload that gives the DAX-generation step everything it needs
(home table, related tables, columns with DAX references, relationships,
USERELATIONSHIP overrides, sibling measures, suggested call) without any
live API calls. Built from the local catalog so it's cheap and
deterministic.
"""
import re

from ..models import Item

# A thin "DirectQuery" wrapper measure is one whose DAX is *only* an
# EXTERNALMEASURE() call (optionally a second one for the format string) with no
# reference to a local table. Its real schema (home table, columns,
# relationships) lives in the source semantic model, so the bundle below
# forwards a wrapper to its source. (Deduping these copies by *name* is handled
# separately via ItemGroups — see ``_collapse_to_group_primaries``.)
_EM_RE = re.compile(
    r'EXTERNALMEASURE\s*\(\s*"([^"]+)"\s*,\s*\w+\s*,\s*"([^"]+)"\s*\)'
)


def _is_external_wrapper(expr_raw: str):
    """Return ``(is_wrapper, em_matches)``. ``is_wrapper`` is True only when the
    DAX is *predominantly* EXTERNALMEASURE — i.e. it references no local table —
    so a hybrid measure that mixes EXTERNALMEASURE with a local CALCULATE is
    treated as a real (source) measure, not a wrapper."""
    em_matches = _EM_RE.findall(expr_raw or '')
    if not em_matches:
        return False, []
    stripped = re.sub(r'EXTERNALMEASURE\s*\([^)]*\)', '', expr_raw or '')
    has_local_refs = bool(
        re.search(r"'[^']+'\s*\[", stripped)
        or re.search(r"(?:^|[^\w'])[A-Za-z_]\w*\s*\[", stripped)
    )
    return (not has_local_refs), em_matches


def _resolve_wrapper_source(measure, em_matches, limit=5):
    """Resolve a wrapper's primary EXTERNALMEASURE target to candidate source
    measures in the catalog. Returns ``(src_name, src_conn, [Item, ...])`` — the
    list is empty when nothing matches and may hold >1 when the source dataset
    name is itself ambiguous."""
    src_measure_name, src_conn = em_matches[0]
    # Connection labels look like "DirectQuery to AS - <dataset_name>".
    # Strip the prefix; fall back to the raw label for icontains.
    src_ds_name = re.sub(
        r'^\s*(?:DirectQuery|Import)\s+to\s+(?:AS\s*-\s*)?',
        '', src_conn, flags=re.IGNORECASE,
    ).strip()
    target_qs = Item.objects.filter(
        deleted=False, item_type='PB_MEASURE', service='powerbi',
        item_name=src_measure_name,
    )
    if measure.organization_id:
        target_qs = target_qs.filter(organization_id=measure.organization_id)
    ds_match = list(target_qs.filter(dataset_name__iexact=src_ds_name)[:limit])
    if not ds_match:
        ds_match = list(target_qs.filter(dataset_name__icontains=src_ds_name)[:limit])
    return src_measure_name, src_conn, ds_match


def _priority_sort_key(m):
    """Sort key where *smaller* means higher priority: the most-used measure
    (most connected reports, then visuals), preferring one that carries a
    description, with item_id as a stable final tie-break."""
    return (
        -(m.connected_reports or 0),
        -(m.connected_visuals or 0),
        0 if (m.description or '').strip() else 1,
        m.item_id or '',
    )


def _pick_group_primary(members):
    """Choose the representative for one measure group: its curated
    ``ItemGroup.primary_item`` when that item is in range (the catalog's
    declared "1st priority" measure), else the highest priority matched member."""
    by_id = {m.item_id: m for m in members}
    primary_id = next(
        (m.item_group.primary_item_id for m in members
         if m.item_group_id and m.item_group and m.item_group.primary_item_id),
        None,
    )
    if primary_id in by_id:
        return by_id[primary_id]
    return min(members, key=_priority_sort_key)


def _collapse_to_group_primaries(measures):
    """Collapse name-matched measures to one representative per ItemGroup. Every
    PB_MEASURE sharing a name shares one group (``kind=measure_name``), so all
    the EXTERNALMEASURE re-exports and cross-dataset copies of e.g. "Failed
    Quotes" fold into a single entry represented by the group's curated
    ``primary_item``. Without this the staged disambiguation below parades every
    copy at the model, burning a tool call per copy until the agent trips its
    request limit. Ordered by priority so ``[0]`` is the canonical measure."""
    groups: dict = {}
    for m in measures:
        groups.setdefault(m.item_group_id or f'solo::{m.item_id}', []).append(m)
    return sorted((_pick_group_primary(ms) for ms in groups.values()),
                  key=_priority_sort_key)


def get_pb_measure_schema(
    measure_name_or_id: str,
    workspace_id: str = '',
    dataset_id: str = '',
    max_columns_per_table: int = 1000,
    trim: bool = False,
) -> str:
    """
    Returns a single Markdown bundle describing everything needed to query a
    PowerBI measure live: its DAX expression, dataset / workspace IDs, the
    home table and 1-hop related tables (with columns + DAX references), and
    the relationships scoped to those tables only.

    Use this in MEASURE FLOW as a one-shot replacement for the
    ``search_pb_columns`` + ``powerbi_get_dataset_schema`` chain when the user
    asks for a measure value broken down by a dimension. The bundle is built
    from the local catalog (no live PowerBI calls), so it is cheap and
    deterministic.

    The returned bundle tells the model to call the measure by reference
    (``[Measure Name]``) inside ``powerbi_run_dax_query`` rather than rewriting
    the DAX from scratch.

    The same measure name recurs across many datasets/workspaces (DirectQuery
    re-exports of one source). These all share one ItemGroup, so the tool first
    collapses them onto the group's curated ``primary_item`` — the catalog's
    canonical pick — and a plain name lookup resolves straight to that one
    measure with no questions asked.

    Disambiguation is staged and only kicks in when a name genuinely spans
    multiple *groups* (e.g. an ``icontains`` match pulling in distinct names):
    multiple workspaces -> return the workspace list; multiple datasets within a
    workspace -> return the dataset list; only a single resolved measure yields
    the bundle. **Do NOT auto-pick a disambiguation list — present it to the
    user and ask them to choose.**

    ``measure_name_or_id``: measure item_id (preferred) or display name.
    ``workspace_id``: optional — pass after the user picks a workspace.
    ``dataset_id``: optional — pass after the user picks a dataset.
    ``max_columns_per_table``: cap on columns rendered per table. Defaults
    to 1000, which is effectively uncapped for normal models. Lower it only
    if you need to keep the bundle small. Columns that participate in
    relationships are always pinned to the top regardless of cap.
    ``trim``: when True, produce a much smaller bundle for use as an LLM
    prompt input. Caps columns per table at 30 (join keys + first 30
    others), drops sibling measures, drops the long "How to construct"
    instruction footer, and elides the Data Type column. Typical size
    reduction: 5-10x. Use when feeding the bundle to a DAX-generation
    model — the human-facing context is gone, but everything the model
    needs to write a working query (DAX expression, tables, columns,
    relationships, USERELATIONSHIP overrides) is preserved.
    """
    query = (measure_name_or_id or '').strip()
    if not query:
        return 'Please provide a measure name or item_id.'

    m_qs = (Item.objects
            .filter(deleted=False, item_type='PB_MEASURE', service='powerbi')
            .select_related('item_group')
            .order_by('-connected_reports', '-connected_visuals', 'item_name'))
    if workspace_id:
        m_qs = m_qs.filter(workspace_id=workspace_id)
    if dataset_id:
        m_qs = m_qs.filter(dataset_id=dataset_id)

    # An exact item_id match short-circuits the staged disambiguation: the user
    # has already pinned a specific measure, so no further questions needed.
    by_id = list(m_qs.filter(item_id=query)[:2])
    if by_id:
        measures = by_id
    else:
        measures = (
            list(m_qs.filter(item_name__iexact=query)[:50])
            or list(m_qs.filter(item_name__icontains=query)[:50])
        )

    if not measures:
        scope_bits = []
        if workspace_id:
            scope_bits.append(f'workspace_id={workspace_id}')
        if dataset_id:
            scope_bits.append(f'dataset_id={dataset_id}')
        scope = f' in scope ({", ".join(scope_bits)})' if scope_bits else ''
        return f"No measure matched '{query}'{scope}."

    # Collapse the name-matched copies down to one representative per measure
    # group (its curated primary_item). The same measure name recurs across
    # every dataset that re-exports it via DirectQuery; without this the staged
    # disambiguation below would parade ~10 near-identical rows at the model
    # (and the agent would burn a tool call per copy, eventually tripping its
    # request limit). Only applies when the caller passed a name — an explicit
    # item_id is already pinned to one measure.
    if not by_id:
        measures = _collapse_to_group_primaries(measures)

    # Global-ambiguity gate — when the caller passed a measure *name* (not an
    # item_id) and also pre-narrowed by workspace_id/dataset_id, the staged
    # disambiguation below sees only the narrowed slice and accepts the scope.
    # That makes it possible for the agent to silently pre-pick a dataset for a
    # name that has genuinely different DAX elsewhere (e.g. 'Transfers {B}'
    # lives in 4 datasets with different definitions). Re-check the full
    # catalog, collapsing each name's copies onto its group primary: if the name
    # still resolves to more than one *group*, list them and require an item_id.
    if not by_id and (workspace_id or dataset_id):
        global_qs = (Item.objects
                     .filter(deleted=False, item_type='PB_MEASURE',
                             service='powerbi')
                     .select_related('item_group')
                     .order_by('-connected_reports', '-connected_visuals',
                               'item_name'))
        global_matches = list(
            global_qs.filter(item_name__iexact=query)[:50]
            or global_qs.filter(item_name__icontains=query)[:50]
        )
        global_defs = _collapse_to_group_primaries(global_matches)
        if len(global_defs) > 1:
            rows = [
                f'- **{m.item_name}** — workspace: '
                f'{m.workspace_name or "?"} (`{m.workspace_id or "?"}`), '
                f'dataset: {m.dataset_name or "?"} '
                f'(`{m.dataset_id or "?"}`) [id={m.item_id}]'
                for m in global_defs[:20]
            ]
            return (
                f"'{query}' resolves to {len(global_defs)} different measure "
                f"groups (distinct names). The caller-supplied scope is not "
                f"enough to pick one safely — do NOT auto-pick. Show these "
                f"candidates to the user, ask which one they mean, then re-run "
                f"with that exact `item_id`:\n"
                + '\n'.join(rows)
            )

    if not by_id:
        # Stage 1 — multiple workspaces? Ask the user which one.
        ws_groups: dict = {}
        for m in measures:
            key = (m.workspace_id or '', m.workspace_name or '?')
            ws_groups.setdefault(key, []).append(m)
        if len(ws_groups) > 1:
            rows = []
            for (ws_id, ws_name), items in sorted(ws_groups.items(), key=lambda x: x[0][1].lower()):
                ds_count = len({i.dataset_id for i in items})
                rows.append(
                    f'- **{ws_name}** (`{ws_id}`) — {len(items)} measure(s) '
                    f'across {ds_count} dataset(s)'
                )
            return (
                f"'{query}' matches measures in {len(ws_groups)} workspaces. "
                f"Ask the user which workspace, then re-run with workspace_id:\n"
                + '\n'.join(rows)
            )

        # Stage 2 — single workspace, multiple datasets? Ask which dataset.
        ds_groups: dict = {}
        for m in measures:
            key = (m.dataset_id or '', m.dataset_name or '?')
            ds_groups.setdefault(key, []).append(m)
        if len(ds_groups) > 1:
            ws_name = measures[0].workspace_name or '?'
            rows = []
            for (ds_id, ds_name), items in sorted(ds_groups.items(), key=lambda x: x[0][1].lower()):
                rows.append(f'- **{ds_name}** (`{ds_id}`) — {len(items)} measure(s)')
            return (
                f"'{query}' matches measures in {len(ds_groups)} datasets within "
                f"workspace **{ws_name}**. Ask the user which dataset, then "
                f"re-run with dataset_id:\n"
                + '\n'.join(rows)
            )

        # Stage 3 — single dataset but still multiple measures (name collision
        # within a dataset, or icontains pulled in siblings). Show item_ids.
        if len(measures) > 1:
            rows = '\n'.join(
                f'- **{m.item_name}** [id={m.item_id}]'
                + (f' — table: {m.table_name}' if m.table_name else '')
                for m in measures[:20]
            )
            return (
                f"'{query}' matches multiple measures within the same dataset. "
                f"Ask the user to pick one, then re-run with the exact item_id:\n{rows}"
            )

    measure = measures[0]

    # EXTERNALMEASURE forwarding: when the measure is a thin-client wrapper
    # (DirectQuery to a remote semantic model), the real schema lives in the
    # source dataset. Most wrappers were already folded onto their source by the
    # collapse step above; this still runs for the by_id path (collapse skipped)
    # and for wrappers whose source could not be uniquely resolved.
    is_wrapper, em_matches = _is_external_wrapper(measure.expression or '')
    if is_wrapper:
        # Pick the primary EM call (first match — measure value, not the
        # FormatString one which the model rarely needs).
        src_measure_name, src_conn, ds_match = _resolve_wrapper_source(
            measure, em_matches,
        )
        if len(ds_match) == 1:
            tgt = ds_match[0]
            forwarded = get_pb_measure_schema(
                tgt.item_id,
                workspace_id=tgt.workspace_id or '',
                dataset_id=tgt.dataset_id or '',
                max_columns_per_table=max_columns_per_table,
            )
            header = (
                f'> **Forwarded from EXTERNALMEASURE**\n'
                f'> Source measure `[{measure.item_name}]` in dataset '
                f'**{measure.dataset_name}** (`{measure.dataset_id}`) is a '
                f'thin-client wrapper. The real schema lives in '
                f'**{tgt.dataset_name}** (`{tgt.dataset_id}`) — bundle below.\n'
                f'> When calling `powerbi_run_dax_query`, you may use either '
                f'side; querying the **source** dataset avoids the DirectQuery '
                f'hop.\n\n'
            )
            return header + forwarded
        if len(ds_match) > 1:
            rows = '\n'.join(
                f'- **{m.item_name}** — dataset: {m.dataset_name} '
                f'(`{m.dataset_id}`), workspace: {m.workspace_name} '
                f'(`{m.workspace_id}`) [id={m.item_id}]'
                for m in ds_match
            )
            return (
                f"`{measure.item_name}` forwards via EXTERNALMEASURE to "
                f"`{src_measure_name}` in source `{src_conn}`, which "
                f"resolves to multiple candidate datasets. Ask the user "
                f"which one, then re-run with that item_id:\n{rows}"
            )
        # No source match — fall through and render the (empty) local
        # bundle so the user at least sees the forwarding pointer.

    home_table = (measure.table_name or '').strip()
    ds_id = measure.dataset_id or ''

    # Root tables = home_table ∪ tables referenced in the DAX expression.
    # Many measures live on a measure-holder table (e.g. 'Ops Metrics') with
    # no columns and no relationships; the real fact table is referenced
    # inside the DAX. Parsing those references gives the model the actual
    # schema graph it needs to filter / group the measure.
    expr = measure.expression or ''
    quoted = re.findall(r"'([^']+)'\s*\[", expr)
    bare = re.findall(r"(?:^|[^\w'])([A-Za-z_][\w]*)\s*\[", expr)
    referenced = []
    seen_ref = set()
    for t in quoted + bare:
        tl = t.strip().lower()
        if tl and tl not in seen_ref:
            seen_ref.add(tl)
            referenced.append(t.strip())

    # Verify each candidate against Item so we never invent a table.
    candidate_names = ([home_table] if home_table else []) + referenced
    if candidate_names and ds_id:
        verified_tables = list(Item.objects.filter(
            deleted=False, item_type='PB_TABLE', service='powerbi',
            dataset_id=ds_id, item_name__in=candidate_names,
        ).only('item_name', 'relationships_json'))
    else:
        verified_tables = []
    table_by_name = {t.item_name: t for t in verified_tables}

    # Roots in deterministic order: home first, then DAX-referenced.
    roots = []
    seen_roots = set()
    for cand in candidate_names:
        key = cand.lower()
        if cand in table_by_name and key not in seen_roots:
            seen_roots.add(key)
            roots.append(cand)

    # 1-hop neighbours of every root.
    related_tables = []
    seen_tables = {r.lower() for r in roots}
    for root in roots:
        for r in (table_by_name[root].relationships_json or []):
            other = (r.get('other_table') or '').strip()
            if other and other.lower() not in seen_tables:
                seen_tables.add(other.lower())
                related_tables.append(other)

    involved_tables = list(roots) + related_tables

    columns_qs = Item.objects.filter(
        deleted=False, item_type='PB_COLUMN', service='powerbi',
        dataset_id=ds_id, table_name__in=involved_tables,
    ).only('item_name', 'table_name', 'datatype', 'column_type', 'description')
    cols_by_table: dict = {}
    for c in columns_qs:
        cols_by_table.setdefault(c.table_name, []).append(c)

    rel_pairs_seen = set()
    rel_rows = []
    rel_sources = []
    for root in roots:
        rels = table_by_name[root].relationships_json or []
        if rels:
            rel_sources.append((root, rels))
    if related_tables and ds_id:
        related_items = Item.objects.filter(
            deleted=False, item_type='PB_TABLE', service='powerbi',
            dataset_id=ds_id, item_name__in=related_tables,
        ).only('item_name', 'relationships_json')
        for t_item in related_items:
            if t_item.relationships_json:
                rel_sources.append((t_item.item_name, t_item.relationships_json))

    for this_table, rels in rel_sources:
        for r in rels:
            this_t = r.get('this_table') or this_table
            this_c = r.get('this_column') or ''
            other_t = r.get('other_table') or ''
            other_c = r.get('other_column') or ''
            if not (this_t and other_t):
                continue
            if other_t.lower() not in seen_tables:
                continue
            role = r.get('role') or 'from'
            this_card = r.get('cardinality') or '?'
            other_card = r.get('other_cardinality') or '?'
            if role == 'from':
                left, lcol, lcard = this_t, this_c, this_card
                right, rcol, rcard = other_t, other_c, other_card
            else:
                left, lcol, lcard = other_t, other_c, other_card
                right, rcol, rcard = this_t, this_c, this_card
            key = (left.lower(), lcol.lower(), right.lower(), rcol.lower(),
                   bool(r.get('is_active', True)))
            if key in rel_pairs_seen:
                continue
            rel_pairs_seen.add(key)
            rel_rows.append({
                'left_table': left, 'left_col': lcol, 'left_card': lcard,
                'right_table': right, 'right_col': rcol, 'right_card': rcard,
                'cross_filter': r.get('cross_filter') or 'single',
                'is_active': bool(r.get('is_active', True)),
            })

    def _card_arrow(lc: str, rc: str) -> str:
        m = {'one': '1', 'many': '*'}
        return f'{m.get(lc, lc)}:{m.get(rc, rc)}'

    # Per-column join targets keyed by (lowercase table, lowercase column).
    # Used to render a 'Joins to' cell next to each column so the DAX author
    # can see at a glance which dimension a key column links into without
    # scrolling to the relationships section.
    join_target_by_col: dict = {}
    for r in rel_rows:
        for tk, ck, ot, oc, lc, rc in (
            (r['left_table'], r['left_col'], r['right_table'], r['right_col'],
             r['left_card'], r['right_card']),
            (r['right_table'], r['right_col'], r['left_table'], r['left_col'],
             r['right_card'], r['left_card']),
        ):
            key = (tk.lower(), ck.lower())
            entry = (
                f"`'{ot}'[{oc}]` ({_card_arrow(lc, rc)}"
                + ('' if r['is_active'] else ', inactive')
                + ')'
            )
            join_target_by_col.setdefault(key, []).append(entry)

    # USERELATIONSHIP extraction. Measures often activate an inactive
    # relationship (e.g. by booked_on instead of from_datetime). The model
    # MUST know this to group the measure correctly — without it, it picks
    # the active relationship and reports the wrong number.
    user_rels = []
    for m in re.finditer(
        r"USERELATIONSHIP\s*\(\s*"
        r"'?([^'\[]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*,\s*"
        r"'?([^'\[]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*\)",
        expr or '',
    ):
        user_rels.append({
            'a_table': m.group(1).strip(),
            'a_col': m.group(2).strip(),
            'b_table': m.group(3).strip(),
            'b_col': m.group(4).strip(),
        })

    # Sibling measures on the same home table — useful as references for
    # related metrics the user might be asking about.
    sibling_measures: list = []
    if home_table and ds_id:
        sibling_measures = list(
            Item.objects.filter(
                deleted=False, item_type='PB_MEASURE', service='powerbi',
                dataset_id=ds_id, table_name=home_table,
            )
            .exclude(item_id=measure.item_id)
            .only('item_name', 'description')
            .order_by('item_name')[:15]
        )

    # Distinguish a measure-container table (zero columns / zero relationships
    # — a UI grouping in the model) from a real fact table. This helps the
    # author point filters at the right place.
    home_has_cols = bool(cols_by_table.get(home_table))
    home_has_rels = home_table and any(
        rs[0] == home_table for rs in rel_sources
    )
    is_measure_container = bool(home_table) and not home_has_cols and not home_has_rels
    fact_tables = [t for t in roots if t != home_table]

    # If every relationship has cross_filter='single', drop the column from
    # the relationships table to reduce noise. Surface only when at least one
    # is bidirectional.
    show_cross_filter = any(
        (r.get('cross_filter') or 'single') != 'single' for r in rel_rows
    )

    out = [
        f'## Measure: **{measure.item_name}**',
        '',
        '| Field | Value |',
        '| --- | --- |',
        f'| DAX Reference | `[{measure.item_name}]` |',
        (
            f'| Home Table | `{home_table or "?"}`'
            + (' _(measure container — no fact data)_' if is_measure_container else '')
            + ' |'
        ),
    ]
    if fact_tables:
        out.append(
            f'| Fact Table(s) | '
            + ', '.join(f'`{t}`' for t in fact_tables)
            + ' _(filter / group by columns from these)_ |'
        )
    out.extend([
        f'| Dataset | {measure.dataset_name or "?"} (`{ds_id or "?"}`) |',
        f'| Workspace | {measure.workspace_name or "?"} (`{measure.workspace_id or "?"}`) |',
        f'| Status | {measure.status} |',
        f'| Format String | `{measure.formatstring or "(none)"}` |',
        '',
    ])
    if measure.description:
        out.append(f'**Description:** {measure.description}')
        out.append('')
    out.append('### DAX Expression (use the measure by reference, do NOT rewrite)')
    out.append('```dax')
    out.append(measure.expression or '(no expression stored)')
    out.append('```')
    out.append('')

    if user_rels:
        out.append('### Activated relationships (USERELATIONSHIP in DAX)')
        out.append(
            'This measure activates the relationship(s) below for its scope. '
            'When grouping/filtering this measure by date or other dimensions, '
            'use the columns on the left of each pair as the filter axis — '
            'NOT the model\'s default active relationship.'
        )
        out.append('| Activated From | Activated To |')
        out.append('| --- | --- |')
        for ur in user_rels:
            out.append(
                f"| `'{ur['a_table']}'[{ur['a_col']}]` | "
                f"`'{ur['b_table']}'[{ur['b_col']}]` |"
            )
        out.append('')

    join_keys_by_table: dict = {}
    for r in rel_rows:
        join_keys_by_table.setdefault(r['left_table'], set()).add(r['left_col'])
        join_keys_by_table.setdefault(r['right_table'], set()).add(r['right_col'])

    # In trim mode the per-table cap is forced to a tight value regardless of
    # what the caller passed. A measure rarely needs more than the first
    # ~30 columns of a fact/dim table to write correct DAX — anything beyond
    # that is unrelated dim attributes the model would ignore anyway.
    cap = (
        min(30, max(1, int(max_columns_per_table or 30)))
        if trim
        else max(1, int(max_columns_per_table or 1000))
    )

    out.append('### Tables in scope')
    if not involved_tables:
        out.append('_No home table or DAX-referenced table resolved for this measure._')
    else:
        roots_set = {r.lower() for r in roots}
        for tname in involved_tables:
            if tname == home_table:
                tag = '(measure container)' if is_measure_container else '(home)'
            elif tname.lower() in roots_set:
                tag = '(fact — referenced in DAX)'
            else:
                tag = '(dimension — joined to fact)'
            out.append(f'#### `{tname}` {tag}')
            tcols = cols_by_table.get(tname, [])
            if not tcols:
                out.append('_No columns indexed for this table in the catalog._')
                out.append('')
                continue

            keys = {k.lower() for k in join_keys_by_table.get(tname, set())}
            tcols_sorted = sorted(tcols, key=lambda x: (x.item_name or '').lower())
            # Join keys pinned to the top; remaining columns rendered up to
            # the cap. Default cap is generous (1000) so a normal table is
            # fully shown — a tighter cap is only useful for monster tables.
            key_cols = [c for c in tcols_sorted if (c.item_name or '').lower() in keys]
            other_cols = [c for c in tcols_sorted if (c.item_name or '').lower() not in keys]
            remaining = max(0, cap - len(key_cols))
            shown = key_cols + other_cols[:remaining]
            hidden = len(tcols_sorted) - len(shown)

            if trim:
                out.append('| Column | DAX Reference | Joins To |')
                out.append('| --- | --- | --- |')
                for c in shown:
                    cname = c.item_name or ''
                    key_marker = ' **(join key)**' if cname.lower() in keys else ''
                    targets = join_target_by_col.get((tname.lower(), cname.lower()), [])
                    joins_cell = '; '.join(targets) if targets else '—'
                    out.append(
                        f"| {cname}{key_marker} | `'{tname}'[{cname}]` | "
                        f"{joins_cell} |"
                    )
            else:
                out.append('| Column | DAX Reference | Type | Data Type | Joins To |')
                out.append('| --- | --- | --- | --- | --- |')
                for c in shown:
                    cname = c.item_name or ''
                    key_marker = ' **(join key)**' if cname.lower() in keys else ''
                    targets = join_target_by_col.get((tname.lower(), cname.lower()), [])
                    joins_cell = '; '.join(targets) if targets else '—'
                    out.append(
                        f"| {cname}{key_marker} | `'{tname}'[{cname}]` | "
                        f"{c.column_type or '—'} | {c.datatype or '—'} | "
                        f"{joins_cell} |"
                    )
            if hidden > 0:
                out.append(
                    f'_…{hidden} more column(s) hidden (column cap = {cap}). '
                    f'Re-run with `max_columns_per_table` raised if you need them._'
                )
            out.append('')

    out.append('### Relationships (scoped to tables above)')
    if not rel_rows:
        out.append('_No relationships found involving these tables._')
    else:
        if show_cross_filter:
            out.append('| From | To | Cardinality | Cross-Filter | Active |')
            out.append('| --- | --- | --- | --- | --- |')
            for r in rel_rows:
                out.append(
                    f"| `'{r['left_table']}'[{r['left_col']}]` | "
                    f"`'{r['right_table']}'[{r['right_col']}]` | "
                    f"{_card_arrow(r['left_card'], r['right_card'])} | "
                    f"{r['cross_filter']} | {'yes' if r['is_active'] else 'NO'} |"
                )
        else:
            out.append('_All relationships use single-direction cross-filter; column omitted._')
            out.append('')
            out.append('| From | To | Cardinality | Active |')
            out.append('| --- | --- | --- | --- |')
            for r in rel_rows:
                out.append(
                    f"| `'{r['left_table']}'[{r['left_col']}]` | "
                    f"`'{r['right_table']}'[{r['right_col']}]` | "
                    f"{_card_arrow(r['left_card'], r['right_card'])} | "
                    f"{'yes' if r['is_active'] else 'NO'} |"
                )
    out.append('')

    if sibling_measures and not trim:
        out.append('### Related measures (siblings on the same home table)')
        out.append('Use these for reference if the user might mean a sibling '
                   'metric. Reference any of them by `[Measure Name]`.')
        out.append('| Measure | DAX Reference | Description |')
        out.append('| --- | --- | --- |')
        for sm in sibling_measures:
            desc = (sm.description or '').replace('|', '\\|').replace('\n', ' ')
            if len(desc) > 120:
                desc = desc[:117] + '...'
            out.append(
                f'| **{sm.item_name}** | `[{sm.item_name}]` | {desc or "—"} |'
            )
        out.append('')

    # Pre-fill the SUMMARIZECOLUMNS template with a sensible default group-by:
    # the active date column on the home/fact table if we can find one, else
    # leave a placeholder.
    suggested_axis = None
    fact_for_axis = fact_tables[0] if fact_tables else home_table
    if fact_for_axis:
        for r in rel_rows:
            if not r['is_active']:
                continue
            if r['left_table'] == fact_for_axis or r['right_table'] == fact_for_axis:
                # Prefer the dimension side (the 'one' end).
                if r['right_card'] == 'one':
                    suggested_axis = (r['right_table'], r['right_col'])
                elif r['left_card'] == 'one':
                    suggested_axis = (r['left_table'], r['left_col'])
                if suggested_axis and 'date' in suggested_axis[0].lower():
                    break  # date axis is the most useful default
    if trim:
        # The DAX-gen agent has its own system prompt covering these rules
        # and emits SUMMARIZECOLUMNS itself; the human-facing instruction
        # footer + suggested-call template are pure waste in trim mode.
        return '\n'.join(out)

    out.append('### How to construct the DAX from this bundle')
    out.append(
        '1. **Reference, never rewrite.** Always call the measure as '
        f'`[{measure.item_name}]` inside `powerbi_run_dax_query`. Do not '
        'inline or modify the DAX expression shown above — the model owns '
        'the definition.'
    )
    out.append(
        '2. **Pick group-by columns from the dimension side.** For each '
        'breakdown the user asks for, look up the relevant fact column in '
        'the **Joins To** cell and use the *target* dimension column on '
        'that pair. Example: to group by date, take `from_datetime`\'s '
        '`Joins To` target (a `D_Date`-style column) — not the raw '
        'fact-side timestamp.'
    )
    out.append(
        '3. **Respect Active vs inactive relationships.** A `(*:1, inactive)` '
        'flag in the Joins To cell means filtering through that pair will '
        'silently produce zero rows unless the measure activates it via '
        '`USERELATIONSHIP`. Check the **Activated relationships** section '
        '(when present) — those are the inactive pairs the measure has '
        'opted into; prefer those columns over the default active one when '
        'they are listed.'
    )
    out.append(
        '4. **Use SUMMARIZECOLUMNS for breakdowns, a single-row EVALUATE '
        'for totals.** For a totals query, replace the group-by columns '
        f'with nothing: `EVALUATE ROW("{measure.item_name}", '
        f'[{measure.item_name}])`.'
    )
    out.append(
        '5. **Filter with `KEEPFILTERS` inside `CALCULATE`,** referencing '
        'the column in `\'Table\'[Column]` form taken from the **DAX '
        'Reference** column above (NOT a value the user typed verbatim). '
        'Combine multiple filters with `&&`.'
    )
    out.append(
        '6. **Time/date defaults.** When the user says "this year", '
        '"last week", "MTD" etc. and you have a `D_Date`-like dimension '
        'in scope, prefer DAX time-intelligence (`DATESYTD`, '
        '`DATESINPERIOD`, `SAMEPERIODLASTYEAR`) over manual date math. '
        'Anchor them to the active date column shown in the Joins To cell.'
    )
    out.append(
        '7. **Dimension lookup rule.** If the column the user wants is '
        'visible above (its `\'Table\'[Column]` reference appears in any '
        '"Tables in scope" section), the join is already proven — go '
        'straight to DAX. ONLY when the column is genuinely absent from '
        'this bundle, call `verify_pb_measure_dimension_link`; if that '
        'tool refuses, tell the user no relationship path exists — do NOT '
        'guess a join, and do NOT fall back to `search_pb_columns`.'
    )
    out.append('')

    out.append('### Suggested live DAX call')
    out.append('Pass the snippet below directly to `powerbi_run_dax_query` '
               '(use the dataset_id and workspace_id from the table above). '
               'Reference the measure by name — do not inline its expression.')
    out.append('```dax')
    out.append('EVALUATE')
    out.append('SUMMARIZECOLUMNS(')
    if suggested_axis:
        out.append(f"    '{suggested_axis[0]}'[{suggested_axis[1]}],")
    else:
        out.append("    -- replace with a column from the 'Tables in scope' section, e.g. 'Date'[Date]")
    out.append(f'    "{measure.item_name}", [{measure.item_name}]')
    out.append(')')
    out.append('```')

    return '\n'.join(out)


def verify_pb_measure_dimension_link(
    measure_name_or_id: str,
    dimension_name_or_id: str,
    dataset_id: str = '',
) -> str:
    """
    Verifies that a dimension column can filter a measure via TMDL
    relationship edges, scoped to the measure's dataset.

    MUST be called before generating any live DAX that groups or filters a
    measure by a column NOT on the measure's home table. If the result is
    REFUSED, do NOT call ``powerbi_run_dax_query`` — explain to the user
    that no valid relationship path exists in this semantic model.

    Pass exact item_ids when possible. If a name is ambiguous the tool
    returns a disambiguation list and refuses to pick — present it to the
    user.
    """
    from ..services.graph_paths import find_relationship_path

    m_query = (measure_name_or_id or '').strip()
    d_query = (dimension_name_or_id or '').strip()
    if not m_query or not d_query:
        return 'Please provide both a measure and a dimension column.'

    m_qs = Item.objects.filter(deleted=False, item_type='PB_MEASURE', service='powerbi')
    if dataset_id:
        m_qs = m_qs.filter(dataset_id=dataset_id)
    measures = (
        list(m_qs.filter(item_id=m_query)[:2])
        or list(m_qs.filter(item_name__iexact=m_query)[:5])
        or list(m_qs.filter(item_name__icontains=m_query)[:5])
    )
    if not measures:
        scope = f' in dataset {dataset_id}' if dataset_id else ''
        return f"No measure matched '{m_query}'{scope}."
    if len(measures) > 1:
        rows = '\n'.join(
            f'- {m.item_name} — dataset: {m.dataset_name} [id={m.item_id}]'
            for m in measures
        )
        return f"Measure '{m_query}' is ambiguous. Re-run with item_id:\n{rows}"
    measure = measures[0]

    c_qs = Item.objects.filter(
        deleted=False, item_type='PB_COLUMN', service='powerbi',
        dataset_id=measure.dataset_id,
    )
    dims = (
        list(c_qs.filter(item_id=d_query)[:2])
        or list(c_qs.filter(item_name__iexact=d_query)[:5])
        or list(c_qs.filter(item_name__icontains=d_query)[:5])
    )
    if not dims:
        return f"No column matched '{d_query}' in dataset {measure.dataset_name}."
    if len(dims) > 1:
        rows = '\n'.join(
            f'- {c.table_name}.{c.item_name} [id={c.item_id}]' for c in dims
        )
        return f"Dimension '{d_query}' is ambiguous in this dataset. Re-run with item_id:\n{rows}"
    dim = dims[0]

    measure_node_id = f'PB_MEASURE::{measure.item_id}'
    dim_node_id = f'PB_COLUMN::{dim.item_id}'
    result = find_relationship_path(measure_node_id, dim_node_id, measure.dataset_id)

    if not result.connected:
        return (
            f"REFUSED — no valid relationship path.\n"
            f"Measure: **{measure.item_name}** (table: {measure.table_name or '?'})\n"
            f"Dimension: **'{dim.table_name}'[{dim.item_name}]**\n"
            f"Dataset: {measure.dataset_name}\n"
            f"Reason: {result.reason}\n\n"
            f"DO NOT call powerbi_run_dax_query. Tell the user there is no "
            f"filter propagation path between this measure and that dimension "
            f"in this semantic model, and suggest they pick a dimension on "
            f"the measure's home table or a related dimension table."
        )

    if result.distance == 0:
        return (
            f"OK — same-table grouping.\n"
            f"Dimension '{dim.table_name}'[{dim.item_name}] lives on the same "
            f"table as **{measure.item_name}**. Live DAX is safe."
        )

    chain_lines = []
    for h in result.cardinality_chain:
        card = f'{h.cardinality or "?"}→{h.other_cardinality or "?"}'
        cf = (f' cross_filter={h.cross_filter}'
              if h.cross_filter and h.cross_filter != 'single' else '')
        flag = ' **(INACTIVE)**' if h.is_active is False else ''
        chain_lines.append(f'  {h.from_label}  --[{card}{cf}]-->  {h.to_label}{flag}')
    chain_text = '\n'.join(chain_lines)

    inactive_warn = ''
    if result.inactive_hops:
        inactive_warn = (
            '\n\nWARNING: path includes INACTIVE relationship(s). Power BI '
            'ignores inactive relationships unless DAX uses USERELATIONSHIP. '
            'Either ask the user to confirm activating it via USERELATIONSHIP, '
            'or refuse.'
        )

    return (
        f"OK — relationship path found.\n"
        f"Measure: **{measure.item_name}**\n"
        f"Dimension: **'{dim.table_name}'[{dim.item_name}]**\n"
        f"Distance: {result.distance} hop"
        f"{'s' if result.distance != 1 else ''}\n\n"
        f"Cardinality chain (filters propagate from one-side to many-side):\n"
        f"{chain_text}{inactive_warn}"
    )
