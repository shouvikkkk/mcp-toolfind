# mcp-toolfind

Audit whether agents can actually **find** your MCP server's tools — not
just whether the server connects.

## The problem

MCP Inspector tells you a server is reachable and how many tools it
exposes. It tells you nothing about whether an agent, faced with that
catalog, will ever *call* the right tool. The Spirit Room study found
that gap is real and large:

> The Spirit Room study (Oct 16, updated Oct 19, 2026, Leonid Bugaev,
> summarized by ClawdBytes) tested a production MCP server with **99
> valid tools** against Claude Code 2.1.301, Codex 0.161, OpenCode
> 2.0.31 and Grok Build 1.0.60 — and agents failed to find valid tools.
>
> Source: https://www.clawdbytes.com/article/2026-10-19-claude-code-codex-and-opencode-find-mcp-tools

Key numbers from that study, as reported in the summary:

- Claude Code exposes tool **names** in a ~1,258-token reminder and
  requires a ToolSearch call for full definitions.
- OpenCode 1.x loads **all 99 definitions at 63,245 tokens/request**.
- OpenCode 2.0.31 starts with a partial catalog of **just 16 tools**.
- Codex truncates **~56,619 tokens** of ALL_TOOLS metadata.
- Descriptions are cut to **~120 chars** in initial catalog views.
- A default list result measured **46,253 tokens**.
- The study recommends a **5,000-token response budget** and **intent
  vocabulary naming**: `invoice_send` beats `ledger_dispatch` for
  "bill/invoice" queries.

So a server can be perfectly healthy, fully connected, and still
unusable in practice — because its tools are badly named, buried past a
truncation cutoff, or invisible in a partial catalog. `mcp-toolfind`
audits exactly that, offline, before you ship the server.

## What it does

Given a `tools/list` JSON file and a file of user intent phrases
(one per line, e.g. `send an invoice`), it simulates how five client
models expose that catalog and reports, per tool and per client,
whether the tool is FOUND, MISSED, TRUNCATED-AWAY, or VISIBLE:

| Client model | Simulation (not client internals) |
|---|---|
| `claude-code` | Name reminder + BM25 ToolSearch simulation; top 10 = FOUND, otherwise MISSED |
| `codex` | ALL_TOOLS truncation simulation; tools serialized in catalog order, everything past a token cutoff is LOST (default 60,000 — a model parameter at the scale of the study's observed ~56,619-token truncation, overridable with `--codex-cutoff`) |
| `opencode-1` | Load-all model; full catalog token cost reported, warned when huge |
| `opencode-2` | Partial catalog: first 16 tools always VISIBLE, the rest via BM25 top-10 search |
| `load-all` | Generic baseline for comparison: every tool VISIBLE |

It also checks the catalog itself:

- **Description truncation loss** — constraint words (`requires`,
  `only`, `must`, `before`) that appear *only after char 120*, so they
  are invisible in initial catalog views.
- **Boilerplate prefixes** — "This tool provides functionality that
  allows…", "This tool allows you to…", etc., which waste the first
  characters a client actually shows.
- **Duplicate / near-duplicate names** — names that are identical after
  normalization, or ≥0.85 similar, so an agent cannot tell them apart.
- **Catalog token budget** per client (estimates, see below).
- **Response-budget check** for an optional `--sample-output` JSON
  against per-client limits: Claude Code 25,000-token file-save
  threshold, Codex 10,000 tokens, OpenCode 2,000 lines / 51,200 bytes.

A tool is *relevant* to an intent when it matches **all** non-stopword
tokens of that intent (name + description + schema keys). That keeps
half-matches (an email tool sharing only "send" with "send an invoice")
from counting as misses, while a badly named tool whose description
still contains the full intent vocabulary *does* count — which is the
whole point. A tool is MISSED overall if it misses **any** intent it is
relevant to: being findable under one phrasing does not rescue it when
the natural phrasing fails.

## Setup

No dependencies. Python 3.8+ standard library only — there is
deliberately **no `requirements.txt`**. Clone and run from the repo root:

```bash
python -m mcp_toolfind audit --tools examples/demo-tools.json \
  --intents examples/demo-intents.txt
```

## Usage

```
python -m mcp_toolfind audit --tools <json> --intents <txt> \
  [--clients claude-code,codex,opencode-1,opencode-2,load-all] \
  [--format text|json] \
  [--probe-cmd "..."] [--probe-timeout 10] \
  [--codex-cutoff 60000] \
  [--sample-output <json>]
```

- `--tools`: a JSON array of `{name, description, inputSchema}` objects
  (a full `tools/list` response envelope with a `tools` key is also
  accepted).
- `--intents`: one user phrase per line; blank lines and `#` comments
  are ignored.
- `--probe-cmd`: instead of a file, spawn a stdio MCP server command
  and run JSON-RPC `initialize` + `tools/list` against it (with a
  timeout). Any probe error fails closed — no audit runs on a catalog
  that could not be retrieved. Only stdio is supported; see
  Limitations.
- `--sample-output`: a JSON file holding a sample tool output (a
  `text`/`output` string, or any JSON document) to check against the
  per-client response limits.

Exit codes: **0** = every tool found/visible in every client, no
warnings. **1** = warnings only (boilerplate, truncation loss,
duplicates, huge catalog, sample over a response limit). **2** = at
least one tool MISSED or TRUNCATED-AWAY in some client, or a
fail-closed input/probe error.

JSON output (`--format json`) carries the same data as the text report,
including per-intent rankings, per-tool verdicts, budgets, checks, and
the exit code, for CI use.

## Demo

`examples/` ships a 20-tool catalog and 8 intents, including the
study's naming lesson in miniature: a well-named `invoice_send` and a
badly named `ledger_dispatch` that does the same job (its description
says, deep inside a long sentence, that it can "send an invoice").

```bash
python -m mcp_toolfind audit --tools examples/demo-tools.json \
  --intents examples/demo-intents.txt
```

Expected verdicts (verified by running the command above):

- For the intent **"send an invoice"**: `invoice_send` ranks **#1**
  (FOUND); `ledger_dispatch` ranks **#11** — just outside the top 10 —
  and is **MISSED in claude-code**. Same tool, same capability, lost
  purely on naming. The audit exits **2**.
- `ledger_dispatch` is VISIBLE in `opencode-2` in this demo, honestly:
  it sits within the first 16 tools, so the partial catalog covers it.
  Move it past position 16 in a larger catalog and it becomes
  search-dependent there too.
- No tool is TRUNCATED-AWAY in this demo: the catalog is only ~863
  estimated tokens, far under the codex model cutoff. Truncation only
  bites at study scale (tens of thousands of tokens).
- Catalog checks also flag, in the demo: truncation loss in
  `ledger_dispatch` (`requires`, `before` only after char 120) and in
  `calendar_create` (`must`, `before` only after char 120); a
  boilerplate description on `weather_get`; and the near-duplicate pair
  `document_search` ~ `document_search_v2` (name similarity 0.93).

Demo output (abridged, real run):

```
Tools: 20   Intents: 8   Catalog: ~863 estimated tokens
  ledger_dispatch              MISSED         VISIBLE        VISIBLE        VISIBLE        VISIBLE
  ...
Failures
--------
  MISSED ledger_dispatch in claude-code
  ...
  truncation loss ledger_dispatch: constraint keyword(s) requires,before
    appear only after char 120
  boilerplate description: weather_get
  duplicate/near-duplicate: document_search ~ document_search_v2
Exit code: 2 (failures found)
```

## Differentiation

- **vs. MCP Inspector**: Inspector verifies connectivity and tool
  counts. `mcp-toolfind` verifies discoverability: given real user
  phrasings, can each client model actually surface each tool? A
  server that passes Inspector can still fail this audit badly.
- **vs. FastMCP Code-Mode / single-server progressive discovery**:
  those improve discovery *within* one server's toolset (e.g. by
  generating code against it). `mcp-toolfind` is a cross-client audit:
  the same catalog scored against five different client exposure
  models, so a server author sees which clients will struggle and why,
  before any user hits the problem.
- **vs. hand-checking names**: the audit is mechanical and repeatable —
  rename a tool, re-run, watch the rank move. That is the loop the
  study's naming recommendation implies but no tooling provided.

## Honest limitations

- **Simulations approximate real client behavior.** No client model
  here reproduces a real client's internals. BM25 ranking is a
  transparent proxy for "would a search surface this tool", not a model
  of what a language model will actually choose. A FOUND verdict is not
  a guarantee a model calls the tool; a MISSED verdict is a strong
  signal it will struggle, not proof.
- **Token counts are heuristic.** The estimator is characters / 4. It
  is not tiktoken, and it will differ from any real tokenizer,
  especially for JSON schemas and non-English text. Treat every token
  figure as an estimate, useful for comparisons and order-of-magnitude
  calls, not as a billing-grade count.
- **BM25 is not model behavior.** Synonyms, paraphrases, and
  model-specific tool-choice quirks are not modeled. The relevance rule
  (all intent tokens must match) is deliberately strict to avoid false
  misses; it can miss genuinely relevant tools phrased with synonyms
  (e.g. "bill" for "invoice").
- **stdio probe only.** No HTTP/SSE transport yet. The probe speaks
  one-JSON-message-per-line JSON-RPC and fails closed on timeouts,
  malformed replies, or missing results.
- **No LLM calls anywhere.** By design: the audit is deterministic,
  offline, and free to run in CI. That is also why it cannot judge
  semantic relevance beyond token matching.
- **Duplicate tool names collapse** in the per-tool verdict map (keyed
  by name); the duplicate check itself still flags them, and renaming
  duplicates is the recommended fix.

## Tests

```bash
python -m unittest discover -s tests
```

44 tests: BM25 ranking correctness (including the name-boost effect),
truncation cutoff math, token estimator sanity, every client model on
fixture catalogs, the stdio probe against a fake JSON-RPC server
fixture, CLI exit codes (0 / 1 / 2), and edge cases — empty catalog,
duplicate names, unicode names, malformed JSON failing closed, and a
missing intents file failing closed.

## License

MIT — see `LICENSE`.
