# Changelog

## 0.2.0 — 2026-11-12

### Added

- **Streamable HTTP probe** (`--probe-http <url>`): audit a remote MCP
  server over the Streamable HTTP transport (spec 2025-03-26 through
  2025-11-25). POSTs `initialize` + `notifications/initialized` +
  `tools/list` to the endpoint, accepts a single JSON reply or a short
  SSE stream, captures a returned `Mcp-Session-Id` and sends it on
  later requests, and re-initializes once on a 404 for a known session.
  SSE replies are read only up to the matching JSON-RPC response, so a
  server holding the stream open cannot stall the probe.
- **Legacy HTTP+SSE probe** (`--probe-http-sse <url>`): covers the
  deprecated 2024-11-05 transport — GET the SSE endpoint, read the
  first `endpoint` event for the POST URL, run the same JSON-RPC
  sequence, match replies on the stream by id.
- `--probe-header "NAME: VALUE"` (repeatable): extra HTTP headers for
  the HTTP probes, e.g. `Authorization: Bearer ...`. No OAuth flow is
  implemented.
- `--protocol-version`: MCP protocol version to declare (default
  `2025-06-18`).
- `mcp_toolfind/httpprobe.py`: stdlib-only implementation (urllib),
  fail-closed like the stdio probe; `tests/fake_http_mcp_server.py`:
  in-process fake HTTP MCP server with JSON, SSE, 404-once, malformed,
  and legacy behaviours.

### Changed

- The `--probe-cmd` / `--probe-http` / `--probe-http-sse` / `--tools`
  sources are now mutually exclusive: giving more than one is a
  fail-closed CLI error instead of silent precedence.
- JSON-RPC error replies from a probe are now reported with the
  server's error message instead of a generic "no result".
- Version 0.1.0 → 0.2.0 (`mcp_toolfind/__init__.py`); the probe
  `clientInfo` version now tracks the package version.
- README: probe usage and limitations updated; the "stdio probe only"
  limitation is closed. Test count 44 → 68.

### Limitations (still)

- The stateless 2026-07-28 Streamable HTTP revision (no initialize,
  `Mcp-Method`/`Mcp-Name` routing headers) is not targeted; a server
  speaking only that revision fails the probe honestly.
- No OAuth flow; authenticated servers need `--probe-header`.

## 0.1.0 — 2026-10-28

- Initial release: five client-model discoverability simulations,
  catalog checks (description truncation loss, boilerplate, duplicate
  names, token budgets, sample-output response limits), stdio probe,
  text + JSON reports, exit codes 0/1/2.
