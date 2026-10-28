"""CLI: python -m mcp_toolfind audit --tools <json> --intents <txt> ...

Exit codes: 0 clean, 1 warnings only, 2 a tool was MISSED /
TRUNCATED-AWAY in some client, or a fail-closed input/probe error.
"""

import argparse
import json
import sys

from .clients import CLIENTS, DEFAULT_CODEX_CUTOFF
from .probe import ProbeError, probe_stdio
from .report import format_json, format_text, load_intents, load_tools, run_audit


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mcp-toolfind",
        description="Audit an MCP server's tool discoverability across "
                    "client models (simulations; see README).")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit", help="audit a tool catalog")
    audit.add_argument("--tools", help="tools/list JSON file (array of tools)")
    audit.add_argument("--intents", required=True,
                       help="intents file, one user phrase per line")
    audit.add_argument("--clients", default=",".join(CLIENTS),
                       help="comma-separated subset of: " + ",".join(CLIENTS))
    audit.add_argument("--format", dest="output_format", default="text",
                       choices=("text", "json"), help="report format")
    audit.add_argument("--probe-cmd",
                       help="stdio MCP server command to probe live; "
                            "its tools/list replaces --tools when given")
    audit.add_argument("--probe-timeout", type=int, default=10,
                       help="seconds to wait for each probe reply (default 10)")
    audit.add_argument("--codex-cutoff", type=int, default=DEFAULT_CODEX_CUTOFF,
                       help="codex model token cutoff (default %d, a model "
                            "parameter, not a measured limit)" % DEFAULT_CODEX_CUTOFF)
    audit.add_argument("--sample-output",
                       help="JSON file with a sample tool output to check "
                            "against per-client response limits")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "audit":
        parser.error("unknown command")
    clients = [c.strip() for c in args.clients.split(",") if c.strip()]
    unknown = [c for c in clients if c not in CLIENTS]
    if unknown:
        print("error: unknown client(s): %s (choose from %s)"
              % (", ".join(unknown), ", ".join(CLIENTS)), file=sys.stderr)
        return 2
    if not clients:
        print("error: --clients must name at least one client", file=sys.stderr)
        return 2
    try:
        intents = load_intents(args.intents)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if not intents:
        print("error: intents file %s contains no intents" % args.intents,
              file=sys.stderr)
        return 2
    try:
        if args.probe_cmd:
            tools = probe_stdio(args.probe_cmd, timeout=args.probe_timeout)
        elif args.tools:
            tools = load_tools(args.tools)
        else:
            print("error: --tools or --probe-cmd is required", file=sys.stderr)
            return 2
    except (ValueError, ProbeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    sample_data = None
    if args.sample_output:
        try:
            with open(args.sample_output, "r", encoding="utf-8") as handle:
                sample_data = json.load(handle)
        except (OSError, ValueError) as exc:
            print("error: cannot read sample output %s: %s"
                  % (args.sample_output, exc), file=sys.stderr)
            return 2
    audit = run_audit(tools, intents, clients=clients,
                      codex_cutoff=args.codex_cutoff,
                      sample_data=sample_data)
    if args.output_format == "json":
        sys.stdout.write(format_json(audit))
    else:
        sys.stdout.write(format_text(audit))
    return audit["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
