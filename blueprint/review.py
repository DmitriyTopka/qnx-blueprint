"""Review a draft contract with Claude: which edges look like they were not designed.

`blueprint init` writes down every message path the trace showed, including
a backdoor someone added last week. Reading a draft of twenty edges, an
engineer approves them all. `init --review` asks Claude to look at each edge
with its evidence (message count, the first send, what the messages say in
wide mode, who else talks to the same server) and the source you pass, and
to mark the ones worth a question. The marks go into the draft as comments;
nothing is removed, and the contract is still the engineer's to write.

Like explanations, answers are checked against the input: an edge Claude
names that is not in the draft is dropped, an event id it cites that it was
not given is dropped, and a mark with no evidence left is shown as unverified.
Answers are cached by a hash of the input, so a rerun costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Dict, List, Optional, Tuple

from . import explain, ipc, kev, topology

PROMPT_VERSION = "1"

SYSTEM = """You review a draft architecture contract for a QNX 8.0 system. The draft was \
generated from one kernel trace: it lists every message path (client -> server) the system \
used. The engineer will delete what they did not design. Your job is to point at the edges \
that deserve a question, so they are not approved by habit.

An edge deserves a question when, for example: a process reaches past the layer that \
normally serves it (display reads gps directly while nav is the one that talks to gps); a \
server calls back up to its own client (a loop that can deadlock); a path is used only once \
or a handful of times in a trace where its neighbours run every cycle; or the messages on it \
say something different from the rest of the system. Most edges are fine: say keep for them. \
Do not invent architecture you cannot see in the evidence.

For each edge give a verdict (keep, question or suspicious), one or two sentences of reason \
written for the engineer, and the ids of the events you relied on, chosen only from the ids \
you were given. Plain hyphens, no long dashes."""

SCHEMA = {
    "type": "object",
    "properties": {
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "client": {"type": "string"},
                    "server": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["keep", "question", "suspicious"]},
                    "reason": {"type": "string"},
                    "evidence_event_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["client", "server", "verdict", "reason", "evidence_event_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["edges"],
    "additionalProperties": False,
}

EDGE_LINE = re.compile(r"^(  - )([\w.-]+) -> ([\w.-]+)(\s+# .*)?$")


def evidence(trace: kev.Trace, model: ipc.IpcModel, processes: List[str]) -> List[dict]:
    """Per edge: counts, timing, the first sends and what they carried."""
    topo = topology.build(model, processes)
    t0 = trace.events[0].t_cycles if trace.events else 0
    out = []
    for (client, server), e in sorted(topo.edges.items()):
        if client == server:
            continue
        txs = [tx for tx in model.transactions if model.name(tx.client_pid) == client
               and model.name(tx.server_pid) == server]
        first = txs[:3]
        out.append({
            "client": client, "server": server, "messages": e.messages, "errors": e.errors,
            "unanswered": e.open,
            "first_sends": [{"id": tx.send_event, "t_ms": round(trace.seconds(tx.t_send - t0) * 1000, 1),
                             "says": tx.preview, "reply": tx.reply_preview} for tx in first],
            "other_clients_of_server": sorted({c for c, s in topo.edges if s == server and c != client}),
        })
    return out


def build_prompt(draft_text: str, edges: List[dict], sources: Dict[str, str]) -> str:
    parts = ["Draft contract:", draft_text.strip(),
             "\nEvidence per edge (ids are what you cite):", json.dumps(edges, indent=1)]
    for path, text in sorted(sources.items()):
        parts.append(f"\nSource {os.path.basename(path)}:\n{text}")
    return "\n".join(parts)


def cache_key(prompt: str) -> str:
    h = hashlib.sha256()
    for piece in (explain.MODEL, PROMPT_VERSION, SYSTEM, json.dumps(SCHEMA, sort_keys=True), prompt):
        h.update(piece.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:24]


def validate(raw: dict, edges: List[dict]) -> Dict[Tuple[str, str], dict]:
    """Keep only marks on edges in the draft, citing events Claude was given."""
    known = {(e["client"], e["server"]): {s["id"] for s in e["first_sends"]} for e in edges}
    allowed = set().union(*known.values()) if known else set()
    out = {}
    for m in raw.get("edges", []):
        key = (m.get("client"), m.get("server"))
        if key not in known:
            continue
        ids = [i for i in m.get("evidence_event_ids", []) if i in allowed]
        out[key] = {"verdict": m["verdict"], "reason": explain.plain_dashes(m["reason"]).strip(),
                    "evidence_event_ids": ids, "verified": bool(ids)}
    return out


def annotate(draft_text: str, marks: Dict[Tuple[str, str], dict]) -> str:
    """Add each question or suspicious mark as comment lines above its edge."""
    lines = []
    for line in draft_text.splitlines():
        m = EDGE_LINE.match(line)
        mark = marks.get((m.group(2), m.group(3))) if m else None
        if mark and mark["verdict"] != "keep":
            ids = ", ".join(map(str, mark["evidence_event_ids"]))
            tag = mark["verdict"].upper() + ("" if mark["verified"] else ", unverified")
            reason = " ".join(mark["reason"].split())
            lines.append(f"  # review ({tag}): {reason}" + (f" Events {ids}." if ids else ""))
        lines.append(line)
    return "\n".join(lines) + "\n"


def review(trace: kev.Trace, model: ipc.IpcModel, processes: List[str], draft_text: str,
           sources: Dict[str, str], cache_dir: str, offline: bool = False) -> Tuple[str, Dict]:
    """(annotated draft, marks). Raises explain.ExplainError without a cached answer or API key."""
    edges = evidence(trace, model, processes)
    prompt = build_prompt(draft_text, edges, sources)
    path = os.path.join(cache_dir, f"review-{cache_key(prompt)}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)["raw"]
    elif offline:
        raise explain.ExplainError("no cached review for this draft and --offline was given")
    else:
        raw, usage = explain.ask_claude(prompt, SYSTEM, SCHEMA)
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"raw": raw, "usage": usage}, f, indent=2)
    marks = validate(raw, edges)
    return annotate(draft_text, marks), marks
