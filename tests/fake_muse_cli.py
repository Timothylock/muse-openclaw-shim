#!/usr/bin/env python3
"""Fake muse-cli used by the tests. Behaviour is driven by $FAKE_MUSE_DIR:

  mode      one of: ok | fail_once | fail_always | no_reply_once | slow
  calls.jsonl   every invocation's argv + MUSE_NO_UPDATE_CHECK is appended here
"""
import json
import os
import sys
import time

d = os.environ["FAKE_MUSE_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": argv,
                        "no_update": os.environ.get("MUSE_NO_UPDATE_CHECK")}) + "\n")

try:
    mode = open(os.path.join(d, "mode")).read().strip()
except FileNotFoundError:
    mode = "ok"

if argv[:1] == ["raw"]:
    sys.exit(99)

if argv[:1] == ["session-start"]:
    n_path = os.path.join(d, "sessions")
    n = int(open(n_path).read()) + 1 if os.path.exists(n_path) else 1
    open(n_path, "w").write(str(n))
    title = argv[argv.index("--title") + 1] if "--title" in argv else None
    print(json.dumps({"session_id": f"sess-{n}", "title": title}, indent=2))
    sys.exit(0)

if argv[:1] == ["send"]:
    # expected: send --thread <id> --wait 280 -- <text>
    if argv[1] != "--thread" or argv[3:6] != ["--wait", "280", "--"] or len(argv) != 7:
        print("bad argv", file=sys.stderr)
        sys.exit(2)
    text = argv[6]
    sends_path = os.path.join(d, "sends")
    n = int(open(sends_path).read()) + 1 if os.path.exists(sends_path) else 1
    open(sends_path, "w").write(str(n))
    if mode == "fail_always" or (mode == "fail_once" and n == 1):
        print("gateway error", file=sys.stderr)
        sys.exit(3)
    if mode == "no_reply_once" and n == 1:
        print(json.dumps({"sent": True, "stream": "s", "note": "no assistant reply"}))
        sys.exit(0)
    if mode == "slow":
        time.sleep(1.2)
    print(json.dumps({"sent": True, "stream": "s1",
                      "reply": {"seq": 5, "role": "assistant", "message_id": "m1",
                                "text": f"echo[{argv[2]}]: {text}"}}, indent=2))
    sys.exit(0)

sys.exit(2)
