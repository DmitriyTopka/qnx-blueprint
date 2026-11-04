# Security policies for the cockpit demo

| File | Where it comes from |
|---|---|
| `cockpit.secpol` | `blueprint secpol` from `../blueprint.yaml`: one type per process, the names the servers attach, one `channel connect` rule per allowed edge |
| `learned-ok.secpol` | `/dev/secpolgenerate/policy` from a QNX 8.0 image built with `mkqnximage --secpol=develop`, after one run of the cockpit with each process started as its type |
| `learned-backdoor.secpol` | the same, after one more run of the backdoor build |
| `backdoor.secpol` | written by hand to test attributes, sets and the `channel_connect` ability |

All four compile with `secpolcompile` from QNX SDP 8.0.

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

The ok build runs as before. With `--secpol=secure` and only the learned
policy, sshd refused our key login, so a secure image needs more rules than
one learning session collected.
