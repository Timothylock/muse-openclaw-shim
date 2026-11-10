#!/usr/bin/env python3
"""muse-openclaw-shim: OpenAI-compatible HTTP shim in front of the Muse Gadget SDK.

Stdlib only. Endpoints:
  GET  /healthz              open, no auth
  GET  /v1/models            Bearer SHIM_API_KEY
  POST /v1/chat/completions  Bearer SHIM_API_KEY (stream or non-stream)

Only the last user message of each request is forwarded; Muse keeps the
conversation state in a per-user side chat (threads.json maps user -> session
UUID). The only command ever run is `musegadget send-user-msg` (a socket
client to a separately running `musegadget run` service), always via an
argument list (never a shell), with the message on stdin.
"""

import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = os.environ.get("SHIM_MODEL_ID", "muse")
# Prepended to every message before it is sent to the agent. The Even app
# offers no system-prompt field, so this is the only place the glasses
# reply rules can live. Set SHIM_MSG_PREFIX="" to disable.
MSG_PREFIX = os.environ.get(
    "SHIM_MSG_PREFIX",
    "[Caveman mode: you talk to smart glasses. Reply caveman style: fewest "
    "words possible, fragments OK, drop articles and filler, facts and "
    "numbers only. Max 2 short lines. No markdown, no links, no greetings. "
    "Be fast: answer direct, at most one quick lookup, no extra tool "
    "calls.]\n\n")
GADGET_CLI = os.environ.get("GADGET_CLI", "musegadget")
DATA_DIR = os.environ.get("SHIM_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".muse-openclaw-shim")
MAX_BODY = 1024 * 1024          # 1 MB request cap
SEND_WAIT = 280                 # passed to send-user-msg --wait
SEND_TIMEOUT = SEND_WAIT + 45   # the CLI's own socket timeout is wait+30s
HEARTBEAT = float(os.environ.get("SHIM_HEARTBEAT", "10"))
RETRIES = 1                     # exactly one retry after a failed send


def log(msg):
    """Operational log to stderr. Never pass message contents or keys here."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"{ts} {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# API key

def load_api_key():
    key = os.environ.get("SHIM_API_KEY", "").strip()
    if key:
        return key
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(DATA_DIR, "api_key")
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            log(f"using generated API key from {path}")
            return key
    except FileNotFoundError:
        pass
    key = "sk-shim-" + secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    os.chmod(path, 0o600)
    log(f"SHIM_API_KEY not set; generated a key into {path} (mode 0600)")
    return key


# --------------------------------------------------------------------------
# Muse Gadget SDK transport

class MuseError(Exception):
    pass


def send_once(session_id, text):
    """One `musegadget send-user-msg` call; text goes on stdin ("-")."""
    try:
        p = subprocess.run(
            [GADGET_CLI, "send-user-msg", "--session-id", session_id,
             "--wait", str(SEND_WAIT), "-"],
            input=text, capture_output=True, text=True,
            timeout=SEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise MuseError(f"timeout after {SEND_TIMEOUT:g}s")
    except OSError as e:
        raise MuseError(f"cannot run musegadget: {e.__class__.__name__}")
    # exit 3 = sent but unanswered (stdout may hold partial text); any
    # non-zero exit is a failure. Never log stdout/stderr contents.
    if p.returncode != 0:
        raise MuseError(f"exit {p.returncode}")
    out = p.stdout.rstrip()
    if not out.strip():
        raise MuseError("no reply")
    return out


def muse_send(session_id, text):
    """Send with exactly one retry. Returns (text, attempts)."""
    last = None
    for attempt in range(1 + RETRIES):
        try:
            return send_once(session_id, text), attempt + 1
        except MuseError as e:
            last = e
            log(f"send attempt {attempt + 1} failed thread={session_id}: {e}")
    raise MuseError(f"failed after {1 + RETRIES} attempts: {last}")


def is_session_id(value):
    """True for a canonical UUID string, the only thing threads.json stores."""
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


# --------------------------------------------------------------------------
# per-user session map

class ThreadStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()          # guards map + user_locks
        self.user_locks = {}
        self.send_locks = {}
        self.map = {}
        try:
            with open(path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.map = {str(k): str(v) for k, v in loaded.items()}
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as e:
            log(f"ignoring unreadable {path}: {e.__class__.__name__}")

    def _lock_for(self, table, key):
        with self.lock:
            return table.setdefault(key, threading.Lock())

    def send_lock(self, thread):
        return self._lock_for(self.send_locks, thread)

    def _save(self):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".threads.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.map, f, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get_or_create(self, user):
        with self._lock_for(self.user_locks, user):
            with self.lock:
                tid = self.map.get(user)
            if tid and is_session_id(tid):
                return tid, False
            if tid:
                log(f"user={user} stored session id is not a UUID; "
                    f"starting a new session")
            # A fresh --session-id starts a new side chat on first use.
            tid = str(uuid.uuid4())
            with self.lock:
                self.map[user] = tid
                self._save()
            return tid, True


# --------------------------------------------------------------------------
# request helpers

def last_user_message(messages):
    if not isinstance(messages, list):
        return None
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):   # OpenAI content parts: keep text parts
                parts = [p.get("text", "") for p in c
                         if isinstance(p, dict) and p.get("type") == "text"]
                return "\n".join(x for x in parts if x)
            return None
    return None


DEDUP = {}
DEDUP_LOCK = threading.Lock()
DEDUP_WINDOW = 10  # seconds; covers the Even app hedged duplicate POSTs


def sanitize_user(u):
    u = u if isinstance(u, str) and u.strip() else "default"
    safe = "".join(ch if ch.isalnum() or ch in "-_.@" else "_" for ch in u.strip())
    return safe[:64] or "default"


class Handler(BaseHTTPRequestHandler):
    server_version = "muse-openclaw-shim/1.0"
    protocol_version = "HTTP/1.1"

    # quiet default access log; we log our own lines without contents
    def log_message(self, fmt, *args):
        pass

    # ---- plumbing
    def _json(self, status, obj, extra_headers=None):
        self._status = status
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message, etype="invalid_request_error", headers=None):
        self._json(status, {"error": {"message": message, "type": etype}}, headers)

    def _authed(self):
        h = self.headers.get("Authorization", "")
        if h[:7].lower() == "bearer ":
            token = h[7:].strip()
            if hmac.compare_digest(token.encode(), self.server.api_key.encode()):
                return True
        self._error(401, "invalid or missing API key", "authentication_error",
                    {"WWW-Authenticate": "Bearer"})
        return False

    def _read_body(self):
        te = self.headers.get("Transfer-Encoding", "")
        if te and te.lower() != "identity":
            self.close_connection = True
            self._error(411, "chunked bodies not supported; send Content-Length")
            return None
        cl = self.headers.get("Content-Length")
        if cl is None:
            self.close_connection = True
            self._error(411, "Content-Length required")
            return None
        try:
            n = int(cl)
        except ValueError:
            self.close_connection = True
            self._error(400, "bad Content-Length")
            return None
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            self._error(413, f"body exceeds {MAX_BODY} bytes")
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "body is not valid JSON")
            return None

    def _log_access(self, method, path, started):
        # one line per request: method, path, status, latency. No contents, no keys.
        log(f"{method} {path} status={getattr(self, '_status', '-')} "
            f"latency={time.time() - started:.1f}s")

    # ---- routes
    def do_GET(self):
        started = time.time()
        path = self.path.split("?", 1)[0]
        try:
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if path in ("/v1/models", "/v1/models/", "/models"):
                if not self._authed():
                    return
                return self._json(200, {"object": "list", "data": [
                    {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "muse"}]})
            self._error(404, "not found")
        finally:
            self._log_access("GET", path, started)

    def do_POST(self):
        started = time.time()
        path = self.path.split("?", 1)[0]
        try:
            return self._do_POST(path)
        finally:
            self._log_access("POST", path, started)

    def _do_POST(self, path):
        # The Even app POSTs to the root of whatever URL it is given (verified
        # against the reverse-engineered G2 protocol), so this single-purpose
        # server treats every POST path as a chat completion.
        if not self._authed():
            self.close_connection = True
            return
        req = self._read_body()
        if req is None:
            return
        if not isinstance(req, dict):
            return self._error(400, "body must be a JSON object")
        text = last_user_message(req.get("messages"))
        if not text or not text.strip():
            return self._error(400, "no user message with text content")
        if MSG_PREFIX:
            text = MSG_PREFIX + text
        user = sanitize_user(req.get("user") or self.headers.get("X-Shim-User")
                             or self.headers.get("x-openclaw-agent-id"))
        stream = bool(req.get("stream"))
        model = req.get("model") or MODEL_ID
        started = time.time()
        if stream:
            self._stream(user, text, model, started)
        else:
            self._complete(user, text, model, started)

    # ---- completion
    def _do_send(self, user, text):
        store = self.server.store
        tid, created = store.get_or_create(user)
        with store.send_lock(tid):
            reply, attempts = muse_send(tid, text)
        return tid, created, reply, attempts

    def _reply_json(self, model, started, reply):
        self._json(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(started),
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def _complete(self, user, text, model, started):
        # The Even app hedges: it fires the same request twice, 1-2 ms apart.
        # Coalesce identical (user, text) requests inside DEDUP_WINDOW so the
        # duplicate waits for the leader's reply instead of double-sending.
        key = (user, text)
        with DEDUP_LOCK:
            ent = DEDUP.get(key)
            if ent is not None and time.time() - ent["ts"] < DEDUP_WINDOW:
                leader = False
            else:
                ent = {"event": threading.Event(), "reply": None, "err": None,
                       "ts": time.time()}
                DEDUP[key] = ent
                leader = True
                for k in [k for k, v in DEDUP.items()
                          if v is not ent and v["event"].is_set()
                          and time.time() - v["ts"] > DEDUP_WINDOW]:
                    del DEDUP[k]
        if not leader:
            ent["event"].wait(timeout=290)
            if ent["reply"] is not None:
                log(f"POST chat user={user} stream=0 status=200 dedup=1 "
                    f"latency={time.time() - started:.1f}s")
                return self._reply_json(model, started, ent["reply"])
            log(f"POST chat user={user} stream=0 status=502 dedup=1 "
                f"latency={time.time() - started:.1f}s err=leader_failed")
            return self._error(502, "upstream muse failed", "upstream_error")
        try:
            tid, created, reply, attempts = self._do_send(user, text)
        except MuseError as e:
            ent["err"] = e
            ent["event"].set()
            log(f"POST /v1/chat/completions user={user} stream=0 status=502 "
                f"latency={time.time() - started:.1f}s err={e}")
            return self._error(502, "upstream muse failed", "upstream_error")
        ent["reply"] = reply
        ent["event"].set()
        log(f"POST /v1/chat/completions user={user} stream=0 status=200 thread={tid} "
            f"new_thread={int(created)} attempts={attempts} "
            f"latency={time.time() - started:.1f}s")
        self._reply_json(model, started, reply)

    def _stream(self, user, text, model, started):
        result = {}

        def work():
            try:
                result["ok"] = self._do_send(user, text)
            except MuseError as e:
                result["err"] = e
            except Exception as e:  # never let the worker die silently
                result["err"] = MuseError(f"internal {e.__class__.__name__}")

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        cid = "chatcmpl-" + uuid.uuid4().hex

        def chunk(delta, finish=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": int(started),
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        def emit(raw):
            self.wfile.write(raw)
            self.wfile.flush()

        def data(obj):
            emit(b"data: " + json.dumps(obj).encode() + b"\n\n")

        try:
            emit(b": heartbeat\n\n")
            while True:
                worker.join(HEARTBEAT)
                if not worker.is_alive():
                    break
                emit(b": heartbeat\n\n")
            if "ok" in result:
                tid, created, reply, attempts = result["ok"]
                data(chunk({"role": "assistant"}))
                data(chunk({"content": reply}))
                data(chunk({}, "stop"))
                status = "200"
                extra = f"thread={tid} new_thread={int(created)} attempts={attempts}"
            else:
                data({"error": {"message": "upstream muse failed",
                                "type": "upstream_error"}})
                status = "502"
                extra = f"err={result.get('err')}"
            emit(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            status, extra = "client_gone", ""
        log(f"POST /v1/chat/completions user={user} stream=1 status={status} {extra} "
            f"latency={time.time() - started:.1f}s".replace("  ", " "))


def make_server(host, port, api_key=None, data_dir=None):
    global DATA_DIR
    if data_dir:
        DATA_DIR = data_dir
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.api_key = api_key or load_api_key()
    srv.store = ThreadStore(os.path.join(DATA_DIR, "threads.json"))
    return srv


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    srv = make_server(host, port)
    log(f"muse-openclaw-shim listening on {host}:{port} data_dir={DATA_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
