# Blueprint on a network stack we did not write: SocketCAN for io-sock

[qnx/socketcan](https://github.com/qnx/socketcan) (Apache-2.0, by BlackBerry,
marked Experimental) brings Linux's SocketCAN to QNX as an `io-sock` module
with a virtual bus driver, `vcan`. Clients open `AF_CAN` sockets and call
`read()` and `write()`; each call is a message to io-sock. It is a second
instance of the network stack, started with `-o prefix=/can`, next to the one
ssh runs on.

We ran it on QNX 8.0 x86_64 in QEMU on 6 November 2026: one `can-read`, a
`can-write` loop, and the CAN stack stopped with SIGSTOP or killed with
SIGKILL mid-trace. Kernel traces come from `tracelogger -w` with a `pidin`
snapshot at the end of each.

What we had to add: the vcan driver ships as source with a build directory
for aarch64 only, so we added an x86_64 one. The prebuilt `mods-can.so` did
not load on our io-sock (`unknown symbol: sockcan_protosw`); that piece is
neither in the released source nor in our io-sock. A small shim of ours
provides the protocol switch and the `/can/dev/socket/56` registration, so
frame delivery, the driver and the samples are BlackBerry's and the glue is
ours. The shim is not in this repository.

| Trace | What happened |
|---|---|
| `stall_plain` | healthy, a frame every 2.5 s, `can-read` in a plain blocking `read()` |
| `stall_quiet` | healthy, the bus goes quiet and `can-read` waits for the next frame |
| `stall_attach` | the same, but the trace starts while `can-read` is already waiting |
| `hang` | the CAN io-sock stopped with SIGSTOP 2.2 s into the trace |
| `death` | the CAN io-sock killed, `can-read` reading with plain `read()` |
| `death_select` | the CAN io-sock killed, `can-read` waiting in `select()` |

`blueprint.yaml` is the draft `blueprint init --only` wrote from a healthy
trace (2 s budget). `isolation.yaml` adds: if io-sock fails, `can-read` must
keep working. `cycle.yaml` adds: a frame must reach `can-read` every second.

## What socketcan taught Blueprint

The first run made Blueprint fail a healthy system:

```
FAIL  [reply-budget] io-sock answers can-read too slowly
      2 of 6 replies from io-sock to can-read took longer than 2000 ms; worst 2603.1 ms.
      where: worst 2603.1 ms: server blocked (CONDVAR) 2602.8 ms, ...
FAIL  [stuck-reply] io-sock took a message from can-read and never replied
      ... The server is not blocked on anyone, so it is stuck in its own code.
```

A blocking `read()` reaches io-sock as `_IO_READ`, and io-sock answers it when
a frame comes. The thread that took it was in CONDVAR, waiting. That is the
client waiting for input, not a slow or stuck server. The fixes, pinned by
`tests/test_socketcan.py`:

1. A read whose time went almost all (90 %) to the server thread that holds it
   being parked (CONDVAR, SEM, INTR, SIGWAITINFO, or back in MsgReceive) is not
   counted against `reply_budget_ms` and is not a stuck reply. Data that must
   come in time is a deadline, and `cycle_ms` says so:

   ```
   blueprint check traces/stall_quiet.kev.gz -c cycle.yaml
   FAIL  [cycle-overrun] can-read -> io-sock cycle stopped: no answer for the last 5226 ms against 1000 ms
   ```

   A stopped server still fails: a read held by a stopped io-sock is reported.
   When the wait began before the trace, only `pidin` shows it, and Blueprint
   says it cannot tell what the server is doing instead of guessing.
2. With io-sock stopped, the finding named a STOPPED thread that held another
   client's message. It now says the process is stopped and names thread 27,
   which holds can-read's read, in CONDVAR.
3. A death was timed at the end of the kernel's teardown, 79 ms after the
   first io-sock thread was gone, and `can-read` had already failed with ESRCH
   and exited. Blueprint now times a death from the server's first thread
   death after its last reply. `death` says io-sock "took can-read down with
   it" (it exits on the error), `death_select` says can-read "stopped making
   progress" (it is never told).
4. `--inject io-sock@2:stop` would have stopped both io-sock, ssh's included.
   `--inject 'io-sock[prefix=/can]@2:stop'` picks the instance by its command
   line. Checked on the target: one process stopped, ssh stayed up.

## What the traces show about socketcan

- On a hang, writers block in `socket()` and readers in `read()` or
  `select()`, with no error; after SIGCONT everything goes on.
- On a death, a plain reader gets ESRCH and exits, a `select()` reader is not
  told and waits for its own timeout, and every later `socket()` fails with
  EAFNOSUPPORT. Nothing retries or restarts the stack.

Not covered here: Blueprint still treats two io-sock as one process in a
contract, and `blueprint init` leaves io-sock out unless you pass `--only`.
