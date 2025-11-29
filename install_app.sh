#!/bin/bash
set -e
echo "==> [App] Starting Application Installation..."

mkdir -p /app/bin

echo "==> [App] Copying Source Code..."
SRC="."
if [ -d "src" ]; then SRC="src"; fi

cp -v $SRC/*.py /app/bin/

if [ -f "/app/bin/main.py" ]; then
    mv /app/bin/main.py /app/bin/gnostr
fi

chmod +x /app/bin/gnostr

echo "==> [App] Configuring Environment Paths..."
SITE_PKG=$(find /app -type d -name "site-packages" | head -n 1)
export PYTHONPATH=$PYTHONPATH:$SITE_PKG

# Find Secret-1.typelib
TYPELIB_FILE=$(find /app -name "Secret-1.typelib" | head -n 1)
if [ -z "$TYPELIB_FILE" ]; then
    echo "❌ CRITICAL ERROR: Secret-1.typelib not found!"
    exit 1
fi
TYPELIB_DIR=$(dirname "$TYPELIB_FILE")
export GI_TYPELIB_PATH=$TYPELIB_DIR:/app/lib/girepository-1.0:/app/lib64/girepository-1.0:$GI_TYPELIB_PATH

echo "==> [App] Build-Time Verification..."
python3 -c "
import sys
try:
    import websocket
    import ecdsa
    import gi
    gi.require_version('Secret', '1')
    from gi.repository import Secret
    print('✅ All imports successful.')
except Exception as e:
    print(f'❌ Import Failed: {e}')
    sys.exit(1)
"
