# Blueprint on a system we did not write: QNX POSIX Publish Subscribe

[QNX POSIX Publish Subscribe](https://github.com/qnx/qnx-posix-publish-subscribe)
(QPP, Apache-2.0, by BlackBerry) is a resource manager that exposes vehicle
signals as files under `/dev/signal`. Publishers write to them, consumers
`poll()` them, and connectors such as `echo-actuator` answer actuation requests.
None of it was written with Blueprint in mind, which is the point of this test.

We built it unchanged for QNX 8.0 x86_64 (`make`, no errors), booted it in
QEMU with `qpp -c signal_catalog.json`, `echo-actuator`, `sample-signal-publisher`
and `sample-signal-consumer`, and recorded five kernel traces with
`tracelogger -w` and a `pidin` snapshot at the end of each, on 6 November 2026.

| Trace | What happened |
|---|---|
| `healthy` | the signal service, connector, publisher and consumer at work |
| `msg_healthy` | the generic message mode: publisher and subscriber |
| `hang` | `qpp` stopped with SIGSTOP 2.2 s into the trace |
| `hang_attach` | the trace starts after `qpp` was already stopped |
| `death` | `qpp` killed with SIGKILL 2.6 s into the trace |

`blueprint.yaml` is the draft `blueprint init` wrote from `healthy`, unedited:
four processes, three message paths, a 50 ms reply budget. `isolation.yaml`
adds one rule: if `qpp` fails, `sample-signal-consumer` must keep working.
`message.yaml` is the draft for `msg_healthy`.

```
blueprint check demo/external/qpp/traces/hang.kev.gz -c demo/external/qpp/blueprint.yaml
FAIL  [stuck-reply] qpp took a message from sample-signal-publisher and never replied
      sample-signal-publisher thread 1 has been REPLY-blocked on qpp for 3216 ms (budget 50.0 ms).
      Server thread 5 was STOPPED at 2786 ms into the trace (a signal such as SIGSTOP, or a
      debugger) and stayed stopped, so it cannot reply until it is continued.
```

## What the traces show about QPP

Read under emulation, so the times are inflated; the shape is not.

- Healthy replies from `qpp` take a median of 0.13 to 0.15 ms and at most
  5.3 ms for each of the three clients. The draft contract passes.
- When `qpp` is stopped, only the publisher blocks on it: its next write is
  received by a `qpp` thread that is then stopped, and the publisher stays
  REPLY-blocked until the end of the trace. The consumer and `echo-actuator`
  wait in `poll()` and simply get no more updates. After SIGCONT everything
  recovers.
- When `qpp` dies, the publisher's next write fails with `EBADF` and it exits;
  the consumer gets no error and sits in `poll()` until its own 10 s timeout;
  `echo-actuator` stays alive and quiet. No client reconnects. For samples
  this is reasonable. For a product built on them it is the question an
  `isolation` rule makes you answer.

## What QPP taught Blueprint

The first run found four weak spots in Blueprint, all fixed in the same
change as these files and pinned by `tests/test_qpp.py`:

1. A stopped server was described as "stuck in its own code". Blueprint now
   reads the STOPPED thread state, from the trace or from `pidin`.
2. `blueprint topology` listed forked shells that had already exited as
   blocked at the end of the trace. Destroyed processes are no longer waiters.
3. The isolation check said `qpp` "took the consumer down with it" while the
   consumer was alive and not blocked. It now says the consumer stopped making
   progress, and that `qpp` is its only server in the contract, so the rule
   asks for something this design cannot do without a timeout or reconnect.
4. `blueprint record --inject` could only kill. `--inject qpp@1.5:stop` now
   freezes a process with SIGSTOP and continues it after `pidin` has seen it.

The draft contract alone does not fail the `death` trace: a dead server is
not a broken contract until you say who must survive it.
