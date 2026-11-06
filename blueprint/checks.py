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
  trace-mode       the trace was recorded without -w (a warning; an error if the
                   contract asks for the priority inversion check)
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


def counts(errors: int, warnings: int) -> str:
    """'1 error, 2 warnings'."""
    return (f"{errors} error{'' if errors == 1 else 's'}, "
            f"{warnings} warning{'' if warnings == 1 else 's'}")


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
    snap: object = None

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
    ctx = Context(trace, model, contract, topo, t_end, snap)
    findings: List[Finding] = []
    findings += check_trace_loss(ctx)
    findings += check_trace_mode(ctx)
    findings += check_unexpected_edges(ctx)
    findings += check_kernel_denied(ctx)
    cycles, cycle_findings = check_wait_cycles(ctx)
    findings += cycle_findings
    findings += check_stuck_replies(ctx, cycles)
    findings += check_reply_budgets(ctx)
    findings += check_cycles(ctx)
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


def check_trace_mode(ctx: Context) -> List[Finding]:
    """A trace recorded without tracelogger -w.

    Without wide mode thread state events carry no priority and messages no
    payload. Edges, replies and blocking still come through, but the priority
    inversion check would see nothing, so it must not quietly pass.
    """
    if ctx.model.sched or not ctx.model.states:
        return []
    args = ctx.trace.attributes.get("TRACELOGGER_ARGS", "")
    how = f" ({args})" if args else ""
    if ctx.contract.priority_inversion_ms is not None:
        return [Finding(
            "trace-mode", ERROR,
            "the trace was not recorded in wide mode, so priority inversion cannot be checked",
            f"The contract sets priority_inversion_ms, but this trace{how} has no thread "
            f"priorities: tracelogger records them only with -w. Record again with "
            f"tracelogger -w.", [], [], {"tracelogger": args})]
    return [Finding(
        "trace-mode", WARNING,
        "the trace was not recorded in wide mode",
        f"This trace{how} has no thread priorities or message contents, which tracelogger "
        f"records only with -w. Edges, replies and blocking are checked as usual; "
        f"explanations get less evidence. Record with tracelogger -w for the full picture.",
        [], [], {"tracelogger": args})]


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
        stopped = _stopped(ctx, w.server_pid, tx.server_tid if tx else None)
        if not stopped and tx is not None and waits_for_data(ctx, tx):
            continue  # a read parked in the server until data comes: cycle_ms checks that it comes
        holder = _last_state(ctx, w.server_pid, tx.server_tid) if tx and tx.server_tid is not None else None
        # If the contract plans for this server failing, the hang itself is a
        # known failure mode; whether it hurt anyone is the isolation check's call.
        planned = w.server in ctx.contract.isolation
        out.append(Finding(
            "stuck-reply", WARNING if planned else ERROR,
            f"{w.server} took a message from {w.process} and never replied",
            f"{w.process} thread {w.tid} has been REPLY-blocked on {w.server} for "
            + (f"{waited:.0f} ms" if w.source == "trace" else f"the whole trace ({waited:.0f} ms) and was already blocked when it started")
            + f" (budget {budget if budget is not None else 'none'} ms). "
            + (stopped[0] if stopped else
               "The server is itself blocked." if server_blocked else
               "The server is not blocked on anyone, so it is stuck in its own code."
               if holder in (ipc.STATE_RUNNING, ipc.STATE_READY) else
               f"Server thread {tx.server_tid}, which holds the message, is in "
               f"{ipc.STATE_NAMES.get(holder, holder)} and not blocked on another server."
               if holder is not None else
               "The server is not blocked on another server; the trace does not show what it is doing.")
            + (" The contract plans for this server failing, see the isolation check." if planned else ""),
            [w.process, w.server],
            ([w.send_event] if w.send_event is not None else []) + ([stopped[1]] if stopped and stopped[1] is not None else []),
            {"waited_ms": round(waited, 1), "tid": w.tid, "from_snapshot": _snapshot_lines([w]),
             "server_stopped": bool(stopped)},
        ))
    return out


def _stopped(ctx: Context, server_pid: Optional[int], server_tid: Optional[int]):
    """(sentence, event index) when a thread of the server is STOPPED (SIGSTOP,
    a debugger) at the end of the trace, else None. A stopped thread keeps a
    received message and cannot reply until it is continued."""
    if server_pid is None:
        return None
    last = {}
    for t, cpu, pid, tid, state, prio, idx in ctx.model.sched:
        if pid == server_pid:
            last[tid] = (t, state, idx)
    for t, pid, tid, state in ctx.model.states:
        if pid == server_pid and (tid not in last or last[tid][0] < t):
            last[tid] = (t, state, None)
    hits = [(tid, t, idx) for tid, (t, state, idx) in last.items() if state == ipc.STATE_STOPPED]
    if hits:
        hits.sort(key=lambda h: (h[0] != server_tid, h[1]))
        tid, t, idx = hits[0]
        t0 = ctx.trace.events[0].t_cycles if ctx.trace.events else t
        when = (f"was already STOPPED when the trace started" if ctx.ms(t - t0) < 1.0
                else f"was STOPPED at {ctx.ms(t - t0):.0f} ms into the trace")
        if server_tid is None or tid == server_tid:
            return (f"Server thread {tid} {when} (a signal such as SIGSTOP, or a debugger) and stayed "
                    f"stopped, so it cannot reply until it is continued.", idx)
        # A stop reaches a blocked thread only when it wakes, so the thread
        # that holds this message may still show its blocked state.
        held = last.get(server_tid)
        held_state = ipc.STATE_NAMES.get(held[1], held[1]) if held else "an unknown state"
        return (f"The server process is stopped: its thread {tid} {when} (a signal such as SIGSTOP, "
                f"or a debugger). Thread {server_tid}, which holds this message, shows "
                f"{held_state} and cannot reply until the process is continued.", idx)
    snap = ctx.snap
    if snap is not None:
        for th in getattr(snap, "threads", []):
            if th.pid == server_pid and th.state == "STOPPED":
                return (f"pidin shows server thread {th.tid} STOPPED (a signal such as SIGSTOP, or a "
                        f"debugger), so it cannot reply until it is continued.", None)
    return None


def _last_state(ctx: Context, pid: Optional[int], tid: Optional[int]) -> Optional[int]:
    state = None
    for t, p, th, st in ctx.model.states:
        if p == pid and th == tid:
            state = st
    return state


# A read() on a socket, device or pipe reaches the server as _IO_READ and is
# answered when data comes. While it waits, the thread that took it sits in
# one of these states (io-sock: CONDVAR) or goes back to MsgReceive.
READ_TYPES = {0x101, 0x11E}
WAIT_STATES = {ipc.STATE_RECEIVE, 11, 14, 16, 17}  # RECEIVE, SIGWAITINFO, CONDVAR, INTR, SEM


def waits_for_data(ctx: Context, tx: ipc.Transaction) -> bool:
    """True when a read spent nearly all of its time parked in the server
    waiting for data, not being served: the client waiting for input, which
    is not the server's latency. cycle_ms is the check for data that must
    come in time."""
    if tx.msg_type not in READ_TYPES or tx.server_tid is None or tx.t_receive is None:
        return False
    t1 = tx.t_reply if tx.t_reply is not None else ctx.t_end
    if t1 <= tx.t_send:
        return False
    key = (tx.server_pid, tx.server_tid)
    state, parked, t = None, 0, tx.t_receive
    for ts, pid, tid, st in ctx.model.states:
        if (pid, tid) != key:
            continue
        if ts <= tx.t_receive:
            state = st
            continue
        if ts >= t1:
            break
        if state in WAIT_STATES:
            parked += ts - t
        state, t = st, ts
    if state in WAIT_STATES:
        parked += t1 - t
    return parked >= 0.9 * (t1 - tx.t_send)


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


EPERM = 1


def check_kernel_denied(ctx: Context) -> List[Finding]:
    """Connections the kernel refused with EPERM: a security policy at work.

    A refused edge the contract forbids means the policy did its job, and the
    build still tries the forbidden path, which is a bug to remove before it
    ships to a system without the policy. A refused edge the contract allows
    means the policy and the contract disagree, or the process was started
    without its type (on -T), and the system cannot work as designed.
    """
    names = set(ctx.contract.processes)
    groups: Dict[Tuple[str, str], List[tuple]] = {}
    for r in ctx.model.refused:
        t, pid, tid, server_pid, chid, errno, index = r
        if errno != EPERM:
            continue
        client, server = ctx.model.name(pid), ctx.model.name(server_pid)
        if client not in names:
            continue
        groups.setdefault((client, server), []).append(r)
    out = []
    for (client, server), items in sorted(groups.items()):
        events = [r[6] for r in items[:3]]
        data = {"refused": len(items), "errno": "EPERM"}
        if server in names and (client, server) not in ctx.contract.allow:
            out.append(Finding(
                "kernel-denied", ERROR,
                f"the kernel refused {client}'s connection to {server}, which the contract forbids",
                f"ConnectAttach from {client} to {server} failed {len(items)} time(s) with EPERM: "
                f"the security policy blocked the path the contract leaves out. The build still "
                f"tries it, so without the policy it would work. Remove the call from {client}.",
                [client, server], events, data))
        elif server in names:
            out.append(Finding(
                "kernel-denied", ERROR,
                f"the kernel refuses {client} -> {server}, which the contract allows",
                f"ConnectAttach from {client} to {server} failed {len(items)} time(s) with EPERM. "
                f"Either {client} was started without its type (on -T {client}_t), or the "
                f"policy has no channel connect rule for this edge. Regenerate it with "
                f"blueprint secpol, or start {client} with its type.",
                [client, server], events, data))
        else:
            out.append(Finding(
                "kernel-denied", WARNING,
                f"the kernel refused {client}'s connection to {server}",
                f"ConnectAttach from {client} to {server}, outside the contract, failed "
                f"{len(items)} time(s) with EPERM.",
                [client, server], events, data))
    return out


def check_reply_budgets(ctx: Context) -> List[Finding]:
    slow: Dict[Tuple[str, str], List[Tuple[float, ipc.Transaction]]] = {}
    for tx in ctx.model.transactions:
        if tx.t_reply is None or tx.status != "replied" or not ctx.in_contract(tx):
            continue
        server = ctx.model.name(tx.server_pid)
        budget = ctx.contract.budget_for(server)
        took = ctx.ms(tx.t_reply - tx.t_send)
        if budget is not None and took > budget and not waits_for_data(ctx, tx):
            slow.setdefault((ctx.model.name(tx.client_pid), server), []).append((took, tx))
    out = []
    for (client, server), items in sorted(slow.items()):
        items.sort(key=lambda x: -x[0])
        total = ctx.topo.edges[(client, server)].replied if (client, server) in ctx.topo.edges else len(items)
        worst, worst_tx = items[0]
        budget = ctx.contract.budget_for(server)
        parts = breakdown(ctx, worst_tx)
        where = describe(parts)
        out.append(Finding(
            "reply-budget", ERROR,
            f"{server} answers {client} too slowly",
            f"{len(items)} of {total} replies from {server} to {client} took longer than "
            f"{budget:g} ms; worst {worst:.1f} ms. Times come from the trace clock; under "
            f"emulation they are inflated, so budgets should be generous.",
            [client, server], [tx.send_event for _, tx in items[:3]],
            {"over_budget": len(items), "replies": total, "worst_ms": round(worst, 1),
             "budget_ms": budget, "breakdown": parts, "where": f"worst {worst:.1f} ms: {where}"},
        ))
    return out


def check_cycles(ctx: Context) -> List[Finding]:
    """A real-time loop misses its period.

    `cycle_ms: {display -> nav: 100}` says display must get an answer from nav
    at least every 100 ms. The gaps are measured between consecutive replies
    and, while the client is still alive, from the last reply to the end of
    the trace: a loop that stopped is the worst overrun of all. The overrun
    is explained with the breakdown of the reply that closed the gap, or of
    the message still open at the end.
    """
    out = []
    pids_by_name: Dict[str, List[int]] = {}
    for pid, name in ctx.model.names.items():
        pids_by_name.setdefault(name, []).append(pid)
    for (client, server), period in sorted(ctx.contract.cycle_ms.items()):
        cpids, spids = pids_by_name.get(client, []), pids_by_name.get(server, [])
        txs = sorted((tx for tx in ctx.model.transactions
                      if tx.client_pid in cpids and tx.server_pid in spids and tx.t_reply is not None
                      and tx.status == "replied"), key=lambda tx: tx.t_reply)
        if not txs:
            continue            # no-traffic and the other checks speak for a loop that never ran
        misses: List[Tuple[float, Optional[ipc.Transaction], Optional[ipc.Transaction]]] = []
        for prev, tx in zip(txs, txs[1:]):
            gap = ctx.ms(tx.t_reply - prev.t_reply)
            if gap > period:
                misses.append((gap, prev, tx))
        last = txs[-1]
        loop_thread = (last.client_pid, last.client_tid)
        alive = last.client_pid not in ctx.model.deaths and ctx.model.thread_state.get(loop_thread) not in (
            ipc.STATE_DEAD, ipc.STATE_DESTROY, None)
        tail = ctx.ms(ctx.t_end - last.t_reply)
        if alive and tail > period:
            misses.append((tail, last, None))
        if not misses:
            continue
        misses.sort(key=lambda m: -m[0])
        worst, prev, closing = misses[0]
        stopped = closing is None
        data = {"period_ms": period, "misses": len(misses), "cycles": len(txs),
                "worst_gap_ms": round(worst, 1), "stopped": stopped}
        events = [prev.reply_event] if prev.reply_event is not None else []
        t_stop = closing.t_reply if closing is not None else ctx.t_end
        parts = walk(ctx, (prev.client_pid, prev.client_tid), prev.t_reply, t_stop, "")
        data["breakdown"], data["worst_ms"] = parts, round(worst, 1)
        data["where"] = f"in the longest gap ({worst:.1f} ms) {client} spent: {describe(parts)}"
        if closing is not None and closing.reply_event is not None:
            events.append(closing.reply_event)
        what = "stopped: no answer for the last " if stopped else "missed its period: worst gap "
        out.append(Finding(
            "cycle-overrun", ERROR,
            f"{client} -> {server} cycle {what}{worst:.0f} ms against {period:g} ms",
            f"{client} must get an answer from {server} at least every {period:g} ms. "
            f"{len(misses)} gap(s) in {len(txs)} replies were longer; the worst was {worst:.1f} ms."
            + (f" The loop was still waiting when the trace ended." if stopped else ""),
            [client, server], events, data))
    return out


def breakdown(ctx: Context, tx: ipc.Transaction, depth: int = 1) -> List[dict]:
    """Split the time of one answered message into where it went.

    Follows the server thread that received the message from the send to the
    reply. Before the receive the message waits: behind another client's
    message, or while the server is preempted or doing something else. After
    it, the server is running, preempted (by whom, at what priority), blocked
    in its own call to another server (which one), sleeping, or in another
    blocked state. The parts add up to the whole reply time, largest first.
    """
    if tx.server_tid is None or tx.t_receive is None or tx.t_reply is None:
        return []
    return walk(ctx, (tx.server_pid, tx.server_tid), tx.t_send, tx.t_reply, "server", tx, depth)


def walk(ctx: Context, key: Tuple[int, int], t0: int, t1: int, role: str,
         queued: Optional[ipc.Transaction] = None, depth: int = 1) -> List[dict]:
    """Where one thread's time went between t0 and t1, from its state changes.

    `queued` is a message the thread had not received yet at t0: until its
    receive, time the thread spent on anything but being preempted counts as
    the message waiting in the queue.
    """
    parts: Dict[str, dict] = {}
    culprits: Dict[str, Dict[str, List[Tuple[int, int]]]] = {}
    inner_of: Dict[str, ipc.Transaction] = {}

    def add(what: str, cycles: int, kind: str, event: Optional[int] = None) -> None:
        if cycles <= 0:
            return
        p = parts.setdefault(what, {"what": what, "kind": kind, "cycles": 0, "events": []})
        p["cycles"] += cycles
        if event is not None and len(p["events"]) < 3:
            p["events"].append(event)

    line = [(t, st) for t, pid, tid, st in ctx.model.states if (pid, tid) == key]
    state = ipc.STATE_RECEIVE if queued is not None else ipc.STATE_RUNNING
    for t, st in line:
        if t > t0:
            break
        state = st
    marks = [(t0, state)] + [(t, st) for t, st in line if t0 < t < t1]
    t_recv = queued.t_receive if queued is not None else t0
    if queued is not None and t_recv not in [t for t, _ in marks]:
        marks.append((t_recv, None))     # split at the receive
        marks.sort(key=lambda m: m[0])
    segments = _running_segments(ctx.model, ctx.t_end) if ctx.model.sched else []
    others = [o for o in ctx.model.transactions
              if queued is not None and o is not queued and o.server_pid == key[0]
              and o.t_receive is not None and o.t_receive < t_recv and (o.t_reply or ctx.t_end) > t0]
    calls = [o for o in ctx.model.transactions
             if (o.client_pid, o.client_tid) == key and o.t_send < t1 and (o.t_reply or ctx.t_end) > t0]
    last = state
    for i, (t, st) in enumerate(marks):
        st = last if st is None else st
        last = st
        t_next = marks[i + 1][0] if i + 1 < len(marks) else t1
        span = t_next - t
        if st == ipc.STATE_READY:
            what = (f"{role} preempted").strip()
            add(what, span, "preempted")
            who = culprits.setdefault(what, {})
            for s0, s1, pid, tid, prio, _ev in segments:
                lo, hi = max(s0, t), min(s1, t_next)
                if hi > lo and (pid, tid) != key and prio > 0:     # priority 0: the idle thread
                    who.setdefault(f"{ctx.model.name(pid)} at priority {prio}", []).append((lo, hi))
        elif t < t_recv:
            busy = [o for o in others if o.t_receive <= t_next and (o.t_reply or ctx.t_end) >= t]
            if busy:
                names = ", ".join(sorted({ctx.model.name(o.client_pid) for o in busy}))
                add(f"queued behind {names}", span, "queued", busy[0].receive_event)
            else:
                add("queued, server busy elsewhere", span, "queued", queued.send_event)
        elif st == ipc.STATE_RUNNING:
            add((f"{role} running").strip(), span, "running")
        elif st in (ipc.STATE_SEND, ipc.STATE_REPLY):
            inner = next((o for o in calls if o.t_send <= t + 1 and (o.t_reply or ctx.t_end) >= t), None)
            target = ctx.model.name(inner.server_pid) if inner else "another server"
            what = (f"{role} waiting on {target}").strip()
            add(what, span, "nested", inner.send_event if inner else None)
            if inner is not None and inner.t_reply is not None:
                prev = inner_of.get(what)
                if prev is None or inner.t_reply - inner.t_send > prev.t_reply - prev.t_send:
                    inner_of[what] = inner
        elif st == 12:
            add((f"{role} sleeping (NANOSLEEP)").strip(), span, "sleep")
        else:
            add((f"{role} blocked ({ipc.STATE_NAMES.get(st, st)})").strip(), span, "blocked")
    out = []
    for p in sorted(parts.values(), key=lambda p: -p["cycles"]):
        p["ms"] = round(ctx.ms(p.pop("cycles")), 3)
        who = {k: _union(v) for k, v in culprits.get(p["what"], {}).items()}
        if who:
            top = sorted(who, key=lambda k: -who[k])[:3]
            p["by"] = [{"thread": k, "ms": round(ctx.ms(who[k]), 3)} for k in top]
        inner = inner_of.get(p["what"])
        # Look inside the nested call only when one call explains most of the wait.
        if inner is not None and depth > 0 and ctx.ms(inner.t_reply - inner.t_send) >= p["ms"] / 2:
            p["inside"] = breakdown(ctx, inner, depth - 1)
        out.append(p)
    return out


def _union(spans: List[Tuple[int, int]]) -> int:
    """Wall time covered by the spans (a thread on two CPUs counts once)."""
    total, end = 0, None
    for a, b in sorted(spans):
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def describe(parts: List[dict], limit: int = 3) -> str:
    """'server preempted 50.4 ms (mostly media at priority 20), server running 2.0 ms'."""
    out = []
    for p in parts[:limit]:
        if p["ms"] < 0.05:
            continue
        text = f"{p['what']} {p['ms']:.1f} ms"
        if p.get("by"):
            text += f" (mostly {p['by'][0]['thread']}, {p['by'][0]['ms']:.1f} ms)"
        if p.get("inside"):
            text += f" (there: {describe(p['inside'], 1)})"
        out.append(text)
    return ", ".join(out)


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
                else:
                    what += _alive_note(ctx, survivor, pids, name)
                events = [fault_event] + [b.send_event for b in blocked]
            title = (f"{name} {fault_kind} and took {survivor} down with it" if died or blocked
                     else f"{name} {fault_kind} and {survivor} stopped making progress")
            out.append(Finding(
                "isolation", ERROR,
                title,
                f"The contract says {survivor} must keep working when {name} fails "
                f"(at least {rule.min_progress} completed messages). {name} {fault_kind}; {what}.",
                [name, survivor], sorted({e for e in events if e is not None}),
                {"fault": fault_kind, "progress": progress, "survivor_died": bool(died)},
            ))
    return out


def _alive_note(ctx: Context, survivor: str, pids: List[int], failed: str) -> str:
    """Why a survivor that is alive and not blocked on the failed server still did nothing."""
    living = [p for p in pids if p not in ctx.model.deaths]
    states = sorted({ipc.STATE_NAMES.get(st, str(st)) for (pid, _tid), st in ctx.model.thread_state.items()
                     if pid in living and st != ipc.STATE_DEAD})
    if not states and ctx.snap is not None:
        states = sorted({t.state for t in getattr(ctx.snap, "threads", []) if t.pid in living})
    servers = sorted({s for (c, s) in ctx.topo.edges if c == survivor and s in ctx.contract.processes})
    note = ""
    if states:
        note += f"; it is still alive ({', '.join(states)}), not blocked on {failed}"
    if servers == [failed]:
        note += (f"; {failed} is its only server in the contract, so with {failed} gone it has "
                 f"nothing to make progress with. If that is acceptable, drop {survivor} from "
                 f"must_survive; if not, it should notice the failure (an error, a timeout, a reconnect)")
    return note


def _fault(ctx: Context, name: str, pids: List[int]) -> Tuple[Optional[int], str, Optional[int]]:
    """When did `name` fail? Death, or the oldest message it never answered."""
    deaths = [(ctx.model.dying.get(p, ctx.model.deaths[p]), ctx.model.death_events[p])
              for p in pids if p in ctx.model.deaths]
    if deaths:
        t, ev = min(deaths)
        return t, "died", ev
    for w in ctx.topo.waiters:
        if w.server == name and w.source == "snapshot" and ctx.trace.events:
            return ctx.trace.events[0].t_cycles, "stopped answering before the trace started", None
    budget = ctx.contract.budget_for(name)
    for tx in ctx.model.transactions:
        if tx.server_pid in pids and tx.status in ("sent", "received") and not waits_for_data(ctx, tx):
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
