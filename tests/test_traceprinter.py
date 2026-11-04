"""Blueprint's parser against traceprinter from QNX SDP 8.0, event by event.

The printouts were made on the target with `traceprinter -f`. docs/TRACEPRINTER.md
has the result for every trace in the repository.
"""

import os
import unittest

from blueprint import tpcompare

HERE = os.path.dirname(__file__)
PAIRS = {
    "x86_64": (os.path.join(HERE, "..", "demo", "cockpit", "traces", "ok.kev"), "ok.tp.gz"),
    "aarch64": (os.path.join(HERE, "data", "aarch64", "ok.kev"), "aarch64-ok.tp.gz"),
    "x86_64 without -w": (os.path.join(HERE, "data", "cockpit_fast_ok.kev"), "cockpit_fast_ok.tp.gz"),
}


class SameAsTraceprinter(unittest.TestCase):
    def test_every_event_matches(self):
        for label, (kev_path, tp_name) in PAIRS.items():
            r = tpcompare.compare(kev_path, os.path.join(HERE, "data", "traceprinter", tp_name))
            self.assertGreater(r["traceprinter_events"], 40000, label)
            self.assertEqual(r["blueprint_events"], r["traceprinter_events"], label)
            self.assertEqual((r["missing_in_blueprint"], r["extra_in_blueprint"], r["detail_mismatches"]),
                             (0, 0, 0), label)
            self.assertGreater(r["details_checked"], 0.8 * r["traceprinter_events"], label)


if __name__ == "__main__":
    unittest.main()
