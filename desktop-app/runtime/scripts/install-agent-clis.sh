#!/usr/bin/env bash
# Install the CLI agent pack used by the Dutang role scheduler.
# The web onboarding page exposes the same recipes one agent at a time.
set -euo pipefail

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing $1" >&2; exit 1; }; }
need npm
need python3

echo "[1/2] npm agents"
npm install --global \
  @openai/codex \
  @anthropic-ai/claude-code \
  @earendil-works/pi-coding-agent \
  @google/gemini-cli \
  opencode-ai \
  openclaw \
  @qwen-code/qwen-code \
  @github/copilot

echo "[2/2] aider"
python3 -m pip install --user --upgrade aider-chat

echo "Installed. If your shell cannot find a command, add ~/.local/bin to PATH."
