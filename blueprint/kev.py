"""Reader for QNX 8.0 kernel trace files (.kev) written by tracelogger.

The file layout is: a text header of TRACE_<KEY>::<value> attributes that ends
with TRACE_HEADER_END::, a copy of the system page (TRACE_SYSPAGE_LEN bytes),
then a stream of 16-byte trace events. Each event is four little-endian 32-bit
words: a header word, the low 32 bits of the timestamp, and two data words.

Events longer than two data words are split into parts that share a header
class/event and timestamp: one CB (begin), zero or more CC (continue) and one
CE (end). Parts of one event are contiguous per CPU but may interleave with
events from other CPUs, so assembly is done per CPU.

This module only depends on the Python standard library and works on any host.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

HEADER_END = b"TRACE_HEADER_END::"

# Header word layout.
STRUCT_S, STRUCT_CB, STRUCT_CC, STRUCT_CE = 0, 1, 2, 3

CLASS_EMPTY = 0
CLASS_CONTROL = 1
CLASS_KER_CALL = 2
CLASS_INT = 3
CLASS_PR_TH = 4
CLASS_SYSTEM = 5
CLASS_USER = 6
CLASS_COMM = 7

CLASS_NAMES = {
    0: "EMPTY", 1: "CONTROL", 2: "KER_CALL", 3: "INT", 4: "PR_TH",
    5: "SYSTEM", 6: "USER", 7: "COMM", 8: "QUIP", 9: "SEC", 10: "QVM", 11: "PMU",
}

CONTROL_TIME = 1
CONTROL_BUFFER = 2
CONTROL_BUFFER_END = 4

# Kernel call events: low 7 bits are the kernel call number, bit 7 marks the
# exit, bit 9 marks the wide (64-bit argument) encoding.
KER_EXIT_BIT = 0x80
KER_WIDE_BIT = 0x200


def _words_to_text(words: List[int]) -> str:
    raw = b"".join(struct.pack("<I", w) for w in words)
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


@dataclass
class RawEvent:
    """One assembled trace event (all parts joined)."""

    index: int          # position of the first part in the file, in events
    cpu: int
    cls: int
    code: int           # event number inside its class (0..1023)
    t_cycles: int       # full 64-bit timestamp in clock cycles
    words: List[int]    # data words after the timestamp, all parts joined

    @property
    def class_name(self) -> str:
        return CLASS_NAMES.get(self.cls, str(self.cls))


@dataclass
class Trace:
    attributes: Dict[str, str]
    syspage: bytes
    events: List[RawEvent] = field(default_factory=list)

    @property
    def cycles_per_sec(self) -> int:
        return int(self.attributes.get("CYCLES_PER_SEC", "0") or 0)

    @property
    def cpu_count(self) -> int:
        return int(self.attributes.get("CPU_NUM", "1") or 1)

    def seconds(self, t_cycles: int) -> float:
        return t_cycles / self.cycles_per_sec if self.cycles_per_sec else 0.0


class KevFormatError(ValueError):
    pass


def parse_header(data: bytes) -> tuple[Dict[str, str], int]:
    """Return the header attributes and the offset right after the header."""
    end = data.find(HEADER_END)
    if not data.startswith(b"TRACE_") or end < 0:
        raise KevFormatError("not a QNX trace file: TRACE_ header not found")
    text = data[:end].decode("ascii", errors="replace")
    attributes: Dict[str, str] = {}
    # Keys are TRACE_<NAME>:: and values run until the next TRACE_ key.
    for chunk in text.split("TRACE_")[1:]:
        if "::" not in chunk:
            continue
        key, value = chunk.split("::", 1)
        attributes[key] = value
    if attributes.get("LITTLE_ENDIAN", "TRUE") != "TRUE":
        raise KevFormatError("big-endian traces are not supported")
    encoding = attributes.get("ENCODING", "")
    if encoding and not encoding.startswith("16 byte"):
        raise KevFormatError(f"unsupported event encoding: {encoding}")
    return attributes, end + len(HEADER_END)


def _iter_slots(data: bytes, offset: int) -> Iterator[tuple[int, int, int, int, int]]:
    count = (len(data) - offset) // 16
    for i in range(count):
        header, t_lsb, d1, d2 = struct.unpack_from("<4I", data, offset + 16 * i)
        yield i, header, t_lsb, d1, d2


def read(path: str) -> Trace:
    with open(path, "rb") as f:
        return parse(f.read())


def parse(data: bytes) -> Trace:
    attributes, offset = parse_header(data)
    syspage_len = int(attributes.get("SYSPAGE_LEN", "0") or 0)
    trace = Trace(attributes, data[offset:offset + syspage_len])
    offset += syspage_len

    msb: Dict[int, int] = {}        # per CPU: high 32 bits of the clock
    last_lsb: Dict[int, int] = {}   # per CPU: last low word, to detect wraps
    pending: Dict[int, RawEvent] = {}

    def full_time(cpu: int, lsb: int) -> int:
        if cpu in last_lsb and lsb < last_lsb[cpu] and last_lsb[cpu] - lsb > 0x80000000:
            msb[cpu] = msb.get(cpu, 0) + 1
        last_lsb[cpu] = lsb
        return (msb.get(cpu, 0) << 32) | lsb

    for i, header, t_lsb, d1, d2 in _iter_slots(data, offset):
        kind = header >> 30
        cpu = (header >> 24) & 0x3F
        cls = (header >> 10) & 0x1F
        code = header & 0x3FF

        if kind == STRUCT_S:
            if cls == CLASS_CONTROL and code == CONTROL_TIME:
                # d1 carries the high word of the clock for this CPU.
                msb[cpu] = d1
                last_lsb[cpu] = t_lsb
            ev = RawEvent(i, cpu, cls, code, full_time(cpu, t_lsb), [d1, d2])
            trace.events.append(ev)
        elif kind == STRUCT_CB:
            pending[cpu] = RawEvent(i, cpu, cls, code, full_time(cpu, t_lsb), [d1, d2])
        else:  # CC or CE
            ev = pending.get(cpu)
            if ev is None or ev.cls != cls or ev.code != code:
                # A part without its beginning (buffer boundary or dropped
                # events). Skip it rather than guess.
                pending.pop(cpu, None)
                continue
            ev.words.extend((d1, d2))
            if kind == STRUCT_CE:
                trace.events.append(pending.pop(cpu))

    trace.events.sort(key=lambda e: (e.t_cycles, e.index))
    return trace


def text_from_words(words: List[int]) -> str:
    return _words_to_text(words)


def ker_call(ev: RawEvent) -> Optional[tuple[int, bool, bool]]:
    """For a KER_CALL event return (call number, is_exit, is_wide)."""
    if ev.cls != CLASS_KER_CALL:
        return None
    return ev.code & 0x7F, bool(ev.code & KER_EXIT_BIT), bool(ev.code & KER_WIDE_BIT)
