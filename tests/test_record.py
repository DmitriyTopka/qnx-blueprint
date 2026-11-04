"""blueprint record without a target: the script it sends and how it reads the answer."""

import json
import os
import subprocess
import tempfile
import unittest

from blueprint import record
from blueprint.record import Injection, RecordError


class ParseInjection(unittest.TestCase):
    def test_name_and_time(self):
        self.assertEqual(Injection.parse("camera@1.5"), Injection("camera", 1.5))

    def test_rejects_bad_input(self):
        for text in ["camera", "@1", "camera@soon", "camera@-1", "cam;reboot@1", "a b@1"]:
            with self.assertRaises(RecordError, msg=text):
                Injection.parse(text)


class RemoteScript(unittest.TestCase):
    def test_trace_starts_before_kills_and_pidin_after(self):
        s = record.remote_script(4, [Injection("nav", 3), Injection("camera", 1.5)])
        trace = s.index("tracelogger -w -s 4 ")
        cam, nav, wait, pidin = (s.index("KILL camera"), s.index("KILL nav"),
                                 s.index("wait $TL"), s.index("pidin >"))
        self.assertLess(trace, cam)
        self.assertLess(cam, nav)          # sorted by time
        self.assertLess(nav, wait)
        self.assertLess(wait, pidin)
        self.assertIn("sleep 1.2\n", s)    # 1.5 s minus the 0.3 s start-up pause
        self.assertIn("sleep 1.5\n", s)    # from 1.5 s to 3 s

    def test_start_command_runs_inside_the_trace(self):
        s = record.remote_script(5, [], start="gps & nav")
        self.assertLess(s.index("tracelogger"), s.index("( gps & nav )"))

    def test_kill_after_trace_end_is_refused(self):
        with self.assertRaises(RecordError):
            record.remote_script(2, [Injection("camera", 2)])

    def test_buffers(self):
        self.assertIn("-n 64 ", record.remote_script(1, [], buffers=64))
        self.assertNotIn(" -n ", record.remote_script(1, []))


class FakeTarget:
    """Stands in for subprocess.run against ssh."""

    def __init__(self, stdout=b"", returncode=0, files=None):
        self.stdout, self.returncode = stdout, returncode
        self.files = files if files is not None else {
            "/tmp/blueprint-rec/run.kev": b"KEV", "/tmp/blueprint-rec/run.pidin": b"PIDIN"}
        self.calls = []

    def __call__(self, argv, input=None, capture_output=True, timeout=None):
        self.calls.append((argv, input))
        if argv[-2:] == ["sh", "-s"]:
            return subprocess.CompletedProcess(argv, self.returncode, self.stdout, b"tracelogger: no")
        if argv[-2] == "cat":
            data = self.files.get(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if data else 1, data or b"", b"" if data else b"No such file")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class Record(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "run.kev")

    def test_copies_files_and_logs_kills(self):
        fake = FakeTarget(stdout=b"inject camera 1 1793748164\ninject nav 0 1793748165\n")
        result = record.record("root@t", self.out, 4, [Injection("camera", 1), Injection("nav", 2)],
                               ssh="ssh -p 2222", run=fake)
        self.assertEqual(fake.calls[0][0], ["ssh", "-p", "2222", "root@t", "sh", "-s"])
        self.assertIn(b"KILL camera", fake.calls[0][1])
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), b"KEV")
        with open(os.path.join(self.dir, "run.pidin"), "rb") as f:
            self.assertEqual(f.read(), b"PIDIN")
        self.assertEqual([(e["process"], e["hit"]) for e in result["log"]], [("camera", True), ("nav", False)])
        with open(os.path.join(self.dir, "run.inject.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["log"][0]["killed"], 1)
        self.assertEqual(fake.calls[-1][0][-3:], ["rm", "-rf", "/tmp/blueprint-rec"])

    def test_remote_failure_is_an_error(self):
        with self.assertRaises(RecordError):
            record.record("root@t", self.out, 1, [], run=FakeTarget(returncode=3))

    def test_missing_trace_is_an_error(self):
        with self.assertRaises(RecordError):
            record.record("root@t", self.out, 1, [], run=FakeTarget(files={}))

    def test_warnings_for_missed_kills(self):
        result = {"log": [{"process": "camera", "hit": True}, {"process": "nav", "hit": False},
                          {"process": "gps", "hit": True}]}
        warnings = record.check_injections(result, deaths=["camera"])
        self.assertEqual(len(warnings), 2)
        self.assertIn("nav was not running", warnings[0])
        self.assertIn("gps was killed but its death is not in the trace", warnings[1])


if __name__ == "__main__":
    unittest.main()
