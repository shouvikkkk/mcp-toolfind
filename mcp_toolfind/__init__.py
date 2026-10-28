"""mcp-toolfind: audit an MCP server's tool discoverability across clients.

Stdlib only. No LLM calls anywhere. Every client model in this package is
a documented *simulation* -- an approximation of observed client behaviour
from the Spirit Room study (see README) -- not a claim about exact client
internals. Token counts are a heuristic (characters / 4), not tiktoken.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
