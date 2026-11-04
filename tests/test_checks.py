"""Contract checks on every recorded cockpit scenario.

Each broken build must fail with exactly the finding it was built to show,
and the fixed builds must pass.
"""

import os
import unittest

from blueprint import checks, contract, kev, snapshot

HERE = os.path.dirname(__file__)
CONTRACT = contract.load(os.path.join(HERE, "..", "demo", "cockpit", "blueprint.yaml"))


def run(scenario):
    trace = kev.read(os.path.join(HERE, "..", "demo", "cockpit", "traces", scenario + ".kev"))
    findings, _ = checks.run(trace, CONTRACT)
    errors = sorted(f.check for f in findings if f.severity == checks.ERROR)
    warnings = sorted(f.check for f in findings if f.severity == checks.WARNING)
    return findings, errors, warnings


class Scenarios(unittest.TestCase):
    def assertResult(self, scenario, errors, warnings=()):
        _, got_errors, got_warnings = run(scenario)
        self.assertEqual(got_errors, sorted(errors), scenario)
        self.assertEqual(got_warnings, sorted(warnings), scenario)

    def test_ok_passes(self):
        self.assertResult("ok", [])

    def test_deadlock(self):
        findings, _, _ = run("deadlock")
        self.assertResult("deadlock", ["unexpected-edge", "wait-cycle"])
        cycle = next(f for f in findings if f.check == "wait-cycle")
        self.assertEqual(sorted(cycle.data["cycle"]), ["gps", "nav"])
        self.assertEqual(cycle.data["victims"], ["display"])

    def test_backdoor(self):
        findings, _, _ = run("backdoor")
        self.assertResult("backdoor", ["unexpected-edge"])
        self.assertEqual(findings[0].processes, ["display", "gps"])

    def test_camera_hang_breaks_isolation(self):
        self.assertResult("camera-hang", ["isolation"], ["stuck-reply"])

    def test_camera_hang_fixed_passes_with_warning(self):
        self.assertResult("camera-hang-fixed", [], ["stuck-reply"])

    def test_camera_dies_breaks_isolation(self):
        findings, _, _ = run("camera-dies")
        self.assertResult("camera-dies", ["isolation"])
        self.assertTrue(findings[0].data["survivor_died"])

    def test_camera_dies_fixed_passes(self):
        self.assertResult("camera-dies-fixed", [])

    def test_slow_reply(self):
        findings, _, _ = run("slow-reply")
        self.assertResult("slow-reply", ["reply-budget"])
        self.assertGreater(findings[0].data["worst_ms"], 150)

    def test_every_finding_has_evidence(self):
        for scenario in ("deadlock", "backdoor", "camera-hang", "camera-dies", "slow-reply"):
            for f in run(scenario)[0]:
                self.assertTrue(f.events, f"{scenario}: {f.check} has no evidence")


class LateTraces(unittest.TestCase):
    """Trace started after the fault: only the pidin snapshot shows the waits."""

    def run_late(self, scenario, with_snapshot=True):
        trace = kev.read(os.path.join(HERE, "..", "demo", "cockpit", "traces", scenario + ".kev"))
        snap = snapshot.load(os.path.join(HERE, "..", "demo", "cockpit", "traces", scenario + ".pidin")) \
            if with_snapshot else None
        findings, _ = checks.run(trace, CONTRACT, snap)
        return sorted(f.check for f in findings if f.severity == checks.ERROR)

    def test_without_snapshot_silence_is_not_a_pass(self):
        self.assertEqual(self.run_late("deadlock-late", with_snapshot=False), ["no-traffic"])

    def test_snapshot_reveals_the_deadlock(self):
        self.assertEqual(self.run_late("deadlock-late"), ["wait-cycle"])

    def test_snapshot_reveals_the_hung_camera(self):
        self.assertEqual(self.run_late("camera-hang-late"), ["isolation"])

    def test_running_system_passes(self):
        self.assertEqual(self.run_late("ok-late"), [])


class ContractValidation(unittest.TestCase):
    def test_rejects_unknown_process_in_allow(self):
        with self.assertRaises(contract.ContractError):
            contract.parse({"processes": ["a"], "allow": ["a -> b"]})

    def test_rejects_bad_arrow(self):
        with self.assertRaises(contract.ContractError):
            contract.parse({"processes": ["a", "b"], "allow": ["a to b"]})

    def test_rejects_unknown_key(self):
        with self.assertRaises(contract.ContractError):
            contract.parse({"processes": ["a"], "alow": []})


if __name__ == "__main__":
    unittest.main()


class PriorityInversion(unittest.TestCase):
    """logger serves a _NTO_CHF_FIXED_PRIORITY channel at priority 10, display
    waits on it at 30, media burns both CPUs at 20. Recorded on QNX 8.0."""

    def test_fixed_priority_logger_is_caught(self):
        findings, errors, _ = run("prio-inversion")
        self.assertEqual(errors.count("priority-inversion"), 2)    # display and nav wait on logger
        f = next(f for f in findings if f.check == "priority-inversion" and f.processes[0] == "display")
        self.assertEqual(f.processes, ["display", "logger"])
        self.assertTrue(f.data["fixed_priority_channel"])
        self.assertEqual((f.data["client_priority"], f.data["server_priority"]), (30, 10))
        self.assertEqual(f.data["culprits"][0]["process"], "media")
        self.assertEqual(f.data["culprits"][0]["priority"], 20)
        self.assertGreater(f.data["worst_ms"], CONTRACT.priority_inversion_ms)

    def test_inheriting_channel_passes_under_the_same_load(self):
        self.assertResult_none("prio-inversion-fixed")

    def test_no_false_alarms_on_other_scenarios(self):
        for scenario in ["ok", "deadlock", "backdoor", "camera-hang", "camera-hang-fixed", "camera-dies",
                         "camera-dies-fixed", "slow-reply", "ok-late", "deadlock-late", "camera-hang-late"]:
            _, errors, _ = run(scenario)
            self.assertNotIn("priority-inversion", errors, scenario)

    def test_off_unless_the_contract_asks(self):
        trace = kev.read(os.path.join(HERE, "..", "demo", "cockpit", "traces", "prio-inversion.kev"))
        con = contract.parse({"processes": CONTRACT.processes,
                              "allow": [f"{a} -> {b}" for a, b in sorted(CONTRACT.allow)]})
        findings, _ = checks.run(trace, con)
        self.assertFalse([f for f in findings if f.check == "priority-inversion"])

    def test_contract_rejects_bad_limit(self):
        for bad in ["soon", -1]:
            with self.assertRaises(contract.ContractError):
                contract.parse({"processes": ["a"], "priority_inversion_ms": bad})

    def assertResult_none(self, scenario):
        _, errors, warnings = run(scenario)
        self.assertEqual((errors, warnings), ([], []), scenario)
