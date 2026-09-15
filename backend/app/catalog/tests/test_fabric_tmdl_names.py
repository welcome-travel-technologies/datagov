"""Fabric quoted-name regressions; runnable with unittest without a database."""

import tempfile
import unittest
from pathlib import Path

from etl.sources.fabric import transform_fabric


class FabricTmdlNameTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def parse_table(self, content):
        path = self.root / "table.tmdl"
        path.write_text(content, encoding="utf-8")
        return transform_fabric.parse_tmdl_table(path)

    def test_apostrophe_measures_keep_their_own_expressions_and_metadata(self):
        result = self.parse_table(
            "table Metrics\n"
            "  measure Existing = 1\n"
            "    lineageTag: existing-tag\n"
            "  /// Visits from partners\n"
            "  measure 'Partner''s Landing Page Visits' = SUM([Visits])\n"
            "    lineageTag: visits-tag\n"
            "    formatString: #,0\n"
            "  measure 'Partner''s Landing page Visits YoY' =\n"
            "    DIVIDE([Partner's Landing Page Visits], [Previous Visits])\n"
            "    lineageTag: yoy-tag\n"
            "  measure Following = 2\n"
        )
        measures = result["measures"]
        self.assertEqual(
            [m["name"] for m in measures],
            ["Existing", "Partner's Landing Page Visits",
             "Partner's Landing page Visits YoY", "Following"],
        )
        self.assertEqual(measures[0]["expression"], "1")
        self.assertEqual(measures[0]["lineage_tag"], "existing-tag")
        self.assertEqual(measures[1]["expression"], "SUM([Visits])")
        self.assertEqual(measures[1]["description"], "Visits from partners")
        self.assertEqual(measures[1]["lineage_tag"], "visits-tag")
        self.assertEqual(measures[1]["formatString"], "#,0")
        self.assertEqual(
            measures[2]["expression"],
            "DIVIDE([Partner's Landing Page Visits], [Previous Visits])",
        )
        self.assertEqual(measures[2]["lineage_tag"], "yoy-tag")
        self.assertEqual(measures[3]["expression"], "2")

    def test_apostrophes_in_table_column_and_partition_headers(self):
        result = self.parse_table(
            "table 'Partner''s Metrics'\n"
            "  lineageTag: table-tag\n"
            "  column 'Partner''s Visits'\n"
            "    dataType: int64\n"
            "    lineageTag: column-tag\n"
            "  partition 'Partner''s Import' = m\n"
            "    source =\n"
            "      let Source = 1 in Source\n"
        )
        self.assertEqual(result["name"], "Partner's Metrics")
        self.assertEqual(result["lineage_tag"], "table-tag")
        self.assertEqual(result["columns"][0]["name"], "Partner's Visits")
        self.assertEqual(result["columns"][0]["lineage_tag"], "column-tag")
        self.assertEqual(result["columns"][0]["type"], "data")
        self.assertEqual(result["columns"][0]["dataType"], "int64")
        self.assertEqual(len(result["partitions"]), 1)
        self.assertEqual(result["partitions"][0]["mode"], "m")
        self.assertIn("let Source", result["partitions"][0]["source"]["expression"])

    def test_quoted_equals_does_not_start_the_expression(self):
        result = self.parse_table(
            "table Metrics\n"
            "  measure 'Partner''s Visits = Target' = IF([Visits] = 10, 1, 0)\n"
        )
        self.assertEqual(result["measures"][0]["name"], "Partner's Visits = Target")
        self.assertEqual(
            result["measures"][0]["expression"], "IF([Visits] = 10, 1, 0)",
        )

    def test_preserves_literal_apostrophes_at_name_boundaries(self):
        for name in ["'Partner's'", "Partners'", "Partner''s", " Partner's Visits "]:
            with self.subTest(name=name):
                quoted = "'" + name.replace("'", "''") + "'"
                result = self.parse_table(
                    "table Metrics\n" + "  measure " + quoted + " = 1\n",
                )
                self.assertEqual(result["measures"][0]["name"], name)

    def test_relationship_names_decode_apostrophes_and_quoted_dots(self):
        path = self.root / "relationships.tmdl"
        path.write_text(
            "relationship partners\n"
            "  fromColumn: 'Partner''s.Visits'.'Partner''s ID'\n"
            "  toColumn: 'Partner''s Directory'.'Owner''s ID'\n"
            "relationship ordinary\n"
            "  fromColumn: Sales.CustomerId\n"
            "  toColumn: Customers.Id\n",
            encoding="utf-8",
        )
        relationships = transform_fabric.parse_tmdl_relationships(path)
        self.assertEqual(len(relationships), 2)
        self.assertEqual(relationships[0]["from_table"], "Partner's.Visits")
        self.assertEqual(relationships[0]["from_column"], "Partner's ID")
        self.assertEqual(relationships[0]["to_table"], "Partner's Directory")
        self.assertEqual(relationships[0]["to_column"], "Owner's ID")
        self.assertEqual(relationships[1]["from_table"], "Sales")
        self.assertEqual(relationships[1]["to_column"], "Id")

    def test_dax_dependencies_use_decoded_table_and_measure_names(self):
        measures = [
            {"Name": "Partner's Visits", "Table_Name": "Metrics",
             "Expression": "SUM('Partner''s Sales'[Owner's Visits])"},
            {"Name": "Growth", "Table_Name": "Metrics",
             "Expression": "[Partner's Visits] / 10"},
        ]
        dependencies = transform_fabric.calculate_dependencies(measures, [], [], [])
        self.assertTrue(any(
            d["Object"] == "Partner's Visits"
            and d["ReferencedObject"] == "Owner's Visits"
            and d["ReferencedTable"] == "Partner's Sales"
            for d in dependencies
        ))
        table_dependencies = [
            d["ReferencedObject"] for d in dependencies
            if d["ReferencedObjectType"] == "PB_TABLE"
        ]
        self.assertEqual(table_dependencies, ["Partner's Sales"])
        self.assertTrue(any(
            d["Object"] == "Growth"
            and d["ReferencedObject"] == "Partner's Visits"
            and d["ReferencedObjectType"] == "PB_MEASURE"
            for d in dependencies
        ))

    def test_plain_and_quoted_names_without_apostrophes_still_parse(self):
        result = self.parse_table(
            "table 'Sales Metrics'\n"
            "  column Amount\n"
            "    dataType: int64\n"
            "  measure Revenue = SUM([Amount])\n"
            "  measure 'Total Visits' = SUM([Visits])\n"
        )
        self.assertEqual(result["name"], "Sales Metrics")
        self.assertEqual(result["columns"][0]["name"], "Amount")
        self.assertEqual(
            [m["name"] for m in result["measures"]], ["Revenue", "Total Visits"],
        )


if __name__ == "__main__":
    unittest.main()
