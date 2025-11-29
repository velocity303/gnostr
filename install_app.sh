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
echo "   -> PYTHONPATH added: $SITE_PKG"

# Find Libsecret Introspection Data (Secret-1.typelib)
TYPELIB_FILE=$(find /app -name "Secret-1.typelib" | head -n 1)

if [ -z "$TYPELIB_FILE" ]; then
    echo "❌ CRITICAL ERROR: Secret-1.typelib not found in /app!"
    exit 1
else
    TYPELIB_DIR=$(dirname "$TYPELIB_FILE")
    export GI_TYPELIB_PATH=$TYPELIB_DIR:/app/lib/girepository-1.0:/app/lib64/girepository-1.0:$GI_TYPELIB_PATH
    echo "   -> GI_TYPELIB_PATH added: $TYPELIB_DIR"
fi

echo "==> [App] Build-Time Verification..."
python3 -c "
import sys
print('   -> Checking Python imports...')
try:
    import websocket
    print(f'   ✅ Websocket found at: {websocket.__file__}')
except ImportError as e:
    print(f'   ❌ Websocket FAILED: {e}')
    sys.exit(1)

try:
    import gi
    # FIX: The Namespace is 'Secret', not 'Libsecret'
    gi.require_version('Secret', '1')
    from gi.repository import Secret
    print('   ✅ Libsecret (Namespace: Secret) found and loaded successfully.')
except Exception as e:
    print(f'   ❌ Libsecret FAILED: {e}')
    sys.exit(1)
"
