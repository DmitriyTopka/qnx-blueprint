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
  trace-loss       the trace is missing kernel buffers, so no verdict on it can be trusted
  priority-inversion  (experimental, opt-in) a client waited on a lower priority
                   server while threads in between used the CPU
"""

from __future__ import annotations

from dataclasses import dataclass, field
import bisect
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
    findings += check_trace_loss(ctx)
    findings += check_unexpected_edges(ctx)
    cycles, cycle_findings = check_wait_cycles(ctx)
    findings += cycle_findings
    findings += check_stuck_replies(ctx, cycles)
    findings += check_reply_budgets(ctx)
    findings += check_isolation(ctx)
    findings += check_priority_inversion(ctx)
    findings += check_traffic(ctx, findings)
    return findings, topo


def check_trace_loss(ctx: Context) -> List[Finding]:
    """Fails a trace that lost kernel buffers.

    A lost reply looks like a stuck server, a lost send hides an edge, so
    neither a pass nor the other findings can be trusted. The other checks
    still run, but the build fails with this reason first.
    """
    gaps = kev.lost_buffers(ctx.trace)
    if not gaps:
        return []
    t0 = ctx.trace.events[0].t_cycles if ctx.trace.events else 0
    missing = sum(b - a - 1 for _cpu, a, b, _t, _i in gaps if b > a)
    cpus = sorted({cpu for cpu, *_ in gaps})
    where = "; ".join(f"CPU {cpu}: buffer {a} then {b}, after {ctx.ms(t - t0) / 1000:.2f} s"
                      for cpu, a, b, t, _i in gaps[:4])
    return [Finding(
        "trace-loss", ERROR,
        f"the trace lost {missing or len(gaps)} kernel buffer(s) on CPU {', '.join(map(str, cpus))}",
        f"The buffer sequence jumps ({where}). Events in those buffers never reached the file: "
        f"a missing reply looks like a stuck server and a missing send hides an edge, so "
        f"neither a pass nor the other findings on this trace can be trusted. Record again "
        f"with more kernel buffers (tracelogger -k), a shorter run, or tracelogger at a "
        f"higher priority than the load (on -p).",
        [], [i for *_rest, i in gaps[:5]],
        {"gaps": [{"cpu": cpu, "after": a, "next": b} for cpu, a, b, _t, _i in gaps],
         "missing_buffers": missing},
    )]


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


# ---- priority inversion (experimental, on when the contract sets priority_inversion_ms)

def _running_segments(model: ipc.IpcModel, t_end: int) -> List[Tuple[int, int, int, int, int, int]]:
    """(start, end, pid, tid, priority, event index) for every stretch a thread ran on a CPU."""
    out = []
    current: Dict[int, Tuple[int, int, int, int, int]] = {}   # cpu -> (start, pid, tid, prio, index)
    for t, cpu, pid, tid, state, prio, index in model.sched:
        cur = current.get(cpu)
        if state == ipc.STATE_RUNNING:
            if cur is not None:
                out.append((cur[0], t, cur[1], cur[2], cur[3], cur[4]))
            current[cpu] = (t, pid, tid, prio, index)
        elif cur is not None and (cur[1], cur[2]) == (pid, tid):
            out.append((cur[0], t, cur[1], cur[2], cur[3], cur[4]))
            del current[cpu]
    for cur in current.values():
        out.append((cur[0], t_end, cur[1], cur[2], cur[3], cur[4]))
    out.sort()
    return out


def _thread_timeline(model: ipc.IpcModel) -> Dict[Tuple[int, int], List[Tuple[int, int, int, int]]]:
    """(pid, tid) -> [(time, state, priority, event index)] in trace order."""
    out: Dict[Tuple[int, int], List[Tuple[int, int, int, int]]] = {}
    for t, _cpu, pid, tid, state, prio, index in model.sched:
        out.setdefault((pid, tid), []).append((t, state, prio, index))
    return out


def _priority_at(timeline, t: int) -> Optional[int]:
    prio = None
    for when, _state, p, _index in timeline:
        if when > t:
            break
        prio = p
    return prio


def check_priority_inversion(ctx: Context) -> List[Finding]:
    """A client waits on a server that runs below the client's priority, and
    the server sits READY while threads with a priority in between use the CPU.

    QNX normally prevents this: a receiving thread takes the priority of the
    client. It happens when the channel was created with
    _NTO_CHF_FIXED_PRIORITY, or when the server waits on something that does
    not pass priority on.
    """
    limit = ctx.contract.priority_inversion_ms
    if limit is None or not ctx.model.sched:
        return []
    timelines = _thread_timeline(ctx.model)
    segments = _running_segments(ctx.model, ctx.t_end)
    starts = [s[0] for s in segments]

    held: Dict[Tuple[str, str], dict] = {}
    for tx in ctx.model.transactions:
        if tx.server_tid is None or tx.t_receive is None or not ctx.in_contract(tx):
            continue
        client_line = timelines.get((tx.client_pid, tx.client_tid))
        server_line = timelines.get((tx.server_pid, tx.server_tid))
        if not client_line or not server_line:
            continue
        pc = _priority_at(client_line, tx.t_send)
        if pc is None:
            continue
        t_stop = tx.t_reply if tx.t_reply is not None else ctx.t_end
        lost = 0
        culprits: Dict[Tuple[int, int, int], int] = {}
        first_events: List[int] = []
        server_prio = None
        for i, (t, state, ps, index) in enumerate(server_line):
            t_next = server_line[i + 1][0] if i + 1 < len(server_line) else ctx.t_end
            lo, hi = max(t, tx.t_receive), min(t_next, t_stop)
            if hi <= lo or state != ipc.STATE_READY or ps >= pc:
                continue
            server_prio = ps
            # Who ran while the server was ready but not running.
            covered: List[Tuple[int, int]] = []
            j = max(bisect.bisect_left(starts, lo) - 64, 0)
            while j < len(segments) and segments[j][0] < hi:
                s0, s1, pid, tid, pm, ev_index = segments[j]
                j += 1
                a, b = max(s0, lo), min(s1, hi)
                if b <= a or not (ps < pm < pc) or (pid, tid) == (tx.server_pid, tx.server_tid):
                    continue
                culprits[(pid, tid, pm)] = culprits.get((pid, tid, pm), 0) + (b - a)
                covered.append((a, b))
                if len(first_events) < 2:
                    first_events.append(ev_index)
            covered.sort()
            end = lo
            for a, b in covered:        # union: two CPUs can both be busy
                if b > end:
                    lost += b - max(a, end)
                    end = b
            if index not in first_events and len(first_events) < 3:
                first_events.insert(0, index)
        if not lost:
            continue
        key = (ctx.model.name(tx.client_pid), ctx.model.name(tx.server_pid))
        entry = held.setdefault(key, {"count": 0, "lost": 0, "worst": 0, "worst_tx": tx,
                                      "culprits": {}, "pc": pc, "ps": server_prio, "events": first_events})
        entry["count"] += 1
        entry["lost"] += lost
        if lost > entry["worst"]:
            entry.update(worst=lost, worst_tx=tx, events=first_events, pc=pc, ps=server_prio)
        for (pid, tid, pm), c in culprits.items():
            name = ctx.model.name(pid)
            entry["culprits"][(name, pm)] = entry["culprits"].get((name, pm), 0) + c

    out = []
    for (client, server), e in sorted(held.items()):
        worst = ctx.ms(e["worst"])
        if worst <= limit:
            continue
        tx = e["worst_tx"]
        channel = ctx.model.channels.get((tx.server_pid, tx.chid)) if tx.chid is not None else None
        fixed = bool(channel and channel.flags & ipc.CHF_FIXED_PRIORITY)
        culprits = sorted(e["culprits"].items(), key=lambda kv: -kv[1])
        who = ", ".join(f"{name} (priority {pm}) {ctx.ms(c):.1f} ms" for (name, pm), c in culprits[:3])
        why = (f"its channel was created with _NTO_CHF_FIXED_PRIORITY, so {server} keeps priority "
               f"{e['ps']} instead of taking {client}'s {e['pc']}" if fixed else
               f"{server} ran at priority {e['ps']} although {client} waited at {e['pc']}")
        events = [tx.send_event] + e["events"] + ([tx.reply_event] if tx.reply_event is not None else [])
        out.append(Finding(
            "priority-inversion", ERROR,
            f"{client} waits on {server} behind lower priority work",
            f"{e['count']} messages from {client} to {server} sat behind threads with a priority "
            f"between the two: {why}. Lost time per message up to {worst:.1f} ms "
            f"(limit {limit:g} ms), {ctx.ms(e['lost']):.1f} ms in total. CPU time taken meanwhile: {who}.",
            [client, server], events,
            {"messages": e["count"], "worst_ms": round(worst, 1), "total_ms": round(ctx.ms(e["lost"]), 1),
             "client_priority": e["pc"], "server_priority": e["ps"], "fixed_priority_channel": fixed,
             "culprits": [{"process": n, "priority": pm, "ms": round(ctx.ms(c), 1)}
                          for (n, pm), c in culprits]},
        ))
    return out
