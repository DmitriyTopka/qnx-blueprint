"""Perfetto export and HTML report on recorded cockpit traces."""

import os
import unittest

from blueprint import checks, contract, ipc, kev, perfetto, report

HERE = os.path.dirname(__file__)
CONTRACT = contract.load(os.path.join(HERE, "..", "demo", "cockpit", "blueprint.yaml"))


def load(scenario):
    trace = kev.read(os.path.join(HERE, "..", "demo", "cockpit", "traces", scenario + ".kev"))
    findings, topo = checks.run(trace, CONTRACT)
    return trace, ipc.build(trace), findings, topo


class PerfettoExport(unittest.TestCase):
    def test_every_answered_message_gets_two_arrows(self):
        trace, model, findings, _ = load("ok")
        doc = perfetto.export(trace, model, CONTRACT.processes, findings)
        events = doc["traceEvents"]
        starts = [e for e in events if e["ph"] == "s"]
        ends = [e for e in events if e["ph"] == "f"]
        self.assertEqual(len(starts), len(ends))
        self.assertEqual(len(starts), 2 * 250)   # 5 edges x 50 messages, request and reply
        names = {e["args"]["name"] for e in events if e["ph"] == "M" and e["name"] == "process_name"}
        self.assertEqual(names, set(CONTRACT.processes))

    def test_blocking_states_are_named(self):
        trace, model, findings, _ = load("deadlock")
        events = perfetto.export(trace, model, CONTRACT.processes, findings)["traceEvents"]
        states = {e["name"] for e in events if e.get("cat") == "state"}
        self.assertIn("REPLY-blocked", states)
        self.assertIn("SEND-blocked", states)
        markers = [e["name"] for e in events if e["ph"] == "i"]
        self.assertTrue(any("wait-cycle" in m for m in markers))


class HtmlReport(unittest.TestCase):
    def test_fail_report_names_the_problem(self):
        trace, model, findings, topo = load("deadlock")
        page = report.render(trace, model, CONTRACT, findings, topo, "deadlock.kev")
        self.assertIn("FAIL", page)
        self.assertIn("deadlock: nav -&gt; gps -&gt; nav", page)
        self.assertIn("edge cycle", page)
        self.assertNotIn("<script", page)                     # no external assets
        self.assertNotIn("<link", page)

    def test_pass_report(self):
        trace, model, findings, topo = load("ok")
        page = report.render(trace, model, CONTRACT, findings, topo, "ok.kev")
        self.assertIn("verdict pass", page)
        self.assertIn("The trace matches the contract.", page)


class ReportLayout(unittest.TestCase):
    def test_cause_first_with_evidence_and_highlighted_path(self):
        trace, model, findings, topo = load("prio-inversion")
        page = report.render(trace, model, CONTRACT, findings, topo, "prio-inversion.kev")
        summary = page[page.index("<ol class=summary>"):page.index("</ol>")]
        self.assertIn("start here", summary)
        self.assertLess(summary.index("priority-inversion"), summary.index("reply-budget"))
        self.assertIn("Show the", page)                     # evidence table per finding
        self.assertIn("_NTO_CHF_FIXED_PRIORITY", page)      # the channel flags note
        self.assertIn("class='edge warn'", page)            # display -> logger in the graph
        self.assertIn("5 errors, 0 warnings", page)

    def test_singular_counts(self):
        self.assertEqual(checks.counts(1, 1), "1 error, 1 warning")
        self.assertEqual(checks.counts(0, 2), "0 errors, 2 warnings")


if __name__ == "__main__":
    unittest.main()
