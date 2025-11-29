#!/bin/bash
set -e
echo "==> [Deps] Starting Dependency Installation..."

# Install all wheels found in the current directory (which includes websocket and ecdsa)
# --no-index tells pip NOT to look online
# --find-links . tells pip to look for wheels in the current folder
pip3 install --verbose --prefix=/app --no-build-isolation --no-deps --no-index --find-links . *.whl

echo "==> [Deps] Verifying installation..."
if [ -d "/app/lib/python3.12/site-packages/websocket" ]; then
    echo "✅ Websocket package installed."
else
    echo "❌ Websocket missing"
    exit 1
fi

if [ -d "/app/lib/python3.12/site-packages/ecdsa" ]; then
    echo "✅ ECDSA package installed."
else
    echo "❌ ECDSA missing"
    exit 1
fi
