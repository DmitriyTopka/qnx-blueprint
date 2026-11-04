# Blueprint's parser against QNX traceprinter

Every trace in this repository was printed with `traceprinter -f` from QNX SDP 8.0 on the target and compared with what Blueprint parses (`python -m blueprint.tpcompare run.kev run.tp`). Each event is matched on time, CPU and class; kernel calls also on the call number and entry or exit, thread states on pid, tid and priority, message sends on rcvid and server pid. Compared on 04.11.2026.

| Trace | Architecture | Events | Details compared | Result |
|---|---|---|---|---|
| `demo/cockpit/traces/backdoor.kev` | x86_64 | 42894 | 35534 | MATCH |
| `demo/cockpit/traces/camera-dies-fixed.kev` | x86_64 | 47626 | 39534 | MATCH |
| `demo/cockpit/traces/camera-dies.kev` | x86_64 | 44054 | 36371 | MATCH |
| `demo/cockpit/traces/camera-hang-fixed.kev` | x86_64 | 41230 | 34363 | MATCH |
| `demo/cockpit/traces/camera-hang-late.kev` | x86_64 | 7972 | 6500 | MATCH |
| `demo/cockpit/traces/camera-hang.kev` | x86_64 | 34857 | 29227 | MATCH |
| `demo/cockpit/traces/deadlock-late.kev` | x86_64 | 7031 | 5717 | MATCH |
| `demo/cockpit/traces/deadlock.kev` | x86_64 | 33405 | 28125 | MATCH |
| `demo/cockpit/traces/ok-late.kev` | x86_64 | 15216 | 12347 | MATCH |
| `demo/cockpit/traces/ok.kev` | x86_64 | 40503 | 33530 | MATCH |
| `demo/cockpit/traces/prio-inversion-fixed.kev` | x86_64 | 49763 | 42279 | MATCH |
| `demo/cockpit/traces/prio-inversion.kev` | x86_64 | 51196 | 43411 | MATCH |
| `demo/cockpit/traces/resmgr-backdoor.kev` | x86_64 | 38677 | 31839 | MATCH |
| `demo/cockpit/traces/resmgr-ok.kev` | x86_64 | 44018 | 36799 | MATCH |
| `demo/cockpit/traces/slow-reply.kev` | x86_64 | 35517 | 29769 | MATCH |
| `tests/data/aarch64/deadlock.kev` | aarch64 | 38329 | 32503 | MATCH |
| `tests/data/aarch64/ok.kev` | aarch64 | 49581 | 41687 | MATCH |
| `tests/data/aarch64/prio-inversion.kev` | aarch64 | 56930 | 48963 | MATCH |
| `tests/data/aarch64/resmgr-backdoor.kev` | aarch64 | 47848 | 40264 | MATCH |
| `tests/data/cockpit_fast_ok.kev` | x86_64, no -w | 58118 | 47925 | MATCH |

| `demo/stock/stock.kev` | x86_64, QNX services only | 59159 | 48192 | MATCH |

In all, 843,924 events in 21 traces, 704,879 of them compared in detail: no event missing, none extra, no detail different.

Three of the printouts are kept compressed in `tests/data/traceprinter/`, and `tests/test_traceprinter.py` runs the comparison on every push.
