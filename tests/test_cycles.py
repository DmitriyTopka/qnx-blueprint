import os
import unittest

from blueprint import checks, contract, kev

COCKPIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "cockpit")
REALTIME = contract.load(os.path.join(COCKPIT, "realtime.yaml"))


def cycle_findings(scenario):
    trace = kev.read(os.path.join(COCKPIT, "traces", scenario + ".kev"))
    findings, _ = checks.run(trace, REALTIME)
    return [f for f in findings if f.check == "cycle-overrun"]


class CycleTest(unittest.TestCase):
    def test_healthy_and_fixed_builds_keep_their_period(self):
        for sc in ("ok", "camera-hang-fixed", "camera-dies-fixed", "prio-inversion-fixed"):
            self.assertEqual(cycle_findings(sc), [], sc)

    def test_slow_reply_overruns_because_nav_sleeps(self):
        (f,) = cycle_findings("slow-reply")
        top = f.data["breakdown"][0]
        self.assertEqual(top["what"], "waiting on nav")
        self.assertEqual(top["inside"][0]["kind"], "sleep")
        self.assertFalse(f.data["stopped"])

    def test_inversion_overruns_because_logger_is_preempted(self):
        (f,) = cycle_findings("prio-inversion")
        logger = next(p for p in f.data["breakdown"] if p["what"] == "waiting on logger")
        self.assertEqual(logger["inside"][0]["kind"], "preempted")

    def test_hung_camera_stops_the_loop(self):
        (f,) = cycle_findings("camera-hang")
        self.assertTrue(f.data["stopped"])
        self.assertEqual(f.data["breakdown"][0]["what"], "waiting on camera")

    def test_contract_rejects_an_edge_it_does_not_allow(self):
        doc = {"processes": ["a", "b"], "allow": ["a -> b"], "cycle_ms": {"b -> a": 10}}
        with self.assertRaises(contract.ContractError):
            contract.parse(doc)


if __name__ == "__main__":
    unittest.main()
