"""Catalog quality checks: truncation loss, boilerplate, duplicates,
budget table, and sample-output response-budget checks.

All thresholds here are model parameters, documented where they are
used -- not claims about client internals. The 120-character figure
comes from the study's observation that descriptions are cut to
~120 chars in initial catalog views; the response limits are the ones
named in the task brief for this audit (Claude Code 25,000-token
file-save threshold, Codex 10,000 tokens, OpenCode 2,000 lines /
51,200 bytes).
"""

import difflib
import json
import re

TRUNCATION_CHARS = 120
TRUNCATION_KEYWORDS = ("requires", "only", "must", "before")

BOILERPLATE_PREFIXES = (
    "this tool provides functionality that allows",
    "this tool allows you to",
    "this tool provides",
    "this tool is designed to",
    "this tool enables you to",
    "this tool helps you to",
)

# Per-client response-budget limits for an optional sample output.
# A value of None means that client has no limit on that axis in this model.
SAMPLE_LIMITS = {
    "claude-code": {"tokens": 25000, "lines": None, "bytes": None},
    "codex": {"tokens": 10000, "lines": None, "bytes": None},
    "opencode-1": {"tokens": None, "lines": 2000, "bytes": 51200},
    "opencode-2": {"tokens": None, "lines": 2000, "bytes": 51200},
    "load-all": {"tokens": None, "lines": None, "bytes": None},
}


def _word_present(text, word):
    return re.search(r"\b" + re.escape(word) + r"\b", text, re.IGNORECASE) is not None


def check_truncation_loss(tool):
    """Keywords that appear ONLY after char 120 of the description.

    If a client shows only the first ~120 characters, those constraints
    are invisible at discovery time -- the classic way a tool gets
    called wrong. Returns the list of such keywords (possibly empty).
    """
    desc = tool.get("description", "") or ""
    head, tail = desc[:TRUNCATION_CHARS], desc[TRUNCATION_CHARS:]
    lost = []
    for keyword in TRUNCATION_KEYWORDS:
        if _word_present(tail, keyword) and not _word_present(head, keyword):
            lost.append(keyword)
    return lost


def check_boilerplate(tool):
    """True if the description starts with a known boilerplate prefix."""
    desc = (tool.get("description", "") or "").strip().lower()
    return any(desc.startswith(prefix) for prefix in BOILERPLATE_PREFIXES)


def _normalized_name(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _token_set(name):
    return set(re.split(r"[^a-z0-9]+", name.lower())) - {""}


def find_duplicates(tools):
    """Duplicate / near-duplicate tool names.

    Flags pairs whose separator-stripped names are identical, whose
    token sets are >=80% similar (Jaccard), or whose normalized names
    score >=0.85 on difflib ratio. Such pairs are ambiguous: an agent
    (or a user) cannot tell from the name which one to call.
    """
    pairs = []
    names = [t.get("name", "") for t in tools]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            norm_a, norm_b = _normalized_name(a), _normalized_name(b)
            reasons = []
            if norm_a and norm_a == norm_b:
                reasons.append("identical-after-normalization")
            set_a, set_b = _token_set(a), _token_set(b)
            if set_a and set_b:
                jaccard = len(set_a & set_b) / len(set_a | set_b)
                if jaccard >= 0.8:
                    reasons.append("token-jaccard=%.2f" % jaccard)
            ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
            if ratio >= 0.85:
                reasons.append("name-similarity=%.2f" % ratio)
            if reasons:
                pairs.append({"tool_a": a, "tool_b": b, "reasons": reasons})
    return pairs


def check_sample_output(sample_data):
    """Check an optional sample output against per-client limits.

    ``sample_data`` is the parsed JSON of the --sample-output file. If
    it is an object with a string "text" or "output" field, that string
    is measured; otherwise the whole JSON document (compact dump) is
    measured. Returns per-client rows with measured tokens/lines/bytes
    and an over-limit flag per axis.
    """
    if isinstance(sample_data, dict) and isinstance(sample_data.get("text"), str):
        text = sample_data["text"]
    elif isinstance(sample_data, dict) and isinstance(sample_data.get("output"), str):
        text = sample_data["output"]
    elif isinstance(sample_data, str):
        text = sample_data
    else:
        text = json.dumps(sample_data, sort_keys=True, separators=(",", ":"))
    from .tokens import estimate_tokens  # local import avoids a cycle
    measured = {
        "tokens": estimate_tokens(text),
        "lines": text.count("\n") + (1 if text else 0),
        "bytes": len(text.encode("utf-8")),
    }
    rows = []
    for client, limits in SAMPLE_LIMITS.items():
        over = []
        for axis in ("tokens", "lines", "bytes"):
            limit = limits[axis]
            if limit is not None and measured[axis] > limit:
                over.append(axis)
        rows.append({"client": client, "limits": limits,
                     "measured": dict(measured), "over_limit": over})
    return measured, rows
