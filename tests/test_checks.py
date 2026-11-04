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


class TraceLoss(unittest.TestCase):
    """A trace with a kernel buffer cut out, the way a dropped buffer looks."""

    @staticmethod
    def without_one_buffer(name, cpu=1, seq=10):
        path = os.path.join(HERE, "..", "demo", "cockpit", "traces", name + ".kev")
        with open(path, "rb") as f:
            data = f.read()
        trace = kev.read(path)
        starts = {(e.cpu, e.words[0]): e.index for e in trace.events
                  if e.cls == kev.CLASS_CONTROL and e.code == kev.CONTROL_BUFFER}
        lo, hi = starts[(cpu, seq)], starts[(cpu, seq + 1)]
        # Event slots are 16 bytes and run to the end of the file.
        slot = 16
        base = len(data) - (max(e.index for e in trace.events) + 1) * slot
        cut = data[:base + lo * slot] + data[base + hi * slot:]
        out = os.path.join(HERE, "data", "_lossy.kev")
        with open(out, "wb") as f:
            f.write(cut)
        return out

    def test_recorded_traces_are_complete(self):
        for name in ["ok", "deadlock", "prio-inversion", "camera-hang-late"]:
            trace = kev.read(os.path.join(HERE, "..", "demo", "cockpit", "traces", name + ".kev"))
            self.assertEqual(kev.lost_buffers(trace), [], name)

    def test_a_missing_buffer_fails_the_check(self):
        path = self.without_one_buffer("ok")
        try:
            trace = kev.read(path)
            self.assertEqual([(c, a, b) for c, a, b, _t, _i in kev.lost_buffers(trace)], [(1, 9, 11)])
            findings, _ = checks.run(trace, CONTRACT)
            loss = [f for f in findings if f.check == "trace-loss"]
            self.assertEqual(len(loss), 1)
            self.assertEqual(loss[0].severity, checks.ERROR)
            self.assertEqual(loss[0].data["missing_buffers"], 1)
        finally:
            os.remove(path)


class ResourceManager(unittest.TestCase):
    """gps as a resource manager at /dev/cockpit/gps: clients use open() and read()."""

    def test_reads_are_seen_as_messages_to_gps(self):
        from blueprint import ipc
        trace = kev.read(os.path.join(HERE, "..", "demo", "cockpit", "traces", "resmgr-ok.kev"))
        model = ipc.build(trace)
        types = {ipc.IO_TYPES.get(tx.msg_type) for tx in model.transactions
                 if model.name(tx.client_pid) == "nav" and model.name(tx.server_pid) == "gps"}
        self.assertIn("_IO_READ", types)
        self.assertIn("_IO_CONNECT", types)
        _, errors, warnings = run("resmgr-ok")
        self.assertEqual((errors, warnings), ([], []))

    def test_display_opening_the_device_is_a_backdoor(self):
        findings, errors, _ = run("resmgr-backdoor")
        self.assertEqual(errors, ["unexpected-edge"])
        self.assertEqual(findings[0].processes, ["display", "gps"])


class FastModeTrace(unittest.TestCase):
    """The healthy cockpit recorded with plain tracelogger, without -w, on QNX 8.0."""

    PATH = os.path.join(HERE, "data", "cockpit_fast_ok.kev")

    def test_edges_still_come_through(self):
        from blueprint import ipc, topology
        model = ipc.build(kev.read(self.PATH))
        topo = topology.build(model, CONTRACT.processes)
        self.assertEqual(set(topo.edges), CONTRACT.allow)
        self.assertEqual(model.sched, [])          # no priorities without -w

    def test_warning_without_the_inversion_check(self):
        con = contract.parse({"processes": CONTRACT.processes,
                              "allow": [f"{a} -> {b}" for a, b in sorted(CONTRACT.allow)]})
        findings, _ = checks.run(kev.read(self.PATH), con)
        self.assertEqual([(f.check, f.severity) for f in findings], [("trace-mode", checks.WARNING)])

    def test_error_when_the_contract_asks_for_it(self):
        findings, _ = checks.run(kev.read(self.PATH), CONTRACT)
        mode = [f for f in findings if f.check == "trace-mode"]
        self.assertEqual(mode[0].severity, checks.ERROR)

    def test_wide_traces_are_not_flagged(self):
        for name in ["ok", "deadlock", "camera-hang-late"]:
            findings, _ = checks.run(kev.read(os.path.join(HERE, "..", "demo", "cockpit", "traces", name + ".kev")), CONTRACT)
            self.assertNotIn("trace-mode", [f.check for f in findings], name)
