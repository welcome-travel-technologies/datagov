"""
Tests for the dbt ETL pipeline: transform_dbt.py parsing and CSV generation.
"""
import os
import json
import tempfile
import shutil
import pytest

# Add the dbt ETL source directory to sys.path so we can import transform_dbt
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'etl', 'sources', 'dbt'))

from etl.sources.dbt.transform_dbt import main as transform_main, generate_custom_id


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_MANIFEST = {
    "metadata": {
        "project_name": "test_project",
        "project_id": "test_project",
    },
    "nodes": {
        "model.test_project.stg_orders": {
            "resource_type": "model",
            "name": "stg_orders",
            "unique_id": "model.test_project.stg_orders",
            "description": "Staging layer for raw orders",
            "schema": "staging",
            "database": "analytics",
            "alias": "stg_orders",
            "original_file_path": "models/staging/stg_orders.sql",
            "path": "models/staging/stg_orders.sql",
            "raw_code": "SELECT * FROM {{ source('raw', 'orders') }}",
            "config": {"materialized": "view"},
            "tags": ["daily", "core"],
            "meta": {"owner": "data-team", "pii": False},
            "access": "public",
            "columns": {
                "order_id": {
                    "name": "order_id", "description": "Primary key", "data_type": "INTEGER",
                    "constraints": [{"type": "not_null"}],
                    "meta": {"primary_key": True},
                },
                "customer_id": {"name": "customer_id", "description": "FK to customers", "data_type": "INTEGER"},
            },
            "depends_on": {"nodes": ["source.test_project.raw.orders"]},
        },
        "model.test_project.dim_customers": {
            "resource_type": "model",
            "name": "dim_customers",
            "unique_id": "model.test_project.dim_customers",
            "description": "Customer dimension table",
            "schema": "marts",
            "database": "analytics",
            "alias": "dim_customers",
            "original_file_path": "models/marts/dim_customers.sql",
            "path": "models/marts/dim_customers.sql",
            "raw_code": "SELECT * FROM {{ ref('stg_orders') }}",
            "config": {"materialized": "table"},
            "columns": {},
            "depends_on": {"nodes": ["model.test_project.stg_orders"]},
        },
        "seed.test_project.country_codes": {
            "resource_type": "seed",
            "name": "country_codes",
            "unique_id": "seed.test_project.country_codes",
            "description": "ISO country codes",
            "schema": "seeds",
            "database": "analytics",
            "alias": "country_codes",
            "config": {"materialized": "seed"},
            "columns": {},
            "depends_on": {"nodes": []},
        },
        "test.test_project.not_null_stg_orders_order_id": {
            "resource_type": "test",
            "name": "not_null_stg_orders_order_id",
            "unique_id": "test.test_project.not_null_stg_orders_order_id",
            "description": "",
            "test_metadata": {"name": "not_null"},
            "raw_code": "SELECT * WHERE order_id IS NULL",
            "depends_on": {"nodes": ["model.test_project.stg_orders"]},
        },
    },
    "sources": {
        "source.test_project.raw.orders": {
            "name": "orders",
            "source_name": "raw",
            "unique_id": "source.test_project.raw.orders",
            "description": "Raw orders from source system",
            "schema": "raw_data",
            "database": "analytics",
            "identifier": "orders",
            "columns": {
                "id": {"name": "id", "description": "Order ID", "data_type": "INT"},
            },
        },
    },
}


SAMPLE_CATALOG = {
    "metadata": {
        "generated_at": "2025-01-01T00:00:00Z",
    },
    "nodes": {
        "model.test_project.stg_orders": {
            "metadata": {
                "type": "VIEW",
                "schema": "staging",
                "database": "analytics",
                "owner": "dbt_user",
            },
            "columns": {
                "order_id": {"name": "order_id", "type": "INTEGER", "index": 1, "comment": ""},
                "customer_id": {"name": "customer_id", "type": "INTEGER", "index": 2, "comment": "FK to customers table"},
                "order_date": {"name": "order_date", "type": "TIMESTAMP_NTZ", "index": 3, "comment": "When the order was placed"},
                "amount": {"name": "amount", "type": "NUMBER(12,2)", "index": 4, "comment": "Order total"},
            },
            "stats": {
                "num_rows": {"id": "num_rows", "label": "# Rows", "value": 42000, "include": True, "description": "Approximate count of rows"},
                "num_bytes": {"id": "num_bytes", "label": "Approximate Size", "value": 1048576, "include": True, "description": "Approximate size"},
                "has_stats": {"id": "has_stats", "label": "Has Stats?", "value": True, "include": False, "description": "Indicates whether stats exist"},
            },
        },
        "model.test_project.dim_customers": {
            "metadata": {
                "type": "TABLE",
                "schema": "marts",
                "database": "analytics",
            },
            "columns": {
                "customer_id": {"name": "customer_id", "type": "INTEGER", "index": 1, "comment": ""},
                "first_name": {"name": "first_name", "type": "VARCHAR(256)", "index": 2, "comment": ""},
                "email": {"name": "email", "type": "VARCHAR(512)", "index": 3, "comment": ""},
            },
            "stats": {},
        },
    },
    "sources": {
        "source.test_project.raw.orders": {
            "metadata": {
                "type": "BASE TABLE",
                "schema": "raw_data",
                "database": "analytics",
            },
            "columns": {
                "id": {"name": "id", "type": "INT", "index": 1, "comment": "Order ID from source"},
                "customer_id": {"name": "customer_id", "type": "INT", "index": 2, "comment": ""},
                "created_at": {"name": "created_at", "type": "TIMESTAMP_LTZ", "index": 3, "comment": "Row creation timestamp"},
            },
            "stats": {
                "num_rows": {"id": "num_rows", "label": "# Rows", "value": 100000, "include": True, "description": "Approximate count of rows"},
                "has_stats": {"id": "has_stats", "label": "Has Stats?", "value": True, "include": False, "description": "Indicates whether stats exist"},
            },
        },
    },
}


@pytest.fixture
def dbt_repo(tmp_path):
    """Create a temporary dbt repo structure with manifest.json and SQL files."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Write manifest.json
    target_dir = repo_dir / "target"
    target_dir.mkdir()
    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(json.dumps(SAMPLE_MANIFEST), encoding="utf-8")

    # Write SQL files
    staging_dir = repo_dir / "models" / "staging"
    staging_dir.mkdir(parents=True)
    (staging_dir / "stg_orders.sql").write_text(
        "SELECT id AS order_id, customer_id FROM {{ source('raw', 'orders') }}"
    )

    marts_dir = repo_dir / "models" / "marts"
    marts_dir.mkdir(parents=True)
    (marts_dir / "dim_customers.sql").write_text(
        "SELECT * FROM {{ ref('stg_orders') }}"
    )

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    return {
        "repo_dir": str(repo_dir),
        "manifest_path": str(manifest_path),
        "output_dir": str(output_dir),
    }


@pytest.fixture
def dbt_repo_with_catalog(dbt_repo, tmp_path):
    """Extend dbt_repo with a catalog.json file alongside manifest.json."""
    target_dir = os.path.join(os.path.dirname(dbt_repo["manifest_path"]))
    catalog_path = os.path.join(target_dir, "catalog.json")
    with open(catalog_path, 'w', encoding='utf-8') as f:
        json.dump(SAMPLE_CATALOG, f)

    return {
        **dbt_repo,
        "catalog_path": catalog_path,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestGenerateCustomId:
    def test_deterministic(self):
        id1 = generate_custom_id("project", "model.test.foo")
        id2 = generate_custom_id("project", "model.test.foo")
        assert id1 == id2

    def test_different_inputs(self):
        id1 = generate_custom_id("project", "model.test.foo")
        id2 = generate_custom_id("project", "model.test.bar")
        assert id1 != id2

    def test_returns_md5_hex(self):
        result = generate_custom_id("a", "b")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)


class TestTransformDbt:
    def test_produces_items_csv(self, dbt_repo):
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        items_csv = os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv")
        assert os.path.exists(items_csv)

    def test_produces_graph_csv(self, dbt_repo):
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        graph_csv = os.path.join(dbt_repo["output_dir"], "dbt_info_graph.csv")
        assert os.path.exists(graph_csv)

    def test_items_csv_contains_models(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        model_names = df[df["item_type"] == "DBT_MODEL"]["item_name"].tolist()
        assert "stg_orders" in model_names
        assert "dim_customers" in model_names

    def test_items_csv_contains_sources(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        sources = df[df["item_type"] == "DBT_SOURCE"]["item_name"].tolist()
        assert "raw.orders" in sources

    def test_items_csv_contains_columns(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        columns = df[df["item_type"] == "DBT_COLUMN"]["item_name"].tolist()
        assert "order_id" in columns
        assert "customer_id" in columns

    def test_items_csv_contains_tests(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        tests = df[df["item_type"] == "DBT_TEST"]["item_name"].tolist()
        assert "not_null_stg_orders_order_id" in tests

    def test_items_csv_contains_seeds(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        seeds = df[df["item_type"] == "DBT_SEED"]["item_name"].tolist()
        assert "country_codes" in seeds

    def test_sql_stored_in_expression(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        stg = df[df["item_name"] == "stg_orders"]
        assert len(stg) == 1
        expr = stg.iloc[0]["expression"]
        # Should contain the SQL from the file (not the manifest raw_code)
        assert "order_id" in expr

    def test_graph_has_dependency_edges(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_graph.csv"))
        # Should have edge: source -> stg_orders
        source_to_stg = df[
            (df["source_type"] == "DBT_SOURCE") &
            (df["target"].str.contains("stg_orders", na=False))
        ]
        assert len(source_to_stg) > 0

        # Should have edge: stg_orders -> dim_customers
        stg_to_dim = df[
            (df["source"].str.contains("stg_orders", na=False)) &
            (df["target"].str.contains("dim_customers", na=False))
        ]
        assert len(stg_to_dim) > 0

    def test_all_items_have_service_dbt(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        assert (df["item_service"] == "dbt").all()

    def test_no_duplicate_item_ids(self, dbt_repo):
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        assert df["item_id"].is_unique

    def test_model_columns_have_dataset_id_set_to_model_unique_id(self, dbt_repo):
        """Column-level bridge depends on dbt COLUMN.dataset_id == model unique_id."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        # Get model columns (columns belonging to stg_orders model)
        model_cols = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["table_name"] == "stg_orders")
        ]
        assert len(model_cols) > 0, "Expected columns for stg_orders model"
        # All these columns should have dataset_id == the model's unique_id
        for _, col in model_cols.iterrows():
            assert col["dataset_id"] == "model.test_project.stg_orders"

    def test_source_columns_have_dataset_id_set_to_source_unique_id(self, dbt_repo):
        """Source columns should have dataset_id == source unique_id."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        source_cols = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["column_type"] == "source")
        ]
        assert len(source_cols) > 0, "Expected columns from dbt sources"
        for _, col in source_cols.iterrows():
            assert col["dataset_id"] == "source.test_project.raw.orders"

    def test_graph_has_model_to_column_edges(self, dbt_repo):
        """Verify DBT_MODEL → COLUMN edges exist in the graph."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_graph.csv"))
        model_col_edges = df[
            (df["source_type"] == "DBT_MODEL") &
            (df["target_type"] == "DBT_COLUMN")
        ]
        assert len(model_col_edges) > 0, "Expected DBT_MODEL → COLUMN edges"
        # stg_orders has 2 columns: order_id, customer_id
        stg_col_edges = model_col_edges[
            model_col_edges["source"].str.contains("stg_orders", na=False)
        ]
        assert len(stg_col_edges) == 2

    def test_graph_has_source_to_column_edges(self, dbt_repo):
        """Verify DBT_SOURCE → COLUMN edges exist in the graph."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_graph.csv"))
        src_col_edges = df[
            (df["source_type"] == "DBT_SOURCE") &
            (df["target_type"] == "DBT_COLUMN")
        ]
        assert len(src_col_edges) > 0, "Expected DBT_SOURCE → COLUMN edges"

    def test_columns_have_description_and_datatype(self, dbt_repo):
        """Verify column descriptions and data types are extracted from manifest."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        order_id_col = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["item_name"] == "order_id") &
            (df["table_name"] == "stg_orders")
        ]
        assert len(order_id_col) == 1
        row = order_id_col.iloc[0]
        assert row["description"] == "Primary key"
        assert row["datatype"] == "INTEGER"


class TestCatalogJsonMerge:
    """Tests for catalog.json enrichment — complete columns, real data types, stats."""

    def test_catalog_adds_undocumented_columns(self, dbt_repo_with_catalog):
        """Catalog should add columns not documented in manifest YAML (order_date, amount)."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo_with_catalog["manifest_path"],
            repo_dir=dbt_repo_with_catalog["repo_dir"],
            output_dir=dbt_repo_with_catalog["output_dir"],
            catalog_path=dbt_repo_with_catalog["catalog_path"],
        )
        df = pd.read_csv(os.path.join(dbt_repo_with_catalog["output_dir"], "dbt_info_items.csv"))

        # stg_orders has 2 columns in manifest, 4 in catalog
        stg_cols = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["table_name"] == "stg_orders")
        ]
        col_names = stg_cols["item_name"].tolist()
        assert "order_id" in col_names
        assert "customer_id" in col_names
        assert "order_date" in col_names, "catalog-only column 'order_date' should be added"
        assert "amount" in col_names, "catalog-only column 'amount' should be added"
        assert len(stg_cols) == 4

    def test_catalog_provides_real_data_types(self, dbt_repo_with_catalog):
        """Catalog-only columns should have database data types from catalog.json."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo_with_catalog["manifest_path"],
            repo_dir=dbt_repo_with_catalog["repo_dir"],
            output_dir=dbt_repo_with_catalog["output_dir"],
            catalog_path=dbt_repo_with_catalog["catalog_path"],
        )
        df = pd.read_csv(os.path.join(dbt_repo_with_catalog["output_dir"], "dbt_info_items.csv"))

        order_date = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["item_name"] == "order_date") &
            (df["table_name"] == "stg_orders")
        ]
        assert len(order_date) == 1
        assert order_date.iloc[0]["datatype"] == "TIMESTAMP_NTZ"

        amount = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["item_name"] == "amount") &
            (df["table_name"] == "stg_orders")
        ]
        assert len(amount) == 1
        assert amount.iloc[0]["datatype"] == "NUMBER(12,2)"

    def test_catalog_adds_columns_for_model_without_manifest_columns(self, dbt_repo_with_catalog):
        """dim_customers has no columns in manifest but 3 in catalog."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo_with_catalog["manifest_path"],
            repo_dir=dbt_repo_with_catalog["repo_dir"],
            output_dir=dbt_repo_with_catalog["output_dir"],
            catalog_path=dbt_repo_with_catalog["catalog_path"],
        )
        df = pd.read_csv(os.path.join(dbt_repo_with_catalog["output_dir"], "dbt_info_items.csv"))

        dim_cols = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["table_name"] == "dim_customers")
        ]
        col_names = dim_cols["item_name"].tolist()
        assert "customer_id" in col_names
        assert "first_name" in col_names
        assert "email" in col_names
        assert len(dim_cols) == 3

    def test_catalog_adds_source_columns(self, dbt_repo_with_catalog):
        """Source 'orders' has 1 column in manifest, 3 in catalog."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo_with_catalog["manifest_path"],
            repo_dir=dbt_repo_with_catalog["repo_dir"],
            output_dir=dbt_repo_with_catalog["output_dir"],
            catalog_path=dbt_repo_with_catalog["catalog_path"],
        )
        df = pd.read_csv(os.path.join(dbt_repo_with_catalog["output_dir"], "dbt_info_items.csv"))

        src_cols = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["column_type"] == "source")
        ]
        col_names = src_cols["item_name"].tolist()
        assert "id" in col_names
        assert "customer_id" in col_names
        assert "created_at" in col_names
        assert len(src_cols) == 3

    def test_manifest_description_preferred_over_catalog_comment(self, dbt_repo_with_catalog):
        """When both manifest and catalog have info, manifest description wins."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo_with_catalog["manifest_path"],
            repo_dir=dbt_repo_with_catalog["repo_dir"],
            output_dir=dbt_repo_with_catalog["output_dir"],
            catalog_path=dbt_repo_with_catalog["catalog_path"],
        )
        df = pd.read_csv(os.path.join(dbt_repo_with_catalog["output_dir"], "dbt_info_items.csv"))

        # order_id has "Primary key" in manifest, empty comment in catalog
        order_id = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["item_name"] == "order_id") &
            (df["table_name"] == "stg_orders")
        ]
        assert order_id.iloc[0]["description"] == "Primary key"

    def test_catalog_comment_used_when_manifest_empty(self, dbt_repo_with_catalog):
        """When manifest has no description, catalog comment is used."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo_with_catalog["manifest_path"],
            repo_dir=dbt_repo_with_catalog["repo_dir"],
            output_dir=dbt_repo_with_catalog["output_dir"],
            catalog_path=dbt_repo_with_catalog["catalog_path"],
        )
        df = pd.read_csv(os.path.join(dbt_repo_with_catalog["output_dir"], "dbt_info_items.csv"))

        # order_date only exists in catalog, comment = "When the order was placed"
        order_date = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["item_name"] == "order_date") &
            (df["table_name"] == "stg_orders")
        ]
        assert order_date.iloc[0]["description"] == "When the order was placed"

    def test_without_catalog_only_manifest_columns(self, dbt_repo):
        """Without catalog, only manifest-documented columns should appear."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))

        # stg_orders should have only 2 columns (from manifest)
        stg_cols = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["table_name"] == "stg_orders")
        ]
        assert len(stg_cols) == 2

        # dim_customers has no columns in manifest
        dim_cols = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["table_name"] == "dim_customers")
        ]
        assert len(dim_cols) == 0

    def test_catalog_stats_in_formatstring(self, dbt_repo_with_catalog):
        """Model stats from catalog should be stored in the formatstring field."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo_with_catalog["manifest_path"],
            repo_dir=dbt_repo_with_catalog["repo_dir"],
            output_dir=dbt_repo_with_catalog["output_dir"],
            catalog_path=dbt_repo_with_catalog["catalog_path"],
        )
        df = pd.read_csv(os.path.join(dbt_repo_with_catalog["output_dir"], "dbt_info_items.csv"))

        stg = df[
            (df["item_type"] == "DBT_MODEL") &
            (df["item_name"] == "stg_orders")
        ]
        assert len(stg) == 1
        fmt = stg.iloc[0]["formatstring"]
        assert "# Rows" in str(fmt)
        assert "42000" in str(fmt)


class TestNewMetadataColumns:
    """Tests for the new metadata columns: database_name, tags, meta, access_level."""

    def test_csv_has_new_metadata_columns(self, dbt_repo):
        """The output CSV should include database_name, tags, meta columns."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        assert "database_name" in df.columns
        assert "tags" in df.columns
        assert "meta" in df.columns

    def test_database_name_extracted(self, dbt_repo):
        """Models with database in manifest should have database_name populated."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        stg = df[df["item_name"] == "stg_orders"]
        assert len(stg) == 1
        assert stg.iloc[0]["database_name"] == "analytics"

    def test_source_database_name_extracted(self, dbt_repo):
        """Sources should have database_name from manifest."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        src = df[df["item_type"] == "DBT_SOURCE"]
        assert len(src) == 1
        assert src.iloc[0]["database_name"] == "analytics"

    def test_tags_extracted_as_json(self, dbt_repo):
        """Model tags should be stored as a JSON list string."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        stg = df[df["item_name"] == "stg_orders"]
        tags = json.loads(stg.iloc[0]["tags"])
        assert isinstance(tags, list)
        assert "daily" in tags
        assert "core" in tags

    def test_meta_extracted_as_json(self, dbt_repo):
        """Model meta should be stored as a JSON dict string."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        stg = df[df["item_name"] == "stg_orders"]
        meta = json.loads(stg.iloc[0]["meta"])
        assert isinstance(meta, dict)
        assert meta.get("owner") == "data-team"

    def test_access_level_in_meta(self, dbt_repo):
        """Model access should be stored inside the meta JSON dict."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        stg = df[df["item_name"] == "stg_orders"]
        meta = json.loads(stg.iloc[0]["meta"])
        assert meta.get("access") == "public"

    def test_column_constraints_in_meta(self, dbt_repo):
        """Column constraints should be stored in the column's meta JSON."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        order_id = df[
            (df["item_type"] == "DBT_COLUMN") &
            (df["item_name"] == "order_id") &
            (df["table_name"] == "stg_orders")
        ]
        assert len(order_id) == 1
        meta = json.loads(order_id.iloc[0]["meta"])
        assert "constraints" in meta
        assert "not_null" in meta["constraints"]
        assert meta.get("primary_key") is True

    def test_empty_tags_default_to_empty_list(self, dbt_repo):
        """Models without tags should have '[]' as tags."""
        import pandas as pd
        transform_main(
            manifest_path=dbt_repo["manifest_path"],
            repo_dir=dbt_repo["repo_dir"],
            output_dir=dbt_repo["output_dir"],
        )
        df = pd.read_csv(os.path.join(dbt_repo["output_dir"], "dbt_info_items.csv"))
        dim = df[df["item_name"] == "dim_customers"]
        assert len(dim) == 1
        tags = json.loads(dim.iloc[0]["tags"])
        assert tags == []
