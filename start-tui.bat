@echo off
echo Starting Claude Replay TUI...
cd /d "%~dp0"
python -m claude_replay.tui %*
