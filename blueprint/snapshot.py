"""Thread states from `pidin` taken on the target when the trace stops.

A kernel trace only shows what changed while it was recording. A thread that
was already SEND- or REPLY-blocked when tracelogger started emits no events
at all, so a system that deadlocked before the trace looks quiet, not broken.
The default `pidin` listing fills that gap: for every blocked thread it
prints the state and the pid it is blocked on.

    pid tid name                 prio STATE       Blocked
 1114137   1 system/bin/nav        10r REPLY       1114134
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+\d+\S*\s+([A-Z_]+)\s*(\S*)")


@dataclass
class ThreadState:
    pid: int
    tid: int
    name: str
    state: str
    blocked_on: Optional[int]


@dataclass
class Snapshot:
    source: str
    threads: List[ThreadState]

    def names(self) -> Dict[int, str]:
        return {t.pid: t.name for t in self.threads}


def parse(text: str, source: str = "") -> Snapshot:
    threads: List[ThreadState] = []
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        pid, tid, path, state, blocked = m.groups()
        # For SEND and REPLY the column is the server pid (optionally pid-tid);
        # for other states it may be an address, which we do not need.
        head = blocked.split("-")[0]
        target = int(head) if state in ("SEND", "REPLY") and head.isdigit() else None
        threads.append(ThreadState(int(pid), int(tid), path.rsplit("/", 1)[-1], state, target))
    return Snapshot(source, threads)


def load(path: str) -> Snapshot:
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse(f.read(), path)
