#!/bin/bash
set -e

echo "==> 1. DIAGNOSTICS: Locating installed libraries..."
# Find where the 'websocket' folder ended up. 
# This helps us see if pip installed it to lib, lib64, or somewhere else.
FOUND_PATH=$(find /app -type d -name "websocket" | head -n 1)

if [ -z "$FOUND_PATH" ]; then
    echo "ERROR: 'websocket' module not found in /app. Pip install failed."
    echo "Dumping /app directory structure:"
    find /app -maxdepth 4
    exit 1
else
    echo "SUCCESS: Found websocket at: $FOUND_PATH"
fi

# Calculate the site-packages path dynamically (e.g., /app/lib/python3.12/site-packages)
# We take the parent directory of the 'websocket' folder we found.
SITE_PACKAGES=$(dirname "$FOUND_PATH")
echo "==> Setting PYTHONPATH to: $SITE_PACKAGES"
export PYTHONPATH=$PYTHONPATH:$SITE_PACKAGES

echo "==> 2. VERIFICATION: Testing Import..."
# This is the critical check. If this fails, the build stops.
python3 -c "import websocket; print('✅ Websocket imported successfully from:', websocket.__file__)"

echo "==> 3. INSTALLATION: Copying App Files..."
mkdir -p /app/bin
cp -v src/main.py /app/bin/gnostr
chmod +x /app/bin/gnostr

echo "==> Build Script Complete."
