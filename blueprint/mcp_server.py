"""Blueprint as an MCP server, so a coding assistant can run the checks itself.

    pip install "qnx-blueprint[mcp] @ git+https://github.com/DmitriyTopka/qnx-blueprint"
    blueprint mcp                      # stdio; add it to your assistant's MCP config

The tools read local files only. Recording a trace stays a command you run
yourself (`blueprint record`), because it talks to a target over ssh and can
kill processes there.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import yaml

from . import api

DEMO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo", "cockpit")
if not os.path.isdir(DEMO):                      # running from a clone
    DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "cockpit")


def build():
    try:
        from mcp.server import MCPServer
    except ImportError as e:
        raise SystemExit("blueprint mcp needs the MCP SDK: pip install 'qnx-blueprint[mcp]' "
                         "(or pip install 'mcp>=2.3')") from e

    server = MCPServer("blueprint", instructions=(
        "Blueprint checks a QNX 8.0 kernel trace (.kev from tracelogger -w) against an architecture "
        "contract (YAML: processes, allowed message edges, reply budgets, real-time cycles, isolation). "
        "Use check_trace after the user records a trace; each finding names the processes, the trace "
        "events behind it and, for slow replies and missed cycles, where the time went. Use "
        "draft_contract when there is no contract yet, and write_policy / check_policy for QNX "
        "security policies. list_demo_traces gives recorded traces to try without a target."))

    @server.tool()
    def check_trace(trace: str, contract: str, snapshot: str = "", policy: str = "") -> Dict[str, Any]:
        """Check a QNX kernel trace against an architecture contract.

        trace: path to a .kev (or .kev.gz) file from `tracelogger -w`.
        contract: path to the YAML contract.
        snapshot: optional pidin output taken when the trace stopped (default: <trace>.pidin if present).
        policy: optional QNX security policy (.secpol) to check against the contract too.
        Returns result (pass or fail), findings with check, title, detail, processes, trace event ids
        and data (data.where and data.breakdown say where a slow reply's or a missed cycle's time went),
        and the message topology the trace showed."""
        return api.check(trace, contract, snapshot, policy)

    @server.tool()
    def draft_contract(trace: str, only: Optional[List[str]] = None, include_system: bool = False) -> str:
        """Draft a contract from a trace of a system that already runs (`blueprint init`).

        Lists the application processes, every message edge the trace showed and reply budgets at
        four times the slowest reply seen. Review it with the user: every edge in the draft is one the
        system made, including ones nobody designed. only: process names to keep."""
        return api.draft_contract(trace, only, include_system)

    @server.tool()
    def write_policy(contract: str, attach: Optional[Dict[str, List[str]]] = None) -> str:
        """Write QNX security policy rules from the contract: one type per process and one
        `channel connect` rule per allowed edge. attach maps a server to the names it attaches,
        for example {"gps": ["/dev/name/local/cockpit-gps"]}. Compile the result with secpolcompile."""
        return api.write_policy(contract, attach)

    @server.tool()
    def check_policy(policy: str, contract: str) -> Dict[str, Any]:
        """Check a security policy (for example one learned with secpolgenerate) against the contract:
        fails for every connection the policy allows and the contract forbids."""
        return api.check_policy(policy, contract)

    @server.tool()
    def list_demo_traces() -> Dict[str, Any]:
        """Recorded QNX 8.0 traces of the demo cockpit, with the contract and what each one shows.
        Pass a trace and the contract to check_trace to see each kind of finding."""
        with open(os.path.join(DEMO, "scenarios.yaml"), encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        return {
            "contract": os.path.join(DEMO, spec["contract"]),
            "realtime_contract": os.path.join(DEMO, "realtime.yaml"),
            "traces": [{"trace": os.path.join(DEMO, "traces", sc["name"] + ".kev"), "story": sc["story"],
                        "expect": sc["expect"], "checks": sc.get("checks", [])} for sc in spec["scenarios"]],
        }

    return server


def main() -> int:
    build().run()
    return 0
