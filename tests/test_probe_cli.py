import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from mcp_toolfind.cli import main
from mcp_toolfind.probe import ProbeError, probe_stdio
from mcp_toolfind.report import load_intents, load_tools, run_audit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAKE_SERVER = os.path.join(HERE, "fake_mcp_server.py")


class TestProbe(unittest.TestCase):
    def test_fake_server_returns_tools(self):
        tools = probe_stdio("%s %s" % (sys.executable, FAKE_SERVER), timeout=10)
        names = [t["name"] for t in tools]
        self.assertEqual(names, ["invoice_send", "weather_get"])

    def test_dead_server_fails_closed(self):
        cmd = "%s -c 'import sys; sys.exit(1)'" % sys.executable
        with self.assertRaises(ProbeError):
            probe_stdio(cmd, timeout=5)

    def test_missing_command_fails_closed(self):
        with self.assertRaises(ProbeError):
            probe_stdio("mcp-toolfind-no-such-binary-xyz", timeout=5)


class TestLoading(unittest.TestCase):
    def test_malformed_json_fails_closed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
            path = f.name
        try:
            with self.assertRaises(ValueError):
                load_tools(path)
        finally:
            os.unlink(path)

    def test_non_array_fails_closed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"name": "x"}, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                load_tools(path)
        finally:
            os.unlink(path)

    def test_missing_intents_file_fails_closed(self):
        with self.assertRaises(ValueError):
            load_intents("/tmp/mcp-toolfind-no-such-intents.txt")

    def test_empty_catalog_is_clean(self):
        audit = run_audit([], ["do something"])
        self.assertEqual(audit["tool_count"], 0)
        self.assertEqual(audit["exit_code"], 0)

    def test_unicode_tool_name_survives(self):
        tools = [{"name": "café_lookup", "description": "Look up a café.",
                  "inputSchema": {}}]
        audit = run_audit(tools, ["look up a café"], clients=["claude-code"])
        self.assertIn("café_lookup", audit["per_tool"])


def _write(tmpdir, name, content):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


class TestCLI(unittest.TestCase):
    def test_clean_catalog_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tools = _write(tmpdir, "tools.json", json.dumps([
                {"name": "invoice_send", "description": "Send an invoice.",
                 "inputSchema": {}}]))
            intents = _write(tmpdir, "intents.txt", "send an invoice\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["audit", "--tools", tools, "--intents", intents,
                             "--clients", "claude-code"])
            self.assertEqual(code, 0)
            self.assertIn("invoice_send", buf.getvalue())

    def test_demo_exit_two_and_json_format(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["audit",
                         "--tools", os.path.join(REPO, "examples", "demo-tools.json"),
                         "--intents", os.path.join(REPO, "examples", "demo-intents.txt"),
                         "--format", "json"])
        self.assertEqual(code, 2)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["per_tool"]["ledger_dispatch"]["claude-code"],
                         "MISSED")
        self.assertEqual(data["per_tool"]["invoice_send"]["claude-code"],
                         "FOUND")

    def test_missing_intents_exit_two(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tools = _write(tmpdir, "tools.json", "[]")
            code = main(["audit", "--tools", tools,
                         "--intents", os.path.join(tmpdir, "nope.txt")])
            self.assertEqual(code, 2)

    def test_warnings_only_exit_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tools = _write(tmpdir, "tools.json", json.dumps([
                {"name": "weather_get",
                 "description": "This tool allows you to check the weather.",
                 "inputSchema": {}}]))
            intents = _write(tmpdir, "intents.txt", "check the weather\n")
            sample = _write(tmpdir, "sample.json",
                            json.dumps({"text": "x" * 40001}))
            code = main(["audit", "--tools", tools, "--intents", intents,
                         "--clients", "claude-code,codex",
                         "--sample-output", sample])
            # Boilerplate warning + sample over Codex limit, but no MISS:
            # weather_get is the only match for its intent, so it is FOUND.
            self.assertEqual(code, 1)

    def test_probe_cli_uses_probed_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            intents = _write(tmpdir, "intents.txt", "send an invoice\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["audit", "--intents", intents,
                             "--clients", "claude-code",
                             "--probe-cmd",
                             "%s %s" % (sys.executable, FAKE_SERVER)])
            self.assertEqual(code, 0)
            self.assertIn("invoice_send", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
