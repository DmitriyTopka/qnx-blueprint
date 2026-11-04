"""blueprint init: a draft contract from a recorded trace."""

import contextlib
import io
import os
import tempfile
import unittest

import yaml

from blueprint import __main__ as cli
from blueprint import checks, contract, init_contract, ipc, kev

HERE = os.path.dirname(__file__)
TRACES = os.path.join(HERE, "..", "demo", "cockpit", "traces")
DESIGNED = contract.load(os.path.join(HERE, "..", "demo", "cockpit", "blueprint.yaml"))


def draft(name, **kw):
    trace = kev.read(os.path.join(TRACES, name + ".kev"))
    model = ipc.build(trace)
    processes, left_out = init_contract.select(model, kw.pop("only", None))
    text = init_contract.draft(trace, model, processes, name + ".kev", **kw)
    return contract.parse(yaml.safe_load(text)), text, left_out


class Draft(unittest.TestCase):
    def test_designed_system_gives_the_designed_edges(self):
        con, _, left_out = draft("ok")
        self.assertEqual(sorted(con.processes), sorted(DESIGNED.processes))
        self.assertEqual(con.allow, DESIGNED.allow)
        self.assertIn("procnto-smp-instr", left_out)
        self.assertIn("devb-eide", left_out)

    def test_draft_passes_its_own_trace_and_catches_regressions(self):
        con, _, _ = draft("ok")
        for name, expect in [("ok", []), ("slow-reply", ["reply-budget"]), ("backdoor", ["unexpected-edge"])]:
            trace = kev.read(os.path.join(TRACES, name + ".kev"))
            findings, _ = checks.run(trace, con)
            self.assertEqual(sorted(f.check for f in findings if f.severity == checks.ERROR), expect, name)

    def test_a_backdoor_in_the_trace_goes_into_the_draft(self):
        con, text, _ = draft("backdoor")
        self.assertIn(("display", "gps"), con.allow)
        self.assertIn("Delete every edge you did not design", text)

    def test_budgets_have_headroom_and_round_numbers(self):
        con, text, _ = draft("ok")
        self.assertIn("slowest reply seen", text)
        nice = {m * 10 ** e for m in (1, 2, 5) for e in range(-1, 4)}
        for budget in con.reply_budget_ms.values():
            self.assertTrue(any(abs(budget - n) < 1e-9 for n in nice), budget)
        self.assertEqual([init_contract._nice(x) for x in (0.3, 1, 1.1, 3, 7, 12, 260)],
                         [0.5, 1, 2, 5, 10, 20, 500])

    def test_only(self):
        con, _, _ = draft("ok", only=["nav", "gps"])
        self.assertEqual(con.processes, ["nav", "gps"])
        self.assertEqual(con.allow, {("nav", "gps")})


class Command(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_writes_and_refuses_to_overwrite(self):
        path = os.path.join(tempfile.mkdtemp(), "blueprint.yaml")
        code, _, _ = self.run_cli("init", os.path.join(TRACES, "ok.kev"), "-o", path, "--system", "cockpit")
        self.assertEqual(code, 0)
        self.assertEqual(contract.load(path).system, "cockpit")
        code, _, err = self.run_cli("init", os.path.join(TRACES, "ok.kev"), "-o", path)
        self.assertEqual(code, 2)
        self.assertIn("--force", err)

    def test_trace_without_traffic_asks_for_processes(self):
        code, _, err = self.run_cli("init", os.path.join(TRACES, "deadlock-late.kev"))
        self.assertEqual(code, 2)
        self.assertIn("--only", err)


class HostileNames(unittest.TestCase):
    """Process names come from the traced system, not from the user."""

    def test_names_cannot_add_keys_or_edges(self):
        trace = kev.read(os.path.join(TRACES, "ok.kev"))
        model = ipc.build(trace)
        pid = next(p for p, n in model.names.items() if n == "gps")
        model.names[pid] = "gps]\nallow:\n  - display -> gps\nx: [y"
        processes, _ = init_contract.select(model, None)
        text = init_contract.draft(trace, model, processes, "ok.kev", system="a: b")
        doc = yaml.safe_load(text)
        con = contract.parse(doc)
        self.assertEqual(con.system, "a: b")                       # quoted, not a new key
        self.assertEqual(set(doc), {"system", "processes", "allow", "reply_budget_ms"})
        self.assertNotIn(model.names[pid], con.processes)          # left out, not written
        self.assertNotIn(("display", "gps"), con.allow)
        self.assertEqual(con.allow, {("display", "camera"), ("display", "logger"), ("display", "nav"),
                                     ("nav", "logger")})

    def test_only_rejects_names_a_contract_cannot_hold(self):
        trace = kev.read(os.path.join(TRACES, "ok.kev"))
        with self.assertRaises(ValueError):
            init_contract.select(ipc.build(trace), ["nav", "gps\nx"])


if __name__ == "__main__":
    unittest.main()
