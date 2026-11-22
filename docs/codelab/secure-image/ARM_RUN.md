# The secure-image codelab on aarch64 (Apple Silicon path), 22 November 2026

An AI agent with no access to our notes followed `docs/codelab/qnx-secure-image.md` on aarch64le, end to end, with three substitutions: the qcc target, the mkqnximage arch and the QEMU line. It needed no policy edits beyond the codelab's own `trace` rule. The normal ssh login on the secure image worked without a `setgid`/`setuid` rule for `sshd_t`, as the codelab says.

## Setup

- Apple M2 with Colima (an aarch64 Linux VM).
- Build tools: QNX SDP 8.0 in an x86_64 Linux container under emulation, Python 3.10.12, `blueprint` from the repository at commit `1eef330`.
- Target: QEMU 6.2.0 `qemu-system-aarch64` in an aarch64 container, TCG only, no hardware acceleration.
- Ports 2240, 2241 and 2242 instead of the codelab's 2230, 2231 and 2232.

## Steps

| # | Step | Command on aarch64 | Matched | Notes |
|---|------|--------------------|---------|-------|
| 1 | blueprint venv | as in the codelab | yes | |
| 2 | Build | `qcc -Vgcc_ntoaarch64le -o cockpit cockpit.c` | yes | 0.8 s, an aarch64 ELF. `gcc_ntoaarch64` fails with `unknown target` |
| 3 | Learning image | `mkqnximage --type=qemu --arch=aarch64le --secpol=develop ...` | yes | `--arch=aarch64` is rejected |
| 4 | Boot learning | the `qemu-system-aarch64` line below | yes | `Startup complete` in 3 to 9 s; `/dev/secpolgenerate` lists all five entries |
| 5 | Teach | as in the codelab | yes | `channel_connect:logger_t,camera_t,nav_t` |
| 6 | Poison | as in the codelab | yes | `logger_t,gps_t,camera_t,nav_t` |
| 7 | Reboot, clean teach | as in the codelab | yes | `display_t has no rules yet`, then the clean set |
| 8 | blueprint secpol check | as in the codelab | yes, numbers differ | same text; the policy has 517 lines, the FAIL is at line 208 |
| 9 | Secure build | `--arch=aarch64le --secpol=secure ...` | yes | 28 s |
| 10 | Secure boot | the same QEMU line with the secure image's ports | yes | `tracelog_start: Operation not permitted` and `initgroups: root: Operation not permitted` as expected |
| 11 | trace rule, rebuild | as in the codelab | yes | 21 s, no `sshd_t` rule needed |
| 12 | md5 proof | `secpolcompile`, `md5sum`, macOS `md5 -q` | yes | all three give `b96324320b338e5c3bf88bade4795e99`; `/dev/secpolgenerate` absent |
| 13 | Three display runs | as in the codelab | yes | identical text |
| 14 | Trace refusals | as in the codelab | yes, wording differs | 200 refusals; the broken-pipe line reads `error: Broken pipe (32)` |
| 15 | blueprint check on the trace | as in the codelab | yes | `FAIL [kernel-denied] ... display ... gps`, 200 EPERM |

## The QEMU line

The full run used mkqnximage's own aarch64 preset, `-machine virt-4.2 -cpu cortex-a57`. The agent then reran the teach, secure, md5, three-run and trace steps with plain `-machine virt -cpu max` and got the same results. That is the line the codelab shows.

## Traps found

- The disk device must come first. With the network device first, boot fails with `No virtio interfaces found` and `No system file system, giving up.`
- The rng device is required. Without it the boot shows `Unable to access /dev/random`, `network stack down` and `PRNG is not seeded`, and ssh times out. The x86_64 line needs no rng device.
- QNX SDP 8.0 host tools exist only for x86_64 Linux and Windows, so a Mac needs an x86_64 Linux VM or container for `qcc`, `mkqnximage` and `secpolcompile`.
- On macOS, `mkqnximage --run` prints "Networking not supported", so the QEMU line has to be written by hand.

## Not checked

- `-accel hvf -cpu host` with Homebrew QEMU directly on macOS. We make no speed claim.
- Whether newer QEMU releases still ship the `virt-4.2` machine. Plain `virt` avoids the question.
- The `kill %1` reboot flow.
