# Security policies for the cockpit demo

| File | Where it comes from |
|---|---|
| `cockpit.secpol` | `blueprint secpol` from `../blueprint.yaml`: one type per process, the names the servers attach, one `channel connect` rule per allowed edge |
| `learned-ok.secpol` | `/dev/secpolgenerate/policy` from a QNX 8.0 image built with `mkqnximage --secpol=develop`, after one run of the cockpit with each process started as its type |
| `learned-backdoor.secpol` | the same, after one more run of the backdoor build |
| `backdoor.secpol` | written by hand to test attributes, sets and the `channel_connect` ability |
| `secure-aarch64.secpol` | learned on QNX 8.0 aarch64 (QEMU virt) over a longer session, plus one hand-written `trace` ability; boots in `--secpol=secure` with ssh working |
| `secure-x86_64.secpol` | the same on x86_64, plus `setgid:0` and `setuid:0` for `sshd_t` (see below) |

All of them compile with `secpolcompile` from QNX SDP 8.0.

```
$ blueprint secpol -c ../blueprint.yaml --check learned-ok.secpol
PASS: learned-ok.secpol 0 errors, 0 warnings against ../blueprint.yaml
$ blueprint secpol -c ../blueprint.yaml --check learned-backdoor.secpol
FAIL  [secpol] policy lets display connect to gps, which the contract forbids
```

secpolgenerate writes down whatever the system did while it watched. One run
of a bad build is enough to put the backdoor into the policy for good, and from
then on the kernel allows it. Checking the learned policy against the contract
catches that before the policy ships.

## How the learned policies were made

1. `mkqnximage --secpol=develop --build` and boot the image.
2. Start each process with its type, without a kernel trace running:
   `on -T gps_t gps &` and so on, then `on -T display_t display 20`.
   secpolgenerate learns from kernel trace events, and a `tracelogger` running
   at the same time takes them away from it: with `cockpit-run.sh`, which
   traces, the learned policy declares the types but has no rules for them.
3. `cat /dev/secpolgenerate/policy`.

## Enforcing it

Boot with `mkqnximage --secpol=develop --policy=learned-ok.secpol` and add
`type display_t restricted` to `/data/var/secpol/config`. The rest of the system
stays unrestricted while display is held to the policy. The backdoor build
then fails in the kernel:

```
# COCKPIT_FAULT=backdoor on -T display_t display 3
cockpit: cannot open cockpit-gps: Operation not permitted
```

The ok build runs as before.

## A fully secure image

`secure-aarch64.secpol` runs a whole system in `mkqnximage --secpol=secure`
mode, with no secpolgenerate and every process held to the policy. It was
learned on the aarch64 image in one session that did what the secure system
has to do: three ssh key logins, scp both ways, the cockpit with each role
as its type (plain and with gps as a resource manager), pidin, slay, on.
tracelogger cannot be learned (see above), so one rule was added by hand:
`allow user_root_t self:ability { trace };`.

On x86_64 the same session gave a policy that worked in develop mode with
every type restricted (secpolgenerate logged no denials), yet in secure mode
sshd dropped the connection after the client offered its key. A second sshd
started with `-d` on another port showed why: `initgroups: root: Operation
not permitted`. Granting `setgid:0` and `setuid:0` to `sshd_t` fixed it. Why
aarch64 does not need the rule we don't know; the two images differ only in
architecture.

The serial logs, the image options, `secpolcompile` checksums that match
`/proc/boot/secpol.bin` on the target, and an ssh session on each image are in
[`evidence/`](evidence/README.md).

Both secure images run the healthy cockpit, and on both the backdoor fails in
the kernel with "Operation not permitted". What the kernel does with the
cockpit on aarch64, recorded and checked:

| Build | Kernel | Blueprint |
|---|---|---|
| ok | allows everything it does | PASS |
| backdoor, display calls `name_open("cockpit-gps")` | `Operation not permitted` | `kernel-denied`: the kernel refused display's connection to gps, which the contract forbids |
| backdoor with gps as a resource manager, `open("/dev/cockpit/gps")` | `Operation not permitted` | `kernel-denied`, the same: the open goes through `ConnectAttach` too |
| deadlock, gps asks nav for a hint | refuses gps's connection to nav, so there is no loop | `kernel-denied` for gps -> nav, and `stuck-reply`: gps keeps retrying and holds nav's message |
| display started without its type | refused even to nav | `kernel-denied` for an edge the contract allows: start it with `on -T display_t` |

Each refusal is in the trace: the kernel call exit of `ConnectAttach` carries
`ret_val:-1, errno:1` (EPERM), 140 of them in the x86_64 backdoor trace and 151
on aarch64, counted the same by Blueprint and by `traceprinter`. The traces are
in `tests/data/secure/`.

The policy keeps the architecture from being broken; the trace shows what
that did to the system.
