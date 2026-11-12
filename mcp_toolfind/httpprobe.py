"""Live HTTP probes of real MCP servers: Streamable HTTP and legacy HTTP+SSE.

FAIL-CLOSED by design, like the stdio probe: any timeout, non-2xx status,
malformed JSON-RPC reply, missing result, or unparseable SSE stream is a
ProbeError -- the caller must not audit a catalog it could not actually
retrieve. Stdlib only (urllib).

Transports
----------
* Streamable HTTP (spec 2025-03-26 through 2025-11-25): one endpoint,
  every message a POST with ``Accept: application/json, text/event-stream``.
  The server answers each request with one JSON object or with a short
  SSE stream. If it creates a session it returns ``Mcp-Session-Id`` and
  the client sends that header on every later request; a 404 for a known
  session means the session is gone and the client re-initializes once.
  Notifications (``notifications/initialized``) are answered 202 with an
  empty body. The stateless 2026-07-28 revision (no initialize handshake,
  ``Mcp-Method``/``Mcp-Name`` routing headers) is NOT targeted: a server
  speaking only that revision will fail this probe, honestly, rather
  than silently auditing a catalog retrieved under the wrong protocol.
* Legacy HTTP+SSE (spec 2024-11-05, deprecated): a long-lived GET stream
  plus a separate POST endpoint announced by the first ``endpoint`` event.
  Kept because many deployed servers still speak it; new servers should
  not start here.

No OAuth flow is implemented: bring your own token with
``--probe-header "Authorization: Bearer ..."``. The session id is an
opaque value, stored and returned verbatim; it is not authentication.
"""

import json
import queue
import threading
import urllib.error
import urllib.request

from . import __version__
from .probe import ProbeError

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "mcp-toolfind", "version": __version__}


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

def parse_sse_events(body):
    """Parse an SSE body into a list of ``{"event", "data", "id"}`` dicts.

    Follows the SSE framing rules: events are separated by a blank line,
    ``data:`` lines join with ``\\n``, ``:`` lines are comments, unknown
    fields are ignored, and a missing ``event:`` means the default
    ``message`` event. ``body`` may be ``str`` or ``bytes``.
    """
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = body
    events = []
    event = {"event": "message", "data": [], "id": None}
    for raw_line in text.splitlines():
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if line == "":
            if event["data"] or event["id"] is not None:
                events.append({"event": event["event"],
                               "data": "\n".join(event["data"]),
                               "id": event["id"]})
            event = {"event": "message", "data": [], "id": None}
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
        else:
            field, value = line, ""
        if field == "data":
            event["data"].append(value)
        elif field == "event":
            event["event"] = value
        elif field == "id":
            event["id"] = value
        # Unknown fields are ignored, per the SSE spec.
    if event["data"] or event["id"] is not None:
        events.append({"event": event["event"],
                       "data": "\n".join(event["data"]),
                       "id": event["id"]})
    return events


def _check_rpc_reply(obj, what):
    """Validate a JSON-RPC reply object; return its ``result`` or fail."""
    if not isinstance(obj, dict):
        raise ProbeError("probe failed: %s reply is not an object" % what)
    if "error" in obj:
        err = obj["error"]
        detail = err.get("message") if isinstance(err, dict) else str(err)
        raise ProbeError("probe failed: %s returned JSON-RPC error: %s"
                         % (what, detail))
    if "result" not in obj:
        raise ProbeError("probe failed: %s reply has no result: %r"
                         % (what, json.dumps(obj)[:200]))
    return obj["result"]


def _clean_tools(result, what):
    """Validate a tools/list result exactly like the stdio probe does."""
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise ProbeError("probe failed: %s result has no tools array" % what)
    cleaned = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ProbeError(
                "probe failed: %s entry has no string name" % what)
        cleaned.append({
            "name": tool["name"],
            "description": tool.get("description", "") or "",
            "inputSchema": tool.get("inputSchema", {}) or {},
        })
    return cleaned


# ---------------------------------------------------------------------------
# Streamable HTTP
# ---------------------------------------------------------------------------

def _http_exchange(url, payload, headers, timeout, request_id=None):
    """POST one JSON body and return ``(status, content_type, reply, headers)``.

    For a request (``request_id`` given), ``reply`` is the JSON-RPC object:
    a single JSON body, or -- for ``text/event-stream`` -- the first
    ``message`` event whose JSON carries the matching id (notifications
    interleaved on the stream are skipped; the stream is not drained
    past the response, so a server that holds it open cannot stall us).
    For a notification (``request_id`` None) the body is not read and
    ``reply`` is None. HTTPError (non-2xx) is returned as a status with
    ``reply`` None so the caller can decide (e.g. 404 session-gone);
    transport failures and timeouts are ProbeError.
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        resp = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:  # noqa: BLE001 - best effort; status is what matters
            pass
        return (exc.code, exc.headers.get_content_type() if exc.headers else "",
                None, exc.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProbeError("probe failed: HTTP request to %s failed: %s"
                         % (url, exc))
    with resp:
        status = resp.status
        content_type = resp.headers.get_content_type()
        if request_id is None:
            return (status, content_type, None, resp.headers)
        if content_type == "application/json":
            try:
                obj = json.loads(resp.read().decode("utf-8", errors="replace"))
            except ValueError:
                raise ProbeError("probe failed: reply is not JSON")
            if not isinstance(obj, dict):
                raise ProbeError("probe failed: reply is not an object")
            return (status, content_type, obj, resp.headers)
        if content_type == "text/event-stream":
            return (status, content_type,
                    _read_sse_response(resp, request_id, timeout),
                    resp.headers)
        raise ProbeError(
            "probe failed: reply has unsupported Content-Type %r"
            % (content_type or ""))


def _read_sse_response(resp, request_id, timeout):
    """Read SSE events until the JSON-RPC response for ``request_id``.

    Line-oriented reads: ``read()`` would block until the server closes
    a stream it may legitimately hold open, so events are consumed one
    line at a time and reading stops at the matching response.
    """
    buffer = ""
    while True:
        try:
            line = resp.readline()
        except (TimeoutError, OSError) as exc:
            raise ProbeError("probe failed: timed out reading SSE reply: %s"
                             % exc)
        if not line:
            raise ProbeError(
                "probe failed: SSE stream ended before the response arrived")
        buffer += line.decode("utf-8", errors="replace")
        if "\n\n" not in buffer and "\r\n\r\n" not in buffer:
            continue
        if "\r\n\r\n" in buffer:
            raw, buffer = buffer.split("\r\n\r\n", 1)
        else:
            raw, buffer = buffer.split("\n\n", 1)
        for event in parse_sse_events(raw + "\n\n"):
            if event["event"] != "message":
                continue
            try:
                obj = json.loads(event["data"])
            except ValueError:
                raise ProbeError(
                    "probe failed: SSE message is not JSON: %r"
                    % event["data"][:200])
            if not isinstance(obj, dict) or obj.get("id") != request_id:
                continue  # notification or another request's reply
            return obj


def probe_http(url, timeout=10, protocol_version=DEFAULT_PROTOCOL_VERSION,
               extra_headers=None):
    """Probe a remote MCP server over Streamable HTTP; return its tools.

    Sequence: POST initialize -> POST notifications/initialized (202) ->
    POST tools/list. ``extra_headers`` maps header name to value (e.g.
    ``{"Authorization": "Bearer ..."}``). The ``Mcp-Session-Id`` returned
    by initialize, if any, is sent on every later request. A 404 on
    tools/list triggers one re-initialize + retry, then fails closed.
    """
    if not url or not url.strip():
        raise ProbeError("probe failed: empty HTTP probe URL")
    if not url.startswith(("http://", "https://")):
        raise ProbeError(
            "probe failed: HTTP probe URL must start with http:// or https://")
    base_headers = {"MCP-Protocol-Version": protocol_version}
    if extra_headers:
        base_headers.update(extra_headers)

    def _initialize(session_id=None):
        headers = dict(base_headers)
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        status, _ctype, obj, resp_headers = _http_exchange(url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": protocol_version, "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        }, headers, timeout, request_id=1)
        if status != 200:
            raise ProbeError(
                "probe failed: initialize answered HTTP %d" % status)
        _check_rpc_reply(obj, "initialize")
        new_session = None
        if resp_headers is not None:
            new_session = resp_headers.get("Mcp-Session-Id")
        return new_session

    def _notify(session_id):
        headers = dict(base_headers)
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        status, _ctype, _obj, _hdrs = _http_exchange(url, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, headers, timeout)
        if status not in (200, 202):
            raise ProbeError(
                "probe failed: notifications/initialized answered HTTP %d"
                % status)

    def _tools_list(session_id):
        headers = dict(base_headers)
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        status, _ctype, obj, _hdrs = _http_exchange(url, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, headers, timeout, request_id=2)
        if status == 404:
            return None  # session gone: caller re-initializes once
        if status != 200:
            raise ProbeError(
                "probe failed: tools/list answered HTTP %d" % status)
        result = _check_rpc_reply(obj, "tools/list")
        return _clean_tools(result, "tools/list")

    session_id = _initialize()
    _notify(session_id)
    tools = _tools_list(session_id)
    if tools is None:
        # One retry after a fresh initialize; a second 404 fails closed.
        session_id = _initialize()
        _notify(session_id)
        tools = _tools_list(session_id)
        if tools is None:
            raise ProbeError(
                "probe failed: tools/list answered HTTP 404 after re-initialize")
    return tools


# ---------------------------------------------------------------------------
# Legacy HTTP+SSE (2024-11-05, deprecated)
# ---------------------------------------------------------------------------

class _LegacySSEStream:
    """A GET SSE stream read in a background thread, like the stdio probe.

    Events are queued as parsed dicts; ``wait_for(id)`` returns the first
    JSON-RPC response event with the given id, skipping notifications.
    """

    def __init__(self, url, headers, timeout):
        self._queue = queue.Queue()
        self._response = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._url = url
        self._headers = headers
        self._timeout = timeout

    def __enter__(self):
        request = urllib.request.Request(self._url, method="GET")
        request.add_header("Accept", "text/event-stream")
        for name, value in self._headers.items():
            request.add_header(name, value)
        try:
            self._response = urllib.request.urlopen(request,
                                                    timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            raise ProbeError(
                "probe failed: SSE endpoint answered HTTP %d" % exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProbeError(
                "probe failed: cannot open SSE stream: %s" % exc)
        if self._response.headers.get_content_type() != "text/event-stream":
            self._response.close()
            raise ProbeError(
                "probe failed: SSE endpoint did not return text/event-stream")
        self._thread.start()
        return self

    def _run(self):
        # Line-oriented reads: read() would block until the server closes
        # a stream it legitimately holds open, so lines are consumed as
        # they arrive and events are queued as blank lines complete them.
        try:
            buffer = ""
            while True:
                line = self._response.readline()
                if not line:
                    break
                buffer += line.decode("utf-8", errors="replace")
                while "\n\n" in buffer or "\r\n\r\n" in buffer:
                    if "\r\n\r\n" in buffer:
                        raw, buffer = buffer.split("\r\n\r\n", 1)
                    else:
                        raw, buffer = buffer.split("\n\n", 1)
                    for event in parse_sse_events(raw + "\n\n"):
                        self._queue.put(event)
        except Exception as exc:  # noqa: BLE001 - fail closed via the queue
            self._queue.put(exc)
        finally:
            self._queue.put(None)  # end-of-stream marker

    def next_event(self, timeout):
        """Return the next parsed SSE event, or raise ProbeError."""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise ProbeError("probe failed: SSE stream timed out")
        if item is None:
            raise ProbeError("probe failed: SSE stream closed")
        if isinstance(item, Exception):
            raise ProbeError("probe failed: SSE stream read failed: %s" % item)
        return item

    def wait_for(self, request_id, timeout):
        """Wait for the JSON-RPC response with ``request_id``."""
        deadline = timeout
        while True:
            event = self.next_event(deadline)
            if event["event"] != "message":
                continue
            try:
                obj = json.loads(event["data"])
            except ValueError:
                raise ProbeError(
                    "probe failed: SSE message is not JSON: %r"
                    % event["data"][:200])
            if not isinstance(obj, dict) or obj.get("id") != request_id:
                continue  # notification or another request's reply
            return _check_rpc_reply(obj, "tools/list" if request_id == 2
                                    else "initialize")

    def __exit__(self, *exc):
        try:
            if self._response is not None:
                self._response.close()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        return False


def probe_http_sse(sse_url, timeout=10,
                   protocol_version=DEFAULT_PROTOCOL_VERSION,
                   extra_headers=None):
    """Probe a legacy (2024-11-05) HTTP+SSE MCP server; return its tools.

    GET the SSE endpoint, read the first ``endpoint`` event for the POST
    URL, then POST initialize / notifications/initialized / tools/list
    there and match replies on the stream by JSON-RPC id.
    """
    if not sse_url or not sse_url.strip():
        raise ProbeError("probe failed: empty HTTP+SSE probe URL")
    if not sse_url.startswith(("http://", "https://")):
        raise ProbeError(
            "probe failed: HTTP+SSE probe URL must start with http:// or https://")
    base_headers = {"MCP-Protocol-Version": protocol_version}
    if extra_headers:
        base_headers.update(extra_headers)

    with _LegacySSEStream(sse_url, base_headers, timeout) as stream:
        endpoint_event = stream.next_event(timeout)
        if endpoint_event["event"] != "endpoint":
            raise ProbeError(
                "probe failed: first SSE event is %r, expected 'endpoint'"
                % endpoint_event["event"])
        post_url = endpoint_event["data"].strip()
        if not post_url:
            raise ProbeError("probe failed: endpoint event carried no URL")
        if post_url.startswith("/"):
            # Relative to the SSE endpoint's origin.
            from urllib.parse import urljoin
            post_url = urljoin(sse_url, post_url)
        if not post_url.startswith(("http://", "https://")):
            raise ProbeError(
                "probe failed: endpoint URL is not http(s): %r" % post_url[:200])

        def _post(payload, expect_statuses=(200, 202)):
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(post_url, data=data, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Accept", "application/json, text/event-stream")
            for name, value in base_headers.items():
                request.add_header(name, value)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ProbeError("probe failed: POST to %s failed: %s"
                                 % (post_url, exc))
            if status not in expect_statuses:
                raise ProbeError(
                    "probe failed: POST answered HTTP %d" % status)
            return status

        _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        })
        stream.wait_for(1, timeout)
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        _post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        result = stream.wait_for(2, timeout)
        return _clean_tools(result, "tools/list")
