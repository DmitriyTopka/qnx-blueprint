# A QNX image with nothing of ours running

`stock.kev` is five seconds of a QNX 8.0 x86_64 image from `mkqnximage`, with
none of the cockpit processes started: only QNX's own services, an ssh login
and a few shell commands (`ls`, `cat`, `pidin`, `uname`, `ifconfig`). It is
here to show that Blueprint reads a system it did not write.

```
blueprint init demo/stock/stock.kev --all --system qnx-stock -o blueprint.yaml
blueprint check demo/stock/stock.kev -c blueprint.yaml
```

`--all` keeps QNX services in the draft; without it `init` leaves them out,
since a contract is usually about your own processes. The draft that came out
is `blueprint.yaml` in this folder, unedited. What it shows about a stock image:

| Path | Messages | What it most likely is (our reading, not checked in the QNX sources) |
|---|---|---|
| `sh -> procnto-smp-instr` | 152, 21 errors | the shell asking the process manager to spawn, open paths and so on; the errors are lookups that failed |
| `io-sock -> procnto-smp-instr` | 110, 10 errors | the network stack |
| `io-sock -> io-usb-otg` | 95, 55 errors | io-sock retrying a USB attach that QEMU has no device for; the same errors fill the system log |
| `io-usb-otg -> procnto-smp-instr` | 60 | |
| `sh -> pipe` | 28 | the pipes between `ls`, `sort` and friends |
| `procnto-smp-instr -> devb-eide` | 19 | the process manager reading files from disk |
| `devb-eide -> procnto-smp-instr` | 10 | |
| `sh -> devb-eide` | 4 | |

The check against its own draft passes. The trace also matches
`traceprinter` event for event (see `docs/TRACEPRINTER.md`).
