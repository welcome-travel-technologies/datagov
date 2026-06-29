"""
Unit tests for etl.sources.dbt.column_lineage.

Pure-python — no DB, no Django. Drives sqlglot over a tiny
source -> staging -> mart chain and asserts the expected
DBT_COLUMN -> DBT_COLUMN edges, including a multi-column derivation
(``CASE WHEN is_cancelled THEN 0 ELSE amount END`` — the SQL analog of the
PowerBI ``CALCULATE(SUM(amount), is_cancelled = 0)`` example).

Run with `pytest catalog/tests/test_column_lineage.py`.
"""
from etl.sources.dbt.column_lineage import (
    build_relation_index,
    extract_column_edges,
    _resolve_uid,
)

PROJECT = "shop"

SRC = "source.shop.raw.orders"
STG = "model.shop.stg_orders"
FCT = "model.shop.fct_revenue"


def _sources():
    return {
        SRC: {"database": "db", "schema": "raw", "identifier": "orders", "name": "orders"},
    }


def _models():
    return {
        STG: {
            "database": "db", "schema": "analytics", "alias": "stg_orders", "name": "stg_orders",
            "compiled_code": "SELECT order_id, amount, is_cancelled FROM `db`.`raw`.`orders`",
        },
        FCT: {
            "database": "db", "schema": "analytics", "alias": "fct_revenue", "name": "fct_revenue",
            "compiled_code": (
                "SELECT order_id, "
                "CASE WHEN is_cancelled THEN 0 ELSE amount END AS revenue "
                "FROM `db`.`analytics`.`stg_orders`"
            ),
        },
    }


def _col(uid, name):
    return f"{uid}::{name}"


def _col_index():
    def cols(uid, names):
        return {n.lower(): (n, _col(uid, n)) for n in names}
    return {
        SRC: cols(SRC, ["order_id", "amount", "is_cancelled"]),
        STG: cols(STG, ["order_id", "amount", "is_cancelled"]),
        FCT: cols(FCT, ["order_id", "revenue"]),
    }


def _catalog():
    def entry(names):
        return {"columns": {n.lower(): {"name": n, "type": "STRING"} for n in names}}
    return {
        SRC: entry(["order_id", "amount", "is_cancelled"]),
        STG: entry(["order_id", "amount", "is_cancelled"]),
        FCT: entry(["order_id", "revenue"]),
    }


def _edges(models=None, sources=None, catalog=None, col_index=None):
    return extract_column_edges(
        models or _models(), sources or _sources(),
        catalog or _catalog(), col_index or _col_index(),
        PROJECT, log=lambda *_a, **_k: None,
    )


def _edge_keys(models=None, sources=None, catalog=None, col_index=None):
    edges = _edges(models, sources, catalog, col_index)
    return {(src_id, tgt_id) for src_id, _sn, tgt_id, _tn, _lt in edges}


def test_source_to_staging_columns():
    keys = _edge_keys()
    assert (_col(SRC, "order_id"), _col(STG, "order_id")) in keys
    assert (_col(SRC, "amount"), _col(STG, "amount")) in keys
    assert (_col(SRC, "is_cancelled"), _col(STG, "is_cancelled")) in keys


def test_model_to_model_and_multicolumn_derivation():
    keys = _edge_keys()
    # revenue is derived from BOTH amount and is_cancelled (CASE WHEN).
    assert (_col(STG, "amount"), _col(FCT, "revenue")) in keys
    assert (_col(STG, "is_cancelled"), _col(FCT, "revenue")) in keys
    assert (_col(STG, "order_id"), _col(FCT, "order_id")) in keys


def test_no_self_or_duplicate_edges():
    edges = _edges()
    keys = [(src_id, tgt_id) for src_id, _sn, tgt_id, _tn, _lt in edges]
    assert all(s != t for s, t in keys)
    assert len(keys) == len(set(keys))


def test_lineage_type_classification():
    """Each edge carries how its target column was derived."""
    ltype = {(src_id, tgt_id): lt for src_id, _sn, tgt_id, _tn, lt in _edges()}
    # plain column passed straight through
    assert ltype[(_col(SRC, "amount"), _col(STG, "amount"))] == "pass-through"
    assert ltype[(_col(STG, "order_id"), _col(FCT, "order_id"))] == "pass-through"
    # revenue = CASE WHEN over 2 upstream columns => transformation
    assert ltype[(_col(STG, "amount"), _col(FCT, "revenue"))] == "transformation"
    assert ltype[(_col(STG, "is_cancelled"), _col(FCT, "revenue"))] == "transformation"


def test_relation_index_resolves_three_and_two_part():
    three, two = build_relation_index(_models(), _sources())
    assert _resolve_uid("db.analytics.stg_orders", three, two) == STG
    assert _resolve_uid("db.raw.orders", three, two) == SRC
    # 2-part fallback (unambiguous here).
    assert _resolve_uid("raw.orders", three, two) == SRC


def test_models_without_compiled_sql_are_skipped():
    models = _models()
    models[STG]["compiled_code"] = ""
    keys = _edge_keys(models=models)
    # staging has no SQL -> its source columns are not linked.
    assert (_col(SRC, "amount"), _col(STG, "amount")) not in keys
    # fct still resolves against the staging column ids.
    assert (_col(STG, "amount"), _col(FCT, "revenue")) in keys


def test_raw_code_is_resolved_when_no_compiled_sql():
    """raw_code with {{ ref() }} / {{ source() }} must yield lineage on its own."""
    sources = {
        SRC: {"database": "db", "schema": "raw", "identifier": "orders",
              "name": "orders", "source_name": "raw"},
    }
    models = {
        STG: {
            "database": "db", "schema": "analytics", "alias": "stg_orders", "name": "stg_orders",
            "relation_name": "`db`.`analytics`.`stg_orders`",
            "depends_on": {"nodes": [SRC]},
            # No compiled_code — only Jinja raw_code.
            "raw_code": "{{ config(materialized='view') }}\n"
                        "SELECT order_id, amount, is_cancelled FROM {{ source('raw', 'orders') }}",
        },
        FCT: {
            "database": "db", "schema": "analytics", "alias": "fct_revenue", "name": "fct_revenue",
            "relation_name": "`db`.`analytics`.`fct_revenue`",
            "depends_on": {"nodes": [STG]},
            "raw_code": "SELECT order_id, "
                        "CASE WHEN is_cancelled THEN 0 ELSE amount END AS revenue "
                        "FROM {{ ref('stg_orders') }}",
        },
    }
    keys = _edge_keys(models=models, sources=sources)
    assert (_col(SRC, "amount"), _col(STG, "amount")) in keys
    assert (_col(STG, "amount"), _col(FCT, "revenue")) in keys
    assert (_col(STG, "is_cancelled"), _col(FCT, "revenue")) in keys


def test_unresolvable_macro_jinja_is_skipped_not_misparsed():
    """A model whose raw_code uses a macro (unresolvable) is skipped, not crashed."""
    models = _models()
    del models[FCT]["compiled_code"]
    models[FCT]["raw_code"] = "SELECT order_id, {{ my_revenue_macro() }} AS revenue FROM {{ ref('stg_orders') }}"
    models[FCT]["depends_on"] = {"nodes": [STG]}
    keys = _edge_keys(models=models)  # must not raise
    # The staging chain (compiled) still resolves; the macro model is skipped.
    assert (_col(SRC, "amount"), _col(STG, "amount")) in keys
    assert (_col(STG, "amount"), _col(FCT, "revenue")) not in keys


def test_unparseable_sql_does_not_raise():
    models = _models()
    models[FCT]["compiled_code"] = "SELECT FROM WHERE ;;; not valid"
    keys = _edge_keys(models=models)  # must not raise
    # staging chain still produced.
    assert (_col(SRC, "amount"), _col(STG, "amount")) in keys
