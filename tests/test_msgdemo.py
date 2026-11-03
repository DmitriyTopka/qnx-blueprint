"""Checks against a real wide-mode trace of demo/msgdemo recorded in QEMU.

Reference numbers come from traceprinter run on the target over the same file:
275 MSG_SENDV/MSG_SENDVNC entries and 275 SND_MESSAGE events.
"""

import os
import unittest

from blueprint import ipc, kev

DATA = os.path.join(os.path.dirname(__file__), "data", "msgdemo_wide.kev")


class MsgdemoTrace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace = kev.read(DATA)
        cls.model = ipc.build(cls.trace)

    def test_header(self):
        self.assertEqual(self.trace.attributes["SYS_RELEASE"], "8.0.0")
        self.assertEqual(self.trace.cpu_count, 2)
        self.assertIn("-w", self.trace.attributes["TRACELOGGER_ARGS"])

    def test_send_count_matches_traceprinter(self):
        self.assertEqual(len(self.model.transactions), 275)

    def test_ping_pairs_have_both_sides(self):
        pings = [tx for tx in self.model.transactions if tx.preview.startswith("ping")]
        self.assertEqual([tx.preview for tx in pings], [f"ping{i}" for i in range(5)])
        for tx in pings:
            self.assertEqual(tx.status, "replied")
            self.assertEqual(self.model.name(tx.client_pid), "msgdemo")
            self.assertEqual(tx.server_pid, tx.client_pid)   # server is a thread of the same process
            self.assertEqual((tx.client_tid, tx.server_tid), (1, 2))
            self.assertEqual(tx.coid, 0x40000001)            # printed by msgdemo as 1073741825
            self.assertEqual(tx.chid, 1)
            self.assertEqual(tx.reply_preview, "ack:" + tx.preview)
            self.assertLess(tx.t_send, tx.t_receive)
            self.assertLess(tx.t_receive, tx.t_reply)

    def test_connect_attach_gives_channel(self):
        side = [c for c in self.model.connections.values() if c.coid == 0x40000001 and
                self.model.name(c.pid) == "msgdemo"]
        self.assertEqual(len(side), 1)
        self.assertEqual(side[0].chid, 1)
        self.assertEqual(side[0].server_pid, side[0].pid)

    def test_most_sends_fully_paired(self):
        full = [tx for tx in self.model.transactions
                if tx.server_tid is not None and tx.t_reply is not None]
        self.assertGreaterEqual(len(full), 270)


if __name__ == "__main__":
    unittest.main()
