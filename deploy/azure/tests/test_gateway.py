"""nginx.conf keeps the private machine routes off the public edge in every spelling.

Static checks of nginx's location rules, no Docker needed. deploy/azure/check_gateway.py
probes a running nginx with the same paths (CI runs it).
"""
import importlib.util
from pathlib import Path
import re
import sys
import unittest

FOLDER = Path(__file__).resolve().parents[1]
CONF = (FOLDER.parents[1] / "nginx.conf").read_text()
LOCATION = re.compile(r"^\s*location\s+(=|\^~|~\*|~)?\s*(\S+)\s*\{(.*?)^\s*\}", re.M | re.S)


def module(name):
    sys.path.insert(0, str(FOLDER))  # check_gateway imports deploy as its own script would
    try:
        spec = importlib.util.spec_from_file_location(name, FOLDER / (name + ".py"))
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result
    finally:
        sys.path.remove(str(FOLDER))


gateway = module("check_gateway")
LOCATIONS = [(m.group(1) or "", m.group(2), m.group(3)) for m in LOCATION.finditer(CONF)]
REGEXES = [entry for entry in LOCATIONS if entry[0] in {"~", "~*"}]
# The probe paths as nginx matches them: %-escapes and dot segments resolved by nginx first.
NORMALIZED = [p.split("?")[0] for p in gateway.BLOCKED if "%" not in p and "/." not in p]


class GatewayConfigTests(unittest.TestCase):
    def test_private_routes_answer_404_from_the_first_regex_case_insensitively(self):
        modifier, pattern, body = REGEXES[0]
        self.assertEqual(modifier, "~*")
        self.assertEqual(body.split(), ["return", "404;"])
        compiled = re.compile(pattern, re.IGNORECASE)
        for path in NORMALIZED:
            with self.subTest(path=path):
                self.assertTrue(compiled.search(path))
        for path in gateway.TO_WEB + gateway.TO_FRONTEND:
            with self.subTest(path=path):
                self.assertFalse(compiled.search(path))

    def test_no_prefix_or_exact_location_bypasses_the_regex(self):
        # nginx checks regex locations only when the longest matching prefix lacks ^~, and an
        # exact (=) match ends the search: neither may cover any spelling of a private route.
        for path in NORMALIZED + [p.lower() for p in NORMALIZED]:
            for modifier, location, _ in LOCATIONS:
                with self.subTest(path=path, location=location):
                    self.assertFalse(modifier == "^~" and path.startswith(location))
                    self.assertFalse(modifier == "=" and path == location)

    def test_release_probes_are_part_of_the_gateway_check(self):
        # deploy.py's release probes (INTERNAL_PROBES), in three spellings per private prefix.
        self.assertTrue(set(gateway.INTERNAL_PROBES) <= set(gateway.BLOCKED))
        for prefix in ("/api/knowledge/internal/", "/api/document-analyzer/internal/"):
            spellings = {p[:len(prefix)] for p in gateway.INTERNAL_PROBES if p.lower().startswith(prefix)}
            self.assertGreaterEqual(len(spellings), 3, prefix)


if __name__ == "__main__":
    unittest.main()
