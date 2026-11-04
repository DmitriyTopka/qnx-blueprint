"""Draft a contract from a recorded trace: `blueprint init run.kev`.

The draft writes down what the system did, not what it should do. Every
edge it lists is one the trace showed, with its message count, and every
reply budget is a suggestion from the slowest reply seen. Reading it and
deleting what you did not intend is the part that makes it a contract.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

from . import ipc, kev, topology

# QNX services and tools that are usually not part of the architecture you
# want to pin down. Matched against the process name.
SYSTEM = re.compile(
    r"^(procnto.*|devb-.*|devc-.*|devf-.*|devnp-.*|io-.*|pipe|slogger2?|random|mqueue|fsevmgr|"
    r"pci-server|dumper|sshd|sshd-.*|qconn|dhcpcd|inetd|ksh|sh|login|tracelogger|pidin|slay|"
    r"on|sleep|cat|ls|secpol.*|smmuman|acpi.*|pid\d+)$")


_PLAIN = re.compile(r"^[A-Za-z0-9_.+-]+$")
# What the contract accepts as a process name (contract.ARROW uses [\w.-]).
_NAME = re.compile(r"^[\w.-]+$")


def _q(text: str) -> str:
    """A YAML scalar for a name that came from the trace. Process names are
    written by the system being checked, so anything beyond a plain name is
    quoted (a JSON string is a valid YAML double-quoted scalar) and cannot
    add keys or list items to the contract."""
    return text if _PLAIN.match(text) else json.dumps(text)


def _nice(ms: float) -> float:
    """Round a budget up to 1, 2 or 5 times a power of ten."""
    if ms <= 0:
        return 1.0
    base = 10 ** math.floor(math.log10(ms))
    for step in (1, 2, 5, 10):
        if ms <= step * base:
            return float(step * base)
    return float(10 * base)


def select(model: ipc.IpcModel, only: Optional[Sequence[str]] = None) -> Tuple[List[str], List[str]]:
    """(processes to put in the contract, processes left out)."""
    talking = set()
    for tx in model.transactions:
        if tx.server_pid is None:
            continue
        talking.add(model.name(tx.client_pid))
        talking.add(model.name(tx.server_pid))
    if only:
        bad = [p for p in only if not _NAME.match(p)]
        if bad:
            raise ValueError(f"{bad[0]!r} cannot be a process name in a contract")
        return [p for p in only], sorted(talking - set(only))
    # A name the contract cannot express is left out (and reported) rather
    # than written into the draft.
    candidates = {p for p in talking if not SYSTEM.match(p) and _NAME.match(p)}
    # Keep only processes that exchange messages with another candidate.
    linked = set()
    for tx in model.transactions:
        if tx.server_pid is None:
            continue
        a, b = model.name(tx.client_pid), model.name(tx.server_pid)
        if a in candidates and b in candidates and a != b:
            linked |= {a, b}
    return sorted(linked), sorted(talking - linked)


def draft(trace: kev.Trace, model: ipc.IpcModel, processes: List[str], source: str = "",
          system: str = "my-system", headroom: float = 4.0) -> str:
    """Contract text for these processes, from what the trace shows."""
    topo = topology.build(model, processes)
    lat: Dict[str, List[float]] = {}
    for tx in model.transactions:
        if tx.t_reply is None or tx.status != "replied":
            continue
        client, server = model.name(tx.client_pid), model.name(tx.server_pid)
        if client in processes and server in processes and client != server:
            lat.setdefault(server, []).append((tx.t_reply - tx.t_send) / trace.cycles_per_sec * 1000)
    seconds = trace.seconds(trace.events[-1].t_cycles - trace.events[0].t_cycles) if trace.events else 0
    lines = [
        f"# Draft contract from {source or 'a trace'} ({seconds:.1f} s, "
        f"{len(topo.edges)} message paths), written by blueprint init.",
        "# It lists what the system did. Delete every edge you did not design, set the",
        f"# budgets you mean (these are {headroom:g}x the slowest reply in this one trace;",
        "# under emulation give more), then run: blueprint check <trace> -c <this file>",
        f"system: {_q(system)}",
        "processes: [" + ", ".join(_q(p) for p in processes) + "]",
        "allow:",
    ]
    for (client, server), e in sorted(topo.edges.items()):
        if client == server:
            continue
        extra = []
        if e.errors:
            extra.append(f"{e.errors} errors")
        if e.open:
            extra.append(f"{e.open} unanswered")
        note = f"{e.messages} messages" + (", " + ", ".join(extra) if extra else "")
        lines.append(f"  - {_q(f'{client} -> {server}')}".ljust(34) + f"# {note}")
    if not any(c != s for c, s in topo.edges):
        lines.append("  []  # the trace shows no messages between these processes")
    budgets = {server: _nice(max(v) * headroom) for server, v in lat.items() if v}
    if budgets:
        lines.append("reply_budget_ms:")
        for server in sorted(budgets):
            v = lat[server]
            lines.append(f"  {_q(server)}: {budgets[server]:g}".ljust(34)
                         + f"# slowest reply seen {max(v):.1f} ms, median {statistics.median(v):.2f} ms")
        lines.append(f"  default: {max(budgets.values()):g}")
    pairs = sorted((c, s) for c, s in topo.edges if c != s)
    client, server = pairs[0] if pairs else ("client", "server")
    lines += [
        "# isolation:                     # which process must keep working when another fails",
        f"#   {_q(server)}:",
        f"#     must_survive: [{_q(client)}]",
        "#     min_progress: 3",
        "# priority_inversion_ms: 2       # experimental, see the README",
    ]
    return "\n".join(lines) + "\n"

