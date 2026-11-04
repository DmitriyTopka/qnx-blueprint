id: blueprint-architecture-contracts
title: Check a QNX system's architecture on every build with Blueprint
summary: Write down which processes may message which, record a kernel trace, and let CI fail when the system stops matching the design.
categories: Developer Tools
tags: beginner
difficulty: 2
status: draft
authors: Blueprint contributors
feedback_link: https://github.com/DmitriyTopka/qnx-blueprint/issues


# Check a QNX system's architecture on every build with Blueprint

## Welcome
Duration: 1:00

A QNX system is a set of processes that talk through synchronous messages:
a client calls `MsgSend()` and waits until the server calls `MsgReply()`.
That design is what lets one process crash without taking the others with
it. It is also where systems quietly lock up, when a client waits on a server
that never answers.

In this codelab you will:

1. replay a recorded five-process system and see Blueprint catch a deadlock,
   a hung camera and a slow server;
2. write an architecture contract for your own system;
3. record a kernel trace on a QNX 8.0 target, kill a process on purpose and check what happened;
4. add the check to GitHub Actions.

No hardware is needed. Steps 1 and 2 run on any Mac, Linux or Windows host
without the QNX SDP.

---

## Prerequisites
Duration: 2:00

For the first part:

- Python 3.10 or newer
- git

For recording your own traces:

- QNX SDP 8.0 (the free non-commercial license from QNX Everywhere is enough)
- a QNX 8.0 target: a QEMU image made with `mkqnximage`, or a board

Optional:

- an Anthropic API key, if you want each finding explained in plain words

---

## Run the demo
Duration: 3:00

Clone the repository and install it:

```console
git clone https://github.com/DmitriyTopka/qnx-blueprint.git
cd qnx-blueprint
python3 -m pip install .
```

Replay every recorded scenario:

```console
blueprint demo
```

You should see thirteen rows and the last line
`all scenarios behaved as expected`. Each row is a real trace recorded on
QNX 8.0 in QEMU from the demo system in `demo/cockpit`: five processes called
gps, nav, camera, logger and display.

| Scenario | What was broken |
|---|---|
| `deadlock` | gps asks nav for a hint while nav waits on gps |
| `backdoor` | display reads gps directly, bypassing nav |
| `camera-hang` | the camera wedges and the display freezes with it |
| `slow-reply` | nav answers in 200 ms against a 50 ms budget |
| `deadlock-late` | the deadlock happened before the trace started |
| `prio-inversion` | logger keeps priority 10 on a fixed priority channel while display waits at 30 |

Positive
: The demo works offline. Explanations of the findings were generated once
with Claude and are cached in `demo/cockpit/explanations`.

---

## Read a report
Duration: 3:00

Open `out/camera-hang.html` in a browser.

- The **Architecture** graph shows the five processes. Grey arrows are
  message paths the contract allows. A red circle marks a process involved in
  an error.
- The **Findings** list says what is wrong. Here: the camera stopped
  answering and took the display down with it. Every finding lists the trace
  events it is based on.
- The **Why** box is Claude's explanation. It names the call at fault and
  proposes a patch. It may only cite events Blueprint gave it; anything else
  is removed before you see it.

Now open `out/camera-hang.perfetto.json` at
[ui.perfetto.dev](https://ui.perfetto.dev). Expand `display`: thread 1 keeps
running while thread 2 sits in `REPLY-blocked` on a `MsgSend to camera` that
never got a reply. Click the message slice to see the arrow to the camera
thread that received it.

Negative
: A send timeout would not have helped here. Channels created with
`name_attach()` have `_NTO_CHF_UNBLOCK` set, so a REPLY-blocked client stays
blocked until the server handles the unblock pulse, and a wedged server
never does. The fixed display reads the camera from its own thread.

---

## Write your first contract
Duration: 5:00

Create `blueprint.yaml` next to your project. List the processes you care
about and the message paths you designed:

```yaml
system: my-system
processes: [sensor, fusion, ui, logger]
allow:
  - ui -> fusion
  - fusion -> sensor
  - fusion -> logger
  - ui -> logger
reply_budget_ms:
  fusion: 30
  default: 20
isolation:
  sensor:
    must_survive: [ui]
    min_progress: 3
```

- `allow`: a message from one listed process to another that is not here
  fails the build. Messages to system servers such as procnto are ignored.
- `reply_budget_ms`: how long a server may keep a client waiting. Times come
  from the trace clock; under emulation they are inflated, so start generous.
- `isolation`: when `sensor` dies or stops answering, `ui` must still
  complete at least three messages.

Blueprint rejects a contract with unknown keys or names that are not in
`processes`, so typos fail loudly.

---

## Record a trace on your target
Duration: 5:00

On the target, start the trace in wide mode, exercise the system, and save
the thread states when the trace stops:

```console
tracelogger -w -s 5 -f /tmp/run.kev
pidin > /tmp/run.pidin
```

Wide mode is required: it records the connection and channel of every
message. The `pidin` snapshot matters when something was already stuck before
tracing began, because a thread that never changes state emits no events.

Copy both files to the host:

```console
scp -P 2222 root@localhost:/tmp/run.kev root@localhost:/tmp/run.pidin .
```

Positive
: If sshd does not start in an image built with `mkqnximage`, check for
`/usr/libexec/sshd-auth does not exist` on the console. Add
`[perms=555] /usr/libexec/sshd-auth=usr/libexec/sshd-auth` to
`local/snippets/ifs_files.custom` and rebuild.

---

## Break it on purpose
Duration: 4:00

A contract that says `camera` may die is only worth something if you kill the
camera and watch. With sshd on the target, Blueprint does that in one step:

```console
blueprint record root@localhost -c blueprint.yaml --inject sensor@1.5 -d 5 -o run.kev --html report.html
```

It starts the trace on the running system, sends SIGKILL to `sensor` 1.5
seconds in, saves `pidin` at the end, copies both files back and checks them.
You should see `inject sensor: killed 1 process(es)` and then either PASS, or
an `isolation` error that names the process `sensor` took down with it.

Negative
: `slay` exits with the number of processes it signalled, not 0 for success.
If you script kills yourself, `slay name && echo killed` prints nothing when the
kill worked. Blueprint reads the count, and reports a kill that found no
process instead of letting the test pass.

If your target is a QEMU VM inside a container, pass the ssh command with
`--ssh`, for example `--ssh "docker exec -i qnx-qemu ssh -p 2222"`.

---

## Check the trace
Duration: 3:00

```console
blueprint check run.kev --contract blueprint.yaml --html report.html --perfetto run.perfetto.json
```

The exit code is 0 when the trace matches the contract, 1 when it does not,
and 2 when an input is invalid. `run.pidin` next to the trace is picked up
automatically.

To have each error explained, pass your sources and an API key:

```console
export ANTHROPIC_API_KEY=...
python3 -m pip install anthropic
blueprint check run.kev -c blueprint.yaml --explain --source src/ui.c --source src/fusion.c --html report.html
```

One explanation costs about 0.04 USD. Answers are cached next to the
contract, so the same finding is never paid for twice.

---

## Add it to CI
Duration: 3:00

Commit a recorded trace (or record one in a job that boots your QEMU image)
and add one step to your workflow:

```yaml
- uses: actions/checkout@v7
- uses: DmitriyTopka/qnx-blueprint@main
  with:
    trace: traces/nightly.kev
    contract: blueprint.yaml
```

The step writes `report.html`, `report.json` and a Perfetto trace to
`blueprint-report/`, puts a summary on the job page and fails the job on any
violation.

---

## What you learned
Duration: 1:00

- How synchronous message passing can freeze a QNX system, and why a send
  timeout does not always save the client.
- How to write the intended architecture down as a contract.
- How to record a wide kernel trace and a `pidin` snapshot on a target.
- How to check a trace locally and in CI, and read the report in Perfetto.

Questions and ideas are welcome in the
[issue tracker](https://github.com/DmitriyTopka/qnx-blueprint/issues).
