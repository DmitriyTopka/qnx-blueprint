# Questions people ask about Blueprint

## How is this different from System Profiler or Perfetto?

Those show you a trace and leave the judging to you. Blueprint starts from a
written design, the contract, and reports where the trace departs from it.
It gives an exit code, so a regression fails a build without anyone opening
a timeline. When you do want the timeline, `--perfetto` writes one with every
message drawn as an arrow.

## Do I need hardware, the SDP or a license to try it?

No. `blueprint demo` replays fifteen traces recorded on QNX 8.0 in QEMU and
runs on any machine with Python 3.10 or newer. Recording your own traces
needs a QNX 8.0 target. A QEMU image from `mkqnximage` is enough, and the free
QNX Everywhere license covers that.

## Which targets has it been run on?

QNX 8.0.0, x86_64, in QEMU with software emulation (TCG) on an Apple M2 Pro
host. We have not run it on aarch64 or on a board yet. The trace format is
the same across architectures, but until we have checked a real aarch64
trace we don't claim it works there.

## What does tracing cost the system?

We have not measured the overhead on hardware. On our QEMU target a
four-second wide trace of the demo is about 1.3 MB. Wide mode
(`tracelogger -w`) is required, because only wide events carry the
connection, channel and priority data the checks need.

## How does it avoid false alarms?

- A server is called stuck only if it has held a client longer than its
  reply budget and five times the slowest reply seen on that edge, so a trace
  that ends mid-reply does not count.
- The priority inversion check does not use the rule "server priority below
  client priority". QNX hands the client's priority to the server, so that
  rule would fire on every healthy system. It looks at what the kernel
  scheduled instead, and it is off unless the contract asks for it.
- A trace that lost kernel buffers fails as `trace-loss` and is not checked as
  if it were complete.
- A trace with no answered message fails as `no-traffic`, so an empty or
  stalled recording cannot pass.
- The recorded scenarios are checked against results written down before the
  checks ran, in `demo/cockpit/scenarios.yaml`, and CI replays them.

## Is Claude required? What does it see?

No. The checks are plain code and decide pass or fail on their own. With
`--explain`, Claude gets one finding at a time: the trace events behind it,
the contract and the source files you name with `--source`. Nothing else
from the trace leaves your machine. Any event id in the answer that
Blueprint did not send is removed, and an answer left without evidence is
marked unverified. One explanation averages about 10,500 input and 1,000
output tokens, about 0.06 USD with claude-opus-5-5. Answers are cached next
to the contract, so CI and the demo run offline.

## How fast is it?

Checking a 51,196-event trace takes about 0.35 s on an Apple M2 Pro. The
parser is pure Python and reads the `.kev` file directly.

## Does it cover resource managers?

Yes. `open()`, `read()` and `devctl()` reach a resource manager as messages,
so Blueprint checks those edges like any `MsgSend`. The demo runs gps as a
resource manager at `/dev/cockpit/gps`, and the evidence names the
resource manager messages (`_IO_CONNECT`, `_IO_READ`, ...).

## What about mutexes and condition variables?

Not covered. The contract is about which processes talk to which, and a
mutex wait stays inside one process. A deadlock on mutexes does not show up
as a `wait-cycle` finding.

## Can the kernel enforce the contract?

Partly, today. `blueprint secpol` writes the channel rules of a QNX security
policy from the contract, and `--check` compares any policy with the
contract. With display held to a policy that matches the contract, the
backdoor build fails in the kernel with "Operation not permitted". We have
not yet built a fully secure image: with only what one learning session
collected, sshd refused our key login in `--secpol=secure` mode.

## How do I write a contract for a system that already exists?

Record a trace and run `blueprint topology run.kev`. It lists every
process-to-process edge it saw, with message counts. Copy the ones you
intended into `allow:` and leave out the ones you didn't. That decision is
the useful part, and it stays with you.

## How do I use it in CI?

Commit a trace, or record one in a job that boots your QEMU image, and add
the action from this repository. It writes an HTML report, JSON and a
Perfetto trace, puts a summary on the job page and fails the job on any
error. See the README.

## Is the timing trustworthy?

Times come from the trace clock. Under emulation they are inflated and
uneven: one re-recording of the healthy demo went over a 20 ms budget once
(25 ms). Set budgets generously under QEMU and measure real latencies on
hardware.

## What is in the repository and what is not?

Our own code, the demo system, traces and policies recorded from it,
and the cached explanations. No SDP files, headers, images or QNX
documentation. Apache-2.0. QNX is a trademark of BlackBerry Limited; this
project is not affiliated with or endorsed by BlackBerry.
