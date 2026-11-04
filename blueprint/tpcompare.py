"""Compare Blueprint's reading of a .kev file with QNX's own traceprinter.

    traceprinter -f run.kev > run.tp          # on the target, from QNX SDP 8.0
    python -m blueprint.tpcompare run.kev run.tp

Every event traceprinter prints is matched with one event Blueprint parsed,
on time, CPU and class; kernel calls also on the call number and entry or
exit; thread states on pid, tid and priority; message sends on rcvid and
server pid. The parser is what every check stands on, so this is the test
that it reads the file the way QNX does.
"""

from __future__ import annotations

import gzip
import re
import sys
from collections import Counter
from typing import Dict, List, Optional, Tuple

from . import ipc, kev

LINE = re.compile(r"^t:0x([0-9a-f]+) CPU:(\d+) (\S+)\s+:(\S+)\s*(.*)$")
FAMILY = {"CONTROL": kev.CLASS_CONTROL, "KER_CALL": kev.CLASS_KER_CALL, "KER_EXIT": kev.CLASS_KER_CALL,
          "INTERRUPT": kev.CLASS_INT, "THREAD": kev.CLASS_PR_TH, "PROCESS": kev.CLASS_PR_TH,
          "SYSTEM": kev.CLASS_SYSTEM, "USER": kev.CLASS_USER, "COMM": kev.CLASS_COMM}
ARG = re.compile(r"(\w+):(0x[0-9a-fA-F]+|-?\d+)")

Key = Tuple[int, int, int, Optional[Tuple]]


def _open(path: str):
    return gzip.open(path, "rt", errors="replace") if path.endswith(".gz") else open(path, errors="replace")


def printer_events(path: str) -> List[Key]:
    out: List[Key] = []
    with _open(path) as f:
        for line in f:
            m = LINE.match(line.rstrip("\n"))
            if not m:
                continue
            t, cpu, cls_name, name, rest = m.groups()
            if cls_name not in FAMILY:
                continue
            args = {k: (int(v, 16) if v.lower().startswith("0x") else int(v, 10))
                    for k, v in ARG.findall(rest)}
            out.append((int(t, 16), int(cpu), FAMILY[cls_name], _detail(cls_name, name, args)))
    return out


def _detail(cls_name: str, name: str, args: Dict[str, int]) -> Optional[Tuple]:
    if cls_name in ("KER_CALL", "KER_EXIT"):
        num = name.rsplit("/", 1)[-1]
        return ("call", int(num) if num.isdigit() else None, cls_name == "KER_EXIT")
    if cls_name == "THREAD" and "pid" in args and "tid" in args:
        return ("thread", args["pid"], args["tid"], args.get("priority"))
    if cls_name == "COMM" and name == "SND_MESSAGE" and "rcvid" in args:
        return ("send", args["rcvid"], args.get("pid"))
    return None


def blueprint_events(trace: kev.Trace) -> List[Key]:
    out: List[Key] = []
    for ev in trace.events:
        w = ev.words
        detail = None
        call = kev.ker_call(ev)
        if call is not None:
            detail = ("call", call[0], call[1])
        elif ev.cls == kev.CLASS_PR_TH and ev.code < 64 and len(w) >= 2:
            detail = ("thread", w[0], w[1], w[2] if len(w) >= 3 else None)
        elif ev.cls == kev.CLASS_COMM and ev.code == ipc.COMM_SND_MESSAGE and len(w) >= 2:
            detail = ("send", w[0], w[1])
        out.append((ev.t_cycles, ev.cpu, ev.cls, detail))
    return out


def compare(kev_path: str, tp_path: str) -> dict:
    trace = kev.read(kev_path)
    ours = blueprint_events(trace)
    theirs = printer_events(tp_path)
    # Match on time, CPU and class first, then check the details of every pair.
    ours_by: Dict[Tuple[int, int, int], List] = {}
    for k in ours:
        ours_by.setdefault(k[:3], []).append(k[3])
    missing, mismatched, checked = 0, [], 0
    for t, cpu, cls, detail in theirs:
        pool = ours_by.get((t, cpu, cls))
        if not pool:
            missing += 1
            continue
        if detail is None:
            pool.pop(0)
            continue
        for i, d in enumerate(pool):
            if d is not None and _agree(d, detail):
                pool.pop(i)
                checked += 1
                break
        else:
            mismatched.append(((t, cpu, cls), detail, pool[:3]))
            pool.pop(0)
    extra = sum(len(v) for v in ours_by.values())
    return {"traceprinter_events": len(theirs), "blueprint_events": len(ours), "details_checked": checked,
            "missing_in_blueprint": missing, "extra_in_blueprint": extra,
            "detail_mismatches": len(mismatched), "examples": mismatched[:5],
            "classes": dict(Counter(k[2] for k in theirs))}


def _agree(ours: Tuple, theirs: Tuple) -> bool:
    if ours[0] != theirs[0]:
        return False
    if ours[0] == "call":
        return (theirs[1] is None or ours[1] == theirs[1]) and ours[2] == theirs[2]
    if ours[0] == "thread":
        return ours[1:3] == theirs[1:3] and (theirs[3] is None or ours[3] == theirs[3])
    return ours[1] == theirs[1] and (theirs[2] is None or ours[2] == theirs[2])


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: python -m blueprint.tpcompare run.kev run.tp[.gz]", file=sys.stderr)
        return 2
    r = compare(argv[0], argv[1])
    for k, v in r.items():
        if k != "examples":
            print(f"{k}: {v}")
    for e in r["examples"]:
        print("mismatch:", e)
    ok = not (r["missing_in_blueprint"] or r["extra_in_blueprint"] or r["detail_mismatches"])
    print("MATCH" if ok else "DIFFERENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
