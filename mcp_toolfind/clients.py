"""Client catalog models.

EVERY MODEL IN THIS FILE IS A SIMULATION, calibrated only loosely to
numbers reported in the Spirit Room study (Oct 16, updated Oct 19, 2026;
see README for the full citation). None of these models claims to
reproduce a client's exact internals:

- ``claude-code``: the study reports Claude Code exposes tool *names* in
a ~1,258-token reminder and requires a ToolSearch call for full
definitions. Model: a tool is FOUND for an intent iff BM25 ranks it in
the top 10 for that intent, otherwise MISSED. The name reminder is
reported as a token estimate but does not by itself count as FOUND,
because a name alone is not a callable definition.
- ``codex``: the study reports Codex truncates ~56,619 tokens of
ALL_TOOLS metadata. Model: tools are serialized in catalog order and
everything past ``codex_cutoff`` tokens is LOST (TRUNCATED-AWAY).
The default cutoff (60,000) is a *model parameter* chosen at the scale
of the observed truncation, not a measured Codex limit -- pass
``--codex-cutoff`` to test other values. The per-output limit
(10,000 tokens) is checked separately in checks.py against a sample.
- ``opencode-1``: the study reports OpenCode 1.x loads all 99 tool
definitions at 63,245 tokens/request. Model: every tool VISIBLE, with
the full catalog token cost reported and a warning above
OPENCOD1_HUGE_TOKENS (a model threshold, not a client limit).
- ``opencode-2``: the study reports OpenCode 2.0.31 starts with a
partial catalog of just 16 tools. Model: the first 16 tools are
VISIBLE; the rest are found only via BM25 top-10 search, as for
claude-code.
- ``load-all``: generic baseline -- every tool VISIBLE, full cost
reported. Useful as a control: if even the baseline looks bad, the
catalog itself (not any client) is the problem.

Verdict vocabulary: FOUND / MISSED / TRUNCATED-AWAY / VISIBLE.
A tool with no intent exercising it is reported VISIBLE (its name is
exposed by the reminder / partial catalog, but no search verdict was
earned) -- it never counts as a failure. Only MISSED and
TRUNCATED-AWAY drive exit code 2.

Relevance: a tool is *relevant* to an intent when it matches *all*
non-stopword tokens of the intent (name + description + schema keys).
This stops unrelated or half-matching tools (an email tool vs. an
invoice intent, which shares only "send") from being counted as misses.
A badly named tool whose description still contains the full intent
vocabulary -- the ledger_dispatch case -- *is* relevant, and is exactly
what the MISS verdict is for. A tool is MISSED overall if it misses
*any* intent it is relevant to: being findable under one phrasing does
not rescue it when the natural phrasing ("send an invoice") fails.
"""

from .search import BM25, document_tokens, tokenize
from .tokens import catalog_tokens, tool_tokens

TOP_K = 10
OPENCODE2_PARTIAL = 16
DEFAULT_CODEX_CUTOFF = 60000
OPENCODE1_HUGE_TOKENS = 30000

CLIENTS = ("claude-code", "codex", "opencode-1", "opencode-2", "load-all")

CLIENT_NOTES = {
    "claude-code": "Model: name reminder + BM25 ToolSearch simulation, top 10 = FOUND.",
    "codex": "Model: ALL_TOOLS truncation simulation; past cutoff = TRUNCATED-AWAY.",
    "opencode-1": "Model: load-all; full catalog token cost reported.",
    "opencode-2": "Model: first 16 tools visible + BM25 search for the rest.",
    "load-all": "Baseline: every tool visible, full catalog cost reported.",
}


def _relevant(doc_tokens_set, intent_tokens):
    return bool(intent_tokens) and set(intent_tokens) <= doc_tokens_set


def evaluate_client(client, tools, intents, codex_cutoff=DEFAULT_CODEX_CUTOFF):
    """Evaluate one client model over a catalog and intents.

    Returns a dict with catalog token cost, per-intent rankings and a
    per-tool verdict map (tool name -> verdict string).
    """
    if client not in CLIENTS:
        raise ValueError("unknown client: %r (choose from %s)" % (client, ", ".join(CLIENTS)))
    names = [t.get("name", "") for t in tools]
    docs = [document_tokens(t) for t in tools]
    doc_sets = [set(d) for d in docs]
    bm25 = BM25(docs) if tools else None

    per_intent = []
    # best_rank[tool_index] = best (lowest) rank across intents, or None
    best_rank = [None] * len(tools)
    relevant = [False] * len(tools)
    missed_any = [False] * len(tools)

    if client in ("claude-code", "opencode-2"):
        for intent in intents:
            intent_tokens = tokenize(intent)
            ranking = bm25.rank(intent_tokens) if bm25 else []
            rank_of = {idx: pos + 1 for pos, (idx, _s) in enumerate(ranking)}
            entries = []
            for idx, tool in enumerate(tools):
                is_rel = _relevant(doc_sets[idx], intent_tokens)
                if is_rel:
                    relevant[idx] = True
                rank = rank_of.get(idx)
                score = dict(ranking).get(idx, 0.0) if ranking else 0.0
                if client == "opencode-2" and idx < OPENCODE2_PARTIAL:
                    verdict = "VISIBLE"
                elif rank is not None and rank <= TOP_K and score > 0:
                    # A zero score means no intent token matched at all:
                    # ranking first among zeros is not being "found".
                    verdict = "FOUND"
                elif is_rel:
                    verdict = "MISSED"
                    missed_any[idx] = True
                else:
                    verdict = "VISIBLE"
                if (rank is not None and score > 0
                        and (best_rank[idx] is None or rank < best_rank[idx])):
                    best_rank[idx] = rank
                entries.append({
                    "tool": names[idx], "rank": rank,
                    "score": round(score, 4), "verdict": verdict,
                    "relevant": is_rel,
                })
            per_intent.append({"intent": intent, "results": entries})
    elif client == "codex":
        cumulative = 0
        truncated = []
        for idx, tool in enumerate(tools):
            cost = tool_tokens(tool)
            before = cumulative
            cumulative += cost
            # Lost if it starts at/after the cutoff, or spills past it.
            lost = before >= codex_cutoff or cumulative > codex_cutoff
            truncated.append(lost)
        for intent in intents:
            entries = []
            for idx, tool in enumerate(tools):
                entries.append({
                    "tool": names[idx], "rank": None, "score": 0.0,
                    "verdict": "TRUNCATED-AWAY" if truncated[idx] else "VISIBLE",
                    "relevant": _relevant(doc_sets[idx], tokenize(intent)),
                })
            per_intent.append({"intent": intent, "results": entries})
    else:  # opencode-1, load-all: everything visible
        for intent in intents:
            entries = [{
                "tool": names[idx], "rank": None, "score": 0.0,
                "verdict": "VISIBLE",
                "relevant": _relevant(doc_sets[idx], tokenize(intent)),
            } for idx in range(len(tools))]
            per_intent.append({"intent": intent, "results": entries})

    per_tool = {}
    for idx, name in enumerate(names):
        if client == "codex":
            # Recompute from the truncation pass above via per_intent row 0,
            # or fall back to VISIBLE for an empty-intents run.
            if per_intent:
                per_tool[name] = per_intent[0]["results"][idx]["verdict"]
            else:
                cost_before = sum(tool_tokens(t) for t in tools[:idx])
                per_tool[name] = ("TRUNCATED-AWAY"
                                  if cost_before >= codex_cutoff else "VISIBLE")
        elif client in ("claude-code", "opencode-2"):
            if client == "opencode-2" and idx < OPENCODE2_PARTIAL:
                per_tool[name] = "VISIBLE"
            elif missed_any[idx]:
                per_tool[name] = "MISSED"
            elif best_rank[idx] is not None and best_rank[idx] <= TOP_K:
                per_tool[name] = "FOUND"
            elif relevant[idx]:
                per_tool[name] = "MISSED"
            else:
                per_tool[name] = "VISIBLE"
        else:
            per_tool[name] = "VISIBLE"

    if client == "opencode-2":
        visible_tokens = sum(tool_tokens(t) for t in tools[:OPENCODE2_PARTIAL])
    else:
        visible_tokens = catalog_tokens(tools)

    return {
        "client": client,
        "note": CLIENT_NOTES[client],
        "catalog_tokens": catalog_tokens(tools),
        "visible_tokens": visible_tokens,
        "tool_count": len(tools),
        "codex_cutoff": codex_cutoff if client == "codex" else None,
        "per_intent": per_intent,
        "per_tool": per_tool,
    }
