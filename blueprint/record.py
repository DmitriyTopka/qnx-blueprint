"""Record a trace on a running QNX 8.0 target over ssh, optionally killing processes.

The target needs only what a stock QNX 8.0 image has: sh, tracelogger, pidin,
slay and sleep. Nothing is installed on it. Files come back through `ssh cat`,
so any ssh command works, including one wrapped in `docker exec`.

    blueprint record root@target -c blueprint.yaml --inject camera@1.5 -o run.kev
    blueprint record root@target -c blueprint.yaml --inject qpp@1.5:stop -o hang.kev
    blueprint record root@target -c blueprint.yaml --inject 'io-sock[prefix=/can]@2:stop'

The trace starts first and the kills happen while it runs, so the death of each
process is in the trace with everything that happened to its clients after it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass

SSH_DEFAULT = "ssh -o BatchMode=yes"
REMOTE_DIR = "/tmp/blueprint-rec"


class RecordError(Exception):
    pass


@dataclass(frozen=True)
class Injection:
    process: str
    at: float  # seconds after the trace started
    action: str = "kill"   # kill: SIGKILL; stop: SIGSTOP, continued after the trace and pidin
    match: str = ""        # NAME[TEXT]: only instances whose command line contains TEXT

    @staticmethod
    def parse(text: str) -> "Injection":
        name, sep, at = text.partition("@")
        if not name or not sep:
            raise RecordError(f"--inject wants NAME@SECONDS or NAME@SECONDS:stop, got {text!r}")
        match = ""
        if name.endswith("]") and "[" in name:
            name, _, match = name[:-1].partition("[")
            if not match or not all(c.isalnum() or c in "-_./=,:" for c in match):
                raise RecordError(f"--inject {text!r}: {match!r} is not a plain command line fragment")
        at, _, action = at.partition(":")
        action = action or "kill"
        if action not in ("kill", "stop"):
            raise RecordError(f"--inject {text!r}: {action!r} is not kill or stop")
        try:
            value = float(at)
        except ValueError:
            raise RecordError(f"--inject {text!r}: {at!r} is not a number of seconds") from None
        if value < 0:
            raise RecordError(f"--inject {text!r}: time must not be negative")
        if not all(c.isalnum() or c in "-_." for c in name):
            raise RecordError(f"--inject {text!r}: {name!r} is not a plain process name")
        return Injection(name, value, action, match)

    @property
    def label(self) -> str:
        return f"{self.process}[{self.match}]" if self.match else self.process


def remote_script(duration: float, injections: list[Injection], start: str = "",
                  buffers: int = 0) -> str:
    """The sh script that runs on the target.

    It prints one line per injection, `inject <name> <processes killed> <epoch>`,
    so the host knows whether each kill hit a running process.
    """
    if duration <= 0:
        raise RecordError("duration must be positive")
    late = [i for i in injections if i.at >= duration]
    if late:
        raise RecordError(f"--inject {late[0].process}@{late[0].at:g} is after the trace ends "
                          f"({duration:g} s)")
    q = shlex.quote
    lines = [
        "#!/bin/sh",
        f"D={REMOTE_DIR}",
        "rm -rf $D; mkdir -p $D || exit 3",
        "command -v tracelogger >/dev/null || { echo 'no tracelogger on the target' >&2; exit 3; }",
        # -w: wide mode, the arguments that tie a message to its channel.
        f"tracelogger -w -s {duration:g}{f' -k {buffers}' if buffers else ''} -f $D/run.kev >$D/tracelogger.log 2>&1 &",
        "TL=$!",
        "sleep 0.3",
    ]
    if start:
        # Started inside the trace, so its ChannelCreate and ConnectAttach are recorded.
        lines += [f"( {start} ) >$D/start.log 2>&1 &", "sleep 1"]
    t = 0.3 + (1.0 if start else 0.0)
    for inj in sorted(injections, key=lambda i: i.at):
        wait = max(inj.at - t, 0)
        if wait:
            lines.append(f"sleep {wait:g}")
        t = max(t, inj.at)
        name = q(inj.process)
        sig = "STOP" if inj.action == "stop" else "KILL"
        if inj.match:
            # Two instances of one binary (a second io-sock) share the name:
            # pick by command line, so the other one (and ssh) is untouched.
            lines.append(_by_args(inj, "BPPIDS"))
            lines.append(f"N=0; for p in $BPPIDS; do kill -s {sig} $p && N=$((N+1)); done; "
                         f"echo \"inject {inj.process} $N $(date +%s) {inj.action}\"")
        else:
            # slay exits with the number of processes it signalled, so 0 means
            # nothing by that name was running.
            lines.append(f"slay -Q -s {sig} {name} >/dev/null 2>&1; "
                         f"echo \"inject {inj.process} $? $(date +%s) {inj.action}\"")
    lines += [
        "wait $TL",
        # Threads that were blocked before the trace started emit no events;
        # pidin shows who they wait on.
        "pidin >$D/run.pidin 2>&1",
    ]
    # A stopped process is continued only after pidin has seen it STOPPED.
    for inj in injections:
        if inj.action == "stop" and inj.match:
            lines.append(_by_args(inj, "BPPIDS"))
            lines.append("for p in $BPPIDS; do kill -s CONT $p; done")
        elif inj.action == "stop":
            lines.append(f"slay -Q -s CONT {q(inj.process)} >/dev/null 2>&1")
    lines += [
        "ls -l $D/run.kev >&2",
    ]
    return "\n".join(lines) + "\n"


def _by_args(inj: Injection, var: str) -> str:
    """sh that sets $var to the pids of `inj.process` whose command line has `inj.match`.
    `pidin -f anA` prints pid, name and arguments on one line."""
    prog = ("{n=$2; sub(/.*\\//, \"\", n)} n == name && index($0, text) {print $1}")
    return (f"{var}=$(pidin -f anA | awk -v name={shlex.quote(inj.process)} "
            f"-v text={shlex.quote(inj.match)} {shlex.quote(prog)})")


def _ssh(ssh: str, host: str) -> list[str]:
    return shlex.split(ssh) + [host]


def record(host: str, out: str, duration: float, injections: list[Injection],
           start: str = "", ssh: str = SSH_DEFAULT, buffers: int = 0,
           run=subprocess.run) -> dict:
    """Record on the target and copy run.kev and run.pidin next to `out`.

    Returns the injection log, also written to <out without .kev>.inject.json.
    """
    script = remote_script(duration, injections, start, buffers)
    base = _ssh(ssh, host)
    proc = run(base + ["sh", "-s"], input=script.encode(), capture_output=True,
               timeout=duration + 60)
    if proc.returncode != 0:
        raise RecordError(f"recording failed on {host} (exit {proc.returncode}): "
                          f"{proc.stderr.decode(errors='replace').strip()[-400:]}")
    log = []
    for line in proc.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) in (4, 5) and parts[0] == "inject":
            killed = int(parts[2]) if parts[2].isdigit() else 0
            log.append({"process": parts[1], "killed": killed, "hit": killed > 0,
                        "target_time": int(parts[3]) if parts[3].isdigit() else None,
                        "action": parts[4] if len(parts) == 5 else "kill"})
    stem = out[:-4] if out.endswith(".kev") else out
    files = {f"{REMOTE_DIR}/run.kev": stem + ".kev", f"{REMOTE_DIR}/run.pidin": stem + ".pidin"}
    os.makedirs(os.path.dirname(os.path.abspath(stem)), exist_ok=True)
    for remote, local in files.items():
        got = run(base + ["cat", remote], capture_output=True, timeout=120)
        if got.returncode != 0 or not got.stdout:
            raise RecordError(f"could not copy {remote} from {host}: "
                              f"{got.stderr.decode(errors='replace').strip()[-200:]}")
        with open(local, "wb") as f:
            f.write(got.stdout)
    run(base + ["rm", "-rf", REMOTE_DIR], capture_output=True, timeout=30)
    result = {"host": host, "duration_s": duration, "start": start or None,
              "injections": [{"process": i.process, "at_s": i.at, "action": i.action,
                              **({"match": i.match} if i.match else {})} for i in injections],
              "log": log, "trace": stem + ".kev", "snapshot": stem + ".pidin"}
    with open(stem + ".inject.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return result


def check_injections(result: dict, deaths: list[str]) -> list[str]:
    """Warnings for kills that missed: the process was not running, or its
    death did not make it into the trace."""
    warnings = []
    for entry in result["log"]:
        stop = entry.get("action") == "stop"
        if not entry["hit"]:
            warnings.append(f"{entry['process']} was not running when Blueprint tried to "
                            f"{'stop' if stop else 'kill'} it")
        elif not stop and entry["process"] not in deaths:
            warnings.append(f"{entry['process']} was killed but its death is not in the trace "
                            f"(trace buffer too small?)")
    return warnings


def print_log(result: dict, out=None) -> None:
    out = out or sys.stdout
    for entry in result["log"]:
        verb = "stopped (continued after the trace)" if entry.get("action") == "stop" else "killed"
        state = f"{verb} {entry['killed']} process(es)" if entry["hit"] else "NOT RUNNING"
        print(f"inject {entry['process']}: {state}", file=out)
