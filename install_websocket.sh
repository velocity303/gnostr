#!/bin/bash
set -e
echo "==> [Websocket] Starting Installation..."

# debug: show where we are
pwd
ls -F

echo "==> [Websocket] Installing with pip..."
# We use the simplest robust command possible
# --prefix=/app ensures it goes to the flatpak runtime directory
# . means "current directory" (where the git repo is)
pip3 install --verbose --prefix=/app .

echo "==> [Websocket] Checking installation..."
# Verify it actually landed in /app
find /app -type d -name "websocket"
