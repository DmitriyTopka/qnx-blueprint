"""Traces recorded on QNX 8.0 images built with mkqnximage --secpol=secure."""
import os
import unittest

from blueprint import checks, contract, kev, snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
SECURE = os.path.join(HERE, "data", "secure")
CON = contract.load(os.path.join(os.path.dirname(HERE), "demo", "cockpit", "blueprint.yaml"))


def run(arch, name):
    trace = kev.read(os.path.join(SECURE, arch, name + ".kev.gz"))
    snap_path = os.path.join(SECURE, arch, name + ".pidin")
    snap = snapshot.load(snap_path) if os.path.exists(snap_path) else None
    findings, _ = checks.run(trace, CON, snap)
    return [f for f in findings if f.severity == checks.ERROR]


class KernelDeniedTest(unittest.TestCase):
    def test_healthy_build_passes_in_secure_mode(self):
        self.assertEqual(run("x86_64", "ok"), [])

    def test_backdoor_refused_by_the_kernel_on_both_architectures(self):
        for arch in ("x86_64", "aarch64"):
            errors = run(arch, "backdoor")
            self.assertEqual([f.check for f in errors], ["kernel-denied"], arch)
            f = errors[0]
            self.assertEqual(f.processes, ["display", "gps"])
            self.assertIn("forbids", f.title)
            self.assertGreater(f.data["refused"], 100)

    def test_backdoor_through_the_resource_manager_is_seen_too(self):
        errors = run("x86_64", "resmgr-backdoor")
        self.assertEqual([(f.check, f.processes) for f in errors], [("kernel-denied", ["display", "gps"])])

    def test_secure_deadlock_is_a_refused_connection(self):
        checks_seen = {f.check: f for f in run("aarch64", "deadlock")}
        self.assertIn("kernel-denied", checks_seen)
        self.assertEqual(checks_seen["kernel-denied"].processes, ["gps", "nav"])

    def test_refused_connect_is_parsed_like_traceprinter(self):
        trace = kev.read(os.path.join(SECURE, "x86_64", "backdoor.kev.gz"))
        from blueprint import ipc
        model = ipc.build(trace)
        # traceprinter prints 140 "CONNECT_ATTACH ret_val:-1, errno:1" lines for this trace
        self.assertEqual(sum(1 for r in model.refused if r[5] == 1), 140)


if __name__ == "__main__":
    unittest.main()
