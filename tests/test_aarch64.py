"""The same checks on traces recorded on QNX 8.0 aarch64le (QEMU virt, cortex-a57).

The cockpit was rebuilt with qcc -Vgcc_ntoaarch64le and run with the same
scripts. Nothing in the parser is architecture specific; these traces keep it
that way.
"""

import os
import unittest

from blueprint import checks, contract, kev, snapshot

HERE = os.path.dirname(__file__)
DATA = os.path.join(HERE, "data", "aarch64")
CONTRACT = contract.load(os.path.join(HERE, "..", "demo", "cockpit", "blueprint.yaml"))

EXPECTED = {
    "ok": [],
    "deadlock": ["unexpected-edge", "wait-cycle"],
    "prio-inversion": ["priority-inversion", "reply-budget"],
    "resmgr-backdoor": ["unexpected-edge"],
}


class Aarch64(unittest.TestCase):
    def test_traces_come_from_aarch64(self):
        trace = kev.read(os.path.join(DATA, "ok.kev"))
        self.assertEqual(trace.attributes.get("MACHINE"), "QEMU_virt")
        self.assertEqual(trace.cpu_count, 2)

    def test_same_results_as_on_x86_64(self):
        for name, expected in EXPECTED.items():
            trace = kev.read(os.path.join(DATA, name + ".kev"))
            snap = snapshot.load(os.path.join(DATA, name + ".pidin"))
            findings, _ = checks.run(trace, CONTRACT, snap)
            errors = sorted({f.check for f in findings if f.severity == checks.ERROR})
            self.assertEqual(errors, expected, name)


if __name__ == "__main__":
    unittest.main()
