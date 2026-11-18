"""Socket transport tests. Run: python3 -m unittest discover -s tests -v

The shim runs as a real subprocess with SHIM_TRANSPORT=socket and
MUSEGADGET_SOCKET pointed at tests/fake_service.py's threaded unix-socket
server, which scripts the musegadget service's subscribe_chat stream and
send acknowledgement.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import shim  # noqa: E402
from fake_service import USER_ID, FakeService, ev  # noqa: E402
from test_shim import ShimCase  # noqa: E402


class Seq:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return self.n


def preamble_then_answer(final_status=True, gap=4.0):
    s = Seq()
    events = [
        (0.1, ev("message.user", s(), message_id=USER_ID, role="user")),
        (0.1, ev("agent.status", s(), activity_code="working",
                 reply_to_message_id=USER_ID)),
        (0.2, ev("message.assistant", s(), message_id="pre-1",
                 reply_to_message_id=USER_ID, display_text="Looking into it…")),
        (0.1, ev("agent.status", s(), activity_code="idle",
                 reply_to_message_id=USER_ID)),
        # the tool gap: the SDK's 3 s quiet rule returned the preamble here
        (gap, ev("delta.message_start", s(), message_id="ans-1",
                 reply_to_message_id=USER_ID)),
        (0.1, ev("delta.text_append", s(), message_id="ans-1", text="8C, ")),
        (0.1, ev("delta.text_append", s(), message_id="ans-1", text="rain today")),
        (0.1, ev("delta.message_done", s(), message_id="ans-1",
                 reply_to_message_id=USER_ID)),
    ]
    if final_status:
        events.append((0.2, ev("task.status", s(), status="completed",
                               reply_to_message_id=USER_ID)))
    return events


class SocketCase(ShimCase):
    transport = "socket"
    quiet = "10"
    preamble_quiet = "30"
    socket_exists = True

    def setUp(self):
        self.sockdir = tempfile.mkdtemp(prefix="shs-")
        self.sock = os.path.join(self.sockdir, "mg.sock")
        self.extra_env = {"MUSEGADGET_SOCKET": self.sock,
                          "SHIM_QUIET_S": self.quiet,
                          "SHIM_PREAMBLE_QUIET_S": self.preamble_quiet}
        self.svc = None
        super().setUp()

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        super().tearDown()
        shutil.rmtree(self.sockdir, ignore_errors=True)

    def serve(self, events, plan=("stream",)):
        self.svc = FakeService(self.sock, events, plan).start()
        return self.svc

    def timed_chat(self, text="weather?"):
        t0 = time.monotonic()
        r, body = self.chat(text, user="tim", history=False)
        return r, body, time.monotonic() - t0, time.monotonic()

    def shim_log(self):
        self.proc.terminate()
        _, err = self.proc.communicate(timeout=5)
        return err.decode()

    def assert_one_send(self, text="weather?"):
        self.assertEqual(len(self.svc.sends), 1)
        self.assertEqual(self.svc.sends[0]["message"], text)
        self.assertEqual(self.calls(), [])      # the CLI was never used


class TestSocketRules(SocketCase):
    def test_a_single_reply_and_task_status_fast(self):
        s = Seq()
        self.serve([
            (0.1, ev("message.user", s(), message_id=USER_ID, role="user")),
            (0.1, ev("delta.message_start", s(), message_id="r1",
                     reply_to_message_id=USER_ID)),
            (0.1, ev("delta.text_append", s(), message_id="r1", text="Timer set ")),
            (0.1, ev("delta.text_append", s(), message_id="r1", text="for 10 minutes")),
            (0.1, ev("delta.message_done", s(), message_id="r1",
                     reply_to_message_id=USER_ID)),
            (0.1, ev("task.status", s(), status="completed",
                     reply_to_message_id=USER_ID)),
        ])
        r, body, elapsed, _ = self.timed_chat()
        self.assertEqual(r.status, 200, body)
        self.assertEqual(self.reply(body), "Timer set for 10 minutes")
        self.assertLess(elapsed, 4.0)           # quiet threshold is 10 s
        self.assert_one_send()
        log = self.shim_log()
        self.assertIn("rule=A", log)
        self.assertNotIn("Timer set", log)
        self.assertNotIn("weather?", log)

    def test_b_preamble_drop_regression(self):
        self.serve(preamble_then_answer(final_status=True, gap=4.0))
        r, body, elapsed, done = self.timed_chat()
        self.assertEqual(r.status, 200, body)
        reply = self.reply(body)
        self.assertIn("8C, rain today", reply)
        self.assertEqual(reply, "Looking into it…\n\n8C, rain today")
        # returned only after the final message, not during the 4 s gap
        self.assertGreater(elapsed, 4.0)
        self.assertIsNotNone(self.svc.last_event_at)
        self.assertGreaterEqual(done, self.svc.last_event_at)
        self.assert_one_send()
        self.assertIn("rule=A", self.shim_log())


class TestSocketQuietRule(SocketCase):
    quiet = "1"

    def test_c_no_status_events_quiet_rule(self):
        s = Seq()
        self.serve([
            (0.1, ev("message.user", s(), message_id=USER_ID, role="user")),
            (0.2, ev("message.assistant", s(), message_id="r1",
                     reply_to_message_id=USER_ID, display_text="8C, rain today")),
        ])
        r, body, elapsed, _ = self.timed_chat()
        self.assertEqual(r.status, 200, body)
        self.assertEqual(self.reply(body), "8C, rain today")
        self.assertGreaterEqual(elapsed, 1.0)   # waited out SHIM_QUIET_S=1
        self.assertLess(elapsed, 6.0)
        self.assert_one_send()
        self.assertIn("rule=B ", self.shim_log())


class TestSocketRulesSlowQuiet(SocketCase):
    quiet = "10"

    def test_d_preamble_only_with_task_status_returns(self):
        s = Seq()
        self.serve([
            (0.1, ev("message.user", s(), message_id=USER_ID, role="user")),
            (0.2, ev("message.assistant", s(), message_id="r1",
                     reply_to_message_id=USER_ID, display_text="Looking into it…")),
            (0.1, ev("task.status", s(), status="completed",
                     reply_to_message_id=USER_ID)),
        ])
        r, body, elapsed, _ = self.timed_chat()
        self.assertEqual(r.status, 200, body)
        self.assertEqual(self.reply(body), "Looking into it…")
        self.assertLess(elapsed, 4.0)
        self.assert_one_send()
        self.assertIn("rule=A", self.shim_log())

    def test_foreign_events_ignored(self):
        # another message's reply and status on the same chat must neither
        # add text nor finish our turn
        s = Seq()
        self.serve([
            (0.1, ev("message.user", s(), message_id=USER_ID, role="user")),
            (0.1, ev("message.assistant", s(), message_id="other-r",
                     reply_to_message_id="someone-else", display_text="NOT OURS")),
            (0.1, ev("task.status", s(), status="completed",
                     reply_to_message_id="someone-else")),
            (0.3, ev("message.assistant", s(), message_id="r1",
                     reply_to_message_id=USER_ID, display_text="Ours")),
            (0.1, ev("task.status", s(), status="completed",
                     reply_to_message_id=USER_ID)),
        ])
        r, body, elapsed, _ = self.timed_chat()
        self.assertEqual(r.status, 200, body)
        self.assertEqual(self.reply(body), "Ours")
        self.assertLess(elapsed, 4.0)


class TestSocketPreambleQuiet(SocketCase):
    """Without a terminal task status the preamble guard, not rule B, holds."""
    quiet = "1"
    preamble_quiet = "30"

    def test_b_variant_no_task_status(self):
        self.serve(preamble_then_answer(final_status=False, gap=3.0))
        r, body, elapsed, done = self.timed_chat()
        self.assertEqual(r.status, 200, body)
        self.assertEqual(self.reply(body), "Looking into it…\n\n8C, rain today")
        self.assertGreaterEqual(done, self.svc.last_event_at)
        self.assertIn("rule=B ", self.shim_log())


class TestSocketFallback(SocketCase):
    def test_e_missing_socket_falls_back_to_cli(self):
        # no fake service: the socket path does not exist
        r, body = self.chat("hello", user="tim", history=False)
        self.assertEqual(r.status, 200, body)
        sids = self.sids()
        self.assertEqual(len(sids), 1)
        self.assertEqual(self.reply(body), f"echo[{sids[0]}]: hello")
        log = self.shim_log()
        self.assertIn("falling back to cli", log)
        self.assertNotIn("hello", log)


class TestSocketFreshChat(SocketCase):
    def test_f_fresh_chat_subscribes_after_send(self):
        s = Seq()
        self.serve([
            (0.1, ev("message.assistant", s(), message_id="r1",
                     reply_to_message_id=USER_ID, display_text="Hi Tim")),
            (0.1, ev("task.status", s(), status="completed",
                     reply_to_message_id=USER_ID)),
        ], plan=("error", "stream"))
        r, body, elapsed, _ = self.timed_chat()
        self.assertEqual(r.status, 200, body)
        self.assertEqual(self.reply(body), "Hi Tim")
        self.assertEqual(self.svc.subscribes, 2)
        self.assert_one_send()
        self.assertIn("fresh=1", self.shim_log())


class TestPreambleOnly(unittest.TestCase):
    def test_true(self):
        for t in ("Looking into it…", "Checking the weather", "One moment",
                  "  let me see...  ", "I’ll check that", "Fetching..."):
            self.assertTrue(shim.is_preamble_only(t), t)

    def test_false(self):
        long_text = ("Looking at the forecast, Toronto stays at 8C with steady "
                     "rain through the evening and clearing overnight")
        for t in ("8C, rain today", "Timer set for 10 minutes", "", "   ",
                  long_text, "Lookingglass is open"):
            self.assertFalse(shim.is_preamble_only(t), t)


class TestTracker(unittest.TestCase):
    def ack(self, tr):
        tr.acknowledge({USER_ID}, fresh=False)

    def test_held_until_ack_and_seq_dedup(self):
        tr = shim.TurnTracker("s1")
        e = dict(ev("message.user", 1, message_id=USER_ID), type="event")
        r = dict(ev("message.assistant", 2, message_id="r1", display_text="x"),
                 type="event")
        tr.take(e)
        tr.take(r)
        tr.take(r)                               # duplicate seq dropped
        self.assertEqual(len(tr.held), 2)
        self.assertEqual(tr.text(), "")
        self.ack(tr)
        self.assertEqual(tr.text(), "x")

    def test_other_user_message_closes_answer(self):
        tr = shim.TurnTracker("s1")
        self.ack(tr)
        tr.take(dict(ev("message.user", 1, message_id=USER_ID), type="event"))
        tr.take(dict(ev("message.user", 2, message_id="other"), type="event"))
        tr.take(dict(ev("message.assistant", 3, message_id="r9", display_text="late"),
                     type="event"))
        self.assertEqual(tr.text(), "")

    def test_rules(self):
        tr = shim.TurnTracker("s1")
        now = time.monotonic()
        self.assertIsNone(tr.rule(now + 100))   # not acknowledged
        self.ack(tr)
        tr.take(dict(ev("message.assistant", 1, message_id="r1",
                        reply_to_message_id=USER_ID, display_text="Checking..."),
                     type="event"))
        t = tr.last_activity
        self.assertIsNone(tr.rule(t + 5, quiet_s=1, preamble_quiet_s=30))
        self.assertEqual(tr.rule(t + 31, quiet_s=1, preamble_quiet_s=30), "C")
        tr.take(dict(ev("message.assistant", 2, message_id="r2",
                        reply_to_message_id=USER_ID, display_text="8C, rain"),
                     type="event"))
        t = tr.last_activity
        self.assertEqual(tr.rule(t + 2, quiet_s=1, preamble_quiet_s=30), "B")
        tr.take(dict(ev("agent.status", 3, activity_code="working",
                        reply_to_message_id=USER_ID), type="event"))
        t = tr.last_activity
        self.assertIsNone(tr.rule(t + 2, quiet_s=1, preamble_quiet_s=30))
        tr.take(dict(ev("task.status", 4, status="completed",
                        reply_to_message_id=USER_ID), type="event"))
        self.assertEqual(tr.rule(tr.last_activity, quiet_s=1, preamble_quiet_s=30), "A")
        self.assertEqual(tr.text(), "Checking...\n\n8C, rain")

    def test_ack_ids(self):
        self.assertEqual(shim.acknowledged_ids(
            {"ok": True, "response": {"result": {"message_id": "m1",
                                                 "reply_to_message_id": "m0"}}}),
            {"m1", "m0"})
        for bad in ({"ok": False, "status": 503}, {"ok": True},
                    {"ok": True, "response": {"session_id": "x"}}):
            with self.assertRaises(shim.MuseError):
                shim.acknowledged_ids(bad)


class TestDefaultPrefix(unittest.TestCase):
    @unittest.skipIf("SHIM_MSG_PREFIX" in os.environ, "prefix overridden in env")
    def test_one_message_rule_in_default_prefix(self):
        self.assertTrue(shim.MSG_PREFIX.startswith("[Caveman mode:"))
        self.assertIn("no extra tool calls. Answer in ONE message only: no "
                      "preamble, no 'looking' or 'checking' status messages. Do "
                      "any lookup first, then send the single final answer.]",
                      shim.MSG_PREFIX)


if __name__ == "__main__":
    unittest.main()
