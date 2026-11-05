# Evidence: the policy from the contract, enforced by the QNX kernel

Recorded on 5 November 2026 from two QNX 8.0 images built with
`mkqnximage --secpol=secure`, one x86_64 and one aarch64 (QEMU virt).
Nothing here is edited by hand except stripping terminal colour codes and
carriage returns.

| File | What it is |
|---|---|
| `image-options-*.txt` | the `mkqnximage` options of each image: `OPT_SECPOL='secure'` and the policy it was built with |
| `secpolcompile.txt` | `secpolcompile` from SDP 8.0 compiling `../secure-*.secpol`, with checksums |
| `serial-*.log` | the serial console of each boot, from `Loading security policy` to `Startup complete` and the system log after it |
| `session-*.txt` | one ssh session on each running image |

## What to look for

**The kernel runs the policy in this repository.** `secpolcompile.txt` ends
with the md5 of the compiled `secure-aarch64.secpol` and `secure-x86_64.secpol`.
Each session prints `md5sum /proc/boot/secpol.bin`, the policy the kernel
loaded at boot. They are the same:

| | compiled here | loaded by the kernel |
|---|---|---|
| aarch64 | `1d7329c936be2ffebb716ee60706f12c` | `1d7329c936be2ffebb716ee60706f12c` |
| x86_64 | `d2c477c8573cddf99264a5693fc306ab` | `d2c477c8573cddf99264a5693fc306ab` |

**Nothing is learning.** `/dev/secpolgenerate` does not exist, so the system
cannot add rules for whatever it sees. Every process is held to the file.

**The kernel enforces it.** With logger, gps, camera and nav started as
their types, the same display binary is run three times:

```
# COCKPIT_FAULT=ok on -T display_t display 3
display 00: route via pos 50.450 | frame 1
...
# COCKPIT_FAULT=backdoor on -T display_t display 3
cockpit: cannot open cockpit-gps: Operation not permitted

# COCKPIT_FAULT=ok display 3
cockpit: cannot open cockpit-nav: Operation not permitted
```

The healthy build works. The backdoor build, which reads gps directly, gets
`Operation not permitted` from `name_open()`: the contract has no edge from
display to gps, so the policy has no rule for it. Display started without
its type is refused even its allowed connection to nav.

The first command of each session is the traced run
(`SECPOL=1 cockpit-run.sh ok`). On aarch64 the 6 s trace ends before display
prints its 50th frame, so the last line there is `Killed` from the script
stopping it; QEMU emulates aarch64 in software and it runs slower.

## Reproduce

Build the images with `mkqnximage --secpol=secure --policy=<file>` from
`../secure-x86_64.secpol` or `../secure-aarch64.secpol`, with the cockpit
built from `../../cockpit.c` and `../../cockpit-run.sh` listed in
`local/snippets/system_files.custom` (one `bin/<role>` line per role, all the
same binary), boot them and run the commands in `session-*.txt`.

The x86_64 image also starts a second sshd with `-d` on port 2200; its debug
lines are in `serial-x86_64.log`. That is how the missing `setgid:0` and
`setuid:0` rules for `sshd_t` were found (see `../README.md`).
