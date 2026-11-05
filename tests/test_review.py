"""init --review without the network: Claude's answer is replaced by a fixed one."""
import os
import tempfile
import unittest
from unittest import mock

import yaml

from blueprint import contract, explain, init_contract, ipc, kev, review

COCKPIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "cockpit")


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.trace = kev.read(os.path.join(COCKPIT, "traces", "backdoor.kev"))
        self.model = ipc.build(self.trace)
        self.processes, _ = init_contract.select(self.model)
        self.draft = init_contract.draft(self.trace, self.model, self.processes, "backdoor.kev")
        self.edges = review.evidence(self.trace, self.model, self.processes)
        self.gps_send = next(e for e in self.edges if (e["client"], e["server"]) == ("display", "gps"))["first_sends"][0]["id"]

    def answer(self):
        return {"edges": [
            {"client": "display", "server": "gps", "verdict": "suspicious",
             "reason": "display reads gps directly \u2014 nav is the only other client.",
             "evidence_event_ids": [self.gps_send, 999999999]},
            {"client": "display", "server": "nav", "verdict": "keep", "reason": "the main path",
             "evidence_event_ids": []},
            {"client": "gps", "server": "display", "verdict": "suspicious", "reason": "not in the draft",
             "evidence_event_ids": [self.gps_send]},
        ]}

    def test_marks_are_checked_and_written_into_a_valid_draft(self):
        with tempfile.TemporaryDirectory() as cache, \
                mock.patch.object(explain, "ask_claude", return_value=(self.answer(), {})) as ask:
            text, marks = review.review(self.trace, self.model, self.processes, self.draft, {}, cache)
            self.assertEqual(ask.call_count, 1)
            # an edge that is not in the draft is dropped, so is an event id Claude was not given
            self.assertNotIn(("gps", "display"), marks)
            self.assertEqual(marks[("display", "gps")]["evidence_event_ids"], [self.gps_send])
            self.assertIn("# review (SUSPICIOUS): display reads gps directly - nav", text)
            self.assertNotIn("\u2014", text)
            contract.parse(yaml.safe_load(text))          # comments only: still a contract
            # the second run is served from the cache
            again, _ = review.review(self.trace, self.model, self.processes, self.draft, {}, cache, offline=True)
            self.assertEqual(ask.call_count, 1)
            self.assertEqual(again, text)

    def test_offline_without_cache_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as cache:
            with self.assertRaises(explain.ExplainError):
                review.review(self.trace, self.model, self.processes, self.draft, {}, cache, offline=True)


if __name__ == "__main__":
    unittest.main()


class RecordedReviewTest(unittest.TestCase):
    """The review Claude actually wrote for the backdoor trace, served from the cache."""

    def test_cached_review_flags_only_the_backdoor(self):
        trace = kev.read(os.path.join(COCKPIT, "traces", "backdoor.kev"))
        model = ipc.build(trace)
        processes, _ = init_contract.select(model)
        draft = init_contract.draft(trace, model, processes, "backdoor.kev", "cockpit")
        text, marks = review.review(trace, model, processes, draft, {},
                                    os.path.join(COCKPIT, "explanations"), offline=True)
        flagged = sorted(k for k, m in marks.items() if m["verdict"] != "keep")
        self.assertEqual(flagged, [("display", "gps")])
        self.assertTrue(marks[("display", "gps")]["verified"])
        self.assertEqual(len(marks), 6)
