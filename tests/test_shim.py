"""Tests for shim.py. Run: python3 -m unittest discover -s tests -v

Each test starts shim.py as a real subprocess with GADGET_CLI pointed at
tests/fake_musegadget.py, so subprocess invocation, stdin handling, env
handling and HTTP framing are all exercised.
"""
import http.client
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(os.path.dirname(HERE), "shim.py")
KEY = "test-key-123"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ShimCase(unittest.TestCase):
    api_key = KEY
    seed_threads = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shimtest-")
        self.bin = os.path.join(self.tmp, "bin")
        self.fake = os.path.join(self.tmp, "fake")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.bin)
        os.makedirs(self.fake)
        stub = os.path.join(self.bin, "fake-musegadget")
        with open(stub, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} "
                    f"{os.path.join(HERE, 'fake_musegadget.py')} \"$@\"\n")
        os.chmod(stub, 0o755)
        if self.seed_threads is not None:
            os.makedirs(self.data, mode=0o700, exist_ok=True)
            with open(os.path.join(self.data, "threads.json"), "w") as f:
                json.dump(self.seed_threads, f)
        self.port = free_port()
        env = dict(os.environ)
        env.update({"GADGET_CLI": stub, "FAKE_GADGET_DIR": self.fake,
                    "SHIM_DATA_DIR": self.data,
                    "HOST": "127.0.0.1", "PORT": str(self.port),
                    "SHIM_HEARTBEAT": "0.3"})
        env.pop("SHIM_API_KEY", None)
        env["SHIM_MSG_PREFIX"] = getattr(self, "msg_prefix", "")
        if self.api_key:
            env["SHIM_API_KEY"] = self.api_key
        self.proc = subprocess.Popen([sys.executable, SHIM], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            self.fail("shim did not start")

    def tearDown(self):
        if self.proc.returncode is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            return
        self.proc.terminate()
        try:
            _, self.stderr = self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, self.stderr = self.proc.communicate()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def set_mode(self, mode):
        with open(os.path.join(self.fake, "mode"), "w") as f:
            f.write(mode)

    def calls(self):
        p = os.path.join(self.fake, "calls.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(l) for l in f]

    def sids(self):
        return [c["argv"][2] for c in self.calls()]

    def threads(self):
        with open(os.path.join(self.data, "threads.json")) as f:
            return json.load(f)

    def req(self, method, path, body=None, key=KEY, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r, payload

    def chat(self, text, user=None, stream=False, history=True):
        msgs = []
        if history:
            msgs += [{"role": "system", "content": "be nice"},
                     {"role": "user", "content": "OLD-QUESTION"},
                     {"role": "assistant", "content": "old answer"}]
        msgs.append({"role": "user", "content": text})
        body = {"model": "muse", "messages": msgs, "stream": stream}
        if user:
            body["user"] = user
        return self.req("POST", "/v1/chat/completions", body)

    def reply(self, body):
        return json.loads(body)["choices"][0]["message"]["content"]


class TestHttp(ShimCase):
    def test_healthz_open(self):
        r, body = self.req("GET", "/healthz", key=None)
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_401_without_or_with_wrong_key(self):
        for key in (None, "wrong"):
            for path in ("/v1/models", "/models"):
                r, _ = self.req("GET", path, key=key)
                self.assertEqual(r.status, 401)
                self.assertEqual(r.getheader("WWW-Authenticate"), "Bearer")
            r, _ = self.req("POST", "/v1/chat/completions",
                            {"messages": [{"role": "user", "content": "hi"}]}, key=key)
            self.assertEqual(r.status, 401)
        self.assertEqual(self.calls(), [])

    def test_models(self):
        for path in ("/v1/models", "/models"):
            r, body = self.req("GET", path)
            self.assertEqual(r.status, 200)
            d = json.loads(body)
            self.assertEqual(d["object"], "list")
            self.assertEqual(d["data"][0]["id"], "muse")

    def test_server_header_branding(self):
        r, _ = self.req("GET", "/healthz", key=None)
        self.assertTrue(r.getheader("Server").startswith("muse-openclaw-shim/"))

    def test_even_app_root_post_and_hedge_dedup(self):
        # The Even app POSTs to the root of the configured URL with
        # model "openclaw", and hedges by sending the same request twice.
        body = {"model": "openclaw",
                "messages": [{"role": "user", "content": "what time is it"}]}
        results = {}

        def fire(i):
            results[i] = self.req("POST", "/", body)

        ts = [threading.Thread(target=fire, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        for i in range(2):
            r, raw = results[i]
            self.assertEqual(r.status, 200, raw)
            self.assertIn("what time is it", self.reply(raw))
            self.assertEqual(json.loads(raw)["model"], "openclaw")
        # the hedged duplicate must not double-send to the agent
        self.assertEqual(len(self.calls()), 1)
        # any other POST path is also a completion (single-purpose server)
        r, _ = self.req("POST", "/v1", body | {"messages": [
            {"role": "user", "content": "second question"}]})
        self.assertEqual(r.status, 200)

    def test_body_limits_and_bad_json(self):
        with socket.create_connection(("127.0.0.1", self.port), 5) as s:
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                      b"Authorization: Bearer " + KEY.encode() + b"\r\n"
                      b"Content-Length: 1048577\r\n\r\n{")
            self.assertTrue(s.recv(4096).startswith(b"HTTP/1.1 413"))
        r, _ = self.req("POST", "/v1/chat/completions", raw=b"{not json")
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/v1/chat/completions",
                        {"messages": [{"role": "system", "content": "s"}]})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.calls(), [])

    def test_streaming(self):
        self.set_mode("slow")   # 1.2s per send vs 0.3s heartbeat
        r, body = self.chat("stream me", stream=True)
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/event-stream"))
        text = body.decode()
        self.assertGreaterEqual(text.count(": heartbeat"), 2)
        events = [l[6:] for l in text.split("\n") if l.startswith("data: ")]
        self.assertEqual(events[-1], "[DONE]")
        chunks = [json.loads(e) for e in events[:-1]]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        self.assertEqual(content, f"echo[{self.sids()[0]}]: stream me")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(all(c["object"] == "chat.completion.chunk" for c in chunks))

    def test_streaming_upstream_failure(self):
        self.set_mode("fail_always")
        r, body = self.chat("x", stream=True)
        self.assertEqual(r.status, 200)
        events = [l[6:] for l in body.decode().split("\n") if l.startswith("data: ")]
        self.assertEqual(events[-1], "[DONE]")
        err = json.loads(events[0])["error"]
        self.assertEqual(err["type"], "upstream_error")
        self.assertEqual(len(self.calls()), 2)

    def test_logs_have_no_contents(self):
        self.set_mode("exit3_once")
        self.chat("SECRET-CONTENT-XYZ", user="carol")
        self.proc.terminate()
        _, err = self.proc.communicate(timeout=5)
        log = err.decode()
        self.assertIn("muse-openclaw-shim listening on", log)
        self.assertIn("user=carol", log)
        self.assertIn("POST /v1/chat/completions status=200", log)  # access line
        self.assertNotIn("SECRET-CONTENT-XYZ", log)
        self.assertNotIn("partial", log)
        self.assertNotIn(KEY, log)


class TestSend(ShimCase):
    def test_completion_via_send_user_msg(self):
        r, body = self.chat("what is 2+2", user="alice")
        self.assertEqual(r.status, 200, body)
        d = json.loads(body)
        self.assertEqual(d["object"], "chat.completion")
        self.assertEqual(d["choices"][0]["message"]["role"], "assistant")
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[:2], ["send-user-msg", "--session-id"])
        self.assertEqual(argv[3:], ["--wait", "280", "-"])
        sid = argv[2]
        self.assertEqual(str(uuid.UUID(sid)), sid)
        self.assertEqual(uuid.UUID(sid).version, 4)
        self.assertEqual(self.reply(body), f"echo[{sid}]: what is 2+2")
        # only the last user message is forwarded
        self.assertFalse(any("OLD-QUESTION" in c["stdin"] or "OLD-QUESTION" in a
                             for c in calls for a in c["argv"]))

    def test_session_reuse_per_user(self):
        for txt in ("hello", "hello again"):
            r, _ = self.chat(txt, user="alice")
            self.assertEqual(r.status, 200)
        r, _ = self.chat("hello", user="bob")
        self.assertEqual(r.status, 200)
        sids = self.sids()
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

    def test_threads_json_bare_uuids(self):
        self.chat("hi", user="alice")
        self.chat("hi", user="bob")
        alice, bob = self.sids()
        self.assertEqual(self.threads(), {"alice": alice, "bob": bob})
        tp = os.path.join(self.data, "threads.json")
        self.assertEqual(stat.S_IMODE(os.stat(tp).st_mode), 0o600)

    def test_fail_once_retries(self):
        self.set_mode("fail_once")
        r, body = self.chat("retry me")
        self.assertEqual(r.status, 200, body)
        self.assertIn("retry me", self.reply(body))
        self.assertEqual(len(self.calls()), 2)

    def test_no_reply_once_retries(self):
        self.set_mode("no_reply_once")
        r, body = self.chat("again")
        self.assertEqual(r.status, 200, body)
        self.assertIn("again", self.reply(body))
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
        self.assertEqual(len(self.calls()), 2)   # exactly one retry

    def test_fail_always_502(self):
        self.set_mode("fail_always")
        r, body = self.chat("doomed")
        self.assertEqual(r.status, 502)
        self.assertEqual(json.loads(body)["error"]["type"], "upstream_error")
        self.assertEqual(len(self.calls()), 2)   # exactly one retry


class TestSeededThreads(ShimCase):
    """Valid stored UUIDs are reused; anything else is replaced by a new one."""
    kept = str(uuid.uuid4())
    seed_threads = {"alice": "not-a-uuid", "bob": kept}

    def test_seeded_ids(self):
        r, _ = self.chat("hi", user="alice")
        self.assertEqual(r.status, 200)
        r, _ = self.chat("hi", user="bob")
        self.assertEqual(r.status, 200)
        alice_sid, bob_sid = self.sids()
        self.assertEqual(uuid.UUID(alice_sid).version, 4)
        self.assertEqual(bob_sid, self.kept)
        self.assertEqual(self.threads(), {"alice": alice_sid, "bob": self.kept})


class TestMsgPrefix(ShimCase):
    msg_prefix = "GLASSES>> "

    def test_prefix_prepended_to_sent_text(self):
        r, body = self.chat("hello there", user="alice")
        self.assertEqual(r.status, 200, body)
        self.assertEqual(self.calls()[0]["stdin"], "GLASSES>> hello there")
        self.assertIn("GLASSES>> hello there", self.reply(body))


class TestGeneratedKey(ShimCase):
    api_key = None

    def test_generated_key_file(self):
        path = os.path.join(self.data, "api_key")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            key = f.read().strip()
        self.assertTrue(key.startswith("sk-shim-"))
        r, _ = self.req("GET", "/v1/models", key=key)
        self.assertEqual(r.status, 200)
        r, _ = self.req("GET", "/v1/models", key=KEY)
        self.assertEqual(r.status, 401)


if __name__ == "__main__":
    unittest.main()
