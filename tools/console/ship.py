#!/usr/bin/env python3
"""Timestamp, classify and record console lines arriving on stdin.

Usage:  <producer> 2>&1 | python3 ship.py --source blender
Every line is echoed back to the terminal unchanged and appended, as JSON, to
~/.gloss/console/<source>.jsonl for push.py to ship to the console artifact.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.expanduser("~/.gloss/console")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# (pattern, tag, level). First match wins; level None means "use the generic
# level heuristic". Patterns are the add-on's own print() strings.
RULES = [
    (r"^GLOSS_CONSOLE ", "session", "ok"),
    (r"^Traceback \(most recent call last\)", "python", "error"),
    (r"^\s+File \".*\", line \d+", "python", "error"),
    (r"^(WS connected)", "ws", "ok"),
    (r"WS connect failed|WS closed|Receive failed|Send failed", "ws", "error"),
    (r"receive_worker|Starting WS client|Stopping WS client|WS client|WS already running", "ws", "info"),
    (r"server error during", "server", "error"),
    (r"timed out waiting for server", "timeout", "error"),
    (r"handler for .* failed|failed to decode message", "router", "error"),
    (r"no handler for message type", "router", "warn"),
    (r"texture push failed|sync_paint_texture worker failed", "sync", "error"),
    (r"Texture update failed", "texture", "error"),
    (r"updating texture|copying prev texture", "texture", "info"),
    (r"\[CREATE BRUSH\].*(failed|timed out)", "brush", "error"),
    (r"\[CREATE BRUSH\]", "brush", "ok"),
    (r"Loading (reference|paint) mesh", "assets", "info"),
    (r"Loaded GlossConfig", "config", "ok"),
    (r"Warning: GlossConfig", "config", "warn"),
    (r"Preview load error", "assets", "error"),
    (r"already registered, skipping", "register", "warn"),
    (r"Task was destroyed but it is pending|^task: <Task pending|RuntimeWarning|coroutine .* was never awaited", "asyncio", "warn"),
    (r"^Blender quit", "session", "info"),
    (r"^\[gloss\]", "status", "info"),
    (r"^received:|mock server listening|fill_status|texture_chunk|brush_status", "server", "info"),
    (r"^Read (blend|prefs)|^Blender \d|^Color management|^Warning:|^Error:|^Info:", "blender", None),
]
COMPILED = [(re.compile(p, re.I), tag, lvl) for p, tag, lvl in RULES]
ERR = re.compile(r"traceback|error|exception|failed|timed out|cannot|refused|denied", re.I)
WARN = re.compile(r"warn|skipping|retry|deprecated|missing", re.I)
OK = re.compile(r"connected|done|synced|listening|loaded|registered|ready", re.I)


def classify(text):
    for rx, tag, lvl in COMPILED:
        if rx.search(text):
            return tag, lvl or generic_level(text)
    return "stdout", generic_level(text)


def generic_level(text):
    if ERR.search(text):
        return "error"
    if WARN.search(text):
        return "warn"
    if OK.search(text):
        return "ok"
    return "info"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="blender", help="blender | server | <name>")
    ap.add_argument("--quiet", action="store_true", help="do not echo to stdout")
    args = ap.parse_args()

    os.makedirs(ROOT, exist_ok=True)
    out_path = os.path.join(ROOT, f"{args.source}.jsonl")
    meta_path = os.path.join(ROOT, f"{args.source}.meta.json")
    out = open(out_path, "a", encoding="utf-8")
    in_traceback = False

    for raw in sys.stdin.buffer:
        text = ANSI.sub("", raw.decode("utf-8", "replace")).rstrip("\r\n")
        if not text.strip():
            continue
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
        tag, lvl = classify(text)
        cont = False
        if text.startswith("Traceback (most recent call last)"):
            in_traceback = True
        elif in_traceback:
            if text.startswith((" ", "\t")):
                cont, tag, lvl = True, "python", "error"
            else:  # the exception line closes the traceback
                tag, lvl, in_traceback = "python", "error", False
        rec = {"t": now, "s": args.source, "lvl": lvl, "tag": tag, "text": text, "cont": cont}
        if tag == "session":
            try:
                meta = json.loads(text[len("GLOSS_CONSOLE "):])
                meta["seen_at"] = now
                with open(meta_path, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh)
            except ValueError:
                pass
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        if not args.quiet:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
