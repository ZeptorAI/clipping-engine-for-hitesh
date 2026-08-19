#!/bin/bash
# Clip Editor - one-time setup (macOS). Double-click to run.
cd "$(dirname "$0")"
echo "============================================"
echo "   Clip Editor - one-time setup (Mac)"
echo "============================================"
echo ""

# Homebrew (needed to install ffmpeg)
if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew (this can take a few minutes)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
# make sure brew is on PATH for this session (Apple Silicon vs Intel)
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
[ -x /usr/local/bin/brew ]   && eval "$(/usr/local/bin/brew shellenv)"

# ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Installing ffmpeg..."
  brew install ffmpeg
fi

# git (for auto-updates)
if ! command -v git >/dev/null 2>&1; then
  echo "Installing git..."
  brew install git
fi

# python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "Installing python3..."
  brew install python
fi

# isolated python environment + dependencies (avoids macOS pip restrictions)
echo "Setting up Python environment..."
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/pip install flask anthropic requests

echo ""
echo "Setup complete!  Now double-click  start.command  to run the app."
echo ""
read -p "Press Enter to close this window..."
