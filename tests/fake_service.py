"""Fake musegadget service local socket, for the socket-transport tests.

A threaded unix-socket server that speaks the two local-socket requests the
shim uses (see the SDK's Service.serve_local):

  {"op": "subscribe_chat", "session_id": ...}  -> NDJSON stream
  {"message": ..., "session_id": ...}          -> one JSON line (send result)

Scripting:
  subscribe_plan  list, one entry per subscribe connection in order:
                  "stream" (ready, then the scripted events once the message
                  was sent) or "error" (an error record before ready).
                  Connections past the end of the list use "stream".
  events          list of (delay_s, event) streamed after the send; event
                  dicts get "type": "event" and the chat's session_id added.
                  The first item should normally be the message.user event.
"""
import json
import os
import socketserver
import threading
import time

USER_ID = "user-msg-0001"


def ev(name, seq, **payload):
    return {"event": name, "seq": seq, "payload": payload}


class FakeService:
    def __init__(self, path, events, subscribe_plan=("stream",)):
        self.path = path
        self.events = list(events)
        self.plan = list(subscribe_plan)
        self.lock = threading.Lock()
        self.sent = threading.Event()
        self.sends = []            # send requests received
        self.subscribes = 0
        self.sent_at = None
        self.events_done_at = None
        self.last_event_at = None
        self.stop = threading.Event()
        svc = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline()
                try:
                    req = json.loads(line)
                except ValueError:
                    return
                if isinstance(req, dict) and req.get("op") == "subscribe_chat":
                    svc._subscribe(self, req)
                else:
                    svc._send(self, req)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        if os.path.exists(path):
            os.unlink(path)
        self.server = Server(path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()

    def _write(self, h, obj):
        h.wfile.write(json.dumps(obj).encode() + b"\n")
        h.wfile.flush()

    def _subscribe(self, h, req):
        with self.lock:
            idx = self.subscribes
            self.subscribes += 1
        mode = self.plan[idx] if idx < len(self.plan) else "stream"
        if mode == "error":
            self._write(h, {"type": "error", "error": "Muse chat subscription failed"})
            return
        self._write(h, {"type": "ready"})
        sid = req.get("session_id")
        if not self.sent.wait(30):
            return
        try:
            for delay, event in self.events:
                if self.stop.wait(delay):
                    return
                event = dict(event, type="event", session_id=sid)
                event["payload"] = dict(event.get("payload", {}), session_id=sid)
                self._write(h, {"type": "chat_event", "event": event})
                self.last_event_at = time.monotonic()
            self.events_done_at = time.monotonic()
            # keep the subscription open, as the real service does
            while not self.stop.wait(0.1):
                pass
        except OSError:
            return

    def _send(self, h, req):
        with self.lock:
            self.sends.append(req)
        self.sent_at = time.monotonic()
        self._write(h, {"ok": True, "status": 200, "response": {
            "message_id": USER_ID, "session_id": req.get("session_id"),
            "is_thread": True}})
        self.sent.set()
