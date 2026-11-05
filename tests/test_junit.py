import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COCKPIT = os.path.join(ROOT, "demo", "cockpit")


def junit(scenario):
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "junit.xml")
        rc = subprocess.run([sys.executable, "-m", "blueprint", "check",
                             os.path.join(COCKPIT, "traces", scenario + ".kev"),
                             "--contract", os.path.join(COCKPIT, "blueprint.yaml"), "--junit", out],
                            cwd=ROOT, capture_output=True).returncode
        return rc, ET.parse(out).getroot()


class JUnitTest(unittest.TestCase):
    def test_pass_has_no_failures(self):
        rc, root = junit("ok")
        self.assertEqual(rc, 0)
        suite = root.find("testsuite")
        self.assertEqual(suite.get("failures"), "0")
        self.assertGreaterEqual(int(suite.get("tests")), 9)

    def test_deadlock_fails_two_checks(self):
        rc, root = junit("deadlock")
        self.assertEqual(rc, 1)
        failed = sorted(c.get("name") for c in root.iter("testcase") if c.find("failure") is not None)
        self.assertEqual(failed, ["unexpected-edge", "wait-cycle"])
        self.assertEqual(root.find("testsuite").get("failures"), "2")


if __name__ == "__main__":
    unittest.main()
