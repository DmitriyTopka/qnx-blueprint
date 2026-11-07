# Blueprint

[![ci](https://github.com/DmitriyTopka/qnx-blueprint/actions/workflows/ci.yml/badge.svg)](https://github.com/DmitriyTopka/qnx-blueprint/actions/workflows/ci.yml)

One contract, two enforcers: Blueprint checks a QNX 8.0 system against its
message architecture in CI, and turns the same file into a security policy
the kernel enforces.

A QNX system is a set of processes that talk through synchronous messages. The
design usually lives in someone's head or on a whiteboard: display asks nav,
nav asks gps, nobody calls back up the chain, and a dead camera must not take
the screen with it. Blueprint lets you write that design down in a few lines of
YAML, the contract.

- **Before release** it checks a real kernel trace against the contract. When
  the system does something the design does not allow, the build fails and
  the report says which process did what, with the trace events to prove it.
- **After release** the same file becomes the channel rules of a QNX security
  policy. Booted in secure mode, the kernel refuses a connection the contract
  does not allow: `cockpit: cannot open cockpit-gps: Operation not permitted`.

System Profiler shows you a trace. Blueprint knows what the trace should look
like and tells you where it does not.

**See it without installing anything:** the reports for every recorded
scenario and the codelab are at
[dmitriytopka.github.io/qnx-blueprint](https://dmitriytopka.github.io/qnx-blueprint/).
A 2:53 video of all of it: [youtu.be/rqJFxPH5Bjs](https://youtu.be/rqJFxPH5Bjs).

## For judges

Links point at release v0.3.0, so the lines stay where they are.

| Criterion | Where to look | What it shows |
|---|---|---|
| Technological Implementation | [`kev.py`](https://github.com/DmitriyTopka/qnx-blueprint/blob/v0.3.0/blueprint/kev.py), [`ipc.py`](https://github.com/DmitriyTopka/qnx-blueprint/blob/v0.3.0/blueprint/ipc.py), [`checks.py`](https://github.com/DmitriyTopka/qnx-blueprint/blob/v0.3.0/blueprint/checks.py), [docs/TRACEPRINTER.md](https://github.com/DmitriyTopka/qnx-blueprint/blob/v0.3.0/docs/TRACEPRINTER.md) | the trace format read from scratch and compared with QNX's `traceprinter`: 1,071,509 events in 25 traces, none missing or different; 137 tests |
| Design | `blueprint demo`, [the reports](https://dmitriytopka.github.io/qnx-blueprint/), [`action.yml`](https://github.com/DmitriyTopka/qnx-blueprint/blob/v0.3.0/action.yml) | one command with no target, an HTML report, JUnit XML and a GitHub Action for CI |
| Potential Impact | [demo/external/qpp](https://github.com/DmitriyTopka/qnx-blueprint/tree/v0.3.0/demo/external/qpp), [demo/external/socketcan](https://github.com/DmitriyTopka/qnx-blueprint/tree/v0.3.0/demo/external/socketcan) | two BlackBerry systems we did not write: a stopped server found, and eight weak spots in Blueprint fixed on their traces |
| Innovation & AI Creativity | [`explain.py`](https://github.com/DmitriyTopka/qnx-blueprint/blob/v0.3.0/blueprint/explain.py), [`review.py`](https://github.com/DmitriyTopka/qnx-blueprint/blob/v0.3.0/blueprint/review.py), [`mcp_server.py`](https://github.com/DmitriyTopka/qnx-blueprint/blob/v0.3.0/blueprint/mcp_server.py) | Claude explains and reviews but never judges; an event id it cites that the trace does not have is dropped |
| Codelab bonus | [the contract codelab](https://dmitriytopka.github.io/qnx-blueprint/codelab/), [the secure-image codelab](https://dmitriytopka.github.io/qnx-blueprint/codelab/secure-image/) | a secure QNX 8.0 image from a policy you can check, with [a recorded live run](https://youtu.be/OHoixOUmPH8) |
| QNX-specific bonus | [demo/cockpit/secpol/evidence](https://github.com/DmitriyTopka/qnx-blueprint/tree/v0.3.0/demo/cockpit/secpol/evidence) | `mkqnximage --secpol=secure` on x86_64 and aarch64: serial logs, the compiled policy matching `/proc/boot/secpol.bin`, the kernel saying `Operation not permitted` |

## Try it in one minute

No QNX target, SDP or API key needed. The repository ships traces recorded on
QNX 8.0 (x86_64, QEMU) from the demo system in `demo/cockpit`.

```
python3 -m venv .venv && . .venv/bin/activate      # or any Python 3.10+ environment
python3 -m pip install .
blueprint demo
```

Without a clone, `python3 -m pip install git+https://github.com/DmitriyTopka/qnx-blueprint`
installs the same recorded traces with the package, and `blueprint demo` runs
from any directory.

```
scenario              expected  got   checks                            story
ok                    pass      pass  -                                 The system as designed.
deadlock              fail      fail  unexpected-edge, wait-cycle       gps asks nav for a hint while nav is waiting on gps.
backdoor              fail      fail  unexpected-edge                   display reads gps directly, bypassing nav.
camera-hang           fail      fail  isolation                         The camera stops answering; display has no protection and freezes.
camera-hang-fixed     pass      pass  -                                 Same stuck camera, display reads it from its own thread.
...
```

Open `out/deadlock.html` for the report, or load `out/deadlock.perfetto.json`
at [ui.perfetto.dev](https://ui.perfetto.dev) to see every message as an arrow
between threads.

A step-by-step tutorial in the QNX codelab format is in
[docs/codelab](docs/codelab/blueprint-architecture-contracts.md), and answers
to the usual questions (overhead, false alarms, what Claude sees, what is not
covered) are in [docs/FAQ.md](docs/FAQ.md).

## The contract

```yaml
system: cockpit
processes: [gps, nav, camera, logger, display]
allow:                      # who may send messages to whom
  - display -> nav
  - display -> camera
  - display -> logger
  - nav -> gps
  - nav -> logger
reply_budget_ms:            # how long a server may keep a client waiting
  nav: 50
  default: 20
isolation:                  # when camera dies or hangs, display keeps working
  camera:
    must_survive: [display]
    min_progress: 3
priority_inversion_ms: 2    # experimental: time a waiting client may lose
                            # to lower priority work
cycle_ms:                   # real-time loops, from the client's side:
  display -> nav: 100       # display needs a route from nav at least every 100 ms
```

`reply_budget_ms` is the server's promise, `cycle_ms` the client's deadline:
display draws a frame about every 55 ms and needs nav's answer for each one.
The demo's `realtime.yaml` is `blueprint.yaml` plus that line.

To start from a system that already runs, record a trace and let Blueprint
write the first draft:

```
blueprint init run.kev --system cockpit -o blueprint.yaml
```

It lists the application processes that message each other (QNX services
such as procnto, devb-* and io-sock are left out, `--only` picks them by
hand), every edge it saw with its message count, and reply budgets at four
times the slowest reply in that trace. A draft taken from a trace with a
backdoor contains the backdoor, so read it and delete what you did not
design.

`--review` asks Claude to read the draft with you. For every edge it gets
the message count, the first sends and what they carried, who else talks to
the same server, and the source you pass with `--source`, and it marks the
edges worth a question as comments in the draft. On the backdoor trace, with
no source passed, it marked one edge of six:

```
allow:
  - display -> camera             # 50 messages
  # review (SUSPICIOUS): Display reads gps directly, bypassing nav, which already queries gps every cycle. Display sends the same 0x01 request as nav, a few tenths of a millisecond after nav's query; confirm this direct path was designed and is not a leftover shortcut. Events 57138, 57457, 58999, 57029, 57363, 58905.
  - display -> gps                # 50 messages
  - display -> logger             # 50 messages
```

That answer cost about 0.03 USD (2,075 input and 905 output tokens) and is
cached in `demo/cockpit/explanations`, so
`blueprint init demo/cockpit/traces/backdoor.kev --system cockpit --review --offline --cache demo/cockpit/explanations`
prints it again without a key. It needs `pip install 'qnx-blueprint[explain]'`.

Nothing is removed. An edge Claude names that is not in the draft, and an
event id it was not given, are dropped; a mark with no evidence left says
unverified. Reviews are cached like explanations.

## Checks

| Check | Fails when |
|---|---|
| `unexpected-edge` | a process sends messages to a server the contract does not allow |
| `wait-cycle` | threads end the trace waiting on each other in a loop (deadlock) |
| `stuck-reply` | a server took a message and never replied |
| `reply-budget` | a server replied, but slower than its budget |
| `cycle-overrun` | a client went longer than its `cycle_ms` without an answer, or its loop stopped |
| `kernel-denied` | the kernel refused a connection with EPERM: a security policy at work |
| `isolation` | a process died or stopped answering and took a protected process down with it |
| `no-traffic` | the trace shows no answered message at all, so it proves nothing |
| `trace-loss` | kernel buffers are missing from the trace (a gap in the per-CPU buffer sequence), so no verdict on it can be trusted |
| `priority-inversion` | a client waits on a lower priority server while threads with a priority in between use the CPU (experimental, on when the contract sets `priority_inversion_ms`) |

A `read()` that waits in the server for data (a socket, a device, a pipe) is
the client waiting for input, not the server's latency: when the thread that
took it spent the wait parked (CONDVAR, SEM, back in MsgReceive), it counts
neither against `reply_budget_ms` nor as a stuck reply. If the data must come
in time, give the client a `cycle_ms`.

Every finding lists the trace events it is based on. Exit code 0 means pass,
1 means the contract is violated, 2 means bad input.

### Where the time went

A slow reply or a missed cycle comes with a breakdown of its worst case,
read from the thread states in the trace. The parts add up to the whole:

```
FAIL  [reply-budget] logger answers display too slowly
      15 of 31 replies from logger to display took longer than 20 ms; worst 52.6 ms. ...
      where: worst 52.6 ms: server preempted 50.4 ms (mostly media at priority 20, 50.3 ms), server running 2.0 ms, ...
FAIL  [cycle-overrun] display -> nav cycle missed its period: worst gap 114 ms against 100 ms
      where: in the longest gap (113.8 ms) display spent: sleeping (NANOSLEEP) 55.3 ms,
      waiting on logger 48.5 ms (there: server preempted 46.6 ms (mostly media at priority 20, 45.1 ms)), ...
```

Queued behind another client, preempted (by which thread, at what priority,
wall time), running, sleeping, or waiting on its own call to another server,
followed one level into that server. The HTML report draws it as a bar.

## Record on your target, with faults

If the target runs sshd, one command records and checks:

```
blueprint record root@192.168.1.20 -c blueprint.yaml --inject camera@1.5 -d 5 \
    -o run.kev --html report.html
```

This starts a wide trace on the running system, kills `camera` with SIGKILL
1.5 seconds in, saves `pidin` when the trace stops, copies `run.kev` and
`run.pidin` back and checks them against the contract. Nothing is installed on
the target: it only needs `tracelogger`, `pidin`, `slay` and `sleep` from a
stock QNX 8.0 image. `--inject` takes any process from the contract and can be
repeated; `--inject qpp@1.5:stop` freezes it with SIGSTOP instead of killing it,
and continues it after `pidin` has seen it stopped. When two instances of one
binary run, `--inject 'io-sock[prefix=/can]@2:stop'` picks the one whose
command line contains `prefix=/can` and leaves the other alone. `--start "..."` launches the system inside the trace instead of
attaching to one that is already up, and `--ssh "..."` replaces the ssh command
(for a jump host or a VM in a container). A kill that finds no such process is
reported, so a test that never broke anything cannot pass by accident.

Recorded on QNX 8.0 in QEMU against the demo system:

```
$ blueprint record root@localhost -c demo/cockpit/blueprint.yaml --inject camera@1.5 -d 4
inject camera: killed 1 process(es)
FAIL  [isolation] camera died and took display down with it
      camera died; display died 38 ms later.
FAIL: cockpit 1 error, 0 warnings
```

The same command against the fixed display passes.

Without ssh, record by hand and copy the files:

```
tracelogger -w -s 5 -f /tmp/run.kev      # -w: wide mode, see Limits
pidin > /tmp/run.pidin                   # thread states when the trace stopped
```

```
blueprint check run.kev --contract blueprint.yaml --html report.html --perfetto run.perfetto.json
```

`--junit results.xml` writes JUnit XML with one test per check, for GitLab,
Jenkins or any other CI that reads test results.

The `.pidin` file next to the trace is picked up automatically. It matters when
the system was already stuck before tracing started: blocked threads produce no
events, and only `pidin` shows who waits on whom.

## Let the kernel enforce it

A trace shows what the system did. A QNX security policy decides what it may
do. Blueprint writes the channel rules of a policy from the contract:

```
blueprint secpol -c blueprint.yaml --attach nav=/dev/name/local/cockpit-nav ... -o cockpit.secpol
```

```
type display_t;
allow_attach nav_t /dev/name/local/cockpit-nav;
# display -> nav
allow display_t nav_t:channel connect;
```

The output compiles with `secpolcompile`. It covers only the processes in the
contract, so merge it into the policy for the whole system.

It also works the other way. `secpolgenerate` learns a policy by watching the
system, and it learns whatever happened, a bad build included. Check what it
learned before you ship it:

```
$ blueprint secpol -c blueprint.yaml --check learned.secpol
FAIL  [secpol] policy lets display connect to gps, which the contract forbids
      learned.secpol line 202 allows display_t to connect to gps_t. ...
```

`blueprint check run.kev --secpol learned.secpol` runs the same comparison
next to the trace checks. On QNX 8.0 (x86_64 and aarch64) booted in fully
secure mode with a policy that matches the contract, the backdoor build fails in the kernel:
`cockpit: cannot open cockpit-gps: Operation not permitted`, and so does
`open()` on the gps resource manager. How the demo
policies were made is in [demo/cockpit/secpol](demo/cockpit/secpol/README.md). A codelab walks through all of it from scratch:
[Lock a QNX 8.0 system to its design](https://dmitriytopka.github.io/qnx-blueprint/codelab/secure-image/).

The refusal is in the trace too. A `ConnectAttach` the policy denies exits
with `ret_val:-1, errno:1`, and a trace recorded on the secure image fails with

```
FAIL  [kernel-denied] the kernel refused display's connection to gps, which the contract forbids
      ConnectAttach from display to gps failed 140 time(s) with EPERM: the security policy
      blocked the path the contract leaves out. The build still tries it, ...
```

A refused edge that the contract allows means the policy and the contract
disagree, or the process was started without its type. The secure-mode traces
are in `tests/data/secure/`, and the serial logs and sessions of both secure
images in [demo/cockpit/secpol/evidence](demo/cockpit/secpol/evidence/README.md).

## Explanations by Claude

```
export ANTHROPIC_API_KEY=...
blueprint check run.kev -c blueprint.yaml --explain --source src/display.c --html report.html
```

The checks decide what is wrong. Claude is only asked to explain one finding at
a time: it gets the trace events behind it (each with an id), the contract and
the source you pass, and returns the cause, the QNX call at fault and a patch.
Event ids it did not receive are removed, and an explanation left with no real
evidence is marked unverified. Answers are cached next to the contract, so the
demo and CI runs work offline. One explanation costs about 0.06 USD with
`claude-opus-5-5`.

## For coding assistants (MCP)

```
python3 -m pip install "qnx-blueprint[mcp] @ git+https://github.com/DmitriyTopka/qnx-blueprint"
blueprint mcp
```

`blueprint mcp` is an MCP server on stdio with five tools: `check_trace`,
`draft_contract`, `write_policy`, `check_policy` and `list_demo_traces`. Add it
to your assistant's MCP configuration (command `blueprint`, argument `mcp`)
and it can check a trace, read where the time went and propose a fix in one
conversation. The tools read local files only; recording a trace over ssh
stays a command you run yourself.

## In CI

```yaml
- uses: DmitriyTopka/qnx-blueprint@v0.3.0
  with:
    trace: traces/nightly.kev
    contract: blueprint.yaml
```

The action writes `report.html`, `report.json`, `junit.xml` (one test per
check, for any CI test view) and a Perfetto trace to `blueprint-report/`, adds a summary to the job page and fails the job on any
violation.

## What it uses from QNX

| File | QNX feature | Why |
|---|---|---|
| `blueprint/kev.py` | kernel trace file format, instrumented kernel `procnto-smp-instr` | reads `tracelogger` output on any host, no SDP needed |
| `blueprint/ipc.py` | `MsgSendv`, `MsgSendvnc`, `MsgReceivev`, `MsgReplyv`, `MsgError` kernel calls; COMM `SND_MESSAGE`, `REPLY_MESSAGE`; `ConnectAttach`, `ChannelCreate` | rebuilds every message transaction with client, server, channel and timing |
| `blueprint/ipc.py` | resource manager protocol (`_IO_CONNECT`, `_IO_READ`, ...) | `open()` and `read()` on a device are messages too, so edges to resource managers are checked the same way |
| `blueprint/ipc.py` | thread states `SEND`, `RECEIVE`, `REPLY` | knows who is blocked on whom when the trace ends |
| `blueprint/checks.py` | thread state events with priority, `_NTO_CHF_FIXED_PRIORITY` from `ChannelCreate` | priority inversion: who ran while a waiting client's server sat READY |
| `blueprint/snapshot.py` | `pidin` blocked-on column | sees waits that started before the trace |
| `blueprint/secpol.py` | security policy language: `type`, `allow_attach`, `channel connect`, the `channel_connect` ability; `secpolcompile`, `secpolgenerate`, `on -T` | turns the contract into kernel-enforced channel rules and checks a learned policy against it |
| `blueprint/record.py` | `tracelogger -w`, `slay -s KILL`, `pidin` on the target over ssh | kills a process while the trace runs, so its death and the reaction of its clients are both in the trace |
| `blueprint/checks.py` | synchronous message passing semantics | deadlock, stuck server, reply budget, isolation |
| `demo/cockpit/cockpit.c` | `name_attach`, `name_open`, `MsgSend`, `MsgReceive`, `MsgReply`, pulses, `_NTO_CHF_UNBLOCK`, `_NTO_CHF_FIXED_PRIORITY`; gps as a resource manager (`resmgr_attach`, `iofunc_*`, `dispatch_*`) | a five-process system with realistic faults |

One thing the demo shows that surprises people: a `TimerTimeout` on `MsgSend`
does not protect a client from a server that stopped answering. Channels created by
`name_attach()` have `_NTO_CHF_UNBLOCK` set, so a REPLY-blocked client that
times out stays blocked until the server handles the unblock pulse. The fixed
display reads the camera from its own thread instead.

## On a system we didn't write

`demo/stock` is a trace of a QNX image with none of our processes running,
only QNX's own services. `blueprint init --all` drafts its map (the shell,
the process manager, devb-eide, io-sock, pipe, io-usb-otg, eight message
paths) and the check against that draft passes. See
[demo/stock/README.md](demo/stock/README.md).

`demo/external/qpp` is BlackBerry's open-source
[QNX POSIX Publish Subscribe](https://github.com/qnx/qnx-posix-publish-subscribe)
service, built unchanged and traced healthy, with its server stopped and
with its server killed. Blueprint names the stopped server and the client
left REPLY-blocked on it, and the run found four weak spots in Blueprint
itself, now fixed. See [demo/external/qpp/README.md](demo/external/qpp/README.md).

`demo/external/socketcan` is BlackBerry's
[SocketCAN for io-sock](https://github.com/qnx/socketcan) on a virtual CAN
bus, with the CAN stack stopped and killed under its clients. Its blocking
reads made Blueprint fail a healthy system; that and three more weak spots
are fixed. The prebuilt module needed a small shim of ours to load, see
[demo/external/socketcan/README.md](demo/external/socketcan/README.md).

## Is the parser right?

Every trace in the repository was also printed with QNX's own `traceprinter`
on the target and compared with Blueprint's reading, event by event: 1,071,509
events on x86_64 and aarch64, with and without wide mode and in secure mode, and not one missing,
extra or different. The table is in [docs/TRACEPRINTER.md](docs/TRACEPRINTER.md),
and `python -m blueprint.tpcompare run.kev run.tp` does the same for your own
trace.

## Limits

- Times come from the trace clock. Under QEMU emulation they are inflated, so
  budgets in the demo are generous. Measure absolute latencies on hardware.
- A trace that lost kernel buffers fails with `trace-loss` instead of being
  checked as if it were complete. We detect the loss from gaps in the buffer
  sequence numbers; we could not make QEMU drop a buffer, so the check is
  tested on a real trace with one buffer cut out.
- Size: a 2 MB trace (51,000 events) takes about 0.4 s and 70 MB of memory.
  The parser keeps the whole trace in memory, so memory grows with the trace;
  a trace of a few hundred megabytes would need several gigabytes (an
  estimate, not measured). Record a few seconds around the behaviour you check.
- A read that was already waiting when the trace started shows only in
  `pidin`, without its message, so Blueprint cannot tell a reader waiting for
  data from a stuck server and reports it. Start the system inside the trace
  (`record --start`) when its clients block in `read()`.
- Two instances of one binary (two io-sock) are one process in a contract.
- Record with `tracelogger -w`. Without wide mode the trace still shows who
  sent to whom and who was blocked (on the healthy demo it gave the same five
  edges), but thread priorities and message contents are missing. Blueprint
  then warns (`trace-mode`), and fails if the contract asks for the priority
  inversion check, rather than skip that check quietly.
- The priority inversion check is experimental. A QNX server normally takes
  its client's priority while it handles the message, so the check does not
  look for "server priority lower than client". It looks at what the kernel
  actually scheduled: the server READY at a lower priority while threads
  between the two priorities run. On a normal channel that should not happen;
  it shows up with `_NTO_CHF_FIXED_PRIORITY` channels. It has no false alarms
  on the recorded demo traces, but it has not seen many real systems yet.

## Building the demo system

`demo/cockpit` builds with `qcc` from QNX SDP 8.0 and runs in a QEMU image made
with `mkqnximage`, on x86_64 or on aarch64 (`--arch=aarch64le`, which needs the
`com.qnx.qnx800.target.qemuvirt` and `com.qnx.qnx800.target.driver.virtio.startup`
packages). The same scenarios give the same results on both. On the virt board
the block device has to come first on the QEMU command line, since virtio-mmio
addresses follow the order of the devices:

```
qemu-system-aarch64 -machine virt-4.2 -cpu cortex-a57 -smp 2 -m 1G \
  -drive file=output/disk-qemu.vmdk,if=none,id=drv0 -device virtio-blk-device,drive=drv0 \
  -netdev user,id=net0,hostfwd=tcp::2223-:22 -device virtio-net-device,netdev=net0 \
  -kernel output/ifs.bin -display none -serial file:serial.log
```
 `demo/cockpit/cockpit-run.sh` records one scenario on the
target. No SDP files, headers or images are included in this repository.

## How it was built

Dmytro Topka built Blueprint for the QNX Global Hackathon, starting on
November 3, 2026, with Claude Code (Anthropic) as the coding agent. The idea was
picked after two rounds of review against the judging criteria and the other
entries. Every claim about QNX was checked on a running QNX 8.0 target
before it went into the code or the docs, and several first guesses did not
survive that: the pid in a send event, what a send timeout does on a
name_attach channel, which errno a denied connection gets, why a learned
policy had no rules. The commit history shows the work as it happened.

## License

Apache-2.0. QNX is a trademark of BlackBerry Limited. This project is not
affiliated with or endorsed by BlackBerry.
