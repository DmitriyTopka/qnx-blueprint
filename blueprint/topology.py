"""Who talks to whom: the process-level message graph of a trace.

Built from ipc.IpcModel. An edge is (client process, server process) and
carries the channel ids used, how many messages went over it and how many
were still unanswered when the trace ended. Threads that ended the trace
blocked in SEND or REPLY are listed with the transaction they wait on: those
are the raw material for the deadlock and isolation checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import ipc


@dataclass
class Edge:
    client: str
    server: str
    chids: Set[int] = field(default_factory=set)
    messages: int = 0
    replied: int = 0
    errors: int = 0
    open: int = 0
    first_event: Optional[int] = None

    def to_json(self) -> dict:
        return {
            "client": self.client, "server": self.server, "chids": sorted(self.chids),
            "messages": self.messages, "replied": self.replied, "errors": self.errors,
            "open": self.open, "first_event": self.first_event,
        }


@dataclass
class Waiter:
    """A thread that was blocked on a message when the trace ended."""

    process: str
    pid: int
    tid: int
    state: str            # SEND (server never received) or REPLY (received, no answer)
    server: str
    server_pid: Optional[int]
    send_event: Optional[int]    # None when only the pidin snapshot shows the wait
    source: str = "trace"        # "trace" or "snapshot"

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Topology:
    edges: Dict[Tuple[str, str], Edge] = field(default_factory=dict)
    waiters: List[Waiter] = field(default_factory=list)
    processes: Set[str] = field(default_factory=set)

    def to_json(self) -> dict:
        return {
            "processes": sorted(self.processes),
            "edges": [e.to_json() for e in sorted(self.edges.values(), key=lambda e: (e.client, e.server))],
            "waiters": [w.to_json() for w in self.waiters],
        }


def build(model: ipc.IpcModel, only: Optional[Iterable[str]] = None, snap=None) -> Topology:
    """Build the graph. With `only`, keep edges where both ends are in it.

    With a pidin snapshot (snapshot.Snapshot), threads that were blocked for
    the whole trace, and so left no events, are added to the waiters.
    """
    keep = set(only) if only else None
    topo = Topology()
    open_by_thread: Dict[Tuple[int, int], ipc.Transaction] = {}

    for tx in model.transactions:
        client = model.name(tx.client_pid)
        server = model.name(tx.server_pid)
        if tx.server_pid is None:
            continue
        if keep is not None and (client not in keep or server not in keep):
            continue
        topo.processes.update((client, server))
        edge = topo.edges.setdefault((client, server), Edge(client, server))
        edge.messages += 1
        if tx.chid is not None:
            edge.chids.add(tx.chid)
        if edge.first_event is None:
            edge.first_event = tx.send_event
        if tx.status == "replied":
            edge.replied += 1
        elif tx.status == "error":
            edge.errors += 1
        else:
            edge.open += 1
            open_by_thread[(tx.client_pid, tx.client_tid)] = tx

    for (pid, tid), tx in open_by_thread.items():
        state = model.thread_state.get((pid, tid))
        if state not in (ipc.STATE_SEND, ipc.STATE_REPLY):
            continue  # the thread moved on (e.g. was killed) after the send
        if model.deaths.get(pid, -1) >= (tx.t_send or 0):
            continue  # the client process was destroyed later in the trace: nobody waits
        topo.waiters.append(Waiter(
            process=model.name(pid), pid=pid, tid=tid,
            state=ipc.STATE_NAMES[state], server=model.name(tx.server_pid),
            server_pid=tx.server_pid, send_event=tx.send_event,
        ))
    if snap is not None:
        known = {(w.pid, w.tid) for w in topo.waiters}
        snap_names = snap.names()
        for t in snap.threads:
            if t.state not in ("SEND", "REPLY") or t.blocked_on is None or (t.pid, t.tid) in known:
                continue
            server = snap_names.get(t.blocked_on) or model.name(t.blocked_on)
            if keep is not None and (t.name not in keep or server not in keep):
                continue
            topo.processes.update((t.name, server))
            topo.waiters.append(Waiter(t.name, t.pid, t.tid, t.state, server, t.blocked_on,
                                       None, "snapshot"))
    topo.waiters.sort(key=lambda w: (w.process, w.tid))
    return topo


def format_text(topo: Topology) -> str:
    lines = []
    for e in sorted(topo.edges.values(), key=lambda e: (e.client, e.server)):
        extra = []
        if e.errors:
            extra.append(f"{e.errors} errors")
        if e.open:
            extra.append(f"{e.open} unanswered")
        chids = ",".join(str(c) for c in sorted(e.chids)) or "?"
        lines.append(f"{e.client:>10} -> {e.server:<10} chid {chids:<4} {e.messages:5} msgs"
                     + (f"  ({', '.join(extra)})" if extra else ""))
    if topo.waiters:
        lines.append("blocked at end of trace:")
        for w in topo.waiters:
            where = (f"send at event {w.send_event}" if w.send_event is not None
                     else "blocked before the trace started, from pidin")
            lines.append(f"  {w.process}[{w.pid}/{w.tid}] {w.state}-blocked on {w.server} ({where})")
    return "\n".join(lines)
