#!/usr/bin/env python3
"""Package new console lines for the Gloss Session Console artifact.

The artifact page cannot reach this machine (its sandbox blocks localhost), so
lines travel through the Claude Code session: this script cuts the new part of
~/.gloss/console/<source>.jsonl into chunk documents in the outbox, and the
session writes them into the artifact database with one ArtifactData batch.

  python3 push.py                 # package everything new (all sources)
  python3 push.py --pending       # print "<n> pending" if there is work, else nothing
  python3 push.py --new-session   # start a fresh session id (new Blender run)
  python3 push.py --link backend=https://claude.ai/artifact/...   # cross-link
"""
import argparse
import datetime as dt
import glob
import json
import os
import socket

ROOT = os.path.expanduser("~/.gloss/console")
OUTBOX = os.path.join(ROOT, "outbox")
STATE = os.path.join(ROOT, "state.json")
MAX_LINES = 400          # per chunk document (well under the 256 KiB doc cap)
MAX_CHUNKS = 10          # per push, to stay inside one 50-write batch


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    return new_session({})


def new_session(state):
    sid = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    state.update({
        "session": sid,
        "started_at": now(),
        "offset": {},
        "seq": {},
        "sources": {},
        "links": state.get("links", {}),
    })
    return state


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)


def sources():
    return [os.path.basename(p)[:-6] for p in sorted(glob.glob(os.path.join(ROOT, "*.jsonl")))]


def read_new(source, offset):
    path = os.path.join(ROOT, f"{source}.jsonl")
    if not os.path.exists(path):
        return [], offset
    size = os.path.getsize(path)
    if size < offset:          # file was truncated: start over
        offset = 0
    lines = []
    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):   # partial line still being written
                fh.seek(pos)
                break
            try:
                lines.append(json.loads(raw.decode("utf-8")))
            except ValueError:
                continue
        offset = fh.tell()
    return lines, offset


def meta(source):
    path = os.path.join(ROOT, f"{source}.meta.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def read_blend_from_log():
    """Fallback: Blender prints 'Read blend: "<path>"' when it opens a file."""
    path = os.path.join(ROOT, "blender.jsonl")
    found = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                if '"Read blend: ' in raw:
                    try:
                        text = json.loads(raw)["text"]
                        found = text.split('"', 2)[1]
                    except (ValueError, IndexError, KeyError):
                        pass
    return found


def session_doc(state):
    doc = {
        "session": state["session"],
        "started_at": state["started_at"],
        "updated_at": now(),
        "host": socket.gethostname(),
        "sources": state["sources"],
        "links": state.get("links", {}),
    }
    m = meta("blender")
    if m:
        doc["blend"] = m.get("blend") or read_blend_from_log()
        doc["blender_version"] = m.get("blender_version")
        doc["addon"] = m.get("addon", {})
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--new-session", action="store_true")
    ap.add_argument("--link", action="append", default=[], metavar="NAME=URL")
    args = ap.parse_args()

    os.makedirs(OUTBOX, exist_ok=True)
    state = load_state()
    if args.new_session:
        new_session(state)
    for item in args.link:
        name, _, url = item.partition("=")
        state.setdefault("links", {})[name] = url

    if args.pending:
        total = 0
        for src in sources():
            path = os.path.join(ROOT, f"{src}.jsonl")
            total += max(0, os.path.getsize(path) - state["offset"].get(src, 0))
        if total:
            print(f"{total} bytes pending")
        return

    writes = []
    for src in sources():
        lines, offset = read_new(src, state["offset"].get(src, 0))
        chunks = [lines[i:i + MAX_LINES] for i in range(0, len(lines), MAX_LINES)][:MAX_CHUNKS]
        shipped = 0
        for chunk in chunks:
            seq = state["seq"].get(src, 0) + 1
            state["seq"][src] = seq
            doc_id = f"{state['session']}-{src}-{seq:05d}"
            body = {
                "session": state["session"], "source": src, "seq": seq,
                "start_ts": chunk[0]["t"], "end_ts": chunk[-1]["t"],
                "count": len(chunk),
                "errors": sum(1 for l in chunk if l["lvl"] == "error"),
                "lines": [{k: v for k, v in l.items() if k != "s"} for l in chunk],
            }
            path = os.path.join(OUTBOX, doc_id + ".json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(body, fh, ensure_ascii=False)
            writes.append({"op": "set", "collection": "chunks", "doc_id": doc_id, "file_path": path})
            shipped += len(chunk)
        if shipped:
            # advance the offset only past what we packaged
            consumed = sum(len(c) for c in chunks)
            if consumed < len(lines):
                # re-read to find the byte offset of the first unshipped line
                lines2, _ = read_new(src, state["offset"].get(src, 0))
                path = os.path.join(ROOT, f"{src}.jsonl")
                with open(path, "rb") as fh:
                    fh.seek(state["offset"].get(src, 0))
                    for _ in range(consumed):
                        fh.readline()
                    offset = fh.tell()
            state["offset"][src] = offset
            s = state["sources"].setdefault(src, {"lines": 0, "chunks": 0, "errors": 0})
            s["lines"] += shipped
            s["chunks"] += len(chunks)
            s["errors"] += sum(1 for c in chunks for l in c if l["lvl"] == "error")
            s["last_push"] = now()
            s["last_ts"] = chunks[-1][-1]["t"]

    sess_path = os.path.join(OUTBOX, "session.json")
    with open(sess_path, "w", encoding="utf-8") as fh:
        json.dump(session_doc(state), fh, ensure_ascii=False)
    writes.append({"op": "set", "collection": "session", "doc_id": "current", "file_path": sess_path})
    save_state(state)
    print(json.dumps({"session": state["session"], "writes": writes}, indent=1))


if __name__ == "__main__":
    main()
