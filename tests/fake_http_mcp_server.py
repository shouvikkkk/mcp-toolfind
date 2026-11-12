"""Fake HTTP MCP servers for httpprobe tests. Stdlib only, in-process.

One ThreadingHTTPServer serves several behaviours on different paths:

* ``/mcp-json``  -- Streamable HTTP, single-JSON replies, issues
  ``Mcp-Session-Id: sess-1`` on initialize and records the session id
  and Authorization header seen on later requests.
* ``/mcp-sse``   -- Streamable HTTP, SSE replies (message events).
* ``/mcp-404``   -- Streamable HTTP; the first tools/list answers 404
  (session gone), then works after a fresh initialize.
* ``/mcp-bad-ctype`` -- 200 with ``text/html`` body.
* ``/mcp-bad-json``  -- 200 application/json with a malformed body.
* ``/mcp-bad-sse``   -- 200 text/event-stream whose message is not JSON.
* ``/sse`` + ``/messages`` -- legacy HTTP+SSE (2024-11-05): the GET
  stream announces the POST endpoint in an ``endpoint`` event, then
  relays JSON-RPC replies as ``message`` events.

``start_server()`` returns ``(server, base_url)``; ``stop_server(server)``
shuts it down. Used only by tests.
"""

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TOOLS = [
    {"name": "invoice_send",
     "description": "Send an invoice to a customer.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "weather_get",
     "description": "Check the weather.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _initialize_result(msg):
    return {"jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"protocolVersion": "2025-06-18",
                       "capabilities": {},
                       "serverInfo": {"name": "fake-http", "version": "0"}}}


def _tools_result(msg):
    return {"jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"tools": TOOLS}}


def _reply_for(msg):
    method = msg.get("method")
    if method == "initialize":
        return _initialize_result(msg)
    if method == "tools/list":
        return _tools_result(msg)
    return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence test output
        pass

    # -- helpers ---------------------------------------------------------
    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _send_json(self, status, obj, extra=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, status, events, extra=None):
        body = "".join(events).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _sse_event(event, data):
        return "event: %s\ndata: %s\n\n" % (event, data)

    # -- Streamable HTTP paths ------------------------------------------
    def _handle_streamable(self, path):
        server = self.server
        msg = self._read_json()
        if path == "/mcp-bad-ctype":
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/mcp-bad-json":
            body = b"{not json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/mcp-bad-sse":
            self._send_sse(200, [self._sse_event("message", "{not json")])
            return
        reply = _reply_for(msg)
        if reply is None:
            # Notification: 202, no body.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/mcp-404" and msg.get("method") == "tools/list":
            server.tools_calls += 1
            if server.tools_calls == 1:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if msg.get("method") == "tools/list":
            server.seen_sessions.append(self.headers.get("Mcp-Session-Id"))
            server.seen_auth.append(self.headers.get("Authorization"))
        extra = {}
        if msg.get("method") == "initialize":
            extra["Mcp-Session-Id"] = "sess-1"
        if path == "/mcp-sse":
            self._send_sse(200, [self._sse_event(
                "message", json.dumps(reply))], extra)
        else:
            self._send_json(200, reply, extra)

    # -- Legacy HTTP+SSE paths ------------------------------------------
    def _handle_legacy_sse(self):
        server = self.server
        session = "legacy-1"
        stream_q = queue.Queue()
        server.streams[session] = stream_q
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(self._sse_event(
                "endpoint", "/messages?session_id=%s" % session).encode("utf-8"))
            self.wfile.flush()
            relayed = 0
            while True:
                try:
                    event = stream_q.get(timeout=0.5)
                except queue.Empty:
                    # The probe closes the stream once it has its reply;
                    # exit promptly instead of idling on an open connection
                    # (which would stall server shutdown in tests).
                    if relayed:
                        break
                    continue
                if event is None:
                    break
                relayed += 1
                self.wfile.write(event.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            server.streams.pop(session, None)

    def _handle_legacy_messages(self):
        server = self.server
        msg = self._read_json()
        parsed = urlparse(self.path)
        session = parse_qs(parsed.query).get("session_id", [None])[0]
        reply = _reply_for(msg)
        if reply is not None and session in server.streams:
            server.streams[session].put(self._sse_event(
                "message", json.dumps(reply)))
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- dispatch --------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/mcp-json", "/mcp-sse", "/mcp-404", "/mcp-bad-ctype",
                    "/mcp-bad-json", "/mcp-bad-sse"):
            self._handle_streamable(path)
        elif path == "/messages":
            self._handle_legacy_messages()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/sse":
            self._handle_legacy_sse()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def start_server():
    """Start the fake server on an ephemeral port; return (server, url)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.tools_calls = 0
    server.seen_sessions = []
    server.seen_auth = []
    server.streams = {}
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    host, port = server.server_address
    return server, "http://%s:%d" % (host, port)


def stop_server(server):
    server.shutdown()
    server.server_close()
