import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from mcp_toolfind.cli import main
from mcp_toolfind.httpprobe import (ProbeError, parse_sse_events,
                                    probe_http, probe_http_sse)
from mcp_toolfind.probe import probe_stdio

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

from tests.fake_http_mcp_server import start_server, stop_server


class HttpProbeTestCase(unittest.TestCase):
    def setUp(self):
        self.server, self.base = start_server()

    def tearDown(self):
        stop_server(self.server)

    def test_streamable_json_replies(self):
        tools = probe_http(self.base + "/mcp-json", timeout=10)
        self.assertEqual([t["name"] for t in tools],
                         ["invoice_send", "weather_get"])

    def test_streamable_sse_replies(self):
        tools = probe_http(self.base + "/mcp-sse", timeout=10)
        self.assertEqual([t["name"] for t in tools],
                         ["invoice_send", "weather_get"])

    def test_session_id_is_sent_on_later_requests(self):
        probe_http(self.base + "/mcp-json", timeout=10)
        self.assertEqual(self.server.seen_sessions, ["sess-1"])

    def test_extra_headers_reach_the_server(self):
        probe_http(self.base + "/mcp-json", timeout=10,
                   extra_headers={"Authorization": "Bearer tok-123"})
        self.assertEqual(self.server.seen_auth, ["Bearer tok-123"])

    def test_404_session_gone_reinitializes_once(self):
        tools = probe_http(self.base + "/mcp-404", timeout=10)
        self.assertEqual([t["name"] for t in tools],
                         ["invoice_send", "weather_get"])
        self.assertEqual(self.server.tools_calls, 2)

    def test_wrong_content_type_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_http(self.base + "/mcp-bad-ctype", timeout=10)

    def test_malformed_json_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_http(self.base + "/mcp-bad-json", timeout=10)

    def test_non_json_sse_message_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_http(self.base + "/mcp-bad-sse", timeout=10)

    def test_unknown_path_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_http(self.base + "/no-such-path", timeout=10)

    def test_empty_url_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_http("", timeout=5)

    def test_non_http_url_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_http("stdio://example.com/mcp", timeout=5)

    def test_unreachable_host_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_http("http://127.0.0.1:1/mcp", timeout=5)

    def test_legacy_http_sse_flow(self):
        tools = probe_http_sse(self.base + "/sse", timeout=10)
        self.assertEqual([t["name"] for t in tools],
                         ["invoice_send", "weather_get"])

    def test_legacy_wrong_path_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_http_sse(self.base + "/no-such-stream", timeout=10)


class SSEParserTestCase(unittest.TestCase):
    def test_multiline_data_joins_with_newline(self):
        events = parse_sse_events(
            "event: message\ndata: {\"a\": 1,\ndata: \"b\": 2}\n\n")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "message")
        self.assertEqual(events[0]["data"], "{\"a\": 1,\n\"b\": 2}")

    def test_comments_and_unknown_fields_ignored(self):
        events = parse_sse_events(
            ": keep-alive\nretry: 5000\ndata: hello\n\n")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "message")
        self.assertEqual(events[0]["data"], "hello")

    def test_missing_event_defaults_to_message(self):
        events = parse_sse_events("data: x\n\n")
        self.assertEqual(events[0]["event"], "message")

    def test_id_field_captured(self):
        events = parse_sse_events("id: evt-9\ndata: x\n\n")
        self.assertEqual(events[0]["id"], "evt-9")

    def test_crlf_line_endings(self):
        events = parse_sse_events("event: endpoint\r\ndata: /m\r\n\r\n")
        self.assertEqual(events[0]["event"], "endpoint")
        self.assertEqual(events[0]["data"], "/m")

    def test_trailing_event_without_blank_line(self):
        events = parse_sse_events("data: last")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"], "last")


class HttpProbeCLITestCase(unittest.TestCase):
    def setUp(self):
        self.server, self.base = start_server()

    def tearDown(self):
        stop_server(self.server)

    def _write(self, tmpdir, name, content):
        path = os.path.join(tmpdir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_probe_http_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            intents = self._write(tmpdir, "intents.txt", "send an invoice\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["audit", "--intents", intents,
                             "--clients", "claude-code",
                             "--probe-http", self.base + "/mcp-json"])
            self.assertEqual(code, 0)
            self.assertIn("invoice_send", buf.getvalue())

    def test_probe_http_with_header_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            intents = self._write(tmpdir, "intents.txt", "send an invoice\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["audit", "--intents", intents,
                             "--clients", "claude-code",
                             "--probe-http", self.base + "/mcp-json",
                             "--probe-header", "Authorization: Bearer tok-123"])
            self.assertEqual(code, 0)
            self.assertEqual(self.server.seen_auth, ["Bearer tok-123"])

    def test_conflicting_probe_sources_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            intents = self._write(tmpdir, "intents.txt", "send an invoice\n")
            code = main(["audit", "--intents", intents,
                         "--probe-cmd", "%s -c 'pass'" % sys.executable,
                         "--probe-http", self.base + "/mcp-json"])
            self.assertEqual(code, 2)

    def test_malformed_probe_header_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            intents = self._write(tmpdir, "intents.txt", "send an invoice\n")
            code = main(["audit", "--intents", intents,
                         "--probe-http", self.base + "/mcp-json",
                         "--probe-header", "Authorization"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
