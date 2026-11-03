"""Export a trace to the Chrome trace event JSON that ui.perfetto.dev opens.

What QNX's own Perfetto probe does not show, and this export does:
  * the real blocking state of every thread (SEND, RECEIVE, REPLY, ...),
    not one generic "sleeping" state,
  * every message as a pair of arrows: client -> server when the server
    receives it, server -> client when it replies,
  * Blueprint findings as markers on the processes they are about.

Each thread gets two tracks: "<tid> state" with its scheduling and blocking
states, and "<tid> messages" with one slice per MsgSend (on the client) or
per handled message (on the server). The arrows connect message slices.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import checks, ipc, kev

MSG_TRACK = 1_000_000   # message track tid = real tid + MSG_TRACK


def export(trace: kev.Trace, model: ipc.IpcModel, only: Optional[Iterable[str]] = None,
           findings: Iterable[checks.Finding] = ()) -> dict:
    keep: Optional[Set[str]] = set(only) if only else None
    t0 = trace.events[0].t_cycles if trace.events else 0
    t_end = trace.events[-1].t_cycles if trace.events else 0
    per_us = trace.cycles_per_sec / 1e6 if trace.cycles_per_sec else 1.0

    def us(t: int) -> float:
        return round((t - t0) / per_us, 3)

    def wanted(pid: Optional[int]) -> bool:
        return pid is not None and (keep is None or model.name(pid) in keep)

    out: List[dict] = []
    threads: Set[Tuple[int, int]] = set()

    # Thread states: a slice from each state change to the next one.
    last: Dict[Tuple[int, int], Tuple[int, int]] = {}
    for t, pid, tid, state in model.states:
        if not wanted(pid):
            continue
        key = (pid, tid)
        threads.add(key)
        if key in last:
            start, prev = last[key]
            _state_slice(out, pid, tid, prev, us(start), us(t))
        last[key] = (t, state)
    for (pid, tid), (start, state) in last.items():
        if state not in (ipc.STATE_DEAD, ipc.STATE_DESTROY):
            _state_slice(out, pid, tid, state, us(start), us(t_end))

    # Messages.
    flow_id = 0
    for tx in model.transactions:
        if not (wanted(tx.client_pid) and wanted(tx.server_pid)):
            continue
        server = model.name(tx.server_pid)
        client = model.name(tx.client_pid)
        end = tx.t_reply if tx.t_reply is not None else t_end
        label = f"MsgSend to {server}"
        if tx.status == "error":
            label += " (error)"
        elif tx.t_reply is None:
            label += " (no reply)"
        args = {"coid": hex(tx.coid), "chid": tx.chid, "message": tx.preview,
                "reply": tx.reply_preview, "status": tx.status, "event": tx.send_event}
        threads.add((tx.client_pid, tx.client_tid))
        out.append(_slice(label, "message", tx.client_pid, tx.client_tid + MSG_TRACK,
                          us(tx.t_send), us(end), args))
        if tx.t_receive is None or tx.server_tid is None:
            continue
        threads.add((tx.server_pid, tx.server_tid))
        s_end = tx.t_reply if tx.t_reply is not None else t_end
        out.append(_slice(f"handle {client}", "message", tx.server_pid, tx.server_tid + MSG_TRACK,
                          us(tx.t_receive), us(s_end), args))
        flow_id += 1
        out += _flow(flow_id, "message", (tx.client_pid, tx.client_tid + MSG_TRACK, us(tx.t_send)),
                     (tx.server_pid, tx.server_tid + MSG_TRACK, us(tx.t_receive)))
        if tx.t_reply is not None:
            flow_id += 1
            at = us(tx.t_reply)
            out += _flow(flow_id, "reply", (tx.server_pid, tx.server_tid + MSG_TRACK, at - 0.001),
                         (tx.client_pid, tx.client_tid + MSG_TRACK, at - 0.001))

    # Findings as markers on the first process they name.
    by_index = {ev.index: ev for ev in trace.events}
    pid_of = {name: pid for pid, name in model.names.items()}
    for f in findings:
        pid = pid_of.get(f.processes[0]) if f.processes else None
        ev = by_index.get(f.events[0]) if f.events else None
        out.append({
            "name": f"{f.severity.upper()} {f.check}: {f.title}", "ph": "i", "s": "p",
            "pid": pid or 0, "tid": 0, "ts": us(ev.t_cycles) if ev else 0,
            "args": {"detail": f.detail, "events": f.events},
        })

    # Names for processes and both tracks of every thread.
    for pid in sorted({p for p, _ in threads}):
        out.append({"name": "process_name", "ph": "M", "pid": pid, "args": {"name": model.name(pid)}})
    for pid, tid in sorted(threads):
        tname = model.thread_names.get((pid, tid), f"t{tid}")
        out.append({"name": "thread_name", "ph": "M", "pid": pid, "tid": tid,
                    "args": {"name": f"{tname} state"}})
        out.append({"name": "thread_name", "ph": "M", "pid": pid, "tid": tid + MSG_TRACK,
                    "args": {"name": f"{tname} messages"}})
        out.append({"name": "thread_sort_index", "ph": "M", "pid": pid, "tid": tid,
                    "args": {"sort_index": tid * 2}})
        out.append({"name": "thread_sort_index", "ph": "M", "pid": pid, "tid": tid + MSG_TRACK,
                    "args": {"sort_index": tid * 2 + 1}})

    return {"traceEvents": out, "displayTimeUnit": "ms",
            "otherData": {"source": "qnx-blueprint", "trace": trace.attributes.get("FILE_NAME", "")}}


def write(path: str, doc: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))


def _slice(name: str, cat: str, pid: int, tid: int, start: float, end: float, args: dict) -> dict:
    return {"name": name, "cat": cat, "ph": "X", "pid": pid, "tid": tid,
            "ts": start, "dur": max(round(end - start, 3), 0.001), "args": args}


def _state_slice(out: List[dict], pid: int, tid: int, state: int, start: float, end: float) -> None:
    if state in (ipc.STATE_READY, ipc.STATE_CREATE, ipc.STATE_DESTROY, ipc.STATE_DEAD):
        return  # ready gaps and lifecycle markers are noise at this zoom level
    name = ipc.STATE_NAMES.get(state, str(state))
    if state in (ipc.STATE_SEND, ipc.STATE_REPLY, ipc.STATE_RECEIVE):
        name += "-blocked"
    out.append(_slice(name, "state", pid, tid, start, end, {}))


def _flow(fid: int, cat: str, src: Tuple[int, int, float], dst: Tuple[int, int, float]) -> List[dict]:
    return [
        {"name": cat, "cat": cat, "ph": "s", "id": fid, "pid": src[0], "tid": src[1], "ts": src[2]},
        {"name": cat, "cat": cat, "ph": "f", "bp": "e", "id": fid, "pid": dst[0], "tid": dst[1], "ts": dst[2]},
    ]
