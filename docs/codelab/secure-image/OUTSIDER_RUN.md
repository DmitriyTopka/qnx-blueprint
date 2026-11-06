# A second reader follows the codelab

On 5 November 2026, 14:40 to 14:51, a second reader followed
`docs/codelab/qnx-secure-image.md` word for word, from an empty folder, with
no access to the author's notes or results. It was an AI agent (Claude), not
a person, run with only the tutorial and a short note on how to reach the
SDP and QEMU in this setup (Docker containers, other ssh ports). Its report
is below, shortened only in formatting. Every problem it found is fixed in
the codelab as published; the list at the end says how.

## Steps as run

| # | Step | Reader's time | Machine time | Matched | Notes |
|---|---|---|---|---|---|
| 1 | Prerequisites | ~1 min | | yes | |
| 2 | qcc build | <1 min | 2.5 s | yes | no warnings |
| 3 | Learning image | ~1 min | build 29.6 s, boot 9 s | yes | `ls /dev/secpolgenerate` identical |
| 4 | Teach | ~2 min | | yes | `channel_connect:logger_t,camera_t,nav_t` identical |
| 5 | Poison run | ~1 min | | partly | same set of types, different order |
| 5b | Reboot, re-teach, save | ~2 min | boot 8 s | partly | "no rules yet" matched; 541 lines, not 534 |
| 6 | Check learned policy | ~1 min | 0.15 s | yes | PASS and FAIL matched, "line 202" correct |
| 7 | Secure image | ~2 min | build 18.8 s, boot 7 s | partly | ssh OK, tracelogger EPERM matched; the `initgroups` line was on the serial console, not in the client |
| 7b | Add trace rule, rebuild | ~1 min | build 13.5 s, boot 6 s | yes | |
| 8 | md5 proof | <1 min | | yes | host = target (`ef393930...`, its own policy) |
| 9 | Kernel refuses | ~1 min | | yes | all three outputs word for word |
| 10 | Trace | ~2 min | trace 3 s | partly | 84 refusals, not 104; Blueprint FAIL matched |
| extra | "no tracelogger while learning" | ~1 min | boot 8 s | confirmed | types declared, no rules |

About 11 minutes in all. Confirmed on the reader's own run: md5 on host and
target the same, backdoor refused, untyped display refused, one bad run
poisons the learner until reboot and a reboot empties it.

## Problems found, and what changed

1. "Repeat the previous step" after the reboot pointed at the poison run.
   Now: go back to *Teach it the system*, healthy build only.
2. The working directory drifted between steps. Now every step starts from
   `$LAB`, set once.
3. A 3 s trace started by hand missed the refusals. Now one line starts the
   trace and the backdoor build together.
4. The poisoned policy was never saved by the reader. Now it is copied out
   before the reboot.
5. The debug sshd message appears on the serial console, and `sshd -d`
   serves one connection. Now said so.
6. "Save the policy as secure.secpol" was vague. Now the exact `cp` and
   `cat >>` commands, and the sshd rule is stated as left out.
7. The pip path was wrong from the lab folder. Now `$LAB/../qnx-blueprint`.
8. "Copy the trace to the host" had no command. Now it has one.
9. Outputs that change per run were presented as exact. Now the intro and
   each step say what varies and what must hold.
10. "Reboot the image" had no command. Now `kill %1` and the same qemu line.
11. `secpolcompile -o /tmp/learned.bin` looked like a needed artifact. Now
    called a syntax check.
12. No `md5sum` on stock macOS. Now `md5 -q` is mentioned.
13. The stock image already has `tracelogger`. Now only `traceprinter` and
    `sshd-auth` are added (the companion codelab said otherwise and is fixed too).
14. Lines ending in `&` hang a one-shot `ssh host "cmd"`. Now: type them in
    an interactive session.
15. Blueprint outputs were shortened without saying so. Now the intro says
    long outputs are shortened.

## Third run, recorded for the video

On 6 November 2026 another agent run followed the codelab again, from an empty
folder, and recorded every step as a terminal session for the demo video. All
steps worked. It found six smaller gaps, now fixed in the text: `ls` prints
the learner's files in columns, the shell prints job lines, `kill %1` before
the first secure boot was only in the prose, `pidin` and the `serial.log`
check had no exact command, `traceprinter | grep -m1` also prints a broken
pipe line, and the first login to a secure image can take about 20 seconds.
Its numbers: line 209 for the backdoor rule, 193 refusals in the trace, the
same md5 on host and target.
