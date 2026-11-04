"""Message-passing semantics on top of raw kernel trace events.

Rebuilds, from a wide-mode trace (tracelogger -w):
  * who is running on each CPU (THREAD class, state RUNNING),
  * process names (PROCESS class, CREATE_NAME and THREAD NAME),
  * channels (ChannelCreate) and connections (ConnectAttach) with their chid,
  * message transactions: client MsgSend -> server MsgReceive -> MsgReply.

Kernel call events do not carry the caller's pid/tid, so the caller is taken
from the last RUNNING thread seen on the same CPU.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import kev

# Kernel call numbers (sys/kercalls.h in QNX 8.0).
KER_MSG_SENDV = 11
KER_MSG_SENDVNC = 12    # non-cancellation send, used by write() and friends
KER_MSG_ERROR = 13
KER_MSG_RECEIVEV = 14
KER_MSG_REPLYV = 15
KER_CHANNEL_CREATE = 35
KER_CHANNEL_DESTROY = 36
KER_CONNECT_ATTACH = 39
KER_CONNECT_DETACH = 40

# COMM class event codes.
COMM_SND_MESSAGE = 0
COMM_SND_PULSE = 1
COMM_REC_MESSAGE = 2
COMM_REC_PULSE = 3
COMM_REPLY = 10
COMM_ERROR = 11

# Thread states used as event codes in the PR_TH class.
STATE_DEAD = 0
STATE_RUNNING = 1
STATE_READY = 2
STATE_SEND = 4
STATE_RECEIVE = 5
STATE_REPLY = 6
STATE_CREATE = 24
STATE_DESTROY = 25
STATE_NAMES = {
    0: "DEAD", 1: "RUNNING", 2: "READY", 3: "STOPPED", 4: "SEND", 5: "RECEIVE",
    6: "REPLY", 7: "MQ_SEND", 8: "MQ_RECEIVE", 9: "WAITPAGE", 10: "SIGSUSPEND",
    11: "SIGWAITINFO", 12: "NANOSLEEP", 13: "MUTEX", 14: "CONDVAR", 15: "JOIN",
    16: "INTR", 17: "SEM", 18: "WAITCTX", 19: "RWLOCK_READ", 20: "RWLOCK_WRITE",
    21: "BARRIER", 22: "PIPE", 24: "CREATE", 25: "DESTROY", 26: "MUTEX", 27: "TRACEBUFFER",
    28: "INTR_ATTACH_EV", 29: "TIMER_DELEGATE", 30: "SEND_NOTIFY",
}

# PROCESS events live in the same PR_TH class, at multiples of 64.
PROC_CREATE = 1 << 6
PROC_DESTROY = 2 << 6
PROC_CREATE_NAME = 3 << 6
PROC_DESTROY_NAME = 4 << 6
THREAD_NAME = 5 << 6

SIDE_CHANNEL = 0x40000000
CHF_FIXED_PRIORITY = 0x0001
# Message types of the resource manager protocol, <sys/iomsg.h> (values
# printed on a QNX 8.0 target). A client's open(), read() or devctl() reaches
# the server as one of these.
IO_TYPES = {0x100: "_IO_CONNECT", 0x101: "_IO_READ", 0x102: "_IO_WRITE", 0x104: "_IO_STAT",
            0x106: "_IO_DEVCTL", 0x109: "_IO_LSEEK", 0x10D: "_IO_OPENFD", 0x113: "_IO_MSG",
            0x115: "_IO_DUP", 0x116: "_IO_CLOSE", 0x119: "_IO_SYNC", 0x11E: "_IO_READ64",
            0x11F: "_IO_WRITE64"}    # _NTO_CHF_FIXED_PRIORITY in <sys/neutrino.h>


@dataclass
class Channel:
    pid: int
    chid: int
    t: int
    flags: int = 0


@dataclass
class Connection:
    pid: int              # process that owns the coid
    coid: int
    server_pid: int       # process that owns the channel
    chid: int
    t: int


@dataclass
class Transaction:
    """One MsgSend from a client, as seen through to the reply (if any)."""

    client_pid: int
    client_tid: int
    coid: int
    t_send: int
    send_event: int                    # index of the MsgSend entry event
    preview: str = ""
    rcvid: Optional[int] = None
    server_pid: Optional[int] = None
    server_tid: Optional[int] = None
    chid: Optional[int] = None
    t_receive: Optional[int] = None
    t_reply: Optional[int] = None
    reply_preview: str = ""
    msg_type: Optional[int] = None     # first 16 bits of the message (wide mode)
    status: str = "sent"               # sent -> received -> replied | error
    receive_event: Optional[int] = None
    reply_event: Optional[int] = None

    @property
    def done(self) -> bool:
        return self.status in ("replied", "error")


@dataclass
class IpcModel:
    trace: kev.Trace
    names: Dict[int, str] = field(default_factory=dict)
    channels: Dict[Tuple[int, int], Channel] = field(default_factory=dict)
    connections: Dict[Tuple[int, int], Connection] = field(default_factory=dict)
    transactions: List[Transaction] = field(default_factory=list)
    thread_state: Dict[Tuple[int, int], int] = field(default_factory=dict)
    deaths: Dict[int, int] = field(default_factory=dict)       # pid -> time it was destroyed
    # Every thread state change in trace order: (time, pid, tid, state).
    states: List[Tuple[int, int, int, int]] = field(default_factory=list)
    # The same changes with scheduling data, when the event carries it (wide
    # mode): (time, cpu, pid, tid, state, priority, event index).
    sched: List[Tuple[int, int, int, int, int, int, int]] = field(default_factory=list)
    thread_names: Dict[Tuple[int, int], str] = field(default_factory=dict)
    death_events: Dict[int, int] = field(default_factory=dict)  # pid -> event index

    def name(self, pid: Optional[int]) -> str:
        if pid is None:
            return "?"
        return self.names.get(pid, f"pid{pid}")


def build(trace: kev.Trace) -> IpcModel:
    model = IpcModel(trace)
    running: Dict[int, Tuple[int, int]] = {}            # cpu -> (pid, tid)
    receiving_on: Dict[Tuple[int, int], int] = {}       # (pid, tid) -> chid
    attach_args: Dict[Tuple[int, int], Tuple[int, int]] = {}  # caller -> (server pid, chid)
    in_send: Dict[Tuple[int, int], Transaction] = {}    # client thread -> open send
    waiting: Dict[Tuple[int, int], Transaction] = {}    # (server pid, rcvid) -> sent, not received
    by_rcvid: Dict[Tuple[int, int], Transaction] = {}   # (server pid, rcvid) -> received, not replied
    create_flags: Dict[Tuple[int, int], int] = {}       # caller -> ChannelCreate flags

    for ev in trace.events:
        w = ev.words

        if ev.cls == kev.CLASS_PR_TH:
            code = ev.code
            if code == PROC_DESTROY and len(w) >= 2:
                model.deaths.setdefault(w[1], ev.t_cycles)
                model.death_events.setdefault(w[1], ev.index)
            elif code in (PROC_CREATE_NAME, PROC_DESTROY_NAME) and len(w) >= 2:
                pid = w[1]
                name = kev.text_from_words(w[2:])
                if name:
                    model.names[pid] = name.rsplit("/", 1)[-1]
            elif code == THREAD_NAME and len(w) >= 2:
                name = kev.text_from_words(w[2:])
                if name:
                    model.thread_names[(w[0], w[1])] = name
            elif code < 64 and len(w) >= 2:
                pid, tid = w[0], w[1]
                model.thread_state[(pid, tid)] = code
                model.states.append((ev.t_cycles, pid, tid, code))
                if len(w) >= 3:
                    model.sched.append((ev.t_cycles, ev.cpu, pid, tid, code, w[2], ev.index))
                if code == STATE_RUNNING:
                    running[ev.cpu] = (pid, tid)
            continue

        if ev.cls == kev.CLASS_COMM:
            if ev.code == COMM_SND_MESSAGE and len(w) >= 2:
                # Emitted on the client's CPU right after MsgSend: rcvid and
                # the pid of the server that owns the channel.
                rcvid, server_pid = w[0], w[1]
                tx = in_send.get(running.get(ev.cpu, (-1, -1)))
                if tx is not None and tx.rcvid is None:
                    tx.rcvid = rcvid
                    tx.server_pid = server_pid
                    waiting[(server_pid, rcvid)] = tx
            elif ev.code == COMM_REPLY and len(w) >= 2:
                tid, pid = w[0], w[1]
                tx = in_send.pop((pid, tid), None)
                if tx is not None and tx.status != "error":
                    tx.status = "replied"
                    tx.t_reply = tx.t_reply or ev.t_cycles
            elif ev.code == COMM_ERROR and len(w) >= 2:
                tid, pid = w[0], w[1]
                tx = in_send.pop((pid, tid), None)
                if tx is not None:
                    tx.status = "error"
                    tx.t_reply = ev.t_cycles
            continue

        call = kev.ker_call(ev)
        if call is None:
            continue
        number, is_exit, wide = call
        caller = running.get(ev.cpu)
        if caller is None:
            continue
        pid, tid = caller

        if number == KER_CHANNEL_CREATE and not is_exit and w:
            create_flags[caller] = w[0]

        elif number == KER_CHANNEL_CREATE and is_exit and w:
            chid = w[0]
            model.channels[(pid, chid)] = Channel(pid, chid, ev.t_cycles,
                                                  create_flags.pop(caller, 0))

        elif number == KER_CONNECT_ATTACH and not is_exit and len(w) >= 3:
            # nd, pid, chid, index, flags; pid 0 means "this process".
            server_pid = w[1] or pid
            attach_args[caller] = (server_pid, w[2])

        elif number == KER_CONNECT_ATTACH and is_exit and w:
            coid = w[0]
            target = attach_args.pop(caller, None)
            if target is not None and coid != 0xFFFFFFFF:
                model.connections[(pid, coid)] = Connection(pid, coid, target[0], target[1], ev.t_cycles)

        elif number in (KER_MSG_SENDV, KER_MSG_SENDVNC) and not is_exit and w:
            coid = w[0]
            preview = kev.text_from_words(w[5:8]) if wide else ""
            tx = Transaction(pid, tid, coid, ev.t_cycles, ev.index, preview)
            if wide and len(w) > 5:
                tx.msg_type = w[5] & 0xFFFF
            conn = model.connections.get((pid, coid))
            if conn is not None:
                tx.chid = conn.chid
            in_send[caller] = tx
            model.transactions.append(tx)

        elif number == KER_MSG_RECEIVEV and not is_exit and w:
            receiving_on[caller] = w[0]

        elif number == KER_MSG_RECEIVEV and is_exit and w:
            rcvid = w[0]
            if rcvid == 0 or rcvid == 0xFFFFFFFF:
                continue  # pulse or error, no reply expected
            tx = waiting.pop((pid, rcvid), None)
            if tx is None:
                continue
            tx.server_pid, tx.server_tid = pid, tid
            tx.chid = tx.chid if tx.chid is not None else receiving_on.get(caller)
            tx.t_receive = ev.t_cycles
            tx.receive_event = ev.index
            tx.status = "received"
            by_rcvid[(pid, rcvid)] = tx

        elif number in (KER_MSG_REPLYV, KER_MSG_ERROR) and not is_exit and w:
            tx = by_rcvid.pop((pid, w[0]), None)
            if tx is not None:
                tx.t_reply = ev.t_cycles
                tx.reply_event = ev.index
                if number == KER_MSG_REPLYV:
                    tx.reply_preview = kev.text_from_words(w[5:8]) if wide else ""
                else:
                    tx.status = "error"

    return model

