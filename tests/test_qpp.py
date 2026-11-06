"""Blueprint on a system we did not write: QNX POSIX Publish Subscribe (QPP).

Traces recorded on QNX 8.0 x86_64 in QEMU on 6 November 2026, see
demo/external/qpp/README.md. These tests pin what that run taught us: a
stopped server is named as stopped, dead forked shells are not reported as
blocked, and an idle survivor is not said to be "taken down".
"""

import os
import unittest

from blueprint import checks, contract, ipc, kev, snapshot, topology

HERE = os.path.dirname(os.path.abspath(__file__))
QPP = os.path.join(os.path.dirname(HERE), "demo", "external", "qpp")


def run(name, contract_file="blueprint.yaml"):
    trace = kev.read(os.path.join(QPP, "traces", name + ".kev.gz"))
    snap = snapshot.load(os.path.join(QPP, "traces", name + ".pidin"))
    c = contract.load(os.path.join(QPP, contract_file))
    findings, topo = checks.run(trace, c, snap)
    return findings, topo


def errors(findings):
    return [f for f in findings if f.severity == checks.ERROR]


class QppTest(unittest.TestCase):
    def test_healthy_passes(self):
        findings, _ = run("healthy")
        self.assertEqual(errors(findings), [])

    def test_hang_names_the_stopped_server(self):
        findings, _ = run("hang")
        stuck = [f for f in findings if f.check == "stuck-reply"]
        self.assertEqual(len(stuck), 1)
        self.assertIn("STOPPED", stuck[0].detail)
        self.assertNotIn("stuck in its own code", stuck[0].detail)
        self.assertTrue(stuck[0].data["server_stopped"])

    def test_hang_before_the_trace_is_found_from_pidin(self):
        findings, _ = run("hang_attach")
        stuck = [f for f in findings if f.check == "stuck-reply"]
        self.assertEqual(len(stuck), 1)
        self.assertIn("already STOPPED when the trace started", stuck[0].detail)

    def test_dead_clients_are_not_waiters(self):
        trace = kev.read(os.path.join(QPP, "traces", "hang.kev.gz"))
        topo = topology.build(ipc.build(trace), None)
        self.assertEqual([(w.process, w.server) for w in topo.waiters],
                         [("sample-signal-publisher", "qpp")])

    def test_draft_contract_does_not_catch_a_dead_server(self):
        # No isolation rule in the draft: a dead server is not a contract error.
        findings, _ = run("death")
        self.assertEqual(errors(findings), [])

    def test_idle_survivor_is_not_taken_down(self):
        findings, _ = run("death", "isolation.yaml")
        iso = [f for f in findings if f.check == "isolation"]
        self.assertEqual(len(iso), 1)
        self.assertIn("stopped making progress", iso[0].title)
        self.assertIn("only server in the contract", iso[0].detail)


if __name__ == "__main__":
    unittest.main()
