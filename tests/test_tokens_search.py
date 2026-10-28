import unittest

from mcp_toolfind.search import BM25, document_tokens, tokenize
from mcp_toolfind.tokens import catalog_tokens, estimate_tokens, tool_tokens


class TestTokens(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens(None), 0)

    def test_chars_over_four(self):
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)
        self.assertEqual(estimate_tokens("a" * 400), 100)

    def test_tool_and_catalog_tokens(self):
        tool = {"name": "x", "description": "hello world", "inputSchema": {}}
        self.assertGreater(tool_tokens(tool), 0)
        self.assertEqual(catalog_tokens([tool, tool]), 2 * tool_tokens(tool))
        self.assertEqual(catalog_tokens([]), 0)


class TestTokenize(unittest.TestCase):
    def test_camel_and_separators(self):
        self.assertEqual(tokenize("sendInvoice"), ["send", "invoice"])
        self.assertEqual(tokenize("ledger_dispatch"), ["ledger", "dispatch"])
        self.assertEqual(tokenize("user-lookup"), ["user", "lookup"])

    def test_plural_and_stopwords(self):
        self.assertEqual(tokenize("send an invoice"), ["send", "invoice"])
        self.assertEqual(tokenize("list invoices"), ["list", "invoice"])

    def test_unicode_kept_as_tokens(self):
        tokens = tokenize("café_lookup")
        self.assertIn("lookup", tokens)
        self.assertTrue(tokens)  # must not crash or empty out


class TestBM25(unittest.TestCase):
    def test_matching_doc_ranks_first(self):
        docs = [["send", "invoice"], ["weather", "city"], ["send", "email"]]
        bm25 = BM25(docs)
        ranked = bm25.rank(["send", "invoice"])
        self.assertEqual(ranked[0][0], 0)
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_no_match_scores_zero(self):
        bm25 = BM25([["weather", "city"]])
        self.assertEqual(bm25.score(["invoice"], 0), 0.0)

    def test_name_boost_beats_long_description(self):
        named = {"name": "invoice_send", "description": "Send an invoice.",
                 "inputSchema": {}}
        buried = {"name": "ledger_dispatch",
                  "description": ("Dispatch a ledger entry to the accounting "
                                  "system for downstream reconciliation and "
                                  "reporting across many business units and "
                                  "fiscal periods, recording the transaction "
                                  "so it can be used later to send an invoice "
                                  "to a customer."),
                  "inputSchema": {}}
        bm25 = BM25([document_tokens(named), document_tokens(buried)])
        ranked = bm25.rank_for_intent("send an invoice")
        self.assertEqual(ranked[0][0], 0)

    def test_empty_corpus(self):
        bm25 = BM25([])
        self.assertEqual(bm25.rank(["invoice"]), [])


if __name__ == "__main__":
    unittest.main()
