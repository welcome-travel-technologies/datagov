"""
Unit tests for catalog.services.bridge_matching.

Pure-python — no DB, no Django. Run with `pytest test_bridge_matching.py`.
"""
from catalog.services.bridge_matching import (
    DbtModelKey,
    PbiTableKey,
    REASON_BQ_FQN,
    REASON_NAME_FULL,
    REASON_NAME_TAIL,
    iter_column_pairs,
    iter_table_pairs,
    match_dbt_to_pbi_table,
    normalize,
    preview_matches,
)


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

def test_normalize_strips_quotes_and_lowercases():
    assert normalize('  `Foo` ') == 'foo'
    assert normalize('"BAR"') == 'bar'
    assert normalize("'baz'") == 'baz'


def test_normalize_handles_none():
    assert normalize(None) == ''


# ---------------------------------------------------------------------------
# match_dbt_to_pbi_table
# ---------------------------------------------------------------------------

def _dbt(item_id, table_name='analytics.dim_driver', database='proj', schema='analytics', alias='dim_driver'):
    return DbtModelKey(item_id=item_id, item_name=alias, database=database, schema=schema, alias=alias, table_name=table_name)


def _pbi(item_id, item_name, project='proj', schema='analytics', source='dim_driver'):
    return PbiTableKey(item_id=item_id, item_name=item_name, bq_project=project, bq_schema=schema, bq_source_name=source)


def test_match_prefers_bq_fqn_even_when_pbi_renamed():
    # PBI table has been renamed in the model ('Driver') but the M-query still
    # points at proj.analytics.dim_driver — FQN match should win.
    pbi = _pbi('p1', 'Driver')
    dbt = [_dbt('d1')]
    m = match_dbt_to_pbi_table(pbi, dbt)
    assert m is not None
    assert m.dbt_item_id == 'd1'
    assert m.pbi_item_id == 'p1'
    assert m.reason == REASON_BQ_FQN


def test_match_falls_back_to_name_when_no_bq():
    # Pure name match — no BQ source on the PBI side (e.g. CSV-loaded).
    pbi = PbiTableKey(item_id='p1', item_name='dim_driver')
    dbt = [_dbt('d1')]
    m = match_dbt_to_pbi_table(pbi, dbt)
    assert m is not None
    assert m.reason == REASON_NAME_TAIL


def test_match_full_name_beats_tail_name():
    pbi = PbiTableKey(item_id='p1', item_name='analytics.dim_driver')
    dbt = [_dbt('d1', table_name='analytics.dim_driver')]
    m = match_dbt_to_pbi_table(pbi, dbt)
    assert m is not None
    assert m.reason == REASON_NAME_FULL


def test_ambiguous_fqn_falls_through_to_name():
    # Two dbt models claim the same FQN — pass A is ambiguous, pass B picks
    # the one whose tail matches.
    a = _dbt('da', alias='dim_driver')
    b = _dbt('db', alias='dim_driver', table_name='other.dim_driver_2')
    # Both share (proj, analytics, dim_driver) on FQN. Make alias different
    # so name disambiguates.
    b = DbtModelKey(item_id='db', item_name='dim_driver', database='proj', schema='analytics', alias='dim_driver', table_name='analytics.dim_driver_2')
    pbi = PbiTableKey(item_id='p1', item_name='dim_driver_2', bq_project='proj', bq_schema='analytics', bq_source_name='dim_driver')
    m = match_dbt_to_pbi_table(pbi, [a, b])
    # a and b have same FQN → ambiguous; tail-name 'dim_driver_2' matches b.
    assert m is not None
    assert m.dbt_item_id == 'db'
    assert m.reason == REASON_NAME_TAIL


def test_no_match_returns_none():
    pbi = PbiTableKey(item_id='p1', item_name='unknown_table')
    dbt = [_dbt('d1')]
    assert match_dbt_to_pbi_table(pbi, dbt) is None


def test_ambiguous_name_returns_none():
    a = DbtModelKey(item_id='da', item_name='x', table_name='analytics.dim_driver')
    b = DbtModelKey(item_id='db', item_name='x', table_name='reporting.dim_driver')
    pbi = PbiTableKey(item_id='p1', item_name='dim_driver')
    # Both tail-match 'dim_driver' → ambiguous → no match.
    assert match_dbt_to_pbi_table(pbi, [a, b]) is None


def test_dbt_split_table_name_when_schema_alias_missing():
    # Older catalog row: schema/alias not yet split out — matcher should
    # parse them from table_name.
    d = DbtModelKey(
        item_id='d1', item_name='dim_driver',
        database='proj', schema=None, alias=None,
        table_name='analytics.dim_driver',
    )
    pbi = _pbi('p1', 'Driver')
    m = match_dbt_to_pbi_table(pbi, [d])
    assert m is not None
    assert m.reason == REASON_BQ_FQN


def test_non_bigquery_dbt_target_falls_back_to_name():
    # dbt model materialises to Postgres ('database' = 'postgres_db'). The
    # PBI side points at a BigQuery project — pass A finds zero matches and
    # we fall through to name.
    d = DbtModelKey(
        item_id='d1', item_name='dim_driver',
        database='postgres_db', schema='analytics', alias='dim_driver',
        table_name='analytics.dim_driver',
    )
    pbi = _pbi('p1', 'dim_driver')  # bq_project='proj' — won't match postgres_db
    m = match_dbt_to_pbi_table(pbi, [d])
    assert m is not None
    assert m.reason == REASON_NAME_TAIL


# ---------------------------------------------------------------------------
# iter_table_pairs (batch generator)
# ---------------------------------------------------------------------------

def test_iter_table_pairs_yields_one_per_pbi():
    dbt = [_dbt('d1'), _dbt('d2', alias='dim_other', table_name='analytics.dim_other')]
    # Build d2 with its own FQN
    dbt[1] = DbtModelKey(
        item_id='d2', item_name='dim_other',
        database='proj', schema='analytics', alias='dim_other',
        table_name='analytics.dim_other',
    )
    pbi = [
        _pbi('p1', 'Driver'),
        _pbi('p2', 'Other', project='proj', schema='analytics', source='dim_other'),
    ]
    out = list(iter_table_pairs(pbi, dbt))
    pairs = {(m.pbi_item_id, m.dbt_item_id) for m in out}
    assert pairs == {('p1', 'd1'), ('p2', 'd2')}
    assert all(m.reason == REASON_BQ_FQN for m in out)


def test_iter_table_pairs_skips_unmatched():
    dbt = [_dbt('d1')]
    pbi = [_pbi('p1', 'Driver'), PbiTableKey(item_id='p2', item_name='lonely_csv_table')]
    out = list(iter_table_pairs(pbi, dbt))
    assert len(out) == 1
    assert out[0].pbi_item_id == 'p1'


# ---------------------------------------------------------------------------
# iter_column_pairs
# ---------------------------------------------------------------------------

def test_iter_column_pairs_case_insensitive():
    dbt_cols = [('dc1', 'Customer_ID'), ('dc2', 'name')]
    pbi_cols = [('pc1', 'customer_id'), ('pc2', 'NAME'), ('pc3', 'extra')]
    out = sorted(iter_column_pairs(pbi_cols, dbt_cols))
    assert out == [('dc1', 'pc1'), ('dc2', 'pc2')]


def test_iter_column_pairs_no_match_returns_empty():
    out = list(iter_column_pairs([('pc1', 'x')], [('dc1', 'y')]))
    assert out == []


# ---------------------------------------------------------------------------
# preview_matches (diagnostic)
# ---------------------------------------------------------------------------

def test_preview_matches_returns_all_passes():
    # FQN matches d1; tail matches d1 too. Preview should report both reasons.
    d = _dbt('d1')
    pbi = _pbi('p1', 'dim_driver')
    out = preview_matches(pbi, [d])
    reasons = {m.reason for m in out}
    assert REASON_BQ_FQN in reasons
    assert REASON_NAME_TAIL in reasons
