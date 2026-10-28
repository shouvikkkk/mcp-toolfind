"""Audit orchestration, loading, and report rendering.

Exit-code contract (documented in README and CLI help):
  0 -- every tool found/visible in every evaluated client, no warnings.
  1 -- warnings only (boilerplate, truncation loss, duplicates, huge
       catalog, sample output over a response limit, empty descriptions).
  2 -- at least one tool MISSED or TRUNCATED-AWAY in some client, or a
       fail-closed input/probe error (handled by the CLI before this).
"""

import json

from .checks import (check_boilerplate, check_sample_output,
                     check_truncation_loss, find_duplicates)
from .clients import CLIENTS, OPENCODE1_HUGE_TOKENS, evaluate_client
from .tokens import catalog_tokens


def load_tools(path):
    """Load a tools/list JSON file. Fail-closed on any malformed input."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ValueError("cannot read tools file %s: %s" % (path, exc))
    except ValueError as exc:
        raise ValueError("tools file %s is not valid JSON: %s" % (path, exc))
    if isinstance(data, dict) and isinstance(data.get("tools"), list):
        data = data["tools"]  # tolerate a full tools/list response envelope
    if not isinstance(data, list):
        raise ValueError("tools file %s must contain a JSON array of tools" % path)
    tools = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError("tools[%d] is not an object" % index)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tools[%d] has no non-empty string name" % index)
        description = entry.get("description", "")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise ValueError("tools[%d] description is not a string" % index)
        schema = entry.get("inputSchema", {})
        if schema is None:
            schema = {}
        if not isinstance(schema, dict):
            raise ValueError("tools[%d] inputSchema is not an object" % index)
        tools.append({"name": name, "description": description,
                      "inputSchema": schema})
    return tools


def load_intents(path):
    """Load one intent phrase per line; blank lines ignored."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise ValueError("cannot read intents file %s: %s" % (path, exc))
    intents = [line.strip() for line in lines
               if line.strip() and not line.strip().startswith("#")]
    return intents


def run_audit(tools, intents, clients=CLIENTS, codex_cutoff=60000,
              sample_data=None):
    """Run every requested client model plus all catalog checks."""
    clients = list(clients)
    client_results = [evaluate_client(c, tools, intents, codex_cutoff)
                      for c in clients]

    per_tool = {}
    missed = []
    truncated = []
    for result in client_results:
        for name, verdict in result["per_tool"].items():
            per_tool.setdefault(name, {})[result["client"]] = verdict
            if verdict == "MISSED":
                missed.append({"client": result["client"], "tool": name})
            elif verdict == "TRUNCATED-AWAY":
                truncated.append({"client": result["client"], "tool": name})

    truncation_loss = []
    boilerplate = []
    empty_description = []
    for tool in tools:
        lost = check_truncation_loss(tool)
        if lost:
            truncation_loss.append({"tool": tool["name"], "keywords": lost})
        if check_boilerplate(tool):
            boilerplate.append(tool["name"])
        if not tool["description"].strip():
            empty_description.append(tool["name"])
    duplicates = find_duplicates(tools)

    budgets = []
    for result in client_results:
        warning = None
        if result["client"] == "opencode-1" and result["catalog_tokens"] > OPENCODE1_HUGE_TOKENS:
            warning = ("catalog is %d estimated tokens; OpenCode 1.x loads "
                       "it all every request" % result["catalog_tokens"])
        budgets.append({
            "client": result["client"],
            "catalog_tokens": result["catalog_tokens"],
            "visible_tokens": result["visible_tokens"],
            "tool_count": result["tool_count"],
            "warning": warning,
        })

    sample = None
    if sample_data is not None:
        measured, rows = check_sample_output(sample_data)
        sample = {"measured": measured, "per_client": rows}

    warnings = []
    warnings.extend("truncation loss in %s: %s" % (row["tool"], ",".join(row["keywords"]))
                    for row in truncation_loss)
    warnings.extend("boilerplate description: %s" % name for name in boilerplate)
    warnings.extend("empty description: %s" % name for name in empty_description)
    warnings.extend("duplicate/near-duplicate: %s ~ %s" % (p["tool_a"], p["tool_b"])
                    for p in duplicates)
    warnings.extend(row["warning"] for row in budgets if row["warning"])
    if sample:
        warnings.extend(
            "sample output over %s limit (%s)" % (row["client"], ",".join(row["over_limit"]))
            for row in sample["per_client"] if row["over_limit"])

    if missed or truncated:
        exit_code = 2
    elif warnings:
        exit_code = 1
    else:
        exit_code = 0

    return {
        "tool_count": len(tools),
        "intent_count": len(intents),
        "catalog_tokens": catalog_tokens(tools),
        "clients": client_results,
        "per_tool": per_tool,
        "missed": missed,
        "truncated": truncated,
        "checks": {
            "truncation_loss": truncation_loss,
            "boilerplate": boilerplate,
            "empty_description": empty_description,
            "duplicates": duplicates,
        },
        "budgets": budgets,
        "sample": sample,
        "warnings": warnings,
        "exit_code": exit_code,
    }


def format_text(audit):
    """Human-readable report. All token figures are estimates."""
    lines = []
    lines.append("mcp-toolfind audit")
    lines.append("==================")
    lines.append("Tools: %d   Intents: %d   Catalog: ~%d estimated tokens"
                 % (audit["tool_count"], audit["intent_count"],
                    audit["catalog_tokens"]))
    lines.append("Token counts are a chars/4 heuristic, not tiktoken; "
                 "client models are simulations (see README).")
    lines.append("")
    lines.append("Catalog token budget (estimates)")
    lines.append("---------------------------------")
    for row in audit["budgets"]:
        line = ("  %-12s catalog ~%6d tokens, visible ~%6d tokens"
                % (row["client"], row["catalog_tokens"], row["visible_tokens"]))
        if row["warning"]:
            line += "  WARNING: " + row["warning"]
        lines.append(line)
    lines.append("")
    lines.append("Per-tool verdicts (FOUND / MISSED / TRUNCATED-AWAY / VISIBLE)")
    lines.append("--------------------------------------------------------------")
    header = "  %-28s %s" % ("tool", " ".join(
        "%-14s" % r["client"] for r in audit["clients"]))
    lines.append(header)
    for name, verdicts in audit["per_tool"].items():
        lines.append("  %-28s %s" % (name, " ".join(
            "%-14s" % verdicts.get(r["client"], "?") for r in audit["clients"])))
    if audit["missed"] or audit["truncated"]:
        lines.append("")
        lines.append("Failures")
        lines.append("--------")
        for row in audit["missed"]:
            lines.append("  MISSED %s in %s" % (row["tool"], row["client"]))
        for row in audit["truncated"]:
            lines.append("  TRUNCATED-AWAY %s in %s" % (row["tool"], row["client"]))
    lines.append("")
    lines.append("Per-intent search detail (search clients only)")
    lines.append("----------------------------------------------")
    for result in audit["clients"]:
        if result["client"] not in ("claude-code", "opencode-2"):
            continue
        lines.append("  [%s]" % result["client"])
        for intent_row in result["per_intent"]:
            ranked = sorted(
                (r for r in intent_row["results"]
                 if r["rank"] is not None and r["score"] > 0),
                key=lambda r: r["rank"])
            top = ", ".join("%s(#%d)" % (r["tool"], r["rank"])
                            for r in ranked[:5])
            missed_here = [r["tool"] for r in intent_row["results"]
                           if r["verdict"] == "MISSED"]
            line = "    %-28s top: %s" % (intent_row["intent"], top or "(none)")
            if missed_here:
                line += "   MISSED relevant: %s" % ", ".join(missed_here)
            lines.append(line)
    lines.append("")
    lines.append("Catalog checks")
    lines.append("--------------")
    checks = audit["checks"]
    if not any(checks.values()):
        lines.append("  (none)")
    for row in checks["truncation_loss"]:
        lines.append("  truncation loss %s: constraint keyword(s) %s appear "
                     "only after char 120" % (row["tool"], ",".join(row["keywords"])))
    for name in checks["boilerplate"]:
        lines.append("  boilerplate description: %s" % name)
    for name in checks["empty_description"]:
        lines.append("  empty description: %s" % name)
    for pair in checks["duplicates"]:
        lines.append("  duplicate/near-duplicate: %s ~ %s (%s)"
                     % (pair["tool_a"], pair["tool_b"], "; ".join(pair["reasons"])))
    if audit["sample"]:
        lines.append("")
        lines.append("Sample-output response budget")
        lines.append("-----------------------------")
        measured = audit["sample"]["measured"]
        lines.append("  measured: ~%d tokens, %d lines, %d bytes"
                     % (measured["tokens"], measured["lines"], measured["bytes"]))
        for row in audit["sample"]["per_client"]:
            status = ("OVER LIMIT: " + ",".join(row["over_limit"])) if row["over_limit"] else "ok"
            lines.append("  %-12s %s" % (row["client"], status))
    lines.append("")
    lines.append("Exit code: %d (%s)" % (
        audit["exit_code"],
        "failures found" if audit["exit_code"] == 2 else
        "warnings only" if audit["exit_code"] == 1 else "clean"))
    return "\n".join(lines) + "\n"


def format_json(audit):
    """Machine-readable report (same data as the text report)."""
    return json.dumps(audit, indent=2, sort_keys=False) + "\n"
