#!/bin/bash
set -e
echo "==> [Gnostr] Starting App Installation..."

# Debug: Show us the files
ls -R

echo "==> [Gnostr] Creating directories..."
mkdir -p /app/bin

echo "==> [Gnostr] Copying source..."
# We expect main.py to be in the root because we mount the dir as '.'
# But if it is in src/, let's handle both cases
if [ -f "src/main.py" ]; then
    cp -v src/main.py /app/bin/gnostr
elif [ -f "main.py" ]; then
    cp -v main.py /app/bin/gnostr
else
    echo "ERROR: Could not find main.py in . or src/"
    exit 1
fi

echo "==> [Gnostr] Setting permissions..."
chmod +x /app/bin/gnostr

echo "==> [Gnostr] Verifying Python Environment..."
# Dynamically find site-packages to be safe
SITE_PKG=$(find /app -type d -name "site-packages" | head -n 1)
export PYTHONPATH=$PYTHONPATH:$SITE_PKG

python3 -c "import websocket; print('SUCCESS: Websocket found at', websocket.__file__)"
