import os
import unittest

from blueprint import checks, contract, kev

COCKPIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "cockpit")


def budget_findings(scenario):
    trace = kev.read(os.path.join(COCKPIT, "traces", scenario + ".kev"))
    con = contract.load(os.path.join(COCKPIT, "blueprint.yaml"))
    findings, _ = checks.run(trace, con)
    return {(f.processes[0], f.processes[1]): f for f in findings if f.check == "reply-budget"}


class BreakdownTest(unittest.TestCase):
    def assert_adds_up(self, f):
        total = sum(p["ms"] for p in f.data["breakdown"])
        self.assertAlmostEqual(total, f.data["worst_ms"], delta=f.data["worst_ms"] * 0.01)

    def test_slow_reply_is_the_sleep(self):
        f = budget_findings("slow-reply")[("display", "nav")]
        self.assert_adds_up(f)
        top = f.data["breakdown"][0]
        self.assertEqual(top["kind"], "sleep")
        self.assertGreater(top["ms"], 150)

    def test_inversion_names_media(self):
        found = budget_findings("prio-inversion")
        direct = found[("display", "logger")]
        self.assert_adds_up(direct)
        top = direct.data["breakdown"][0]
        self.assertEqual(top["kind"], "preempted")
        self.assertTrue(top["by"][0]["thread"].startswith("media at priority 20"))
        # nav waits on logger, and inside that call logger is preempted by media
        chained = found[("display", "nav")]
        self.assert_adds_up(chained)
        top = chained.data["breakdown"][0]
        self.assertEqual(top["kind"], "nested")
        self.assertEqual(top["inside"][0]["kind"], "preempted")
        self.assertIn("where", chained.data)


if __name__ == "__main__":
    unittest.main()
