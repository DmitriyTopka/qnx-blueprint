"""Topology of the cockpit demo, one recorded trace per scenario.

Traces were recorded in QEMU with demo/cockpit/cockpit-run.sh (tracelogger -w).
"""

import os
import unittest

from blueprint import ipc, kev, topology

DATA = os.path.join(os.path.dirname(__file__), "..", "demo", "cockpit", "traces")
COCKPIT = ["gps", "nav", "camera", "logger", "display"]
NORMAL_EDGES = {("display", "nav"), ("display", "camera"), ("display", "logger"),
                ("nav", "gps"), ("nav", "logger")}


def load(scenario):
    model = ipc.build(kev.read(os.path.join(DATA, scenario + ".kev")))
    return topology.build(model, COCKPIT)


class CockpitTopology(unittest.TestCase):
    def test_ok_has_exactly_the_designed_edges(self):
        topo = load("ok")
        self.assertEqual(set(topo.edges), NORMAL_EDGES)
        for edge in topo.edges.values():
            self.assertEqual((edge.messages, edge.replied, edge.open), (50, 50, 0))
        self.assertEqual(topo.waiters, [])

    def test_backdoor_adds_display_to_gps(self):
        topo = load("backdoor")
        self.assertEqual(set(topo.edges) - NORMAL_EDGES, {("display", "gps")})

    def test_deadlock_leaves_a_cycle_of_blocked_threads(self):
        topo = load("deadlock")
        waits = {(w.process, w.state, w.server) for w in topo.waiters}
        self.assertEqual(waits, {("nav", "REPLY", "gps"), ("gps", "SEND", "nav"),
                                 ("display", "REPLY", "nav")})

    def test_camera_hang_freezes_display_main_thread(self):
        waits = [(w.process, w.tid, w.state, w.server) for w in load("camera-hang").waiters]
        self.assertEqual(waits, [("display", 1, "REPLY", "camera")])

    def test_camera_hang_fixed_only_reader_thread_waits(self):
        topo = load("camera-hang-fixed")
        waits = [(w.process, w.tid, w.state, w.server) for w in topo.waiters]
        self.assertEqual(waits, [("display", 2, "REPLY", "camera")])
        self.assertEqual(topo.edges[("display", "nav")].messages, 50)

    def test_camera_dies_stops_display(self):
        self.assertLess(load("camera-dies").edges[("display", "nav")].messages, 50)
        self.assertEqual(load("camera-dies-fixed").edges[("display", "nav")].messages, 50)


if __name__ == "__main__":
    unittest.main()
