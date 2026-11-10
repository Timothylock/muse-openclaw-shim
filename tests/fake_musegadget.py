#!/usr/bin/env python3
"""Fake `musegadget` used by the shim tests. Driven by $FAKE_GADGET_DIR:

  mode         one of: ok | fail_once | fail_always | exit3_once |
               exit3_always | no_reply_once | slow
  calls.jsonl  every invocation's argv + stdin is appended here

Only `send-user-msg --session-id <uuid> --wait 280 -` is accepted; anything
else (run, pair, info, ...) is recorded and exits 99 so tests can spot it.
"""
import json
import os
import sys
import time

d = os.environ["FAKE_GADGET_DIR"]
argv = sys.argv[1:]
stdin = sys.stdin.read() if argv[:1] == ["send-user-msg"] else ""
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")

try:
    mode = open(os.path.join(d, "mode")).read().strip()
except FileNotFoundError:
    mode = "ok"

if argv[:1] != ["send-user-msg"]:
    sys.exit(99)

if (len(argv) != 6 or argv[1] != "--session-id"
        or argv[3:6] != ["--wait", "280", "-"]):
    print("bad argv", file=sys.stderr)
    sys.exit(2)
sid = argv[2]

sends_path = os.path.join(d, "sends")
n = int(open(sends_path).read()) + 1 if os.path.exists(sends_path) else 1
open(sends_path, "w").write(str(n))

if mode == "fail_always" or (mode == "fail_once" and n == 1):
    print("error: service unreachable", file=sys.stderr)
    sys.exit(1)
if mode == "exit3_always" or (mode == "exit3_once" and n == 1):
    sys.stdout.write("partial answ")
    sys.exit(3)
if mode == "no_reply_once" and n == 1:
    sys.exit(0)
if mode == "slow":
    time.sleep(1.2)

print(f"echo[{sid}]: {stdin}")
sys.exit(0)
