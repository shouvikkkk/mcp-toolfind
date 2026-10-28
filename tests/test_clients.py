import unittest

from mcp_toolfind.clients import evaluate_client


def tool(name, description=""):
    return {"name": name, "description": description or "Do %s." % name,
            "inputSchema": {"type": "object", "properties": {}}}


def invoice_competitors():
    """10 name-matching tools that outrank a description-only tool.

    Mirrors examples/demo-tools.json: only invoice_send's description
    also says "send", so the "send" term keeps enough IDF for the
    send-named tools to outrank a description-only invoice mention.
    """
    descriptions = {
        "invoice_send": "Send an invoice to a customer.",
        "invoice_get": "Get an invoice by id.",
        "invoice_list": "List invoices for a customer.",
        "invoice_cancel": "Cancel an invoice.",
        "payment_send": "Send a payment to a vendor.",
        "email_send": "Send an email to a user.",
        "message_send": "Send a message to a channel.",
        "notification_send": "Send a notification to a user.",
        "report_send": "Send a report to a manager.",
        "file_send": "Send a file to a recipient.",
    }
    return [tool(n, descriptions[n]) for n in descriptions]


class TestClaudeCode(unittest.TestCase):
    def test_badly_named_tool_missed(self):
        tools = invoice_competitors() + [tool(
            "ledger_dispatch",
            "Dispatch a ledger entry to the accounting system for downstream "
            "reconciliation and reporting across multiple business units and "
            "fiscal periods, recording the transaction so that it can be "
            "used later to send an invoice to a customer.")]
        result = evaluate_client("claude-code", tools, ["send an invoice"])
        self.assertEqual(result["per_tool"]["ledger_dispatch"], "MISSED")
        self.assertEqual(result["per_tool"]["invoice_send"], "FOUND")

    def test_well_named_tool_found(self):
        result = evaluate_client("claude-code",
                                 [tool("invoice_send", "Send an invoice.")],
                                 ["send an invoice"])
        self.assertEqual(result["per_tool"]["invoice_send"], "FOUND")

    def test_unrelated_tool_not_a_miss(self):
        result = evaluate_client("claude-code",
                                 [tool("weather_get", "Check the weather.")],
                                 ["send an invoice"])
        self.assertEqual(result["per_tool"]["weather_get"], "VISIBLE")

    def test_empty_catalog(self):
        result = evaluate_client("claude-code", [], ["send an invoice"])
        self.assertEqual(result["per_tool"], {})
        self.assertEqual(result["catalog_tokens"], 0)


class TestCodex(unittest.TestCase):
    def test_truncation_cutoff_math(self):
        tools = [tool("a", "x" * 400), tool("b", "y" * 400), tool("c", "z" * 400)]
        # Cutoff sits just past the first tool, so tools 2 and 3 are lost.
        from mcp_toolfind.tokens import tool_tokens
        cutoff = tool_tokens(tools[0]) + 10
        result = evaluate_client("codex", tools, ["do a"], codex_cutoff=cutoff)
        self.assertEqual(result["per_tool"]["a"], "VISIBLE")
        self.assertEqual(result["per_tool"]["b"], "TRUNCATED-AWAY")
        self.assertEqual(result["per_tool"]["c"], "TRUNCATED-AWAY")

    def test_small_catalog_nothing_truncated(self):
        tools = [tool("a"), tool("b")]
        result = evaluate_client("codex", tools, ["do a"])
        self.assertEqual(result["per_tool"]["a"], "VISIBLE")
        self.assertEqual(result["per_tool"]["b"], "VISIBLE")

    def test_unknown_client_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_client("nope", [], [])


class TestOpencodeAndBaseline(unittest.TestCase):
    def test_opencode1_loads_all(self):
        tools = [tool("t%d" % i) for i in range(30)]
        result = evaluate_client("opencode-1", tools, ["do t0"])
        self.assertTrue(all(v == "VISIBLE" for v in result["per_tool"].values()))
        self.assertGreater(result["catalog_tokens"], 0)

    def test_opencode2_first_sixteen_visible(self):
        tools = [tool("t%d" % i) for i in range(20)]
        result = evaluate_client("opencode-2", tools, ["do t0"])
        for i in range(16):
            self.assertEqual(result["per_tool"]["t%d" % i], "VISIBLE")

    def test_opencode2_search_beyond_sixteen(self):
        tools = [tool("t%d" % i) for i in range(17)]
        tools.append(tool("zebra_lookup", "Look up a zebra by name."))
        result = evaluate_client("opencode-2", tools, ["zebra lookup"])
        self.assertEqual(result["per_tool"]["zebra_lookup"], "FOUND")

    def test_load_all_visible(self):
        tools = [tool("a"), tool("b")]
        result = evaluate_client("load-all", tools, ["do a"])
        self.assertEqual(result["per_tool"], {"a": "VISIBLE", "b": "VISIBLE"})


if __name__ == "__main__":
    unittest.main()
