# Gloss Session Console

Live view of Blender's terminal output while testing the add-on, published as a
Claude artifact with a diagnostics panel keyed to the add-on's own failure
messages. Console: https://claude.ai/artifact/J4nmS1kP1Zw4nYncsQWo5b

## How lines travel

    Blender (pty) ──► ship.py ──► ~/.gloss/console/blender.jsonl
    backend log   ──► ship.py ──► ~/.gloss/console/server.jsonl
                                    │
                        push.py cuts new lines into ~/.gloss/console/outbox/*.json
                                    │
                 the Claude Code session writes them into the artifact database
                                    │
                        the page subscribes and re-renders (db capability)

The artifact sandbox cannot reach this machine, so the last hop is the Claude
Code session: it watches the log for growth and pushes. Keep that session open
while testing; latency is the poll interval (about 15 s).

## Files

- `launch_blender.sh <scene.blend>` — runs Blender under `script` (a pty, so
  every print flushes immediately), adds `blender_hook.py`, pipes into `ship.py`.
- `blender_hook.py` — runs inside Blender; prints one `GLOSS_CONSOLE {json}`
  header (Blender version, add-on version + commit, .blend path).
- `ship.py --source <name>` — timestamps and classifies stdin lines
  (tag + level, traceback grouping), appends JSONL, echoes to the terminal.
- `push.py` — packages new lines into chunk documents (≤400 lines each) plus
  `session/current`, and prints the writes for one `ArtifactData` batch.
  - `--pending` prints one line when there is unpushed output (used by the watcher)
  - `--new-session` starts a fresh session id for a new Blender run
  - `--link backend=<url>` cross-links a backend artifact on the page

## Backend stream

Pipe the server log into the same console under the `server` source; the page
merges it into the timeline by timestamp:

    ssh <gpu-host> 'tail -F /path/to/glaze_server.log' | python3 tools/console/ship.py --source server

## Database shape

- `session/current`: `{session, started_at, blend, blender_version, addon:{version,commit}, sources:{blender:{lines,chunks,errors,last_push,last_ts}}, links:{}}`
- `chunks/<session>-<source>-<seq>`: `{session, source, seq, start_ts, end_ts, count, errors, lines:[{t,lvl,tag,text,cont}]}`
