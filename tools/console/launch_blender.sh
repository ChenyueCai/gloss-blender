#!/bin/zsh
# Launch Blender under a pseudo-terminal so every print() lands in the console
# immediately, then record it with ship.py. Extra args go to Blender.
#   tools/console/launch_blender.sh "/path/to/scene.blend"
set -u
HERE="${0:A:h}"
BLENDER="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
mkdir -p ~/.gloss/console
# `script` gives Blender a tty (line-buffered C and Python stdio) and relays
# the transcript to our pipe; the transcript file itself is discarded.
script -q /dev/null "$BLENDER" --python "$HERE/blender_hook.py" "$@" 2>&1 \
  | python3 "$HERE/ship.py" --source blender
