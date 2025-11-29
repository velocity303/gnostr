#!/bin/bash
set -e
echo "==> [Deps] Starting Dependency Installation..."

pip3 install --verbose --prefix=/app --no-build-isolation --no-deps --ignore-installed .

echo "==> [Deps] Verifying installation..."
if [ -d "/app/lib/python3.12/site-packages/websocket" ]; then
    echo "SUCCESS: Websocket package found in standard location."
else
    find /app -name "websocket"
fi
