"""Blueprint on BlackBerry's SocketCAN for io-sock (github.com/qnx/socketcan).

Traces recorded on QNX 8.0 x86_64 in QEMU on 6 November 2026, see
demo/external/socketcan/README.md. These tests pin what that run taught us: a
reader parked in a blocking read() is waiting for data, not a slow or stuck
server; the thread that holds a message is named when its process is
stopped; a death starts when the server's threads go, not when the kernel
finishes the teardown.
"""

import os
import unittest

from blueprint import checks, contract, kev, snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
CAN = os.path.join(os.path.dirname(HERE), "demo", "external", "socketcan")


def run(name, contract_file="blueprint.yaml"):
    trace = kev.read(os.path.join(CAN, "traces", name + ".kev.gz"))
    snap = snapshot.load(os.path.join(CAN, "traces", name + ".pidin"))
    findings, _ = checks.run(trace, contract.load(os.path.join(CAN, contract_file)), snap)
    return findings


def errors(findings):
    return [f for f in findings if f.severity == checks.ERROR]


class SocketCanTest(unittest.TestCase):
    def test_reads_that_wait_for_frames_are_not_slow_replies(self):
        # Frames 2.5 s apart: each read() waits about 2.6 s in io-sock, budget 2 s.
        self.assertEqual(errors(run("stall_plain")), [])

    def test_quiet_bus_is_not_a_stuck_server(self):
        self.assertEqual(errors(run("stall_quiet")), [])
        self.assertEqual(errors(run("stall_quiet", "isolation.yaml")), [])

    def test_frames_that_must_come_are_a_cycle_deadline(self):
        over = [f for f in run("stall_quiet", "cycle.yaml") if f.check == "cycle-overrun"]
        self.assertEqual(len(over), 1)
        self.assertIn("stopped", over[0].title)

    def test_a_wait_older_than_the_trace_is_not_called_stuck_code(self):
        # The trace started while can-read was already in read(): only pidin
        # shows the wait, so Blueprint cannot tell, and says so.
        stuck = [f for f in run("stall_attach") if f.check == "stuck-reply"]
        self.assertEqual(len(stuck), 1)
        self.assertNotIn("stuck in its own code", stuck[0].detail)
        self.assertIn("does not show what it is doing", stuck[0].detail)

    def test_stopped_stack_names_the_thread_holding_the_read(self):
        stuck = {f.processes[0]: f for f in run("hang") if f.check == "stuck-reply"}
        self.assertEqual(sorted(stuck), ["can-read", "can-write"])
        self.assertIn("The server process is stopped", stuck["can-read"].detail)
        self.assertIn("Thread 27, which holds this message, shows CONDVAR", stuck["can-read"].detail)
        self.assertIn("Server thread 29 was STOPPED", stuck["can-write"].detail)

    def test_reader_that_exits_on_the_error_died_with_the_stack(self):
        iso = [f for f in run("death", "isolation.yaml") if f.check == "isolation"]
        self.assertEqual(len(iso), 1)
        self.assertIn("took can-read down with it", iso[0].title)
        self.assertTrue(iso[0].data["survivor_died"])

    def test_reader_that_is_never_told_stopped_making_progress(self):
        iso = [f for f in run("death_select", "isolation.yaml") if f.check == "isolation"]
        self.assertEqual(len(iso), 1)
        self.assertIn("stopped making progress", iso[0].title)
        self.assertFalse(iso[0].data["survivor_died"])


if __name__ == "__main__":
    unittest.main()
