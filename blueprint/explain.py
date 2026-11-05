"""Explain a finding in plain words and propose a fix, using Claude.

The checks are deterministic and decide what is wrong. Claude only explains:
it gets one finding, the trace events behind it (each with an id), the
contract and, optionally, the source of the processes involved, and returns
JSON with the cause, the evidence it relied on, the QNX call at fault and a
patch. Anything it cites that was not in its input is removed before the
explanation is shown, and an explanation left without evidence is marked
unverified.

Explanations are cached on disk by a hash of the input, so a report can be
regenerated, and the demo can run, without network access or an API key.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import checks, contract as contract_mod, ipc, kev

MODEL = "claude-opus-5-5"
PROMPT_VERSION = "2"
KEY_FILE = os.path.expanduser("~/.config/blueprint/anthropic_key")

DOCS = {
    "MsgSend": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/m/msgsend.html",
    "MsgReceive": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/m/msgreceive.html",
    "MsgReply": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/m/msgreply.html",
    "MsgError": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/m/msgerror.html",
    "TimerTimeout": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/t/timertimeout.html",
    "ChannelCreate": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/c/channelcreate.html",
    "ConnectAttach": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/c/connectattach.html",
    "name_attach": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/n/name_attach.html",
    "name_open": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.lib_ref/topic/n/name_open.html",
    "Synchronous message passing": "https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.sys_arch/topic/ipc_Sync_messaging.html",
}

SYSTEM_PROMPT = """You explain architecture violations found in QNX 8.0 kernel traces to the \
engineer who owns the system. A deterministic checker has already decided what is wrong; \
your job is to explain why it happened and how to fix it, in terms of QNX message passing.

Background you can rely on:
- MsgSend() blocks the client: SEND-blocked until the server calls MsgReceive(), then \
REPLY-blocked until the server calls MsgReply() or MsgError(). A server that never replies \
keeps the client REPLY-blocked forever.
- Two processes that each send to the other while holding the other's message deadlock: \
neither can receive while it is itself blocked in MsgSend().
- Channels created by name_attach() have _NTO_CHF_UNBLOCK set. A REPLY-blocked client that \
times out (TimerTimeout) or is signalled does not unblock; the kernel sends the server a \
_PULSE_CODE_UNBLOCK pulse and the client stays blocked until the server replies or handles \
it. A send timeout therefore does not protect a client from a wedged server; isolating the \
call in its own thread, or a server that serves pulses while busy, does.
- When a server process dies, clients blocked on it are unblocked with an error, and later \
sends on the connection fail. A client that treats that error as fatal dies with the server.
- Priority inheritance: a server receiving a message runs at the client's priority, so \
"the server has lower priority" is usually not the cause of a slow reply. The exception is a \
channel created with _NTO_CHF_FIXED_PRIORITY: its receiving threads keep their own priority, \
so threads with a priority between client and server can run while the client waits.

Rules:
- Use only the events you are given. Cite them by their id in evidence_event_ids; cite only \
ids that appear in the input.
- Name the QNX call at fault (for example MsgSend in display, MsgReply missing in camera).
- The fix must be concrete: a small patch to the given source when source is provided, \
otherwise a precise description of the change. If the right fix is to change the contract \
instead of the code, say so.
- doc_refs may only contain names from the list in the input.
- Write for a busy engineer: cause in two or three sentences, no preamble."""

SCHEMA = {
    "type": "object",
    "properties": {
        "cause": {"type": "string"},
        "evidence_event_ids": {"type": "array", "items": {"type": "integer"}},
        "qnx_call": {"type": "string"},
        "fix_summary": {"type": "string"},
        "fix_patch": {"type": "string"},
        "doc_refs": {"type": "array", "items": {"type": "string", "enum": sorted(DOCS)}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["cause", "evidence_event_ids", "qnx_call", "fix_summary", "fix_patch",
                 "doc_refs", "confidence"],
    "additionalProperties": False,
}


@dataclass
class Explanation:
    finding_title: str
    cause: str
    evidence_event_ids: List[int]
    qnx_call: str
    fix_summary: str
    fix_patch: str
    doc_refs: Dict[str, str]
    confidence: str
    verified: bool = True
    dropped: List[str] = field(default_factory=list)
    model: str = MODEL
    usage: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return dict(self.__dict__)


class ExplainError(RuntimeError):
    pass


def evidence_pack(trace: kev.Trace, model: ipc.IpcModel, finding: checks.Finding,
                  limit: int = 60) -> List[dict]:
    """The trace events behind a finding, as numbered lines for the prompt."""
    t0 = trace.events[0].t_cycles if trace.events else 0

    def ms(t: Optional[int]) -> Optional[float]:
        return None if t is None else round(trace.seconds(t - t0) * 1000, 3)

    involved = set(finding.processes)
    seeds = set(finding.events)
    lines: Dict[int, dict] = {}

    def add(event: Optional[int], t: Optional[int], what: str) -> None:
        if event is not None and event not in lines:
            lines[event] = {"id": event, "t_ms": ms(t), "what": what}

    related = [tx for tx in model.transactions
               if tx.send_event in seeds
               or (model.name(tx.client_pid) in involved and model.name(tx.server_pid) in involved)]
    # Keep the seeds plus the last few exchanges between the involved processes.
    related = [tx for tx in related if tx.send_event in seeds] + \
              [tx for tx in related if tx.send_event not in seeds][-12:]
    for tx in related:
        c = f"{model.name(tx.client_pid)}[tid {tx.client_tid}]"
        s = f"{model.name(tx.server_pid)}[tid {tx.server_tid}]" if tx.server_tid else model.name(tx.server_pid)
        add(tx.send_event, tx.t_send,
            f"{c} MsgSend to {model.name(tx.server_pid)} (coid {tx.coid:#x}, chid {tx.chid}) "
            f"message {tx.preview!r}"
            + (f" ({ipc.IO_TYPES[tx.msg_type]}, resource manager protocol)"
               if tx.msg_type in ipc.IO_TYPES else ""))
        if tx.receive_event is not None:
            add(tx.receive_event, tx.t_receive, f"{s} MsgReceive got the message from {c}")
        if tx.reply_event is not None:
            verb = "MsgError" if tx.status == "error" else "MsgReply"
            add(tx.reply_event, tx.t_reply, f"{s} {verb} to {c} reply {tx.reply_preview!r}")
        elif tx.status in ("sent", "received"):
            state = "SEND" if tx.status == "sent" else "REPLY"
            add(-tx.send_event - 1, None,
                f"at trace end {c} is still {state}-blocked on {model.name(tx.server_pid)} "
                f"(no reply to event {tx.send_event})")
    for pid, t in model.deaths.items():
        if model.name(pid) in involved:
            add(model.death_events[pid], t, f"process {model.name(pid)} (pid {pid}) destroyed")
    sched = {index: (t, cpu, pid, tid, state, prio)
             for t, cpu, pid, tid, state, prio, index in model.sched if index in seeds}
    for index, (t, cpu, pid, tid, state, prio) in sched.items():
        add(index, t, f"{model.name(pid)}[tid {tid}] is {ipc.STATE_NAMES.get(state, state)} "
                      f"at priority {prio} on CPU {cpu}")
    for (pid, chid), ch in model.channels.items():
        if model.name(pid) in involved and finding.check == "priority-inversion":
            flags = [n for bit, n in ((0x1, "_NTO_CHF_FIXED_PRIORITY"), (0x2, "_NTO_CHF_UNBLOCK"),
                                      (0x8, "_NTO_CHF_DISCONNECT"), (0x40, "_NTO_CHF_COID_DISCONNECT"))
                     if ch.flags & bit]
            add(-(20_000_000 + chid + 1000 * (pid % 1000)), ch.t,
                f"{model.name(pid)} ChannelCreate chid {chid} flags {ch.flags:#x} ({' | '.join(flags) or 'none'})")
    for i, line in enumerate(finding.data.get("from_snapshot", [])):
        add(-(10_000_000 + i), None, f"{line} (state when the trace stopped)")
    for seed in seeds:
        if seed not in lines:
            add(seed, None, "event referenced by the checker")
    ordered = sorted(lines.values(), key=lambda l: (l["t_ms"] is None, l["t_ms"] or 0, l["id"]))
    return ordered[:limit]


def build_prompt(finding: checks.Finding, pack: List[dict], con: contract_mod.Contract,
                 contract_text: str, sources: Dict[str, str]) -> str:
    parts = [
        "Finding from the checker:",
        json.dumps({k: finding.to_json()[k] for k in ("check", "severity", "title", "detail", "processes")},
                   indent=1),
        "\nTrace events (ids are what you cite; negative ids are end-of-trace states):",
        "\n".join(f"[{l['id']}] {'' if l['t_ms'] is None else str(l['t_ms']) + ' ms '}{l['what']}" for l in pack),
        f"\nArchitecture contract ({os.path.basename(con.source) or 'blueprint.yaml'}):\n{contract_text.strip()}",
    ]
    for path, text in sorted(sources.items()):
        parts.append(f"\nSource {os.path.basename(path)}:\n{text}")
    parts.append("\ndoc_refs may be chosen from: " + ", ".join(sorted(DOCS)))
    return "\n".join(parts)


def plain_dashes(value):
    """Replace en and em dashes in model text with a plain hyphen, recursively."""
    if isinstance(value, str):
        return value.replace("\u2014", "-").replace("\u2013", "-")
    if isinstance(value, list):
        return [plain_dashes(v) for v in value]
    if isinstance(value, dict):
        return {k: plain_dashes(v) for k, v in value.items()}
    return value


def cache_key(prompt: str) -> str:
    h = hashlib.sha256()
    for piece in (MODEL, PROMPT_VERSION, SYSTEM_PROMPT, json.dumps(SCHEMA, sort_keys=True), prompt):
        h.update(piece.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:24]


def validate(raw: dict, finding: checks.Finding, pack: List[dict]) -> Explanation:
    allowed = {l["id"] for l in pack}
    ids, dropped = [], []
    for i in raw.get("evidence_event_ids", []):
        (ids if i in allowed else dropped).append(i)
    refs = {name: DOCS[name] for name in raw.get("doc_refs", []) if name in DOCS}
    return Explanation(
        finding_title=finding.title, cause=raw["cause"], evidence_event_ids=ids,
        qnx_call=raw["qnx_call"], fix_summary=raw["fix_summary"], fix_patch=raw["fix_patch"],
        doc_refs=refs, confidence=raw["confidence"], verified=bool(ids),
        dropped=[f"event {i} is not in the evidence" for i in dropped],
    )


def _api_key() -> Optional[str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    return None


def ask_claude(prompt: str, system: Optional[str] = None, schema: Optional[dict] = None) -> tuple[dict, dict]:
    """One structured call; system and schema default to the explanation ones."""
    system = SYSTEM_PROMPT if system is None else system
    schema = SCHEMA if schema is None else schema
    try:
        import anthropic
    except ImportError as e:
        raise ExplainError("the anthropic package is needed for explanations (pip install anthropic)") from e
    key = _api_key()
    if not key:
        raise ExplainError(f"no API key: set ANTHROPIC_API_KEY or put it in {KEY_FILE}")
    client = anthropic.Anthropic(api_key=key)
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": schema}},
            # If a safety classifier declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise ExplainError("the API key was rejected") from e
    except anthropic.RateLimitError as e:
        raise ExplainError("rate limited by the API, try again later") from e
    except anthropic.BadRequestError as e:
        raise ExplainError(f"the API rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise ExplainError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ExplainError("could not reach the API") from e

    if response.stop_reason == "refusal":
        raise ExplainError("the model declined to explain this finding")
    if response.stop_reason == "max_tokens":
        raise ExplainError("the explanation was cut off (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), "")
    usage = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
    }
    return json.loads(text), usage


def explain(trace: kev.Trace, model: ipc.IpcModel, finding: checks.Finding,
            con: contract_mod.Contract, contract_text: str, sources: Dict[str, str],
            cache_dir: str, offline: bool = False) -> Explanation:
    pack = evidence_pack(trace, model, finding)
    prompt = build_prompt(finding, pack, con, contract_text, sources)
    key = cache_key(prompt)
    path = os.path.join(cache_dir, f"{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        exp = validate(plain_dashes(cached["raw"]), finding, pack)
        exp.usage = cached.get("usage", {})
        return exp
    if offline:
        raise ExplainError("no cached explanation for this finding and --offline was given")
    raw, usage = ask_claude(prompt)
    raw = plain_dashes(raw)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"raw": raw, "usage": usage, "finding": finding.title}, f, indent=2)
    exp = validate(raw, finding, pack)
    exp.usage = usage
    return exp
