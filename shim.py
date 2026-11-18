#!/usr/bin/env python3
"""muse-openclaw-shim: OpenAI-compatible HTTP shim in front of the Muse Gadget SDK.

Stdlib only. Endpoints:
  GET  /healthz              open, no auth
  GET  /v1/models            Bearer SHIM_API_KEY
  POST /v1/chat/completions  Bearer SHIM_API_KEY (stream or non-stream)

Only the last user message of each request is forwarded; Muse keeps the
conversation state in a per-user side chat (threads.json maps user -> session
UUID). By default (SHIM_TRANSPORT=socket) messages go straight to the
separately running `musegadget run` service's local unix socket and the
shim follows the chat's event stream itself (see DESIGN.md section 6). With
SHIM_TRANSPORT=cli, or when the socket is unreachable before anything was
sent, the only command ever run is `musegadget send-user-msg` (a socket
client to the same service), always via an argument list (never a shell),
with the message on stdin.
"""

import hmac
import json
import os
import re
import secrets
import socket
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
    "calls. Answer in ONE message only: no preamble, no 'looking' or "
    "'checking' status messages. Do any lookup first, then send the single "
    "final answer.]\n\n")
GADGET_CLI = os.environ.get("GADGET_CLI", "musegadget")
DATA_DIR = os.environ.get("SHIM_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".muse-openclaw-shim")
MAX_BODY = 1024 * 1024          # 1 MB request cap
SEND_WAIT = 280                 # passed to send-user-msg --wait
SEND_TIMEOUT = SEND_WAIT + 45   # the CLI's own socket timeout is wait+30s
HEARTBEAT = float(os.environ.get("SHIM_HEARTBEAT", "10"))
RETRIES = 1                     # exactly one retry after a failed send

# Socket transport (see DESIGN.md section 6). "socket" talks to the
# musegadget service's local unix socket directly and decides itself when
# Muse's answer is complete; "cli" is the original send-user-msg --wait path.
TRANSPORT = os.environ.get("SHIM_TRANSPORT", "socket").strip().lower() or "socket"
SOCKET_PATH = (os.environ.get("MUSEGADGET_SOCKET")
               or "/run/musegadget/musegadget.sock")
QUIET_S = float(os.environ.get("SHIM_QUIET_S", "12"))
PREAMBLE_QUIET_S = float(os.environ.get("SHIM_PREAMBLE_QUIET_S", "30"))
EMPTY_GRACE_S = 3.0             # task finished with no text: wait for late text
SUBSCRIBE_READY_S = 15.0        # wait for the subscription's ready record
SEND_ACK_S = 60.0               # wait for the send's acknowledgement line
FRESH_SUBSCRIBE_TRIES = 3       # subscribe attempts after a fresh-chat send
MAX_LINE = 512 * 1024           # one NDJSON record (SDK caps events at 256 KiB)


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
# Socket transport: direct to the musegadget service's local unix socket.
#
# The SDK's send_and_wait / MuseTurn.complete() calls a turn done after 3 s
# of quiet once every seen reply is done, which returns only the preamble
# when Muse answers "Looking into it..." and then goes quiet doing tool
# work. This transport follows the same chat events but completes on
# rules that cannot fire between a preamble and the real answer.

_PREAMBLE_RE = re.compile(
    r"^(looking|checking|searching|working|fetching|getting|one moment|"
    r"let me|i'll|i will)\b", re.IGNORECASE)


def is_preamble_only(text):
    """True when the collected text looks like a status note, not an answer."""
    t = (text or "").strip()
    if not t or len(t) > 80:
        return False
    if _PREAMBLE_RE.match(t.replace("’", "'")):
        return True
    return t.endswith("…") or t.endswith("...")


_TERMINAL_TASK = frozenset(("completed", "failed", "errored", "cancelled", "canceled"))
_STATUS_EVENTS = ("agent.status", "task.status")
_MESSAGE_EVENTS = ("delta.message_start", "delta.text_append",
                   "delta.message_done", "message.assistant")
# C0/C1 controls except tab and newline, and lone surrogates (as the SDK).
_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_MAX_HELD_EVENTS = 64
_MAX_TEXT_BYTES = 64 * 1024
_MAX_REPLIES = 32


def _text_bytes(payload):
    return sum(len(payload[k].encode("utf-8"))
               for k in ("text", "display_text", "content", "transcript")
               if isinstance(payload.get(k), str))


def _transcript_text(value, message_id=None):
    """Assistant text from Muse's structured transcript (SDK transcript_text)."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    messages = value.get("messages", [])
    if not isinstance(messages, list):
        return ""
    assistants = [m for m in messages
                  if isinstance(m, dict) and m.get("role") == "assistant"]
    if message_id is not None:
        assistants = [m for m in assistants if m.get("id") == message_id]
    if not assistants:
        return ""
    content = assistants[-1].get("content", [])
    if not isinstance(content, list):
        return ""
    return "\n".join(i["text"] for i in content
                     if isinstance(i, dict) and i.get("type") == "text"
                     and isinstance(i.get("text"), str))


def acknowledged_ids(result):
    """Our user message id(s) from a send_chat result.

    Mirrors the SDK's acknowledgement() + _acknowledged_message_ids().
    """
    if not isinstance(result, dict) or not result.get("ok"):
        status = result.get("status") if isinstance(result, dict) else None
        raise MuseError(f"send rejected status={status}")
    response = result.get("response")
    if not isinstance(response, dict):
        raise MuseError("send not acknowledged")
    inner = response.get("result", response)
    if not isinstance(inner, dict):
        raise MuseError("invalid acknowledgement")
    ids = {inner[k] for k in ("message_id", "reply_to_message_id")
           if isinstance(inner.get(k), str) and inner[k]}
    if not ids:
        raise MuseError("acknowledgement omitted the user message id")
    return ids


class TurnTracker:
    """One request followed through the chat event stream.

    A simplified port of the SDK's _ReplyReader (routing) and MuseTurn
    (state) for a non-owned side chat. Thread-safe: the subscription reader
    thread calls take()/end(); the sender waits on `cond`.
    """

    def __init__(self, session_id):
        self.session_id = session_id
        self.cond = threading.Condition()
        self.user_ids = None          # None until the send is acknowledged
        self.after_note = False       # inside our answer (after our message.user)
        self.last_seq = 0
        self.last_row_seq = 0
        self.held = []
        self.held_text = 0
        self.replies = {}             # message id -> {"text", "done"}, arrival order
        self.ignored = set()
        self.busy = False
        self.task_finished = False
        self.last_activity = time.monotonic()
        self.ended = None             # why the subscription stopped, if it did

    # ---- input (reader thread)
    def take(self, event):
        with self.cond:
            if not isinstance(event, dict):
                return
            seq = event.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                # message rows carry their own sequence, apart from live events
                if event.get("event") in ("message.user", "message.assistant"):
                    if seq <= self.last_row_seq:
                        return
                    self.last_row_seq = seq
                else:
                    if seq <= self.last_seq:
                        return
                    self.last_seq = seq
            if self.user_ids is None:
                payload = event.get("payload")
                if len(self.held) >= _MAX_HELD_EVENTS:
                    raise ValueError("too many events before the acknowledgement")
                if isinstance(payload, dict):
                    self.held_text += _text_bytes(payload)
                if self.held_text > _MAX_TEXT_BYTES:
                    raise ValueError("too much text before the acknowledgement")
                self.held.append(event)
                return
            self._route(event)
            self.cond.notify_all()

    def end(self, reason):
        with self.cond:
            if self.ended is None:
                self.ended = reason
            self.cond.notify_all()

    def acknowledge(self, user_ids, fresh):
        with self.cond:
            self.user_ids = set(user_ids)
            self.after_note = fresh   # fresh chat: everything that follows is ours
            held, self.held = self.held, []
            for event in held:
                self._route(event)
            self.cond.notify_all()

    # ---- routing (_ReplyReader._route)
    def _route(self, event):
        payload = event.get("payload")
        if event.get("type") != "event" or not isinstance(payload, dict):
            return
        chat = payload.get("session_id") or event.get("session_id")
        if chat and self.session_id and chat != self.session_id:
            return
        mid = payload.get("message_id") or event.get("message_id") or payload.get("id")
        mid = mid if isinstance(mid, str) else None
        name = event.get("event")
        if name == "message.user":
            self.after_note = mid in self.user_ids
            return
        parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
        parent = parent if isinstance(parent, str) else None
        if not (self.after_note or parent in self.user_ids or mid in self.replies
                or parent in self.replies):
            return
        if name in _STATUS_EVENTS:
            self._status(name, payload, parent)
        elif name in _MESSAGE_EVENTS:
            self._message(name, payload, mid, parent)

    # ---- state (MuseTurn._status / _message, not-owned-chat path)
    def _status(self, name, payload, parent):
        # A shared chat can carry statuses for other requests: ignore an
        # explicitly foreign parent. Parentless statuses count.
        if parent and parent not in self.user_ids and parent not in self.replies:
            return
        activity = payload.get("activity_code")
        status = payload.get("status")
        terminal = isinstance(status, str) and status in _TERMINAL_TASK
        if name == "task.status" and isinstance(status, str):
            self.task_finished = terminal
        if isinstance(activity, str):
            self.busy = (activity not in ("", "online", "idle")
                         and activity not in _TERMINAL_TASK)
        elif isinstance(status, str):
            self.busy = status not in ("", "idle") and not terminal
        if name == "task.status" and self.task_finished:
            self.busy = False
        self.last_activity = time.monotonic()

    def _message(self, name, payload, mid, parent):
        if not mid or payload.get("role") == "user":
            return
        reply = self.replies.get(mid)
        if parent and parent not in self.user_ids and parent not in self.replies:
            if reply is None:
                self.ignored.add(mid)
            return
        if reply is None:
            if mid in self.ignored:
                return
            if len(self.replies) >= _MAX_REPLIES:
                raise ValueError("too many reply messages")
            reply = {"text": "", "done": False}
            self.replies[mid] = reply
        self.last_activity = time.monotonic()
        # SDK _update_reply: deltas accumulate; the final text replaces them.
        if name == "delta.text_append" and not reply["done"]:
            delta = payload.get("text")
            if isinstance(delta, str):
                reply["text"] += _CONTROLS.sub("", delta)
        if name in ("delta.message_done", "message.assistant"):
            final = payload.get("display_text")
            if not isinstance(final, str):
                transcript = payload.get("transcript")
                final = (transcript if isinstance(transcript, str)
                         else _transcript_text(transcript, mid))
                if not final and not isinstance(transcript, str):
                    final = payload.get("content")
            if isinstance(final, str):
                reply["text"] = _CONTROLS.sub("", final)
            if (name == "delta.message_done"
                    or payload.get("display_text_ready") is not False):
                reply["done"] = True
        if len(reply["text"].encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ValueError("reply text over 64 KiB")

    # ---- output
    def text(self):
        """Reply texts, blank-line separated (SDK _reply_text)."""
        return "\n\n".join(r["text"].strip() for r in self.replies.values()
                           if r["text"].strip())

    def rule(self, now, quiet_s=None, preamble_quiet_s=None):
        """The completion rule that fires now, or None. Call with cond held."""
        quiet_s = QUIET_S if quiet_s is None else quiet_s
        preamble_quiet_s = PREAMBLE_QUIET_S if preamble_quiet_s is None else preamble_quiet_s
        if self.user_ids is None:
            return None
        replies = list(self.replies.values())
        text = self.text()
        quiet = now - self.last_activity
        if self.task_finished and all(r["done"] for r in replies):
            if text:
                return "A"                       # task finished, replies done
            return "empty" if quiet >= EMPTY_GRACE_S else None
        if not text or not any(r["done"] for r in replies):
            return None
        # Judge the latest message: "Looking into it..." followed by a short
        # real answer must not keep counting as preamble-only.
        latest = [r["text"] for r in replies if r["text"].strip()][-1]
        if is_preamble_only(latest):
            return "C" if quiet >= preamble_quiet_s else None
        if self.busy:                            # Muse says it is still working
            return "B_busy" if quiet >= preamble_quiet_s else None
        return "B" if quiet >= quiet_s else None


class SocketUnavailable(Exception):
    """Nothing was sent; the caller may use the CLI transport instead."""


class _Subscription:
    """One `subscribe_chat` connection to the local service."""

    def __init__(self, path, session_id):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(SUBSCRIBE_READY_S)
            self.sock.connect(path)
            # Never half-close: the service ends a subscription on any input
            # or EOF from the client.
            self.sock.sendall(json.dumps({"op": "subscribe_chat",
                                          "session_id": session_id}).encode() + b"\n")
        except BaseException:
            self.sock.close()
            raise
        self.file = self.sock.makefile("rb")
        self.thread = None

    def wait_ready(self):
        """True on `ready`; False on an error record or EOF. Raises on timeout."""
        while True:
            line = self.file.readline(MAX_LINE)
            if not line:
                return False
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") == "ready":
                self.sock.settimeout(None)
                return True
            if rec.get("type") == "error":
                return False

    def start(self, tracker):
        self.thread = threading.Thread(target=self._pump, args=(tracker,), daemon=True)
        self.thread.start()

    def _pump(self, tracker):
        reason = "closed"
        try:
            while True:
                line = self.file.readline(MAX_LINE)
                if not line:
                    break
                if not line.endswith(b"\n") and len(line) >= MAX_LINE:
                    reason = "oversized event"
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") == "chat_event":
                    tracker.take(rec.get("event"))
                elif rec.get("type") == "error":
                    reason = "subscription error"
                    break
        except ValueError as e:          # tracker size bounds
            reason = str(e)
        except OSError as e:
            reason = f"socket {e.__class__.__name__}"
        tracker.end(reason)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.file.close()
        except OSError:
            pass
        self.sock.close()


def _open_subscription(session_id):
    """(subscription or None, why). Raises OSError if the socket is unreachable."""
    sub = _Subscription(SOCKET_PATH, session_id)
    try:
        if sub.wait_ready():
            return sub, "ready"
        why = "error"
    except (OSError, ValueError) as e:   # includes socket.timeout
        why = f"{e.__class__.__name__}"
    sub.close()
    return None, why


def _socket_send_message(session_id, text, timeout):
    """Send one message, no wait. Returns the send_chat result dict.

    Raises SocketUnavailable when the connect fails (nothing sent) and
    MuseError for anything after that (the message may have landed).
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        try:
            s.connect(SOCKET_PATH)
        except OSError as e:
            raise SocketUnavailable(f"connect {e.__class__.__name__}")
        try:
            s.sendall(json.dumps({"message": text, "session_id": session_id}).encode()
                      + b"\n")
            with s.makefile("rb") as f:
                line = f.readline(MAX_LINE)
        except OSError as e:
            raise MuseError(f"send failed: {e.__class__.__name__}")
    finally:
        s.close()
    if not line:
        raise MuseError("send: no acknowledgement")
    try:
        result = json.loads(line)
    except ValueError:
        raise MuseError("send: unreadable acknowledgement")
    return result


def socket_send(session_id, text):
    """Send via the local socket and wait for the whole answer.

    Returns (reply, 1). Raises SocketUnavailable before anything was sent,
    MuseError after. Never sends the message twice.
    """
    started = time.monotonic()
    deadline = started + SEND_WAIT
    tracker = TurnTracker(session_id)
    try:
        sub, why = _open_subscription(session_id)
    except OSError as e:
        raise SocketUnavailable(f"subscribe connect {e.__class__.__name__}")
    fresh = sub is None
    if fresh and why != "error":
        # Timed out / broken before ready: nothing sent yet, let the CLI try.
        raise SocketUnavailable(f"subscribe {why}")
    try:
        if sub is not None:
            sub.start(tracker)
        result = _socket_send_message(session_id, text,
                                      max(1.0, min(SEND_ACK_S, deadline - time.monotonic())))
        ids = acknowledged_ids(result)
        if fresh:
            # The chat did not exist before this message; follow it now.
            for attempt in range(FRESH_SUBSCRIBE_TRIES):
                try:
                    sub, why = _open_subscription(session_id)
                except OSError as e:
                    sub, why = None, f"connect {e.__class__.__name__}"
                if sub is not None:
                    break
                log(f"socket fresh-chat subscribe attempt {attempt + 1} failed "
                    f"thread={session_id}: {why}")
                time.sleep(0.5)
            if sub is None:
                raise MuseError("sent, but could not follow the fresh chat")
            sub.start(tracker)
        try:
            tracker.acknowledge(ids, fresh)
        except ValueError as e:          # held events broke the size bounds
            tracker.end(str(e))
        with tracker.cond:
            while True:
                now = time.monotonic()
                rule = tracker.rule(now)
                if rule:
                    break
                if tracker.ended is not None:
                    rule = "stream_end"
                    break
                if now >= deadline:
                    rule = "D"
                    break
                tracker.cond.wait(min(0.2, deadline - now))
            reply = tracker.text()
            nreplies = len(tracker.replies)
            ended = tracker.ended
    finally:
        if sub is not None:
            sub.close()
    elapsed = time.monotonic() - started
    partial = rule in ("D", "stream_end")
    log(f"socket send thread={session_id} rule={rule} replies={nreplies} "
        f"fresh={int(fresh)} elapsed={elapsed:.1f}s"
        + (" partial=1" if partial and reply else "")
        + (f" ended={ended}" if rule == "stream_end" else ""))
    if rule == "empty" or not reply:
        raise MuseError(f"no reply (rule={rule})")
    return reply, 1


def transport_send(session_id, text):
    """The configured transport. Returns (reply, attempts) like muse_send."""
    if TRANSPORT != "socket":
        return muse_send(session_id, text)
    try:
        return socket_send(session_id, text)
    except SocketUnavailable as e:
        log(f"socket transport unavailable thread={session_id}: {e}; "
            f"falling back to cli")
        return muse_send(session_id, text)


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
            reply, attempts = transport_send(tid, text)
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
