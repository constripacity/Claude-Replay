#!/bin/bash
# Start the Claude Replay terminal UI (companion to the web dashboard).
# Pass --url to point at a remote server. Requires `claude-replay serve` running.

cd "$(dirname "$0")"
python3 -m claude_replay.tui "$@"
