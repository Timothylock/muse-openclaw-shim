"""Gadget-backend tests: SHIM_BACKEND=gadget with GADGET_CLI pointed at
tests/fake_musegadget.py. Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import stat
import sys
import unittest
import uuid

from test_shim import HERE, KEY, ShimCase


class GadgetCase(ShimCase):
    seed_threads = None

    def prepare(self, env):
        stub = os.path.join(self.bin, "fake-musegadget")
        with open(stub, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} "
                    f"{os.path.join(HERE, 'fake_musegadget.py')} \"$@\"\n")
        os.chmod(stub, 0o755)
        env.update({"SHIM_BACKEND": "gadget", "GADGET_CLI": stub,
                    "FAKE_GADGET_DIR": self.fake})
        if self.seed_threads is not None:
            os.makedirs(self.data, mode=0o700, exist_ok=True)
            with open(os.path.join(self.data, "threads.json"), "w") as f:
                json.dump(self.seed_threads, f)

    def reply(self, body):
        return json.loads(body)["choices"][0]["message"]["content"]

    def threads(self):
        with open(os.path.join(self.data, "threads.json")) as f:
            return json.load(f)


class TestGadgetBackend(GadgetCase):
    def test_completion_via_send_user_msg(self):
        r, body = self.chat("what is 2+2", user="alice")
        self.assertEqual(r.status, 200, body)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[:2], ["send-user-msg", "--session-id"])
        self.assertEqual(argv[3:], ["--wait", "280", "-"])
        sid = argv[2]
        self.assertEqual(str(uuid.UUID(sid)), sid)
        self.assertEqual(uuid.UUID(sid).version, 4)
        self.assertEqual(self.reply(body), f"echo[gadget:{sid}]: what is 2+2")
        self.assertFalse(any("OLD-QUESTION" in c["stdin"] or "OLD-QUESTION" in a
                             for c in calls for a in c["argv"]))

    def test_session_reuse_per_user(self):
        for txt in ("hello", "hello again"):
            r, _ = self.chat(txt, user="alice")
            self.assertEqual(r.status, 200)
        r, _ = self.chat("hello", user="bob")
        self.assertEqual(r.status, 200)
        sids = [c["argv"][2] for c in self.calls()]
        self.assertEqual(len(sids), 3)
        self.assertEqual(sids[0], sids[1])
        self.assertNotEqual(sids[0], sids[2])
        self.assertTrue(all(c["argv"][0] == "send-user-msg" for c in self.calls()))

    def test_text_on_stdin_verbatim(self):
        text = "-n leading dash\nsecond line\n\n  third  "
        r, body = self.chat(text, user="alice")
        self.assertEqual(r.status, 200, body)
        call = self.calls()[0]
        self.assertEqual(call["stdin"], text)
        self.assertNotIn(text, call["argv"])
        self.assertTrue(self.reply(body).endswith("-n leading dash\nsecond line\n\n  third"))

    def test_fail_once_retries(self):
        self.set_mode("fail_once")
        r, body = self.chat("retry me")
        self.assertEqual(r.status, 200, body)
        self.assertIn("retry me", self.reply(body))
        self.assertEqual(len(self.calls()), 2)

    def test_exit3_once_retries_and_ignores_partial(self):
        self.set_mode("exit3_once")
        r, body = self.chat("slow one")
        self.assertEqual(r.status, 200, body)
        self.assertNotIn("partial", self.reply(body))
        self.assertEqual(len(self.calls()), 2)

    def test_exit3_always_502(self):
        self.set_mode("exit3_always")
        r, body = self.chat("doomed")
        self.assertEqual(r.status, 502)
        self.assertEqual(json.loads(body)["error"]["type"], "upstream_error")
        self.assertEqual(len(self.calls()), 2)

    def test_fail_always_502(self):
        self.set_mode("fail_always")
        r, _ = self.chat("doomed")
        self.assertEqual(r.status, 502)
        self.assertEqual(len(self.calls()), 2)

    def test_no_reply_once_retries(self):
        self.set_mode("no_reply_once")
        r, body = self.chat("again")
        self.assertEqual(r.status, 200, body)
        self.assertIn("again", self.reply(body))
        self.assertEqual(len(self.calls()), 2)

    def test_threads_json_gadget_ids(self):
        self.chat("hi", user="alice")
        sid = self.calls()[0]["argv"][2]
        self.assertEqual(self.threads(), {"alice": f"gadget:{sid}"})
        tp = os.path.join(self.data, "threads.json")
        self.assertEqual(stat.S_IMODE(os.stat(tp).st_mode), 0o600)

    def test_streaming(self):
        r, body = self.chat("stream me", stream=True)
        self.assertEqual(r.status, 200)
        events = [l[6:] for l in body.decode().split("\n") if l.startswith("data: ")]
        self.assertEqual(events[-1], "[DONE]")
        content = "".join(json.loads(e)["choices"][0]["delta"].get("content", "")
                          for e in events[:-1])
        self.assertTrue(content.endswith("]: stream me"))

    def test_logs_have_no_contents(self):
        self.set_mode("exit3_once")
        self.chat("SECRET-CONTENT-XYZ", user="carol")
        self.proc.terminate()
        _, err = self.proc.communicate(timeout=5)
        log = err.decode()
        self.assertIn("user=carol", log)
        self.assertIn("backend=gadget", log)
        self.assertNotIn("SECRET-CONTENT-XYZ", log)
        self.assertNotIn("partial", log)
        self.assertNotIn(KEY, log)


class TestGadgetBackendSwitch(GadgetCase):
    # threads.json left over from the muse-cli backend
    seed_threads = {"alice": "sess-1", "bob": "gadget:" + str(uuid.uuid4())}

    def test_muse_cli_id_treated_as_new_thread(self):
        r, _ = self.chat("hi", user="alice")
        self.assertEqual(r.status, 200)
        r, _ = self.chat("hi", user="bob")
        self.assertEqual(r.status, 200)
        alice_sid, bob_sid = (c["argv"][2] for c in self.calls())
        self.assertNotEqual(alice_sid, "sess-1")
        self.assertEqual(uuid.UUID(alice_sid).version, 4)
        self.assertEqual("gadget:" + bob_sid, self.seed_threads["bob"])
        self.assertEqual(self.threads()["alice"], "gadget:" + alice_sid)


class TestMuseCliIgnoresGadgetIds(ShimCase):
    """Switching back: a gadget id must never reach `muse-cli send`."""

    def prepare(self, env):
        os.makedirs(self.data, mode=0o700, exist_ok=True)
        with open(os.path.join(self.data, "threads.json"), "w") as f:
            json.dump({"alice": "gadget:" + str(uuid.uuid4())}, f)

    def test_gadget_id_replaced(self):
        r, body = self.chat("hi", user="alice")
        self.assertEqual(r.status, 200, body)
        self.assertEqual(self.calls()[0]["argv"], ["session-start", "--title", "shim-alice"])
        self.assertEqual(self.send_calls()[0]["argv"][2], "sess-1")


if __name__ == "__main__":
    unittest.main()
