"""Optional live stdio probe of a real MCP server.

FAIL-CLOSED by design: any timeout, malformed JSON-RPC reply, missing
result, or non-zero exit is a ProbeError -- the caller must not audit a
catalog it could not actually retrieve. Only the stdio transport is
supported; HTTP/SSE are explicitly out of scope (see README limitations).

Protocol: one JSON-RPC 2.0 message per line, as MCP stdio servers
speak. Sequence: initialize -> notifications/initialized -> tools/list.
"""

import json
import queue
import shlex
import subprocess
import threading


class ProbeError(Exception):
    """Raised for any probe failure. The audit must not proceed on it."""


def _read_line(proc, timeout):
    """Read one stdout line, failing closed on timeout / EOF."""
    q = queue.Queue()

    def _reader():
        try:
            q.put(proc.stdout.readline())
        except Exception as exc:  # noqa: BLE001 - fail closed, surface as ProbeError
            q.put(exc)

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    try:
        item = q.get(timeout=timeout)
    except queue.Empty:
        raise ProbeError("probe timed out waiting for a server reply")
    if isinstance(item, Exception):
        raise ProbeError("probe read failed: %s" % item)
    if not item:
        raise ProbeError("probe failed: server closed stdout (EOF)")
    return item


def _send(proc, message):
    try:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise ProbeError("probe failed writing to server: %s" % exc)


def _request(proc, message, timeout):
    _send(proc, message)
    line = _read_line(proc, timeout)
    try:
        reply = json.loads(line)
    except ValueError:
        raise ProbeError("probe failed: server reply is not JSON: %r" % line[:200])
    if not isinstance(reply, dict) or "result" not in reply:
        raise ProbeError("probe failed: reply has no result: %r" % line[:200])
    return reply["result"]


def probe_stdio(command, timeout=10):
    """Spawn ``command`` (shell-split) and return its tools/list tools.

    Raises ProbeError on any failure. Returns a list of tool dicts with
    at least a string ``name``; tools without one are rejected.
    """
    if not command or not command.strip():
        raise ProbeError("probe failed: empty command")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ProbeError("probe failed: cannot parse command: %s" % exc)
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
    except OSError as exc:
        raise ProbeError("probe failed: cannot start server command: %s" % exc)
    try:
        _request(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "mcp-toolfind", "version": "0.1.0"},
            },
        }, timeout)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = _request(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, timeout)
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - best effort cleanup
            proc.kill()
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise ProbeError("probe failed: tools/list result has no tools array")
    cleaned = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ProbeError("probe failed: tools/list entry has no string name")
        cleaned.append({
            "name": tool["name"],
            "description": tool.get("description", "") or "",
            "inputSchema": tool.get("inputSchema", {}) or {},
        })
    return cleaned
