"""Fake stdio MCP server fixture for probe tests.

Speaks one JSON-RPC 2.0 message per line: answers `initialize` and
`tools/list`, ignores notifications. Used only by tests.
"""

import json
import sys


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            continue
        if msg["method"] == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                              "result": {"protocolVersion": "2024-11-05",
                                         "capabilities": {},
                                         "serverInfo": {"name": "fake", "version": "0"}}}),
                  flush=True)
        elif msg["method"] == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                              "result": {"tools": [
                                  {"name": "invoice_send",
                                   "description": "Send an invoice to a customer.",
                                   "inputSchema": {"type": "object", "properties": {}}},
                                  {"name": "weather_get",
                                   "description": "Check the weather.",
                                   "inputSchema": {"type": "object", "properties": {}}},
                              ]}}), flush=True)


if __name__ == "__main__":
    main()
