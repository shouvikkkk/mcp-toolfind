"""Heuristic token estimation.

MODEL / APPROXIMATION, NOT A MEASUREMENT: token counts here are
``ceil(len(text) / 4)`` -- a common rough rule of thumb for English text.
They are NOT tiktoken counts and will differ from any real tokenizer,
especially for JSON schemas, code, and non-English text. They are good
enough to compare catalogs and to spot order-of-magnitude blow-ups
(e.g. a 60k-token catalog), which is what this audit needs. Every report
labels these numbers as estimates.
"""

import json
import math


def estimate_tokens(text):
    """Estimate tokens for a string as ceil(chars / 4). Empty -> 0."""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    return int(math.ceil(len(text) / 4.0))


def tool_tokens(tool):
    """Estimate tokens for one tool: name + description + inputSchema.

    The serialization is compact JSON with sorted keys, so the estimate
    is stable for a given tool definition.
    """
    blob = json.dumps(tool, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return estimate_tokens(blob)


def catalog_tokens(tools):
    """Sum of :func:`tool_tokens` over a catalog, in catalog order."""
    return sum(tool_tokens(t) for t in tools)


def name_tokens(tool):
    """Estimate tokens for just the tool name (the reminder view)."""
    return estimate_tokens(tool.get("name", ""))
