# Security policies for the cockpit demo

| File | Where it comes from |
|---|---|
| `cockpit.secpol` | `blueprint secpol` from `../blueprint.yaml`: one type per process, the names the servers attach, one `channel connect` rule per allowed edge |
| `learned-ok.secpol` | `/dev/secpolgenerate/policy` from a QNX 8.0 image built with `mkqnximage --secpol=develop`, after one run of the cockpit with each process started as its type |
| `learned-backdoor.secpol` | the same, after one more run of the backdoor build |
| `backdoor.secpol` | written by hand to test attributes, sets and the `channel_connect` ability |
| `secure-aarch64.secpol` | learned on QNX 8.0 aarch64 (QEMU virt) over a longer session, plus one hand-written `trace` ability; boots in `--secpol=secure` with ssh working |

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
A shorter session on x86_64 left out part of the ssh login, and sshd refused
the key in secure mode. tracelogger cannot be learned (see above), so one
rule was added by hand: `allow user_root_t self:ability { trace };`.

What the kernel does with the cockpit there, recorded and checked:

| Build | Kernel | Blueprint |
|---|---|---|
| ok | allows everything it does | PASS |
| backdoor, display calls `name_open("cockpit-gps")` | `Operation not permitted` | `no-traffic`: display never started its loop |
| backdoor with gps as a resource manager, `open("/dev/cockpit/gps")` | `Operation not permitted` | PASS: the forbidden edge never happened |
| deadlock, gps asks nav for a hint | refuses gps's connection to nav, so there is no loop | `stuck-reply`: gps keeps retrying and holds nav's message |
| display started without its type | refused even to nav | |

The policy keeps the architecture from being broken; the trace shows what
that did to the system.
