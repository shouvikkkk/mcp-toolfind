import unittest

from mcp_toolfind.checks import (check_boilerplate, check_sample_output,
                                 check_truncation_loss, find_duplicates)


def tool(name, description=""):
    return {"name": name, "description": description, "inputSchema": {}}


class TestTruncationLoss(unittest.TestCase):
    def test_keyword_only_after_120_flagged(self):
        desc = ("x " * 70) + "requires approval."
        self.assertEqual(check_truncation_loss(tool("t", desc)), ["requires"])

    def test_keyword_in_head_not_flagged(self):
        desc = "This requires approval. " + ("y " * 70)
        self.assertEqual(check_truncation_loss(tool("t", desc)), [])

    def test_short_description_clean(self):
        self.assertEqual(check_truncation_loss(tool("t", "Send an invoice.")),
                         [])


class TestBoilerplateAndDuplicates(unittest.TestCase):
    def test_boilerplate_prefixes(self):
        self.assertTrue(check_boilerplate(
            tool("t", "This tool allows you to check the weather.")))
        self.assertTrue(check_boilerplate(
            tool("t", "This tool provides functionality that allows sending.")))
        self.assertFalse(check_boilerplate(tool("t", "Send an invoice.")))

    def test_duplicate_names_flagged(self):
        tools = [tool("user_lookup"), tool("user-lookup"), tool("weather_get")]
        pairs = find_duplicates(tools)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["tool_a"], "user_lookup")

    def test_near_duplicate_flagged(self):
        tools = [tool("document_search"), tool("document_search_v2")]
        self.assertEqual(len(find_duplicates(tools)), 1)

    def test_distinct_names_clean(self):
        tools = [tool("invoice_send"), tool("weather_get")]
        self.assertEqual(find_duplicates(tools), [])


class TestSampleOutput(unittest.TestCase):
    def test_codex_limit_exceeded_but_not_claude(self):
        measured, rows = check_sample_output({"text": "x" * 40001})
        by_client = {r["client"]: r for r in rows}
        self.assertIn("tokens", by_client["codex"]["over_limit"])
        self.assertNotIn("tokens", by_client["claude-code"]["over_limit"])

    def test_opencode_line_limit(self):
        measured, rows = check_sample_output({"text": "\n".join(["line"] * 2001)})
        by_client = {r["client"]: r for r in rows}
        self.assertIn("lines", by_client["opencode-1"]["over_limit"])
        self.assertEqual(by_client["load-all"]["over_limit"], [])

    def test_small_sample_clean(self):
        _measured, rows = check_sample_output({"text": "ok"})
        self.assertTrue(all(not r["over_limit"] for r in rows))


if __name__ == "__main__":
    unittest.main()
