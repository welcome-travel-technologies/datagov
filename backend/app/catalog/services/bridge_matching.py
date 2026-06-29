"""
Reusable matcher that bridges dbt models to PowerBI tables.

Given the BigQuery FQN persisted on PowerBI tables (bq_project, bq_schema,
bq_source_name) and the equivalent triple on dbt models (database_name,
schema_name, alias), the matcher returns the unique dbt → PBI link, falling
back to display-name matching when the FQN is unavailable.

Pure-python: no Django, no SQL. Callers (run_workflow_final, the rebridge
command, the chatbot tool) are responsible for I/O.
"""
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

# Reason codes for the bridge_reason column on NetworkEdge.
REASON_BQ_FQN = 'bq_fqn'
REASON_NAME_FULL = 'name_full'
REASON_NAME_TAIL = 'name_tail'


@dataclass(frozen=True)
class PbiTableKey:
    item_id: str
    item_name: str
    bq_project: Optional[str] = None
    bq_schema: Optional[str] = None
    bq_source_name: Optional[str] = None


@dataclass(frozen=True)
class DbtModelKey:
    item_id: str
    item_name: str
    database: Optional[str] = None
    schema: Optional[str] = None
    alias: Optional[str] = None
    # Legacy: 'schema.alias' as stored in catalog_item.table_name. Used as
    # fallback for name-based matching and to derive schema/alias if the
    # split fields are missing.
    table_name: Optional[str] = None


@dataclass(frozen=True)
class BridgeMatch:
    dbt_item_id: str
    pbi_item_id: str
    reason: str  # one of REASON_*


def normalize(value: Optional[str]) -> str:
    """Trim, lower-case, strip backticks/quotes; collapse to empty string on None."""
    if value is None:
        return ''
    return str(value).strip().strip('`').strip('"').strip("'").lower()


def _dbt_fqn_triple(d: DbtModelKey) -> Optional[Tuple[str, str, str]]:
    """Return normalized (database, schema, alias) if all three are known, else None.

    Falls back to splitting table_name when schema/alias aren't set explicitly
    so older catalog rows that pre-date migration 0009 still bridge.
    """
    db = normalize(d.database)
    schema = normalize(d.schema)
    alias = normalize(d.alias)
    if not (schema and alias) and d.table_name:
        parts = str(d.table_name).split('.')
        if len(parts) >= 2 and not schema:
            schema = normalize(parts[-2])
        if not alias:
            alias = normalize(parts[-1])
    if not (db and schema and alias):
        return None
    return (db, schema, alias)


def _pbi_fqn_triple(p: PbiTableKey) -> Optional[Tuple[str, str, str]]:
    proj = normalize(p.bq_project)
    schema = normalize(p.bq_schema)
    src = normalize(p.bq_source_name)
    if not (proj and schema and src):
        return None
    return (proj, schema, src)


def match_dbt_to_pbi_table(
    pbi: PbiTableKey,
    candidates: Sequence[DbtModelKey],
) -> Optional[BridgeMatch]:
    """Decide whether a single PBI table maps to one of the dbt candidates.

    Returns a BridgeMatch on a unique match. Returns None when no pass yields
    a single dbt candidate (zero matches OR ambiguous — both fall through).
    """
    pbi_triple = _pbi_fqn_triple(pbi)
    if pbi_triple is not None:
        fqn_hits = [d for d in candidates if _dbt_fqn_triple(d) == pbi_triple]
        if len(fqn_hits) == 1:
            return BridgeMatch(fqn_hits[0].item_id, pbi.item_id, REASON_BQ_FQN)
        # Ambiguous → fall through to name match.

    pbi_name = normalize(pbi.item_name)
    if pbi_name:
        full_hits = [d for d in candidates if normalize(d.table_name) == pbi_name]
        if len(full_hits) == 1:
            return BridgeMatch(full_hits[0].item_id, pbi.item_id, REASON_NAME_FULL)

        tail_hits = []
        for d in candidates:
            tn = d.table_name or ''
            tail = tn.split('.')[-1] if tn else ''
            if normalize(tail) == pbi_name:
                tail_hits.append(d)
        if len(tail_hits) == 1:
            return BridgeMatch(tail_hits[0].item_id, pbi.item_id, REASON_NAME_TAIL)

    return None


def iter_table_pairs(
    pbi_tables: Iterable[PbiTableKey],
    dbt_models: Iterable[DbtModelKey],
) -> Iterator[BridgeMatch]:
    """Yield BridgeMatch for every PBI table that uniquely maps to a dbt model.

    Builds three indexes once (FQN, full-name, tail-name) for O(1) lookup per
    PBI table. Matches that are ambiguous in pass A fall through to pass B.
    """
    dbt_models = list(dbt_models)

    fqn_index: dict = defaultdict(list)
    name_full_index: dict = defaultdict(list)
    name_tail_index: dict = defaultdict(list)

    for d in dbt_models:
        triple = _dbt_fqn_triple(d)
        if triple is not None:
            fqn_index[triple].append(d)
        if d.table_name:
            name_full_index[normalize(d.table_name)].append(d)
            name_tail_index[normalize(str(d.table_name).split('.')[-1])].append(d)

    for p in pbi_tables:
        match: Optional[BridgeMatch] = None

        pbi_triple = _pbi_fqn_triple(p)
        if pbi_triple is not None:
            hits = fqn_index.get(pbi_triple, [])
            if len(hits) == 1:
                match = BridgeMatch(hits[0].item_id, p.item_id, REASON_BQ_FQN)

        if match is None:
            pbi_name = normalize(p.item_name)
            if pbi_name:
                hits = name_full_index.get(pbi_name, [])
                if len(hits) == 1:
                    match = BridgeMatch(hits[0].item_id, p.item_id, REASON_NAME_FULL)
                else:
                    hits = name_tail_index.get(pbi_name, [])
                    if len(hits) == 1:
                        match = BridgeMatch(hits[0].item_id, p.item_id, REASON_NAME_TAIL)

        if match is not None:
            yield match


def iter_column_pairs(
    pbi_columns: Iterable[Tuple[str, str]],
    dbt_columns: Iterable[Tuple[str, str]],
) -> Iterator[Tuple[str, str]]:
    """For two column lists belonging to a matched table pair, yield
    (dbt_col_id, pbi_col_id) for every pair whose names match case-insensitively.

    Inputs are (item_id, item_name) tuples. Phase 1 uses exact case-insensitive
    name matching; Phase 2 may relax to tolerate `_` ↔ space and casing.
    """
    pbi_lookup: dict = {}
    for pc_id, pc_name in pbi_columns:
        key = normalize(pc_name)
        if key:
            pbi_lookup.setdefault(key, (pc_id, pc_name))

    for dc_id, dc_name in dbt_columns:
        hit = pbi_lookup.get(normalize(dc_name))
        if hit:
            yield (dc_id, hit[0])


def preview_matches(
    pbi: PbiTableKey,
    candidates: Sequence[DbtModelKey],
) -> List[BridgeMatch]:
    """Diagnostic helper: return ALL candidate matches across passes (not just
    the unique one). Useful for the chatbot tool / UI tooltip explaining why a
    match was made — or why it wasn't.
    """
    out: List[BridgeMatch] = []
    pbi_triple = _pbi_fqn_triple(pbi)
    if pbi_triple is not None:
        for d in candidates:
            if _dbt_fqn_triple(d) == pbi_triple:
                out.append(BridgeMatch(d.item_id, pbi.item_id, REASON_BQ_FQN))

    pbi_name = normalize(pbi.item_name)
    if pbi_name:
        for d in candidates:
            if normalize(d.table_name) == pbi_name:
                out.append(BridgeMatch(d.item_id, pbi.item_id, REASON_NAME_FULL))
            else:
                tail = (d.table_name or '').split('.')[-1]
                if normalize(tail) == pbi_name:
                    out.append(BridgeMatch(d.item_id, pbi.item_id, REASON_NAME_TAIL))
    return out
