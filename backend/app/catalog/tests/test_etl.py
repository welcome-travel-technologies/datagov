import sys
import os
import json
import tempfile
import collections
import responses
from unittest.mock import patch, MagicMock
from django.test import TestCase

# Add the etl directory to path so we can import the scripts
sys.path.append(os.path.join(os.path.dirname(__file__), '../../etl'))

from sources.fabric import extract_fabric
from sources.fabric import transform_fabric


class ETLTests(TestCase):
    @responses.activate
    def test_extract_fabric(self):
        # Mock auth response
        responses.add(
            responses.POST,
            'https://login.microsoftonline.com/tenant/oauth2/v2.0/token',
            json={'access_token': 'fake_token'},
            status=200
        )

        # Mock workspaces and items (SemanticModels and Reports)
        responses.add(
            responses.GET,
            'https://api.fabric.microsoft.com/v1/workspaces/ws_1/items?type=SemanticModel',
            json={'value': [{'id': '123', 'displayName': 'TestModel'}]},
            status=200
        )
        
        responses.add(
            responses.GET,
            'https://api.fabric.microsoft.com/v1/workspaces/ws_1/items?type=Report',
            json={'value': []},
            status=200
        )
        
        # Mock getDefinition
        responses.add(
            responses.POST,
            'https://api.fabric.microsoft.com/v1/workspaces/ws_1/items/123/getDefinition',
            json={'definition': {'parts': []}},
            status=200
        )

        def mock_log(msg):
            pass

        real_etl_dir = os.path.join(os.path.dirname(__file__), '../../etl/sources/fabric')
        
        import sources.fabric.transform_fabric
        sys.modules['transform_fabric'] = sources.fabric.transform_fabric
        
        with patch('sources.fabric.transform_fabric.main') as mock_transform:
            extract_fabric.run_fabric_extraction(
                'tenant', 'client', 'secret', ['ws_1'], real_etl_dir, mock_log
            )
            self.assertTrue(mock_transform.called)

    def test_transform_custom_id(self):
        # Test the custom ID logic
        id1 = transform_fabric.generate_custom_id("ws1", "table1", "col1")
        self.assertIsNotNone(id1)
        id2 = transform_fabric.generate_custom_id("WS1", "Table1", " Col1 ")
        self.assertEqual(id1, id2)


class TransformFabricGenerateCustomIdTests(TestCase):
    """Tests for generate_custom_id — the core hashing function."""

    def test_returns_32_char_hex_string(self):
        result = transform_fabric.generate_custom_id("a", "b")
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in result))

    def test_case_insensitive(self):
        self.assertEqual(
            transform_fabric.generate_custom_id("WS1", "PB_TABLE"),
            transform_fabric.generate_custom_id("ws1", "pb_table"),
        )

    def test_whitespace_stripped(self):
        self.assertEqual(
            transform_fabric.generate_custom_id(" ws1 ", "col"),
            transform_fabric.generate_custom_id("ws1", "col"),
        )

    def test_none_args_filtered_out(self):
        # None args are skipped, so ("ws1", None, "col") == ("ws1", "col")
        self.assertEqual(
            transform_fabric.generate_custom_id("ws1", None, "col"),
            transform_fabric.generate_custom_id("ws1", "col"),
        )

    def test_different_args_produce_different_ids(self):
        id_a = transform_fabric.generate_custom_id("ws1", "table_a")
        id_b = transform_fabric.generate_custom_id("ws1", "table_b")
        self.assertNotEqual(id_a, id_b)

    def test_deterministic(self):
        # Same call twice must return the same value (also tests lru_cache safety)
        id1 = transform_fabric.generate_custom_id("ds", "lineage-tag-xyz")
        id2 = transform_fabric.generate_custom_id("ds", "lineage-tag-xyz")
        self.assertEqual(id1, id2)

    def test_quotes_and_backticks_stripped(self):
        # Quotes/backticks are removed before hashing
        self.assertEqual(
            transform_fabric.generate_custom_id("'ws1'", '"table"'),
            transform_fabric.generate_custom_id("ws1", "table"),
        )


class TransformFabricExtractFieldsTests(TestCase):
    """Tests for extract_fields_from_visual."""

    def test_extracts_property_field(self):
        config = '{"Property": "City"}'
        fields = transform_fabric.extract_fields_from_visual(config)
        self.assertIn("City", fields)

    def test_extracts_name_field(self):
        config = '{"Name": "Sales.Amount"}'
        fields = transform_fabric.extract_fields_from_visual(config)
        self.assertIn("Sales.Amount", fields)

    def test_filters_short_matches(self):
        # 1-2 char matches should be ignored
        config = '{"Property": "AB", "Name": "x"}'
        fields = transform_fabric.extract_fields_from_visual(config)
        self.assertNotIn("AB", fields)
        self.assertNotIn("x", fields)

    def test_filters_numeric_matches(self):
        config = '{"Property": "123"}'
        fields = transform_fabric.extract_fields_from_visual(config)
        self.assertNotIn("123", fields)

    def test_empty_string_returns_empty_set(self):
        self.assertEqual(transform_fabric.extract_fields_from_visual(""), set())

    def test_multiple_fields_extracted(self):
        config = '{"Property": "City", "Name": "Revenue", "Property": "Country"}'
        fields = transform_fabric.extract_fields_from_visual(config)
        self.assertIn("City", fields)
        self.assertIn("Revenue", fields)


class TransformFabricParseBigQuerySourceTests(TestCase):
    """Tests for parse_bigquery_source."""

    def test_sql_from_clause(self):
        # Production M-code wraps native SQL in Value.NativeQuery — bare SELECT
        # never appears outside a BigQuery connector context.
        m_code = (
            'let Source = GoogleBigQuery.Database(null), '
            'Q = Value.NativeQuery(Source, "SELECT * FROM myproject.mydataset.mytable") in Q'
        )
        p, s, t, k = transform_fabric.parse_bigquery_source(m_code)
        self.assertEqual(p, "myproject")
        self.assertEqual(s, "mydataset")
        self.assertEqual(t, "mytable")
        self.assertEqual(k, "SQL Query")

    def test_sql_backtick_syntax(self):
        m_code = (
            'let Source = GoogleBigQuery.Database(null), '
            'Q = Value.NativeQuery(Source, "SELECT col FROM `proj-1.dataset_a.table_b`") in Q'
        )
        p, s, t, k = transform_fabric.parse_bigquery_source(m_code)
        self.assertEqual(p, "proj-1")
        self.assertEqual(s, "dataset_a")
        self.assertEqual(t, "table_b")
        self.assertEqual(k, "SQL Query")

    def test_empty_returns_none_tuple(self):
        result = transform_fabric.parse_bigquery_source("")
        self.assertEqual(result, (None, None, None, None))

    def test_non_string_returns_none_tuple(self):
        result = transform_fabric.parse_bigquery_source(None)
        self.assertEqual(result, (None, None, None, None))

    def test_no_match_returns_none(self):
        m_code = "let Source = SomeOtherConnector() in Source"
        p, s, t, k = transform_fabric.parse_bigquery_source(m_code)
        self.assertIsNone(p)
        self.assertIsNone(s)
        self.assertIsNone(t)
        self.assertIsNone(k)

    def test_structured_navigation(self):
        # M-code generated by Power Query when a user picks a BQ table via the
        # Navigator UI — no native SQL, just nested {[Name=...,Kind=...]} steps.
        m_code = (
            'let\n'
            'Source = GoogleBigQuery.Database([BillingProject = null]),\n'
            '#"Navigation 1" = Source{[Name = "adroit-nectar-124618"]}[Data],\n'
            '#"Navigation 2" = #"Navigation 1"{[Name = "dbt_9_python", Kind = "Schema"]}[Data],\n'
            '#"Navigation 3" = #"Navigation 2"{[Name = "tbo_forecast_confidence_clusters", Kind = "Table"]}[Data]\n'
            'in #"Navigation 3"'
        )
        p, s, t, k = transform_fabric.parse_bigquery_source(m_code)
        self.assertEqual(p, "adroit-nectar-124618")
        self.assertEqual(s, "dbt_9_python")
        self.assertEqual(t, "tbo_forecast_confidence_clusters")
        self.assertEqual(k, "Table")

    def test_sharepoint_excel_does_not_leak_filename(self):
        # SharePoint+Excel M-code contains [Name="..."] but is NOT a BigQuery
        # source. Must not leak the Excel filename into bq_project.
        m_code = (
            'let\n'
            'SharePointSite = SharePoint.Files("https://example.sharepoint.com/", [ApiVersion=15]),\n'
            'File = SharePointSite{[Name="targets.xlsx"]}[Content],\n'
            'Source = Excel.Workbook(File, null, true)\n'
            'in Source'
        )
        result = transform_fabric.parse_bigquery_source(m_code)
        self.assertEqual(result, (None, None, None, None))


class TransformFabricCalculateDependenciesTests(TestCase):
    """Tests for calculate_dependencies."""

    def test_measure_references_column(self):
        measures = [{"Name": "Revenue", "Expression": "[Sales Amount]", "Type": "measure"}]
        deps = transform_fabric.calculate_dependencies(measures, [], [], [])
        self.assertTrue(any(
            d["Object"] == "Revenue" and d["ReferencedObject"] == "Sales Amount"
            and d["ReferencedObjectType"] == "PB_COLUMN"
            for d in deps
        ))

    def test_measure_references_another_measure(self):
        measures = [
            {"Name": "Profit", "Expression": "[Revenue] - [Cost]", "Type": "measure"},
            {"Name": "Revenue", "Expression": "SUM([Sales])", "Type": "measure"},
            {"Name": "Cost", "Expression": "SUM([Expenses])", "Type": "measure"},
        ]
        deps = transform_fabric.calculate_dependencies(measures, [], [], [])
        profit_deps = [d for d in deps if d["Object"] == "Profit"]
        ref_types = {d["ReferencedObjectType"] for d in profit_deps}
        self.assertIn("PB_MEASURE", ref_types)

    def test_no_duplicates(self):
        # [Sales] appears twice in the expression — should only produce one dep
        measures = [{"Name": "M", "Expression": "[Sales] + [Sales]", "Type": "measure"}]
        deps = transform_fabric.calculate_dependencies(measures, [], [], [])
        keys = [(d["Object"], d["ObjectType"], d["ReferencedObject"], d["ReferencedObjectType"]) for d in deps]
        self.assertEqual(len(keys), len(set(keys)))

    def test_calculated_column_deps(self):
        columns = [{"Name": "FullName", "Expression": "[FirstName] & [LastName]", "Type": "calculated"}]
        deps = transform_fabric.calculate_dependencies([], columns, [], [])
        self.assertTrue(any(d["Object"] == "FullName" for d in deps))

    def test_data_column_ignored(self):
        # Non-calculated columns have no DAX expression to parse
        columns = [{"Name": "City", "Expression": "", "Type": "data"}]
        deps = transform_fabric.calculate_dependencies([], columns, [], [])
        self.assertFalse(any(d["Object"] == "City" for d in deps))

    def test_bare_table_reference(self):
        measures = [{"Name": "Count", "Expression": "COUNTROWS(Sales)", "Type": "measure"}]
        deps = transform_fabric.calculate_dependencies(measures, [], ["Sales"], [])
        self.assertTrue(any(
            d["Object"] == "Count" and d["ReferencedObject"] == "Sales"
            and d["ReferencedObjectType"] == "PB_TABLE"
            for d in deps
        ))

    def test_empty_inputs_return_empty(self):
        deps = transform_fabric.calculate_dependencies([], [], [], [])
        self.assertEqual(deps, [])

    def test_consumer_table_propagated(self):
        # ObjectTable lets the graph builder pick the right dataset twin when
        # the same measure name exists in multiple datasets.
        measures = [{"Name": "M", "Table_Name": "T", "Expression": "[X]"}]
        deps = transform_fabric.calculate_dependencies(measures, [], [], [])
        self.assertTrue(all(d["ObjectTable"] == "T" for d in deps))

    def test_qualified_column_carries_referenced_table(self):
        # 'TableName'[Col] should record the producer's table so the column
        # hash is computed against the right (dataset, table, name) triple.
        # The unqualified [Col] regex pass also emits a row with ReferencedTable=None;
        # what matters is that at least one row carries the qualified table.
        measures = [{"Name": "M", "Table_Name": "T", "Expression": "'Other'[Col]"}]
        deps = transform_fabric.calculate_dependencies(measures, [], [], [])
        qualified = [d for d in deps
                     if d["ReferencedObjectType"] == "PB_COLUMN"
                     and d["ReferencedObject"] == "Col"
                     and d["ReferencedTable"] == "Other"]
        self.assertEqual(len(qualified), 1)


class TransformFabricParseTmdlTableTests(TestCase):
    """Tests for parse_tmdl_table — parses TMDL files into structured dicts."""

    def _write_tmdl(self, content):
        """Write content to a temp .tmdl file and return its path."""
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.tmdl', delete=False, encoding='utf-8')
        f.write(content)
        f.close()
        return f.name

    def test_parses_table_name(self):
        path = self._write_tmdl("table Sales\n  lineageTag: abc-123\n")
        result = transform_fabric.parse_tmdl_table(path)
        self.assertEqual(result["name"], "Sales")
        os.unlink(path)

    def test_parses_table_lineage_tag(self):
        path = self._write_tmdl("table Sales\n  lineageTag: abc-123\n")
        result = transform_fabric.parse_tmdl_table(path)
        self.assertEqual(result["lineage_tag"], "abc-123")
        os.unlink(path)

    def test_parses_measure(self):
        tmdl = (
            "table Sales\n"
            "  measure Revenue = SUM([Amount])\n"
            "    lineageTag: meas-001\n"
            "    description: Total revenue\n"
        )
        path = self._write_tmdl(tmdl)
        result = transform_fabric.parse_tmdl_table(path)
        self.assertEqual(len(result["measures"]), 1)
        m = result["measures"][0]
        self.assertEqual(m["name"], "Revenue")
        self.assertIn("SUM", m["expression"])
        os.unlink(path)

    def test_parses_calculated_column(self):
        tmdl = (
            "table Customers\n"
            "  column FullName = [First] & \" \" & [Last]\n"
            "    lineageTag: col-001\n"
        )
        path = self._write_tmdl(tmdl)
        result = transform_fabric.parse_tmdl_table(path)
        self.assertEqual(len(result["columns"]), 1)
        c = result["columns"][0]
        self.assertEqual(c["name"], "FullName")
        self.assertEqual(c["type"], "calculated")
        os.unlink(path)

    def test_parses_data_column(self):
        tmdl = (
            "table Orders\n"
            "  column OrderDate\n"
            "    dataType: dateTime\n"
            "    lineageTag: col-002\n"
        )
        path = self._write_tmdl(tmdl)
        result = transform_fabric.parse_tmdl_table(path)
        self.assertEqual(len(result["columns"]), 1)
        self.assertEqual(result["columns"][0]["type"], "data")
        os.unlink(path)

    def test_triple_slash_description(self):
        tmdl = (
            "table Sales\n"
            "  /// This is the revenue measure\n"
            "  measure Revenue = SUM([Amount])\n"
        )
        path = self._write_tmdl(tmdl)
        result = transform_fabric.parse_tmdl_table(path)
        self.assertIn("revenue measure", result["measures"][0]["description"])
        os.unlink(path)

    def test_unknown_table_name_for_empty_file(self):
        path = self._write_tmdl("")
        result = transform_fabric.parse_tmdl_table(path)
        self.assertEqual(result["name"], "Unknown")
        os.unlink(path)


class TransformFabricTopologicalPropagationTests(TestCase):
    """Tests for the topological propagation logic used in main() for usage stats."""

    def _run_topo(self, edges):
        """
        Run the topological propagation algorithm on a list of edge dicts.
        Returns node_stats defaultdict.
        """
        fwd = collections.defaultdict(list)
        id_to_name_type = {}
        for g in edges:
            fwd[g["source_id"]].append((g["target_id"], g["target_type"]))
            id_to_name_type[g["source_id"]] = (g["source"], g["source_type"])
            id_to_name_type[g["target_id"]] = (g["target"], g["target_type"])

        node_stats = collections.defaultdict(lambda: {
            "connected_reports": set(), "connected_report_pages": set(),
            "connected_visuals": set(), "connected_measures": set(),
            "connected_columns": set(), "connected_tables": set(),
            "downstream_report_ids": set(),
        })

        all_ids = set(id_to_name_type.keys())
        in_degree = {nid: 0 for nid in all_ids}
        for src_id, children in fwd.items():
            for tgt_id, _ in children:
                if tgt_id in in_degree:
                    in_degree[tgt_id] += 1

        topo_queue = collections.deque(nid for nid in all_ids if in_degree[nid] == 0)
        topo_order = []
        while topo_queue:
            nid = topo_queue.popleft()
            topo_order.append(nid)
            for child_id, _ in fwd.get(nid, []):
                if child_id in in_degree:
                    in_degree[child_id] -= 1
                    if in_degree[child_id] == 0:
                        topo_queue.append(child_id)

        for nid in reversed(topo_order):
            stats = node_stats[nid]
            for child_id, child_type in fwd.get(nid, []):
                if child_id == nid:
                    continue
                child_name = id_to_name_type.get(child_id, (child_id, child_type))[0]
                if child_type == "PB_REPORT":
                    stats["connected_reports"].add(child_name)
                    stats["downstream_report_ids"].add(child_id)
                elif child_type == "PB_PAGE":
                    stats["connected_report_pages"].add(child_name)
                elif child_type == "PB_VISUAL":
                    stats["connected_visuals"].add(child_name)
                elif child_type == "PB_MEASURE":
                    stats["connected_measures"].add(child_name)
                elif child_type == "PB_COLUMN":
                    stats["connected_columns"].add(child_name)
                elif child_type == "PB_TABLE":
                    stats["connected_tables"].add(child_name)
                child_stats = node_stats[child_id]
                for key in ("connected_reports", "downstream_report_ids",
                            "connected_report_pages", "connected_visuals",
                            "connected_measures", "connected_columns", "connected_tables"):
                    stats[key] |= child_stats[key]

        return node_stats

    def test_linear_chain_column_sees_all_downstream(self):
        """COLUMN -> VISUAL -> PAGE -> REPORT: column should see all three."""
        edges = [
            {"source_id": "COL::1", "source": "MyCol", "source_type": "PB_COLUMN",
             "target_id": "VIS::2", "target": "MyVis", "target_type": "PB_VISUAL"},
            {"source_id": "VIS::2", "source": "MyVis", "source_type": "PB_VISUAL",
             "target_id": "PAG::3", "target": "MyPage", "target_type": "PB_PAGE"},
            {"source_id": "PAG::3", "source": "MyPage", "source_type": "PB_PAGE",
             "target_id": "REP::4", "target": "MyReport", "target_type": "PB_REPORT"},
        ]
        stats = self._run_topo(edges)
        col = stats["COL::1"]
        self.assertIn("MyVis", col["connected_visuals"])
        self.assertIn("MyPage", col["connected_report_pages"])
        self.assertIn("MyReport", col["connected_reports"])
        self.assertIn("REP::4", col["downstream_report_ids"])

    def test_report_node_has_no_downstream(self):
        """A REPORT node at the end of the chain should have empty stats."""
        edges = [
            {"source_id": "PAG::3", "source": "MyPage", "source_type": "PB_PAGE",
             "target_id": "REP::4", "target": "MyReport", "target_type": "PB_REPORT"},
        ]
        stats = self._run_topo(edges)
        self.assertEqual(len(stats["REP::4"]["connected_reports"]), 0)
        self.assertEqual(len(stats["REP::4"]["downstream_report_ids"]), 0)

    def test_diamond_graph_no_duplicate_counting(self):
        """Two paths to the same REPORT should count it only once (set semantics)."""
        edges = [
            {"source_id": "COL::A", "source": "ColA", "source_type": "PB_COLUMN",
             "target_id": "VIS::B", "target": "VisB", "target_type": "PB_VISUAL"},
            {"source_id": "COL::A", "source": "ColA", "source_type": "PB_COLUMN",
             "target_id": "VIS::C", "target": "VisC", "target_type": "PB_VISUAL"},
            {"source_id": "VIS::B", "source": "VisB", "source_type": "PB_VISUAL",
             "target_id": "REP::D", "target": "RepD", "target_type": "PB_REPORT"},
            {"source_id": "VIS::C", "source": "VisC", "source_type": "PB_VISUAL",
             "target_id": "REP::D", "target": "RepD", "target_type": "PB_REPORT"},
        ]
        stats = self._run_topo(edges)
        col_a = stats["COL::A"]
        self.assertEqual(col_a["connected_visuals"], {"VisB", "VisC"})
        self.assertEqual(col_a["connected_reports"], {"RepD"})
        self.assertEqual(col_a["downstream_report_ids"], {"REP::D"})

    def test_isolated_node_has_empty_stats(self):
        """A node with no edges should have all-empty stat sets."""
        edges = [
            {"source_id": "COL::X", "source": "Orphan", "source_type": "PB_COLUMN",
             "target_id": "VIS::Y", "target": "SomeVis", "target_type": "PB_VISUAL"},
        ]
        stats = self._run_topo(edges)
        # VIS::Y has no forward edges — its stats should be empty
        vis_stats = stats["VIS::Y"]
        self.assertEqual(len(vis_stats["connected_reports"]), 0)
        self.assertEqual(len(vis_stats["connected_visuals"]), 0)

    def test_measure_dependency_chain(self):
        """MEASURE A -> MEASURE B: A should see B in connected_measures."""
        edges = [
            {"source_id": "MEAS::A", "source": "BaseM", "source_type": "PB_MEASURE",
             "target_id": "MEAS::B", "target": "DerivedM", "target_type": "PB_MEASURE"},
        ]
        stats = self._run_topo(edges)
        self.assertIn("DerivedM", stats["MEAS::A"]["connected_measures"])

    def test_table_to_column_edge(self):
        """TABLE -> COLUMN: table should see column in connected_columns."""
        edges = [
            {"source_id": "TBL::1", "source": "Sales", "source_type": "PB_TABLE",
             "target_id": "COL::2", "target": "Amount", "target_type": "PB_COLUMN"},
        ]
        stats = self._run_topo(edges)
        self.assertIn("Amount", stats["TBL::1"]["connected_columns"])


# =============================================
# Power BI Usage extraction (per ws × report × user × month)
# =============================================
from datetime import date
from sources.fabric import extract_usage


class ExtractUsageHelperTests(TestCase):
    """Pure-function helpers in extract_usage.py — no network involved."""

    def test_normalize_guid_strips_braces_and_uppercases(self):
        self.assertEqual(extract_usage._normalize_guid('{abc-123}'), 'ABC-123')
        self.assertEqual(extract_usage._normalize_guid('  abc-123  '), 'ABC-123')
        self.assertEqual(extract_usage._normalize_guid(None), '')
        self.assertEqual(extract_usage._normalize_guid(''), '')

    def test_first_of_month_n_months_ago_returns_first_of_month(self):
        # 0 months ago = current month's first day
        d = extract_usage._first_of_month_n_months_ago(0)
        self.assertEqual(d.day, 1)

    def test_first_of_month_handles_year_boundary(self):
        # Patch datetime.now to a fixed point so the test is deterministic.
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone
        fixed = datetime(2026, 2, 15, tzinfo=timezone.utc)
        # Replace the imported `datetime` symbol in extract_usage with a Mock
        # whose .now(...) returns our fixed datetime — its real .date() then runs.
        mock_datetime = MagicMock()
        mock_datetime.now.return_value = fixed
        with patch.object(extract_usage, 'datetime', mock_datetime):
            # 4 months before Feb 2026 = Oct 2025
            d = extract_usage._first_of_month_n_months_ago(4)
        self.assertEqual(d, date(2025, 10, 1))

    def test_build_views_dax_targets_modern_report_views(self):
        dax = extract_usage._build_views_dax(date(2026, 3, 1))
        self.assertIn("DATE(2026,3,1)", dax)
        # Modern schema: row-grain 'Report views' counted with COUNTROWS.
        self.assertIn("'Report views'[Date]", dax)
        self.assertIn("COUNTROWS('Report views')", dax)

    def test_build_legacy_views_dax_targets_views_table(self):
        dax = extract_usage._build_legacy_views_dax(date(2026, 3, 1))
        self.assertIn("DATE(2026,3,1)", dax)
        self.assertIn("'Views'[Date]", dax)
        # Legacy schema: pre-aggregated GranularViewsCount.
        self.assertIn("GranularViewsCount", dax)


class RunUsageExtractionIntegrationTests(TestCase):
    """End-to-end test of run_usage_extraction with HTTP mocked.

    Two workspaces — one has the usage dataset (3 view rows aggregating to 2
    monthly buckets, plus dropping a zero-view row), the other doesn't.
    Asserts the CSV is written with the right rows + columns.
    """

    @responses.activate
    def test_falls_back_to_legacy_schema(self):
        """Modern 'Report views' query 400s (un-migrated dataset) → the
        extractor falls back to the legacy Views/Reports/Users queries and
        still produces UPN/display-name rows."""
        # Workspace listing (populates workspace_name_by_id)
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups',
            json={'value': [
                {'id': 'ws_with', 'name': 'Has Usage'},
                {'id': 'ws_without', 'name': 'No Usage'},
            ]},
            status=200,
        )
        # ws_with: list datasets — has 'Report Usage Metrics Model'
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups/ws_with/datasets',
            json={'value': [
                {'id': 'ds_other', 'name': 'Some Other Model'},
                {'id': 'ds_usage', 'name': 'Report Usage Metrics Model'},
            ]},
            status=200,
        )
        # ws_without: no usage dataset
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups/ws_without/datasets',
            json={'value': [{'id': 'ds_x', 'name': 'Other'}]},
            status=200,
        )

        # executeQueries calls for ws_with, returned in FIFO order:
        #   1) modern 'Report views' query → 400 (dataset on legacy schema)
        #   2-4) legacy Views, Reports, Users → 200
        views_payload = {'results': [{'tables': [{'rows': [
            # Two May rows (alice + bob, same report) → aggregate to 8 views
            {'Views[ReportGuid]': '{rpt-A}', 'Views[UserGuid]': 'u-alice',
             'Views[Date]': '2026-05-01', 'Views[Platform]': 'Web',
             'Views[DistributionMethod]': 'Workspace', 'Views[ReportPage]': 'Page 1',
             '[Views]': 5},
            {'Views[ReportGuid]': '{RPT-A}', 'Views[UserGuid]': '{u-bob}',
             'Views[Date]': '2026-05-15', 'Views[Platform]': 'Web',
             'Views[DistributionMethod]': 'Workspace', 'Views[ReportPage]': 'Page 1',
             '[Views]': 3},
            # Zero-view row should be skipped
            {'Views[ReportGuid]': 'rpt-A', 'Views[UserGuid]': 'u-alice',
             'Views[Date]': '2026-05-20', 'Views[Platform]': 'Web',
             'Views[DistributionMethod]': 'Workspace', 'Views[ReportPage]': 'Page 1',
             '[Views]': 0},
        ]}]}]}
        reports_payload = {'results': [{'tables': [{'rows': [
            {'[ReportGuid]': '{rpt-A}', '[DisplayName]': 'Report A'},
        ]}]}]}
        users_payload = {'results': [{'tables': [{'rows': [
            {'[UserGuid]': 'U-ALICE', '[UserPrincipalName]': 'alice@example.com',
             '[GivenName]': 'Alice', '[FamilyName]': 'Anderson'},
            {'[UserGuid]': 'u-bob', '[UserPrincipalName]': 'bob@example.com',
             '[GivenName]': 'Bob', '[FamilyName]': ''},
        ]}]}]}

        # 1) modern 'Report views' query fails → triggers legacy fallback.
        responses.add(
            responses.POST,
            'https://api.powerbi.com/v1.0/myorg/groups/ws_with/datasets/ds_usage/executeQueries',
            json={'error': {'code': 'DatasetExecuteQueriesError'}},
            status=400,
        )
        # 2-4) legacy Views, Reports, Users succeed.
        for payload in (views_payload, reports_payload, users_payload):
            responses.add(
                responses.POST,
                'https://api.powerbi.com/v1.0/myorg/groups/ws_with/datasets/ds_usage/executeQueries',
                json=payload,
                status=200,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            logs = []
            out_path = extract_usage.run_usage_extraction(
                token='fake-token',
                workspace_ids=['ws_with', 'ws_without'],
                etl_dir=tmpdir,
                log=logs.append,
                months=3,
            )
            self.assertTrue(os.path.exists(out_path))
            self.assertTrue(out_path.endswith('fabric_info_usage.csv'))

            import csv as _csv
            with open(out_path, newline='', encoding='utf-8') as fh:
                rows = list(_csv.DictReader(fh))

            self.assertEqual(len(rows), 2, f'Expected 2 user-grain rows, got {rows}')
            by_user = {r['user_email']: r for r in rows}
            self.assertEqual(by_user['alice@example.com']['view_count'], '5')
            self.assertEqual(by_user['alice@example.com']['report_name'], 'Report A')
            self.assertEqual(by_user['alice@example.com']['user_display_name'], 'Alice Anderson')
            self.assertEqual(by_user['alice@example.com']['workspace_name'], 'Has Usage')
            self.assertEqual(by_user['alice@example.com']['month'], '2026-05-01')
            self.assertEqual(by_user['bob@example.com']['view_count'], '3')

        # Skip-log line for ws_without should be present
        self.assertTrue(any('No Usage' in l and 'skip' in l for l in logs),
                        f'No skip log for ws_without: {logs}')
        # The [ok] line should mark the workspace as resolved via the legacy path
        self.assertTrue(any('[ok]' in l and 'legacy' in l for l in logs),
                        f'No legacy-schema ok log: {logs}')

    @responses.activate
    def test_modern_schema_writes_csv(self):
        """Modern 'Report views' schema: row-grain counted via COUNTROWS, user
        identified by UserGuid (UPN/name no longer exposed)."""
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups',
            json={'value': [{'id': 'ws_with', 'name': 'Has Usage'}]},
            status=200,
        )
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups/ws_with/datasets',
            json={'value': [{'id': 'ds_usage', 'name': 'Usage Metrics Report'}]},
            status=200,
        )

        # executeQueries: 1) modern 'Report views', 2) modern Users lookup.
        views_payload = {'results': [{'tables': [{'rows': [
            {'Report views[ReportId]': 'rpt-A', 'Report views[ReportName]': 'Report A',
             'Report views[UserKey]': 11, 'Report views[Date]': '2026-05-01T00:00:00',
             'Report views[ConsumptionMethod]': 'PowerBI Web',
             'Report views[DistributionMethod]': 'Workspace', '[Views]': 5},
            {'Report views[ReportId]': 'rpt-A', 'Report views[ReportName]': 'Report A',
             'Report views[UserKey]': 22, 'Report views[Date]': '2026-05-15T00:00:00',
             'Report views[ConsumptionMethod]': 'PowerBI Web',
             'Report views[DistributionMethod]': 'Workspace', '[Views]': 3},
            # Zero-view row is skipped
            {'Report views[ReportId]': 'rpt-A', 'Report views[ReportName]': 'Report A',
             'Report views[UserKey]': 11, 'Report views[Date]': '2026-05-20T00:00:00',
             'Report views[ConsumptionMethod]': 'PowerBI Web',
             'Report views[DistributionMethod]': 'Workspace', '[Views]': 0},
        ]}]}]}
        users_payload = {'results': [{'tables': [{'rows': [
            {'[UserKey]': 11, '[UserGuid]': '{u-alice}'},
            {'[UserKey]': 22, '[UserGuid]': 'u-bob'},
        ]}]}]}
        for payload in (views_payload, users_payload):
            responses.add(
                responses.POST,
                'https://api.powerbi.com/v1.0/myorg/groups/ws_with/datasets/ds_usage/executeQueries',
                json=payload,
                status=200,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            logs = []
            out_path = extract_usage.run_usage_extraction(
                token='fake-token', workspace_ids=['ws_with'],
                etl_dir=tmpdir, log=logs.append, months=3,
            )
            import csv as _csv
            with open(out_path, newline='', encoding='utf-8') as fh:
                rows = list(_csv.DictReader(fh))

        self.assertEqual(len(rows), 2, f'Expected 2 user-grain rows, got {rows}')
        by_user = {r['user_email']: r for r in rows}
        # UserGuid (normalised) becomes the user identifier; no email/display name.
        self.assertEqual(by_user['U-ALICE']['view_count'], '5')
        self.assertEqual(by_user['U-ALICE']['report_name'], 'Report A')
        self.assertEqual(by_user['U-ALICE']['user_display_name'], '')
        self.assertEqual(by_user['U-ALICE']['platform'], 'PowerBI Web')
        self.assertEqual(by_user['U-ALICE']['month'], '2026-05-01')
        self.assertEqual(by_user['U-BOB']['view_count'], '3')
        self.assertTrue(any('[ok]' in l and 'modern' in l for l in logs),
                        f'No modern-schema ok log: {logs}')

    @responses.activate
    def test_empty_modern_dataset_falls_through_to_legacy_dataset(self):
        """A workspace carrying BOTH usage datasets — an empty modern
        'Usage Metrics Report' and a populated legacy 'Report Usage Metrics
        Model' — must not stop at the empty modern one. The extractor should
        fall through to the legacy dataset and still produce view rows.

        This is the regression for workspaces that reported
        '0 grain rows, 0 views (modern)' while their legacy dataset held the
        actual usage (Commercial / Finance / Drivers' Operations)."""
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups',
            json={'value': [{'id': 'ws_both', 'name': 'Has Both'}]},
            status=200,
        )
        # Workspace has the new (empty) dataset AND the legacy (populated) one.
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups/ws_both/datasets',
            json={'value': [
                {'id': 'ds_new', 'name': 'Usage Metrics Report'},
                {'id': 'ds_legacy', 'name': 'Report Usage Metrics Model'},
            ]},
            status=200,
        )

        # ds_new: modern 'Report views' query succeeds but returns NO rows, and
        # the best-effort Users lookup is also empty.
        empty_payload = {'results': [{'tables': [{'rows': []}]}]}
        for _ in range(2):  # views query, then modern Users lookup
            responses.add(
                responses.POST,
                'https://api.powerbi.com/v1.0/myorg/groups/ws_both/datasets/ds_new/executeQueries',
                json=empty_payload, status=200,
            )

        # ds_legacy: modern query 400s (no 'Report views'), then legacy
        # Views/Reports/Users succeed with real data.
        responses.add(
            responses.POST,
            'https://api.powerbi.com/v1.0/myorg/groups/ws_both/datasets/ds_legacy/executeQueries',
            json={'error': {'code': 'DatasetExecuteQueriesError'}}, status=400,
        )
        views_payload = {'results': [{'tables': [{'rows': [
            {'Views[ReportGuid]': '{rpt-A}', 'Views[UserGuid]': 'u-alice',
             'Views[Date]': '2026-05-01', 'Views[Platform]': 'Web',
             'Views[DistributionMethod]': 'Workspace', 'Views[ReportPage]': 'Page 1',
             '[Views]': 7},
        ]}]}]}
        reports_payload = {'results': [{'tables': [{'rows': [
            {'[ReportGuid]': '{rpt-A}', '[DisplayName]': 'Report A'},
        ]}]}]}
        users_payload = {'results': [{'tables': [{'rows': [
            {'[UserGuid]': 'u-alice', '[UserPrincipalName]': 'alice@example.com',
             '[GivenName]': 'Alice', '[FamilyName]': 'Anderson'},
        ]}]}]}
        for payload in (views_payload, reports_payload, users_payload):
            responses.add(
                responses.POST,
                'https://api.powerbi.com/v1.0/myorg/groups/ws_both/datasets/ds_legacy/executeQueries',
                json=payload, status=200,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            logs = []
            out_path = extract_usage.run_usage_extraction(
                token='fake-token', workspace_ids=['ws_both'],
                etl_dir=tmpdir, log=logs.append, months=3,
            )
            import csv as _csv
            with open(out_path, newline='', encoding='utf-8') as fh:
                rows = list(_csv.DictReader(fh))

        # The legacy dataset's single view row must come through — NOT zeroed
        # out by the empty modern dataset that was tried first.
        self.assertEqual(len(rows), 1, f'Expected the legacy row, got {rows}')
        self.assertEqual(rows[0]['user_email'], 'alice@example.com')
        self.assertEqual(rows[0]['view_count'], '7')
        self.assertEqual(rows[0]['report_name'], 'Report A')
        self.assertTrue(any('[ok]' in l and 'legacy' in l for l in logs),
                        f'No legacy-schema ok log: {logs}')

    @responses.activate
    def test_writes_header_only_csv_when_no_data(self):
        """If no workspace has the usage dataset, CSV is still written (header only)."""
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups',
            json={'value': []}, status=200,
        )
        responses.add(
            responses.GET,
            'https://api.powerbi.com/v1.0/myorg/groups/ws_x/datasets',
            json={'value': []}, status=200,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = extract_usage.run_usage_extraction(
                token='t', workspace_ids=['ws_x'], etl_dir=tmpdir,
                log=lambda _: None, months=1,
            )
            with open(out_path, encoding='utf-8') as fh:
                content = fh.read()
            self.assertIn('month,workspace_id', content)
            # Just the header line
            self.assertEqual(content.strip().count('\n'), 0)


# ──────────────────────────────────────────────────────────────────────────────
# PBIR (new Fabric report format) parser tests
# ──────────────────────────────────────────────────────────────────────────────
#
# Newer Fabric reports ship as a folder tree (definition/pages/<id>/page.json
# + visuals/<id>/visual.json) instead of a single root report.json. We added
# parse_report_layout_pbir to handle that, and the workspace-loop dispatch in
# process_fabric_repo falls back to it when the legacy file is absent.
#
# These tests use trimmed copies of real exports under tests/fixtures/fabric_pbir/
# so we exercise the actual JSON shapes Fabric produces — not synthetic data
# that could drift from reality.

FIXTURE_BASE = os.path.join(
    os.path.dirname(__file__), 'fixtures', 'fabric_pbir',
    'e47f9550-e39b-4241-b292-90b5a34c6b17', 'Reports',
)


import unittest


class ParseReportLayoutPbirTests(unittest.TestCase):
    """parse_report_layout_pbir against real PBIR fixtures.

    Inherits from unittest.TestCase (not django.test.TestCase) because the
    parser is pure I/O against on-disk JSON — no Django ORM involved. Avoids
    needlessly spinning up the test DB.
    """

    def test_pbir_minimal_single_page_single_visual(self):
        # Zoe is the smallest real PBIR report we have: one page, one visual.
        # Guards the loop boundaries (entering pages_dir, entering visuals dir).
        rd = os.path.join(FIXTURE_BASE, 'Zoe')
        usage, stats = transform_fabric.parse_report_layout_pbir(
            rd, 'WS', 'TestWS', 'Zoe',
        )
        self.assertEqual(stats['report_name'], 'Zoe')
        self.assertEqual(stats['total_pages'], 1)
        self.assertEqual(stats['total_visuals'], 1)
        # Every usage row carries the report-level identifiers.
        self.assertTrue(all(u['report_name'] == 'Zoe' for u in usage))
        self.assertTrue(all(u['workspace_id'] == 'WS' for u in usage))

    def test_pbir_multi_page_multi_visual(self):
        # CRM is a 10-page report (trimmed to 3 visuals/page in the fixture).
        rd = os.path.join(FIXTURE_BASE, 'CRM')
        usage, stats = transform_fabric.parse_report_layout_pbir(
            rd, 'WS', 'TestWS', 'CRM',
        )
        self.assertEqual(stats['total_pages'], 10)
        self.assertEqual(stats['total_visuals'], 30)
        # Real-world coverage check: a multi-page report should produce
        # distinct page_name values.
        pages = {u['page_name'] for u in usage}
        self.assertEqual(len(pages), 10)

    def test_pbir_extracts_measure_field_names(self):
        # The whole point of the parser is field-level lineage. Confirm the
        # regex extractor finds at least *some* measure/column names from the
        # PBIR visual.json shape (Property/Name/queryRef patterns).
        rd = os.path.join(FIXTURE_BASE, 'CRM')
        usage, _ = transform_fabric.parse_report_layout_pbir(
            rd, 'WS', 'TestWS', 'CRM',
        )
        fields = {u['field_name'] for u in usage if u['field_name']}
        self.assertGreater(len(fields), 0,
            'PBIR parser produced zero field names — regex patterns may have drifted')

    def test_pbir_visual_id_includes_title_when_present(self):
        # Visual id in usage rows is "<title> (<guid>)" so users can recognise
        # the visual without cross-referencing the export. Confirm at least
        # one row in CRM carries a recognisable title-bearing id (i.e. not
        # just a bare guid).
        rd = os.path.join(FIXTURE_BASE, 'CRM')
        usage, _ = transform_fabric.parse_report_layout_pbir(
            rd, 'WS', 'TestWS', 'CRM',
        )
        # Every visual_id we emit ends with " (<id>)" — the title prefix may
        # be "Unknown" if a visual has no title, but the suffix is invariant.
        self.assertTrue(all(u['visual_id'].endswith(')') for u in usage))

    def test_pbir_returns_empty_when_pages_dir_missing(self):
        # Defensive case: a PBIR-shaped folder with no definition/pages/
        # should return an empty parse rather than raise.
        with tempfile.TemporaryDirectory() as tmp:
            usage, stats = transform_fabric.parse_report_layout_pbir(
                tmp, 'WS', 'TestWS', 'Empty',
            )
            self.assertEqual(usage, [])
            self.assertEqual(stats['total_pages'], 0)
            self.assertEqual(stats['total_visuals'], 0)


class WorkspaceReportDispatchTests(unittest.TestCase):
    """Mimic the per-report dispatch block inside process_fabric_repo and
    confirm legacy + PBIR reports both produce Reports_Stats rows from the
    same workspace folder. Regression guard against a future change that
    accidentally shadows the legacy path with the PBIR fallback (or vice
    versa)."""

    def _dispatch(self, reports_dir, ws_id='WS', ws_name='TestWS'):
        """Re-implement just the file-detection branch so we can assert on
        the per-report outcomes without spinning up the whole transform
        pipeline (which expects a full workspace tree, semantic models, etc.).
        Mirror the real loop in transform_fabric.process_fabric_repo."""
        kinds = {}
        stats_rows = []
        for item_name in os.listdir(reports_dir):
            item_path = os.path.join(reports_dir, item_name)
            report_json = os.path.join(item_path, 'report.json')
            pbir_report_json = os.path.join(item_path, 'definition', 'report.json')
            report_data = None
            stats = None
            if os.path.exists(report_json):
                report_data, stats = transform_fabric.parse_report_layout(
                    report_json, ws_id, ws_name, item_name,
                )
                kinds[item_name] = 'legacy'
            elif os.path.exists(pbir_report_json):
                report_data, stats = transform_fabric.parse_report_layout_pbir(
                    item_path, ws_id, ws_name, item_name,
                )
                kinds[item_name] = 'pbir'
            if stats is not None:
                stats_rows.append(stats)
        return kinds, stats_rows

    def test_legacy_and_pbir_reports_both_picked_up(self):
        kinds, stats_rows = self._dispatch(FIXTURE_BASE)
        # All 4 fixture reports must yield a stats row.
        self.assertEqual(len(stats_rows), 4)
        # COMMERCIAL + SEO_reporting are legacy; CRM + Zoe are PBIR.
        self.assertEqual(kinds.get('COMMERCIAL'), 'legacy')
        self.assertEqual(kinds.get('SEO_reporting'), 'legacy')
        self.assertEqual(kinds.get('CRM'), 'pbir')
        self.assertEqual(kinds.get('Zoe'), 'pbir')

    def test_legacy_takes_precedence_over_pbir_when_both_exist(self):
        # Belt-and-braces: if a report folder somehow has *both* a root
        # report.json and a definition/report.json, the legacy parser must
        # win (it's the if-branch). This protects backwards-compat for any
        # hand-mixed folders.
        with tempfile.TemporaryDirectory() as tmp:
            rd = os.path.join(tmp, 'Hybrid')
            os.makedirs(os.path.join(rd, 'definition', 'pages', 'p1', 'visuals', 'v1'))
            # Legacy report.json: minimal but with one section
            with open(os.path.join(rd, 'report.json'), 'w', encoding='utf-8') as f:
                json.dump({'sections': [
                    {'displayName': 'LegacyPage', 'visualContainers': [
                        {'config': '{"name": "v1", "singleVisual": {}}'},
                    ]},
                ]}, f)
            # PBIR sidecar that shouldn't be touched
            with open(os.path.join(rd, 'definition', 'report.json'), 'w', encoding='utf-8') as f:
                json.dump({}, f)
            with open(os.path.join(rd, 'definition', 'pages', 'p1', 'page.json'), 'w', encoding='utf-8') as f:
                json.dump({'displayName': 'PbirPage'}, f)
            with open(os.path.join(rd, 'definition', 'pages', 'p1', 'visuals', 'v1', 'visual.json'), 'w', encoding='utf-8') as f:
                json.dump({'name': 'v1'}, f)

            kinds, stats_rows = self._dispatch(tmp)
            self.assertEqual(kinds, {'Hybrid': 'legacy'})
            self.assertEqual(stats_rows[0]['total_pages'], 1)
            # Legacy parser counted 1 visual (the visualContainer);
            # PBIR sidecar was ignored.
            self.assertEqual(stats_rows[0]['total_visuals'], 1)
