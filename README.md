# Blueprint

Architecture contracts for QNX 8.0 systems, checked against the kernel trace.

A QNX system is a set of processes that talk through synchronous messages. The
design usually lives in someone's head or on a whiteboard: display asks nav,
nav asks gps, nobody calls back up the chain, and a dead camera must not take
the screen with it. Blueprint lets you write that design down in a few lines of
YAML and checks a real trace against it. When the system does something the
design does not allow, the build fails and the report says which process did
what, with the trace events to prove it.

System Profiler shows you a trace. Blueprint knows what the trace should look
like and tells you where it does not.

## Try it in one minute

No QNX target, SDP or API key needed. The repository ships traces recorded on
QNX 8.0 (x86_64, QEMU) from the demo system in `demo/cockpit`.

```
python3 -m venv .venv && . .venv/bin/activate      # or any Python 3.10+ environment
python3 -m pip install .
blueprint demo
```

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
```

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

## Checks

| Check | Fails when |
|---|---|
| `unexpected-edge` | a process sends messages to a server the contract does not allow |
| `wait-cycle` | threads end the trace waiting on each other in a loop (deadlock) |
| `stuck-reply` | a server took a message and never replied |
| `reply-budget` | a server replied, but slower than its budget |
| `isolation` | a process died or stopped answering and took a protected process down with it |
| `no-traffic` | the trace shows no answered message at all, so it proves nothing |
| `trace-loss` | kernel buffers are missing from the trace (a gap in the per-CPU buffer sequence), so no verdict on it can be trusted |
| `priority-inversion` | a client waits on a lower priority server while threads with a priority in between use the CPU (experimental, on when the contract sets `priority_inversion_ms`) |

Every finding lists the trace events it is based on. Exit code 0 means pass,
1 means the contract is violated, 2 means bad input.

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
repeated. `--start "..."` launches the system inside the trace instead of
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
tracelogger -w -s 5 -f /tmp/run.kev      # wide mode is required
pidin > /tmp/run.pidin                   # thread states when the trace stopped
```

```
blueprint check run.kev --contract blueprint.yaml --html report.html --perfetto run.perfetto.json
```

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
policies were made is in [demo/cockpit/secpol](demo/cockpit/secpol/README.md).

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

## In CI

```yaml
- uses: DmitriyTopka/qnx-blueprint@v0.1.0
  with:
    trace: traces/nightly.kev
    contract: blueprint.yaml
```

The action writes `report.html`, `report.json` and a Perfetto trace to
`blueprint-report/`, adds a summary to the job page and fails the job on any
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
- Wide mode (`tracelogger -w`) is required: the fast mode drops the arguments
  that tie a message to its connection and channel.
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

## License

Apache-2.0. QNX is a trademark of BlackBerry Limited. This project is not
affiliated with or endorsed by BlackBerry.
