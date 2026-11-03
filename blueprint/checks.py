"""Contract checks: compare what the trace shows with what the contract says.

Every finding points at trace events (index of the event in the .kev file),
so a report, a Perfetto view or an explanation can show the evidence.

Checks:
  unexpected-edge  a process sent messages to a server the contract does not allow
  wait-cycle       threads ended the trace waiting on each other in a loop (deadlock)
  stuck-reply      a server received a message and never replied
  reply-budget     a server answered, but slower than its budget
  isolation        a process failed (died or stopped answering) and took
                   another one down with it
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import contract as contract_mod
from . import ipc, kev, topology

ERROR = "error"
WARNING = "warning"


@dataclass
class Finding:
    check: str
    severity: str
    title: str
    detail: str
    processes: List[str]
    events: List[int] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Context:
    trace: kev.Trace
    model: ipc.IpcModel
    contract: contract_mod.Contract
    topo: topology.Topology
    t_end: int

    def ms(self, cycles: int) -> float:
        return cycles / self.trace.cycles_per_sec * 1000.0 if self.trace.cycles_per_sec else 0.0

    def in_contract(self, tx: ipc.Transaction) -> bool:
        names = self.contract.processes
        return self.model.name(tx.client_pid) in names and self.model.name(tx.server_pid) in names


def run(trace: kev.Trace, contract: contract_mod.Contract,
        snap=None) -> Tuple[List[Finding], topology.Topology]:
    model = ipc.build(trace)
    topo = topology.build(model, contract.processes, snap)
    t_end = trace.events[-1].t_cycles if trace.events else 0
    ctx = Context(trace, model, contract, topo, t_end)
    findings: List[Finding] = []
    findings += check_unexpected_edges(ctx)
    cycles, cycle_findings = check_wait_cycles(ctx)
    findings += cycle_findings
    findings += check_stuck_replies(ctx, cycles)
    findings += check_reply_budgets(ctx)
    findings += check_isolation(ctx)
    findings += check_traffic(ctx, findings)
    return findings, topo


def check_unexpected_edges(ctx: Context) -> List[Finding]:
    out = []
    for (client, server), edge in sorted(ctx.topo.edges.items()):
        if (client, server) in ctx.contract.allow:
            continue
        out.append(Finding(
            "unexpected-edge", ERROR,
            f"{client} talks to {server}, which the contract does not allow",
            f"{edge.messages} message{'s' if edge.messages != 1 else ''} from {client} to {server} on chid "
            f"{','.join(map(str, sorted(edge.chids))) or '?'}. Allowed servers for {client}: "
            f"{', '.join(sorted(s for c, s in ctx.contract.allow if c == client)) or 'none'}.",
            [client, server], [edge.first_event] if edge.first_event is not None else [],
            {"messages": edge.messages},
        ))
    return out


def _find_cycles(graph: Dict[str, Set[str]]) -> List[List[str]]:
    cycles: List[List[str]] = []
    seen: Set[frozenset] = set()

    def walk(node: str, path: List[str]) -> None:
        for nxt in sorted(graph.get(node, ())):
            if nxt in path:
                cycle = path[path.index(nxt):]
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cycle)
            else:
                walk(nxt, path + [nxt])

    for start in sorted(graph):
        walk(start, [start])
    return cycles


def check_wait_cycles(ctx: Context) -> Tuple[List[List[str]], List[Finding]]:
    graph: Dict[str, Set[str]] = {}
    for w in ctx.topo.waiters:
        graph.setdefault(w.process, set()).add(w.server)
    cycles = _find_cycles(graph)
    out = []
    for cycle in cycles:
        members = set(cycle)
        waits = [w for w in ctx.topo.waiters if w.process in members and w.server in members]
        victims = sorted({w.process for w in ctx.topo.waiters
                          if w.server in members and w.process not in members})
        chain = " -> ".join(cycle + [cycle[0]])
        steps = "; ".join(f"{w.process} is {w.state}-blocked on {w.server}" for w in waits)
        detail = f"Wait-for loop {chain}: {steps}. Nobody in the loop can ever reply."
        if victims:
            detail += f" Also stuck behind it: {', '.join(victims)}."
        out.append(Finding(
            "wait-cycle", ERROR, f"deadlock: {chain}", detail,
            cycle + victims, sorted(w.send_event for w in waits if w.send_event is not None),
            {"cycle": cycle, "victims": victims, "from_snapshot": _snapshot_lines(waits)},
        ))
    return cycles, out


def check_stuck_replies(ctx: Context, cycles: List[List[str]]) -> List[Finding]:
    in_cycle = {p for c in cycles for p in c}
    out = []
    for w in ctx.topo.waiters:
        if w.state != "REPLY" or w.process in in_cycle or w.server in in_cycle:
            continue
        if w.server_pid in ctx.model.deaths:
            continue  # reported by the isolation check
        tx = _open_tx(ctx, w) if w.send_event is not None else None
        t_start = tx.t_send if tx else ctx.trace.events[0].t_cycles if ctx.trace.events else ctx.t_end
        waited = ctx.ms(ctx.t_end - t_start)   # a snapshot wait lasted at least the whole trace
        budget = ctx.contract.budget_for(w.server)
        if budget is not None and waited <= budget:
            continue  # still within budget when the trace stopped
        slowest = _slowest_reply(ctx, w.pid, w.server_pid)
        if slowest is not None and waited < 5 * slowest:
            continue  # a slow server cut off by the end of the trace, not a stuck one
        server_blocked = any(o.process == w.server for o in ctx.topo.waiters)
        # If the contract plans for this server failing, the hang itself is a
        # known failure mode; whether it hurt anyone is the isolation check's call.
        planned = w.server in ctx.contract.isolation
        out.append(Finding(
            "stuck-reply", WARNING if planned else ERROR,
            f"{w.server} took a message from {w.process} and never replied",
            f"{w.process} thread {w.tid} has been REPLY-blocked on {w.server} for "
            + (f"{waited:.0f} ms" if w.source == "trace" else f"the whole trace ({waited:.0f} ms) and was already blocked when it started")
            + f" (budget {budget if budget is not None else 'none'} ms). "
            + ("The server is itself blocked." if server_blocked else
               "The server is not blocked on anyone, so it is stuck in its own code.")
            + (" The contract plans for this server failing, see the isolation check." if planned else ""),
            [w.process, w.server], [w.send_event] if w.send_event is not None else [],
            {"waited_ms": round(waited, 1), "tid": w.tid, "from_snapshot": _snapshot_lines([w])},
        ))
    return out


def _slowest_reply(ctx: Context, client_pid: int, server_pid: Optional[int]) -> Optional[float]:
    times = [ctx.ms(tx.t_reply - tx.t_send) for tx in ctx.model.transactions
             if tx.client_pid == client_pid and tx.server_pid == server_pid
             and tx.status == "replied" and tx.t_reply is not None]
    return max(times) if times else None


def _snapshot_lines(waits) -> List[str]:
    return [f"pidin: {w.process} pid {w.pid} tid {w.tid} {w.state}-blocked on {w.server} pid {w.server_pid}"
            for w in waits if w.source == "snapshot"]


def check_traffic(ctx: Context, findings: List[Finding]) -> List[Finding]:
    """A trace with no traffic between contract processes proves nothing."""
    done = sum(1 for tx in ctx.model.transactions if tx.status == "replied" and ctx.in_contract(tx))
    if done:
        return []
    if any(f.severity == ERROR for f in findings):
        return []  # another error already explains the silence
    return [Finding(
        "no-traffic", ERROR,
        "no completed messages between the contract's processes",
        "The trace contains no answered message between any two processes of the contract, "
        "so it cannot show the architecture working. Either the system is stuck (take a pidin "
        "snapshot with the trace to see who is blocked on whom) or the trace missed it.",
        list(ctx.contract.processes), [], {"completed": 0},
    )]


def _open_tx(ctx: Context, w: topology.Waiter) -> Optional[ipc.Transaction]:
    for tx in reversed(ctx.model.transactions):
        if tx.send_event == w.send_event:
            return tx
    return None


def check_reply_budgets(ctx: Context) -> List[Finding]:
    slow: Dict[Tuple[str, str], List[Tuple[float, ipc.Transaction]]] = {}
    for tx in ctx.model.transactions:
        if tx.t_reply is None or tx.status != "replied" or not ctx.in_contract(tx):
            continue
        server = ctx.model.name(tx.server_pid)
        budget = ctx.contract.budget_for(server)
        took = ctx.ms(tx.t_reply - tx.t_send)
        if budget is not None and took > budget:
            slow.setdefault((ctx.model.name(tx.client_pid), server), []).append((took, tx))
    out = []
    for (client, server), items in sorted(slow.items()):
        items.sort(key=lambda x: -x[0])
        total = ctx.topo.edges[(client, server)].replied if (client, server) in ctx.topo.edges else len(items)
        worst = items[0][0]
        budget = ctx.contract.budget_for(server)
        out.append(Finding(
            "reply-budget", ERROR,
            f"{server} answers {client} too slowly",
            f"{len(items)} of {total} replies from {server} to {client} took longer than "
            f"{budget:g} ms; worst {worst:.1f} ms. Times come from the trace clock; under "
            f"emulation they are inflated, so budgets should be generous.",
            [client, server], [tx.send_event for _, tx in items[:3]],
            {"over_budget": len(items), "replies": total, "worst_ms": round(worst, 1),
             "budget_ms": budget},
        ))
    return out


def check_isolation(ctx: Context) -> List[Finding]:
    out = []
    pids_by_name: Dict[str, List[int]] = {}
    for pid, name in ctx.model.names.items():
        pids_by_name.setdefault(name, []).append(pid)

    for name, rule in sorted(ctx.contract.isolation.items()):
        fault_t, fault_kind, fault_event = _fault(ctx, name, pids_by_name.get(name, []))
        if fault_t is None:
            continue
        for survivor in rule.must_survive:
            pids = pids_by_name.get(survivor, [])
            died = [p for p in pids if p in ctx.model.deaths and ctx.model.deaths[p] >= fault_t]
            progress = sum(
                1 for tx in ctx.model.transactions
                if tx.t_send >= fault_t and tx.status == "replied" and ctx.in_contract(tx)
                and (tx.client_pid in pids) and ctx.model.name(tx.server_pid) != name
            )
            blocked = [w for w in ctx.topo.waiters if w.process == survivor and w.server == name]
            if progress >= rule.min_progress:
                continue  # kept working after the fault (a later normal exit is fine)
            if died:
                after = ctx.ms(ctx.model.deaths[died[0]] - fault_t)
                what = f"{survivor} died {after:.0f} ms later"
                events = [fault_event, ctx.model.death_events[died[0]]]
            else:
                what = f"{survivor} completed only {progress} messages afterwards"
                if blocked:
                    what += f" and its thread {blocked[0].tid} is still {blocked[0].state}-blocked on {name}"
                events = [fault_event] + [b.send_event for b in blocked]
            out.append(Finding(
                "isolation", ERROR,
                f"{name} {fault_kind} and took {survivor} down with it",
                f"The contract says {survivor} must keep working when {name} fails "
                f"(at least {rule.min_progress} completed messages). {name} {fault_kind}; {what}.",
                [name, survivor], sorted({e for e in events if e is not None}),
                {"fault": fault_kind, "progress": progress, "survivor_died": bool(died)},
            ))
    return out


def _fault(ctx: Context, name: str, pids: List[int]) -> Tuple[Optional[int], str, Optional[int]]:
    """When did `name` fail? Death, or the oldest message it never answered."""
    deaths = [(ctx.model.deaths[p], ctx.model.death_events[p]) for p in pids if p in ctx.model.deaths]
    if deaths:
        t, ev = min(deaths)
        return t, "died", ev
    for w in ctx.topo.waiters:
        if w.server == name and w.source == "snapshot" and ctx.trace.events:
            return ctx.trace.events[0].t_cycles, "stopped answering before the trace started", None
    budget = ctx.contract.budget_for(name)
    for tx in ctx.model.transactions:
        if tx.server_pid in pids and tx.status in ("sent", "received"):
            if budget is None or ctx.ms(ctx.t_end - tx.t_send) > budget:
                return tx.t_send, "stopped answering", tx.send_event
    return None, "", None
