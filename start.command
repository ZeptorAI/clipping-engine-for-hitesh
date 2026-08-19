#!/bin/bash
# Clip Editor - launch (macOS). Double-click to run.
cd "$(dirname "$0")"
echo "============================================"
echo "   Clip Editor"
echo "   Keep this window open while you use it."
echo "   Close it when done to stop the app."
echo "============================================"
echo ""

# pull the latest editing logic Hitesh pushed (silent, never blocks launch)
echo "Checking for updates..."
git pull --quiet 2>/dev/null || true
echo ""

# use the isolated environment if setup made one, else fall back to python3
PY="python3"
[ -x "./.venv/bin/python" ] && PY="./.venv/bin/python"

# open the browser a moment after the server starts
( sleep 3; open http://127.0.0.1:5000 ) &

echo "Opening http://127.0.0.1:5000 ..."
"$PY" app.py
echo ""
echo "The app has stopped."
read -p "Press Enter to close..."
