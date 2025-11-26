#!/bin/bash
set -e
echo "==> [Websocket] Starting Installation..."
pip3 install --verbose --prefix=/app .
echo "==> [Websocket] Checking installation..."
find /app -type d -name "websocket"
