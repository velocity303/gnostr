#!/bin/bash
set -e
echo "==> [Gnostr] Starting App Installation..."
mkdir -p /app/bin

echo "==> [Gnostr] Copying Python source files..."
SRC_DIR="."
if [ -d "src" ]; then SRC_DIR="src"; fi

cp -v $SRC_DIR/*.py /app/bin/

if [ -f "/app/bin/main.py" ]; then
    mv /app/bin/main.py /app/bin/gnostr
else
    echo "ERROR: main.py not found"
    exit 1
fi

chmod +x /app/bin/gnostr

echo "==> [Gnostr] Verifying Environment..."
SITE_PKG=$(find /app -type d -name "site-packages" | head -n 1)
export PYTHONPATH=$PYTHONPATH:$SITE_PKG
# We also need to help it find the Libsecret typelib we just built
export GI_TYPELIB_PATH=/app/lib/girepository-1.0:/app/lib64/girepository-1.0:$GI_TYPELIB_PATH

python3 -c "import websocket; print('SUCCESS: Websocket found')"
# Optional: Verify Libsecret is found during build
python3 -c "import gi; from gi.repository import Libsecret; print('SUCCESS: Libsecret found')"
