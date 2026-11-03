"""Explanations: evidence pack, validation of what the model returns, offline cache.

No test here calls the API: the demo explanations are cached in
demo/cockpit/explanations and are looked up by a hash of the prompt.
"""

import os
import unittest

from blueprint import checks, contract, explain, ipc, kev

HERE = os.path.dirname(__file__)
DEMO = os.path.join(HERE, "..", "demo", "cockpit")
CONTRACT_PATH = os.path.join(DEMO, "blueprint.yaml")
CONTRACT = contract.load(CONTRACT_PATH)


def setup(scenario, check):
    trace = kev.read(os.path.join(HERE, "..", "demo", "cockpit", "traces", scenario + ".kev"))
    findings, _ = checks.run(trace, CONTRACT)
    return trace, ipc.build(trace), next(f for f in findings if f.check == check)


class EvidencePack(unittest.TestCase):
    def test_deadlock_pack_has_both_sends_and_end_states(self):
        trace, model, finding = setup("deadlock", "wait-cycle")
        pack = explain.evidence_pack(trace, model, finding)
        ids = {l["id"] for l in pack}
        self.assertTrue(set(finding.events) <= ids)
        self.assertTrue(any("still SEND-blocked on nav" in l["what"] for l in pack))
        self.assertTrue(any("still REPLY-blocked on gps" in l["what"] for l in pack))


class Validation(unittest.TestCase):
    def test_invented_evidence_and_docs_are_dropped(self):
        trace, model, finding = setup("deadlock", "wait-cycle")
        pack = explain.evidence_pack(trace, model, finding)
        real = pack[0]["id"]
        raw = {"cause": "c", "evidence_event_ids": [real, 999999999], "qnx_call": "MsgSend",
               "fix_summary": "f", "fix_patch": "", "doc_refs": ["MsgSend", "Made up page"],
               "confidence": "high"}
        exp = explain.validate(raw, finding, pack)
        self.assertEqual(exp.evidence_event_ids, [real])
        self.assertEqual(list(exp.doc_refs), ["MsgSend"])
        self.assertTrue(exp.verified)
        self.assertEqual(len(exp.dropped), 1)

    def test_no_real_evidence_means_unverified(self):
        trace, model, finding = setup("backdoor", "unexpected-edge")
        pack = explain.evidence_pack(trace, model, finding)
        raw = {"cause": "c", "evidence_event_ids": [123456789], "qnx_call": "x",
               "fix_summary": "f", "fix_patch": "", "doc_refs": [], "confidence": "low"}
        self.assertFalse(explain.validate(raw, finding, pack).verified)


class OfflineCache(unittest.TestCase):
    def test_demo_explanations_are_served_from_cache(self):
        with open(CONTRACT_PATH, encoding="utf-8") as f:
            ctext = f.read()
        with open(os.path.join(DEMO, "cockpit.c"), encoding="utf-8") as f:
            sources = {"demo/cockpit/cockpit.c": f.read()}
        for scenario, check in [("deadlock", "wait-cycle"), ("camera-hang", "isolation"),
                                ("camera-dies", "isolation"), ("slow-reply", "reply-budget")]:
            trace, model, finding = setup(scenario, check)
            exp = explain.explain(trace, model, finding, CONTRACT, ctext, sources,
                                  os.path.join(DEMO, "explanations"), offline=True)
            self.assertTrue(exp.verified, scenario)
            self.assertTrue(exp.cause)


if __name__ == "__main__":
    unittest.main()
